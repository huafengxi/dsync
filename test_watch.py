#!/usr/bin/env python3
"""test_watch.py — unit tests for the resident ``watch`` link (group gate).

Everything here runs on SYNTHETIC trees under ``tempfile.mkdtemp()``
(never ``~/m/agents``, never a real inbox). Transport-level checks drive
the LOCAL ``rsync`` binary with exactly the flags the watch link builds;
command-construction checks stub ``subprocess.run`` so no ssh is needed.

Covers:
+ T1 ``ChangeSignal`` + ``cycle_trigger`` — the entire trigger model
  (first-event debounce window; the forced deadline is the completeness
  bound and is evaluated before any pending-event check)
+ T2 ``unmarked_files`` — the push whitelist AND the pull protect source
+ T3 ``WATCH_EXCLUDES`` is EMPTY (the receiver's in-flight ledger moved
  into process memory ⇒ nothing on disk is writer-local any more) and the
  exclusion plumbing still works: a re-added pattern drops the name at any
  depth, suffix/glob only (look-alikes / ``*.msg`` / final ``ack/<id>``
  keep syncing)
+ T4 ``watch_rsync`` command construction in both directions, the moved
  count taken from rsync -v's own output, gc convergence after a
  successful pull (and never under --dry-run), failure propagation
+ T5 real rsync: a local original is never overwritten by a pull, landed
  files are marked ``replica``, byte-identical files are not rewritten,
  a replica is never pushed back
+ T6 gc delete-list consumption: anchored patterns, ``gc/`` self-tree
  refusal, malformed-entry refusal, the FORCED audit marker, deletion
  convergence (subtree, type mismatch, idempotency)
+ T7 flag/CLI invariants: ``-c`` present, ``-I`` absent, no ``--delete``
  anywhere on the watch path, ``watch`` refuses ``--delete`` at argparse
+ T8 detectors: the remote inotify script is valid Python and really
  emits one line per event with excluded names dropped; the local inotify
  thread really notes a change

Run: ``python3 dsync/test_watch.py`` — under ``sg replica -c ...`` when
the login session lacks the supplementary group (the group-gate tests
skip without replica membership, everything else still runs).
"""

import argparse
import contextlib
import grp
import importlib.util
import io
import os
import pwd
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load("ssh_sync", os.path.join(_HERE, "ssh-sync.py"))

PASS = 0
FAIL = []
SKIP = []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def skip(name, why):
    SKIP.append(name)
    print(f"  skip {name}  ({why})")


def mktmp(prefix):
    d = tempfile.mkdtemp(prefix=f"watch-{prefix}-")
    normalize_group(d)
    return d


# The gate is "group == replica means a synced replica", so a test tree
# must be built with every path in a NON-replica group. A session started
# via `sg replica` has replica as its PRIMARY group, which would make
# freshly written files look like replicas; chgrping to the passwd primary
# gid makes the trees identical under both session shapes.
ORIG_GID = pwd.getpwuid(os.getuid()).pw_gid


def normalize_group(path):
    """chgrp a path (and its children) to the non-replica baseline group."""
    if ORIG_GID == REPLICA_GID:
        return
    try:
        os.chown(path, -1, ORIG_GID)
    except OSError:
        pass
    if os.path.isdir(path) and not os.path.islink(path):
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    os.chown(os.path.join(root, name), -1, ORIG_GID)
                except OSError:
                    pass


def write(root, rel, body="x"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(body)
    normalize_group(p)
    return p


def read(root, rel):
    try:
        with open(os.path.join(root, rel)) as f:
            return f.read()
    except OSError:
        return None


try:
    REPLICA_GID = grp.getgrnam(ss.REPLICA_GROUP).gr_gid
except KeyError:
    REPLICA_GID = None


def gate_usable():
    """Group-gate tests need the replica group, membership in it, AND a
    different baseline group to build 'originals' with."""
    return REPLICA_GID is not None \
        and ORIG_GID != REPLICA_GID


def can_chgrp(d):
    """Functional check that this user can chgrp to the replica group."""
    if REPLICA_GID is None:
        return False
    p = write(d, ".probe-chgrp")
    try:
        os.chown(p, -1, REPLICA_GID)
        return True
    except OSError:
        return False
    finally:
        try:
            os.remove(p)
        except OSError:
            pass


def mark_replica(root, rel):
    os.chown(os.path.join(root, rel), -1, REPLICA_GID)


def args_ns(**kw):
    base = dict(dry_run=False, verbose=False, bwlimit=None, exclude=[],
                interval=ss.DEFAULT_INTERVAL, debounce=ss.DEFAULT_DEBOUNCE,
                min_cycle=ss.DEFAULT_MIN_CYCLE,
                stats_every=ss.DEFAULT_STATS_EVERY)
    base.update(kw)
    return argparse.Namespace(**base)


def rsync_local(flags, extra, src, dst, stdin):
    """Run the LOCAL rsync with the watch link's flags (no ssh)."""
    cmd = [ss.RSYNC_BIN, flags] + extra + [src.rstrip("/") + "/",
                                           dst.rstrip("/") + "/"]
    return subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, timeout=120)


# ------------------------------------------------- T1 the trigger model
print("== T1 ChangeSignal + cycle_trigger ==")
sig = ss.ChangeSignal("local")
check("fresh signal is idle", sig.pending_since() is None)
sig.note()
t_first = sig.pending_since()
check("a noted event makes the signal pending", t_first is not None)
time.sleep(0.05)
sig.note(3)
check("FIRST-event window: later events do not move the timestamp",
      sig.pending_since() == t_first, (sig.pending_since(), t_first))
ts, n = sig.take()
check("take() reports the count and the first timestamp",
      ts == t_first and n == 4, (ts, n))
check("take() drains the signal", sig.pending_since() is None)

now = 1000.0
quiet = ss.ChangeSignal("quiet")
busy = ss.ChangeSignal("remote")
check("no events, deadline ahead -> no cycle",
      ss.cycle_trigger(now, now + 10, 0.5, (quiet,)) is None)
check("QUIET tree past the deadline -> the deadline itself is the trigger",
      ss.cycle_trigger(now, now - 1, 0.5, (quiet,)) == "interval deadline")
check("exactly at the deadline counts as due",
      ss.cycle_trigger(now, now, 0.5, (quiet,)) == "interval deadline")
# an event noted `debounce` ago (rebuild the timestamp deterministically)
busy.note()
with busy._lock:
    busy._ts = now - 0.5
check("event pending exactly for `debounce` -> cycle",
      ss.cycle_trigger(now, now + 10, 0.5, (busy,)) == "remote change")
with busy._lock:
    busy._ts = now - 0.4
check("event pending for less than `debounce` -> wait",
      ss.cycle_trigger(now, now + 10, 0.5, (busy,)) is None)
with busy._lock:
    busy._ts = now - 5
check("the deadline wins over a pending event (ordering is the guarantee)",
      ss.cycle_trigger(now, now - 1, 0.5, (busy,)) == "interval deadline")
check("signals are consulted in order",
      ss.cycle_trigger(now, now + 10, 0.5, (quiet, busy)) == "remote change")

# ------------------------------------------- T2/T3 whitelist & excludes
print("== T2/T3 unmarked_files + WATCH_EXCLUDES shape ==")
src = mktmp("whitelist")
write(src, "task/t1/spec.json", "{}")
write(src, "task/t1/inbox/x.msg", "envelope")
write(src, "task/t1/inbox/ack/x", "final ack")
write(src, "keep.pendingx", "look-alike")
write(src, "replica.msg", "pulled copy")
os.makedirs(os.path.join(src, "adir"), exist_ok=True)
os.symlink("task/t1/spec.json", os.path.join(src, "link.json"))
normalize_group(src)
if gate_usable() and can_chgrp(src):
    mark_replica(src, "replica.msg")
    got, ok = ss.unmarked_files(src, REPLICA_GID, ss.WATCH_EXCLUDES)
    check("whitelist walk reports no error", ok is True)
    check("whitelist = my originals only (replica / dir / symlink out)",
          sorted(got) == ["keep.pendingx", "task/t1/inbox/ack/x",
                          "task/t1/inbox/x.msg", "task/t1/spec.json"],
          sorted(got))
    check("INVARIANT the sync-face exclusion list is EMPTY (the receiver's "
          "in-flight ledger moved into process memory ⇒ nothing on disk is "
          "writer-local any more)", ss.WATCH_EXCLUDES == [], ss.WATCH_EXCLUDES)
    check("INVARIANT look-alikes / envelopes / final acks all sync",
          {"keep.pendingx", "task/t1/inbox/x.msg",
           "task/t1/inbox/ack/x"} <= set(got), got)
    # 管道仍在（重新加一条排除 = 改一个常量）：深度无关的后缀/glob 匹配逐字不变
    write(src, "task/t1/inbox/ack/z.ledger", "synthetic")
    demo = ["*.ledger"]
    ex, ok2 = ss.unmarked_files(src, REPLICA_GID, demo)
    check("plumbing intact: a re-added pattern drops the name at any depth "
          "(look-alikes / envelopes untouched)",
          ok2 is True
          and not any(q.endswith(".ledger") for q in ex)
          and ss._excluded("a/b/ack/x.ledger", demo)
          and ss._excluded("x.ledger", demo)
          and not ss._excluded("x.ledgerx", demo)
          and not ss._excluded("x.msg", demo), ex)
else:
    skip("unmarked_files group tests", "no replica group membership here")

# --------------------------------------- T4 watch_rsync construction
print("== T4 watch_rsync command construction ==")
cw = mktmp("cw")
write(cw, "mine/keep.txt", "K")
write(cw, "mine/new.txt", "N")
write(cw, "gc/delete-list.000001", "gone/\n")
write(cw, "gone/inner.txt", "G")
normalize_group(cw)


class _R:
    """rsync that 'transferred' two files (plus header/summary noise)."""
    returncode = 0
    stdout = ("sending incremental file list\n"
              "mine/new.txt\n"
              "mine/keep.txt\n"
              "newdir/\n"
              "\n"
              "sent 100 bytes  received 20 bytes  400.00 bytes/sec\n"
              "total size is 10  speedup is 0.08\n")
    stderr = ""


if gate_usable() and can_chgrp(cw):
    write(cw, "rep.txt", "R")
    mark_replica(cw, "rep.txt")
    calls = []
    gc_calls = []

    def fake_run(cmd, verbose=False, check=True, capture=False):
        calls.append(("ssh-run", cmd))
        return _R()

    def fake_sub(cmd, input=None, **kw):
        calls.append(("rsync", cmd, input))
        return _R()

    real_run, real_sub = ss.run, ss.subprocess.run
    real_apply = ss.apply_gc_deletions
    ss.run, ss.subprocess.run = fake_run, fake_sub
    ss.apply_gc_deletions = lambda d: gc_calls.append(d) or 0
    try:
        # --- push
        calls.clear()
        ok, n = ss.watch_rsync("CFG", "dev", cw, "/hub", args_ns(), "push",
                               REPLICA_GID)
        rs = [c for c in calls if c[0] == "rsync"][0]
        listed = sorted(x for x in rs[2].split("\0") if x)
        check("push: the hub directory is (re)created every cycle",
              any(c[0] == "ssh-run" and "mkdir" in " ".join(c[1])
                  for c in calls), calls)
        check("push: whole-tree whitelist = every original of mine (the "
              "sync-face exclusion list is empty ⇒ nothing is held back)",
              ok and listed == ["gc/delete-list.000001", "gone/inner.txt",
                                "mine/keep.txt", "mine/new.txt"],
              (ok, listed))
        check("push: --files-from + --from0 + --chown, never --delete",
              "--files-from=-" in rs[1] and "--from0" in rs[1]
              and f"--chown=:{ss.REPLICA_GROUP}" in rs[1]
              and "--delete" not in rs[1], rs[1])
        check("push: the moved count comes from rsync -v's own output "
              "(summary lines and directory entries are not files)",
              n == 2, n)
        check("push: gc deletions are pull-side only", gc_calls == [],
              gc_calls)

        # --- push with nothing of mine -> no rsync at all
        empty = mktmp("empty")
        calls.clear()
        ok, n = ss.watch_rsync("CFG", "dev", empty, "/hub", args_ns(),
                               "push", REPLICA_GID)
        check("push: an empty whitelist is silent (no rsync, ok)",
              ok is True and n == 0
              and not any(c[0] == "rsync" for c in calls), (ok, n, calls))

        # --- pull
        calls.clear()
        gc_calls.clear()
        ok, n = ss.watch_rsync("CFG", "dev", cw, "/hub", args_ns(), "pull",
                               REPLICA_GID)
        rs = [c for c in calls if c[0] == "rsync"][0]
        pats = rs[2].splitlines()
        check("pull: protect entries are ANCHORED at the transfer root",
              "/mine/keep.txt" in pats and "/mine/new.txt" in pats
              and "mine/keep.txt" not in pats, pats)
        check("pull: a replica of mine is NOT protected (it may be updated)",
              "/rep.txt" not in pats, pats)
        check("pull: gc delete-list subtree pattern present",
              "/gone/" in pats, pats)
        check("pull: no wildcard reached --exclude-from (protect entries "
              "are anchored paths; WATCH_EXCLUDES is empty ⇒ a glob could "
              "only come from a corrupted protect entry, which is refused)",
              ss.WATCH_EXCLUDES == []
              and not any(set(p) & ss.GLOB_UNSAFE for p in pats), pats)
        check("pull: --exclude-from, no --files-from, never --delete",
              "--exclude-from=-" in rs[2 - 1]
              and "--files-from" not in " ".join(rs[1])
              and "--delete" not in rs[1], rs[1])
        check("pull: gc deletions converge after a successful transfer",
              gc_calls == [cw], gc_calls)
        check("pull: no per-cycle remote mkdir on the pull side",
              not any(c[0] == "ssh-run" for c in calls), calls)

        # --- pull under --dry-run removes nothing
        gc_calls.clear()
        ss.watch_rsync("CFG", "dev", cw, "/hub", args_ns(dry_run=True),
                       "pull", REPLICA_GID)
        check("pull: --dry-run converges no gc deletion",
              gc_calls == [], gc_calls)

        # --- failure propagates (no stale-exit tolerance on this path)
        class _Bad:
            returncode = 23
            stdout = ('rsync: [sender] link_stat "/hub/x" failed: No such '
                      'file or directory (2)\n'
                      'rsync error: some files/attrs were not transferred '
                      '(code 23)\n')
            stderr = ""

        ss.subprocess.run = lambda cmd, input=None, **kw: (
            calls.append(("rsync", cmd, input)) or _Bad())
        ok, n = ss.watch_rsync("CFG", "dev", cw, "/hub", args_ns(), "push",
                               REPLICA_GID)
        check("a non-zero rsync exit fails the cycle (the loop backs off "
              "and the next whole-tree cycle re-negotiates)",
              ok is False and n == 0, (ok, n))
        ss.subprocess.run = fake_sub
    finally:
        ss.run, ss.subprocess.run = real_run, real_sub
        ss.apply_gc_deletions = real_apply
else:
    skip("watch_rsync construction tests", "no replica group membership")

# ------------------------------------------------- T5 real transport
print("== T5 real rsync: the group gate holds ==")
if gate_usable() and can_chgrp(mktmp("probe")):
    hub = mktmp("hub")
    node = mktmp("node")
    # hub side: a file only the hub has, one both sides have (different
    # bytes), and a stale *.pending ledger left over by the retired
    # in-flight implementation (nobody reads it; it now travels like any
    # other unmarked file ⇒ every node converges on the same leftovers).
    write(hub, "fresh.txt", "FROM_HUB")
    write(hub, "shared.txt", "HUB_VERSION")
    write(hub, "inbox/ack/led.pending", "GHOST")
    write(hub, "inbox/ack/keep.msg", "ENVELOPE")
    # node side: an original at the same path as a hub file (must be
    # protected) and a replica that must never be pushed back.
    write(node, "shared.txt", "NODE_ORIGINAL")
    write(node, "mine.txt", "NODE_ONLY")
    write(node, "rep.txt", "PULLED")
    mark_replica(node, "rep.txt")

    wl, ok = ss.unmarked_files(node, REPLICA_GID, ss.WATCH_EXCLUDES)
    check("real transport: the whitelist walk succeeded", ok is True)
    r = rsync_local(ss.WATCH_RSYNC_FLAGS,
                    ["--files-from=-", "--from0",
                     f"--chown=:{ss.REPLICA_GROUP}"],
                    node, hub, "\0".join(sorted(wl)) + "\0")
    check("real transport: push exit 0", r.returncode == 0, r.stdout[-400:])
    check("real transport: my original reached the hub",
          read(hub, "mine.txt") == "NODE_ONLY")
    check("real transport: a landed replica is MARKED on the receiver",
          os.lstat(os.path.join(hub, "mine.txt")).st_gid == REPLICA_GID)
    check("real transport: my own replica was NOT pushed back",
          not os.path.exists(os.path.join(hub, "rep.txt")))

    protect, _ = ss.unmarked_files(node, REPLICA_GID, ss.WATCH_EXCLUDES)
    gc_ex, _, _ = ss.gc_exclude_and_targets(node)
    stdin = "".join(p + "\n" for p in
                    ["/" + x for x in protect] + gc_ex + ss.WATCH_EXCLUDES)
    r = rsync_local(ss.WATCH_RSYNC_FLAGS,
                    ["--exclude-from=-", f"--chown=:{ss.REPLICA_GROUP}"],
                    hub, node, stdin)
    check("real transport: pull exit 0", r.returncode == 0, r.stdout[-400:])
    check("INVARIANT a local original is NEVER overwritten by a pull",
          read(node, "shared.txt") == "NODE_ORIGINAL",
          read(node, "shared.txt"))
    check("INVARIANT the hub's own file arrived",
          read(node, "fresh.txt") == "FROM_HUB")
    check("INVARIANT a landed file is marked replica",
          os.lstat(os.path.join(node, "fresh.txt")).st_gid == REPLICA_GID)
    check("INVARIANT a stale *.pending DOES travel (the sync-face exclusion "
          "list is empty ⇒ leftovers converge instead of diverging per node)",
          read(node, "inbox/ack/led.pending") == "GHOST")
    check("INVARIANT a look-alike envelope DOES travel",
          read(node, "inbox/ack/keep.msg") == "ENVELOPE")

    # byte-identical files are not rewritten (mtime + inode stay put):
    # the accident class of a flag that rewrites every candidate.
    st0 = os.stat(os.path.join(node, "fresh.txt"))
    time.sleep(0.05)
    protect, _ = ss.unmarked_files(node, REPLICA_GID, ss.WATCH_EXCLUDES)
    stdin = "".join("/" + p + "\n" for p in protect)
    r = rsync_local(ss.WATCH_RSYNC_FLAGS,
                    ["--exclude-from=-", f"--chown=:{ss.REPLICA_GROUP}"],
                    hub, node, stdin)
    st1 = os.stat(os.path.join(node, "fresh.txt"))
    check("INVARIANT a byte-identical replica is not rewritten "
          "(same inode, same mtime)",
          r.returncode == 0 and (st0.st_ino, st0.st_mtime_ns)
          == (st1.st_ino, st1.st_mtime_ns), (st0.st_ino, st1.st_ino))
else:
    skip("real-transport tests", "no replica group membership here")

# ------------------------------------------------- T6 gc consumption
print("== T6 gc delete-list consumption ==")
g = mktmp("gc")
write(g, "gc/delete-list.000001", "task/dead/\nold.txt\n")
write(g, "gc/delete-list.000002",
      "task/dead2/ #FORCED:audit-mark\ngc/delete-list.000000\n"
      "gc/state.json\n/abs/path\n../escape\nwild*card\n")
write(g, "keep.txt", "KEEP")
write(g, "old.txt", "STALE")
write(g, "task/dead/inner.txt", "STALE")
write(g, "task/dead2/inner.txt", "STALE")
ex, targets, refused = ss.gc_exclude_and_targets(g)
check("gc: entries become ANCHORED patterns (file + subtree)",
      "/old.txt" in ex and "/task/dead/" in ex and "/task/dead2/" in ex, ex)
check("gc: the FORCED audit marker is stripped, the path applied",
      any(t[0] == "task/dead2" and t[1] for t in targets), targets)
check("gc: newer lists are unioned (both files present)",
      {t[0] for t in targets} >= {"old.txt", "task/dead", "task/dead2"},
      targets)
check("INVARIANT gc/ internals are refused (a forged list cannot touch the "
      "GC tree); a compression entry would be the only legal one",
      not any(t[0].startswith("gc/") and t[0] != "gc/delete-list.000000"
              for t in targets), targets)
check("gc: malformed entries are refused (absolute / .. / glob chars)",
      any("/abs/path" in r for r in refused)
      and any("../escape" in r for r in refused)
      and any("wild*card" in r for r in refused), refused)
n = ss.apply_gc_deletions(g)
check("gc: listed paths are deleted (file + whole subtree)",
      read(g, "old.txt") is None and read(g, "task/dead/inner.txt") is None
      and read(g, "task/dead2/inner.txt") is None, n)
check("gc: unlisted files are untouched", read(g, "keep.txt") == "KEEP")
check("gc: the delete-lists themselves survive (replayable)",
      read(g, "gc/delete-list.000001") is not None
      and read(g, "gc/delete-list.000002") is not None)
check("gc: it reports how many paths it removed", n == 3, n)
check("gc: idempotent — a second round removes nothing",
      ss.apply_gc_deletions(g) == 0)
# type mismatch converges instead of retrying forever
g2 = mktmp("gc2")
write(g2, "gc/delete-list.000001", "adir\nbfile/\n")
os.makedirs(os.path.join(g2, "adir"))
write(g2, "adir/inner.txt", "x")
write(g2, "bfile", "plain file at a dir entry")
ss.apply_gc_deletions(g2)
check("gc: a FILE entry at a directory removes the subtree",
      not os.path.exists(os.path.join(g2, "adir")))
check("gc: a DIR entry at a plain file removes the file",
      not os.path.exists(os.path.join(g2, "bfile")))

# ------------------------------------------------- T7 flags & CLI
print("== T7 flags & CLI invariants ==")
check("INVARIANT -c present in watch flags", "c" in ss.WATCH_RSYNC_FLAGS,
      ss.WATCH_RSYNC_FLAGS)
check("INVARIANT -I absent from watch flags", "I" not in ss.WATCH_RSYNC_FLAGS,
      ss.WATCH_RSYNC_FLAGS)
check("INVARIANT no -a/-g/-o spelling in watch flags",
      "a" not in ss.WATCH_RSYNC_FLAGS and "g" not in ss.WATCH_RSYNC_FLAGS,
      ss.WATCH_RSYNC_FLAGS)
wr_src = open(os.path.join(_HERE, "ssh-sync.py")).read()
wr = wr_src.split("def watch_rsync(")[1].split("\ndef local_inotify_thread")[0]
check("INVARIANT watch_rsync never passes --delete", "--delete" not in wr,
      [l for l in wr.splitlines() if "--delete" in l])
check("INVARIANT watch_rsync keeps --chown=:replica",
      "--chown=:{REPLICA_GROUP}" in wr)
check("INVARIANT push feeds --files-from, pull feeds --exclude-from",
      '"--files-from=-"' in wr and '"--exclude-from=-"' in wr)
check("INVARIANT the pull stream carries protect + gc + WATCH_EXCLUDES",
      "protect + gc_excludes + WATCH_EXCLUDES" in wr)
p_watch = None
try:
    sys.argv = ["ssh-sync.py", "watch", "--delete"]
    with contextlib.redirect_stderr(io.StringIO()):
        ss.main()
except SystemExit as e:
    p_watch = e.code
except Exception as e:            # preflight/other: only exit codes matter
    p_watch = getattr(e, "code", None)
check("watch still refuses --delete (argparse exit 2)", p_watch == 2, p_watch)

# ------------------------------------------------- T8 detectors
print("== T8 detectors ==")
try:
    compile(ss.REMOTE_PY_INOTIFY_SCRIPT, "remote-inotify", "exec")
    check("the remote event script is valid Python", True)
except SyntaxError as e:
    check("the remote event script is valid Python", False, e)

try:
    import inotify_simple  # noqa: F401
    HAVE_INO = True
except ImportError:
    HAVE_INO = False

def read_lines(stream, seconds):
    """Every line a stream produces within ``seconds``.

    A reader THREAD, not select(): ``select`` on a text-mode pipe reports
    the OS pipe, while ``readline()`` has already buffered everything it
    read — so a select-bounded loop silently drops the buffered lines and
    a detector that emits nothing cannot hang the suite either way."""
    got = []

    def _drain():
        for line in stream:
            got.append(line.strip())

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    time.sleep(seconds)
    return list(got)


if HAVE_INO:
    d = mktmp("remote-watch")
    # 生产排除面为空（WATCH_EXCLUDES == []）⇒ 用合成模式验管道仍在
    excludes = ",".join(ss.WATCH_EXCLUDES + ["*.ledger"])
    proc = subprocess.Popen(
        [sys.executable, "-c", ss.REMOTE_PY_INOTIFY_SCRIPT, d, excludes],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        time.sleep(1.0)                     # let it register the watches
        # A subtree created together with its files races the watch
        # registration (inotify is not recursive): the directory event is
        # what a real link relies on, and the whole-tree cycle it triggers
        # picks the inner files up. Create the directory first, then the
        # files, to test the event path itself.
        os.makedirs(os.path.join(d, "sub"), exist_ok=True)
        time.sleep(0.7)
        write(d, "sub/hello.txt", "hi")     # a real change
        write(d, "sub/led.ledger", "synthetic excluded name")
        write(d, "sub/real.msg", "envelope")
        lines = read_lines(proc.stdout, 4)
        check("the remote script reports a change as `EV <path>` line(s)",
              any(l.startswith("EV ") and "hello.txt" in l for l in lines),
              lines)
        check("the remote script extends its watches to a new subtree "
              "(the directory event is what re-anchors a raced subtree)",
              any(l.endswith("/sub") for l in lines), lines)
        check("the remote script still reports a normal name",
              any("real.msg" in l for l in lines), lines)
        check("INVARIANT the remote script drops sync-face-excluded names "
              "(plumbing intact though the production list is empty)",
              not any(".ledger" in l for l in lines), lines)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # the local inotify thread, in this process
    ld = mktmp("local-watch")
    real_stop = ss._STOP
    ss._STOP = type(real_stop)()
    sig_before = ss._LOCAL_SIG.take()
    t = threading.Thread(target=ss.local_inotify_thread,
                         kwargs={"local_dir": ld}, daemon=True)
    t.start()
    try:
        time.sleep(1.0)
        os.makedirs(os.path.join(ld, "a"), exist_ok=True)
        time.sleep(0.7)
        ss._LOCAL_SIG.take()                # drop the directory event
        write(ld, "a/b.txt", "data")
        got, deadline = None, time.time() + 8
        while time.time() < deadline and got is None:
            got = ss._LOCAL_SIG.pending_since()
            time.sleep(0.05)
        check("the local inotify thread notes a change in a new subtree",
              got is not None)
        ss._LOCAL_SIG.take()
        real_excludes = ss.WATCH_EXCLUDES
        ss.WATCH_EXCLUDES = ["*.ledger"]   # 生产面为空 ⇒ 换合成模式验管道
        try:
            write(ld, "a/led.ledger", "synthetic excluded name")
            time.sleep(1.5)
            check("INVARIANT the local thread drops sync-face-excluded names "
                  "(plumbing intact though the production list is empty)",
                  ss._LOCAL_SIG.pending_since() is None)
        finally:
            ss.WATCH_EXCLUDES = real_excludes
    finally:
        ss._STOP.set()
        t.join(timeout=5)
        ss._STOP = real_stop
        ss._LOCAL_SIG.take()
else:
    skip("live detector tests", "inotify_simple not installed here")

# ------------------------------------- T9 consumer-side iron rules
print("== T9 consumer-side iron rules + refusal throttle ==")
ir = mktmp("iron")
write(ir, "gc/delete-list.000001",
      "agents/\ntask/\nrun\ntopic/dispatcher/inbox/a.msg\n"
      "gc/state.json\ntask/ok/\n")
write(ir, "task/ok/f.txt", "listed")
write(ir, "task/keep.txt", "KEEP")
write(ir, "topic/dispatcher/inbox/a.msg", "mailbox")
write(ir, "run/agentd.dev.lock", "lock")
os.makedirs(os.path.join(ir, "agents"), exist_ok=True)
write(ir, "agents/oops.txt", "stray")
ex, targets, refused = ss.gc_exclude_and_targets(ir)
rel = {t[0] for t in targets}
check("INVARIANT the consumer refuses bare family containers (one such "
      "line would delete a whole clan on every node)",
      not ({"agents", "task", "run"} & rel), rel)
check("INVARIANT the consumer refuses a PROTECTED system asset",
      not any(r.startswith("topic/dispatcher") for r in rel), rel)
check("INVARIANT the consumer refuses gc/ internals",
      not any(r == "gc/state.json" for r in rel), rel)
check("a concrete participant entry still passes",
      "task/ok" in rel and "/task/ok/" in ex, (rel, ex))
ss._gc_refuse_reported[:] = [0.0, 0]
n = ss.apply_gc_deletions(ir)
check("the refused shapes are NOT deleted, the listed participant is",
      n == 1 and os.path.exists(os.path.join(ir, "agents/oops.txt"))
      and os.path.exists(os.path.join(ir, "run/agentd.dev.lock"))
      and os.path.exists(os.path.join(ir, "topic/dispatcher/inbox/a.msg"))
      and os.path.exists(os.path.join(ir, "task/keep.txt"))
      and not os.path.exists(os.path.join(ir, "task/ok")), n)

_logged = []
_real_wlog = ss.wlog
ss.wlog = lambda m: _logged.append(m)
try:
    ss._gc_refuse_reported[:] = [0.0, 0]
    ss.gc_exclude_and_targets(ir)
    ss.gc_exclude_and_targets(ir)
    ss.gc_exclude_and_targets(ir)
    check("refusals warn once per gap, not once per read (a resident "
          "process reads the lists every cycle)",
          len([m for m in _logged if "refused" in m]) == 1,
          [m[:60] for m in _logged])
    first = [m for m in _logged if "refused" in m][0]
    ss.gc_exclude_and_targets(ir)
    ss.gc_exclude_and_targets(ir)
    check("suppressed refusals are counted, not dropped",
          len([m for m in _logged if "refused" in m]) == 1, first[:120])
    ss._gc_refuse_reported[0] = 0.0     # pretend the gap elapsed
    ss.gc_exclude_and_targets(ir)
    again = [m for m in _logged if "refused" in m]
    check("after the gap it reports again, carrying the suppressed count",
          len(again) == 2 and "suppressed since the last report" in again[1],
          [m[:100] for m in again])
finally:
    ss.wlog = _real_wlog
    ss._gc_refuse_reported[:] = [0.0, 0]

# ------------------------------------- T10 graceful stop
print("== T10 signal handling ==")
_real_stop = ss._STOP
ss._STOP = type(_real_stop)()
_err = io.StringIO()
_real_stderr = sys.stderr
try:
    sys.stderr = _err
    ss._signal_handler(15, None)
finally:
    sys.stderr = _real_stderr
check("the handler only flips the flag (writing to stderr inside a signal "
      "handler can re-enter the buffered writer and cost the graceful exit)",
      ss._STOP.is_set() and _err.getvalue() == "", repr(_err.getvalue()))
ss._STOP = _real_stop

# ----------------------------------------------------------------------
print(f"\n{PASS} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
if FAIL:
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys.exit(1)
