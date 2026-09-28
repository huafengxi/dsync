#!/usr/bin/env python3

"""
ssh-sync.py — Sync a local directory with a remote directory over SSH.

Transport is rsync over ssh; if rsync is missing the script errors out
and asks you to install it (no degraded fallback). One-shot
``push``/``pull``/``both`` keep plain ``rsync -a`` semantics (no
overwrite arbitration at all) and accept ``--delete``. The resident
``watch`` link NEVER deletes and refuses the flag at the argparse layer.

SSH config: hosts come from ``<workspace>/env/.live/ssh-hosts``
(materialized by ``make env.materialize`` from ``env/ssh-hosts``);
``SSH_SYNC_CONFIG`` overrides the path — node machines that do not carry
the workspace secrets point it at ``~/.ssh/config``. Every ssh/rsync call
passes ``-F <config>``, and a connectivity preflight
(``ssh -o BatchMode=yes -o ConnectTimeout=8 <host> true``) runs before
any transfer.

Watch mode: the group gate
=======================================================================
``watch`` is a resident bidirectional sync daemon whose overwrite safety
comes from a FILESYSTEM GROUP GATE — never from a timestamp:

    Every file has exactly ONE possible writer (single-writer
    discipline). The ``replica`` group marks "a replica that sync landed
    here"; an UNMARKED file is the local original.

Both pipelines encode file ORIGIN in group metadata:

+ push (whitelist — ship everything I write)::

    find <local> ! -group replica -type f  ->  rsync -rlptDvc \
        --files-from=- --from0 --chown=:replica <local>/ <host>:<remote>/

+ pull (blacklist — fetch everything I did NOT write, mark replicas)::

    protect list = local files NOT in replica  ->  rsync -rlptDvc \
        --exclude-from=- --chown=:replica <host>:<remote>/ <local>/

Rules, point by point:

1. ``-c`` (--checksum) is mandatory: the skip decision is a CONTENT
   checksum, never a timestamp, so no clock trick can strand a change.
   Trade-off (documented, accepted): a byte-identical file is not
   re-transferred, so a replica that LOST its mark but already matches
   content is not healed by a transfer — marking is the post-pull fix-up's
   job. Worst case is harmless extra push churn, never data loss.
2. ``-a`` is spelled out as ``-rlptDv`` with ``-g`` and ``-o`` DROPPED:
   the landed group is decided in exactly one place
   (``--chown=:replica``), and uids differ across machines.
3. ``--chown=:replica`` resolves the group BY NAME on the receiver, so
   gids need not match across machines — the single cross-machine
   consensus is the string ``replica`` in each /etc/group. The RUNNING
   USER MUST BE A MEMBER there (chgrp to a non-member group is EPERM) and
   rsync SILENTLY IGNORES a failing chown: hence the startup gates.
4. Exclusions: the built-in sync-face list ``WATCH_EXCLUDES`` (currently
   EMPTY — see "Sync-face exclusions" below) and the gc delete-list
   (point 8). There is no user exclusion list. Lock files are correct in both
   directions by construction (each machine writes its own: unmarked
   locally → pushed, and protected on pull). ``session/`` travels like
   any other subtree.
5. Both lists travel via stdin (``--files-from=-`` / ``--exclude-from=-``);
   no temp files. Protect and gc entries are ANCHORED (``/rel``, ``/rel/``)
   so a slashless pattern cannot match on basename elsewhere in the tree.
6. Failure is ASYMMETRIC: an unmarked file is never overwritten (worst
   case = a stale replica lingers, never data loss), and every protected
   or refused path is logged ("why didn't it sync? → it is unmarked here").
7. Glob defense: protect-list and gc-list entries containing ``*?[`` or a
   newline are REFUSED with a warning — rsync patterns are globs, and a
   corrupted protection is worse than a missed sync.
8. GC delete-list: deletions do not propagate through a no---delete mesh,
   so the hub-side GC tool (``dsync/gc.py``, run on the hub only) publishes
   cumulative lists at ``gc/delete-list.<seq>``. The PULL side unions every
   local list into the exclude stream (listed paths are never received
   again) and, after each successful transfer, DELETES local copies of
   every listed path (including unmarked node originals — that closes the
   only resurrection window). Idempotent and replayable. The push side is
   untouched: nodes delete their own copies, so there is nothing left to
   push, and the hub never pushes. ``gc/`` internals (except compression
   entries ``gc/delete-list.NNNNNN``) are REFUSED at consumption, so a
   forged list planted on any node and pushed around the mesh can never
   touch the GC tree itself. Type-mismatched entries (file entry at a
   directory, dir entry at a symlink/file) converge by deleting the actual
   target.

Startup gates (watch refuses to start on any failure)
=======================================================================
Three gates, because only these three prove what the link needs:

+ local ``grp.getgrnam("replica")`` resolves (the group must exist here);
+ local functional self-test: a probe file in the local dir can actually be
  chgrped to ``replica`` (pull's landing mark depends on it, and rsync
  would otherwise silently skip the marking);
+ END-TO-END ``--chown`` pipeline probe: a probe file is really transferred
  with ``--chown=:replica`` into a remote scratch dir and the landed group
  MUST be ``replica``.

The end-to-end probe subsumes "the group exists on the receiver", "the
receiver's ssh user can chgrp to it" and "both rsync builds negotiate
--chown", so those three are DIAGNOSTICS: they run only after the probe
fails, to name the root cause in one line each.

Binary resolution: the LOCAL rsync binary is resolved once at import —
``/usr/local/bin/rsync`` when present and executable, else
``shutil.which("rsync")`` — so an inherited daemon PATH can never silently
downgrade it. The REMOTE rsync is probed for ``/usr/local/bin/rsync`` at
startup and, when present, pinned with ``--rsync-path`` (which covers the
remote program in BOTH pipeline directions).

Cycle model
=======================================================================
Detectors answer ONE question — "did anything change?" — and never decide
what may be shipped. There are no snapshots, no candidate lists and no
per-path bookkeeping anywhere in the link:

+ ONE loop per link; a cycle = one whole-tree push followed by one
  whole-tree pull. rsync's own ``-c`` comparison decides what moves, so a
  cycle is correct no matter what the detectors saw or missed;
+ a cycle runs when a detector event has been pending for ``--debounce``
  seconds, or when ``--interval`` seconds have passed since the last cycle.
  The deadline is ITSELF a trigger and is evaluated before any
  short-circuit, so a completely quiet tree still re-anchors every
  ``--interval`` seconds: that deadline is the completeness bound, and it
  holds whatever the detectors did (dead stream, inotify overflow, no local
  backend at all). FIRST-event debounce window: the timestamp is taken when
  nothing is pending and later events only bump a count, so under
  continuous churn a cycle still fires within ``--debounce`` of the first
  event (a last-event window would starve forever);
+ ``--min-cycle`` is the rate floor between two cycles (a whole-tree pair
  costs ~1 s at 10^4 files / 150 MB — see lore facts for the measured
  baselines);
+ any cycle re-anchors everything, so a FAILED cycle has nothing to
  re-queue: it backs off to the ``--interval`` cadence and the next cycle
  re-negotiates the whole tree;
+ a cycle that moved nothing is SILENT (no log line) — the moved set comes
  from rsync -v's own output. In particular the echo of my own push (remote
  event → pull → every path protected) costs one idle round and no line;
+ counters are summarized every ``--stats-every`` seconds; warnings and
  errors are always immediate. ``SSH_SYNC_DEBUG=1`` additionally logs every
  cycle with its trigger, event counts and duration.

There is deliberately NO write-stability guard: writers use atomic
tmp+rename (no half-written files), and readers retry on the next cycle
when parsing fails (agentd file protocol §11.6). A residual non-atomic
writer may be caught mid-write; that half-copy is application-tolerable.
SIGTERM/SIGINT exit gracefully.

Sync-face exclusions: NONE (``WATCH_EXCLUDES`` is empty)
=======================================================================
The list used to carry exactly one pattern, ``*.pending`` — the receiver's
two-phase-delivery IN-FLIGHT ledger, which only its writer ever read, so
shipping it bought nothing and cost twice (ghost-ledger reinjection plus an
mtime other machinery read as the ledger's age).

That ledger no longer exists on disk: the receiver's in-flight state moved
into process memory (claim = an entry in a per-process table; the final
``ack/<id>`` is written only after the injected text is verified in the
session jsonl), so there is nothing left to exclude. ``*.msg`` envelopes
and final ``ack/<id>`` files sync exactly as before.

The plumbing stays (the constant, the detector filter and the pull
``--exclude-from`` slot) because it is generic: re-adding an exclusion is
one list entry, and it still has NO runtime switch — sync-face exclusions
are safety semantics, not a performance knob. Stale ``*.pending`` files
left behind by the old implementation are harmless (protocol §15.4: nobody
reads them as a delivery signal any more) and they now sync like any other
unmarked file, so every node converges on the same leftovers instead of
holding a machine-local set. Their removal has two sides: a LIVE mailbox
(some session still drains it) self-cleans — the receiver unlinks the
legacy mark when it sees the matching final ack
(``core.LEGACY_PENDING_SUFFIX``); a DEAD mailbox (a finished task
directory that nobody drains) can only be removed via ``dsync/gc.py add``.

Detection backends
=======================================================================
+ LOCAL: inotify_simple (Linux) → watchdog/FSEvents (macOS, ``pip install
  watchdog``) → none. With no local backend the link still works: the
  ``--interval`` deadline alone drives it (latency ≤ interval).
+ REMOTE: ``ssh host python3 -c <inotify_simple script>`` streaming one
  line per event, RECONNECTED with backoff (5/10/30 s, reset after a 60 s
  healthy run) whenever the stream ends; each reconnect attempt also notes
  an event, so a blind window is closed by the next cycle rather than left
  to the deadline. A remote without python3 + inotify_simple is a HARD
  startup failure (one ERROR line naming the fix) — silently degrading
  would hide a broken detector, and completeness is already the deadline's
  job.

Both detectors filter ``WATCH_EXCLUDES`` names (currently empty), so an
excluded name's churn cannot wake the loop.

Environment: ``SSH_SYNC_CONFIG`` (ssh config path), ``SSH_SYNC_DEBUG=1``
(log every cycle, including idle ones).

Existing trees must be one-shot tagged with ``dsync/replica-tag.py``
(ownership rules: that script's docstring) before the first watch start.
"""

import argparse
import fnmatch
import grp
import os
import shlex
import shutil
import signal
import stat as statmod
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

try:
    from inotify_simple import INotify, flags as iflags
    HAVE_INOTIFY = True
except ImportError:
    HAVE_INOTIFY = False

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAVE_WATCHDOG = True
except ImportError:
    HAVE_WATCHDOG = False

_WORKSPACE = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEFAULT_SSH_CONFIG = os.path.join(_WORKSPACE, "env", ".live", "ssh-hosts")

# Defaults for the optional `watch` positional args: the cross-machine
# agents/ sync link (star topology, hub = the neutral directory
# dev:/data/shared/agents; each machine runs its own link — see
# env/services.yml `agents-sync` and svc/agents-sync-loop.sh).
DEFAULT_LOCAL_DIR = os.path.join(_WORKSPACE, "agents")
DEFAULT_REMOTE = "dev:/data/shared/agents"

# ServerAlive*: a half-dead link (the peer vanished without sending FIN —
# laptop Wi-Fi/sleep is the common case) blocks reads and writes forever
# and would take the whole cycle loop with it. Keepalive probing makes
# such a connection drop within <=45s, after which the remote stream
# reconnects and the --interval deadline keeps the link honest.
# ControlMaster: every handshake of one link (the per-cycle remote mkdir,
# both rsync runs, the event stream) reuses a single master connection
# instead of paying TCP+auth each time. %C = hash of local/remote/port/user
# (needs OpenSSH >= 7.2); ControlPersist=60 keeps the master across the
# gaps between short commands and cleans up 60s after the process exits.
_SSH_CONTROL_PATH = os.path.join(os.path.expanduser("~"), ".ssh", "cm-%C")
SSH_TIMEOUT_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                    "-o", "ServerAliveInterval=15",
                    "-o", "ServerAliveCountMax=3",
                    "-o", "ControlMaster=auto",
                    "-o", f"ControlPath={_SSH_CONTROL_PATH}",
                    "-o", "ControlPersist=60"]

# Group gate (see module docstring). The ONE cross-machine consensus is
# this group NAME: --chown=:replica resolves it by name on each receiver,
# so gids may differ per machine.
REPLICA_GROUP = "replica"
# rsync flags for the watch pipelines: -a minus -o and -g (the landed
# group is decided solely by --chown=:replica; owner preservation is
# neither wanted nor possible across machines with different uids).
# -c (--checksum) forbids any timestamp-based skip: the skip decision is a
# content checksum (bytes differ -> ship). Never trade it for -I
# (--ignore-times): -I also REWRITES every candidate at the receiver even
# when the bytes are identical, which at mesh scale floods the detectors
# and turns every cycle into a whole-mesh rewrite.
WATCH_RSYNC_FLAGS = "-rlptDvc"
# Characters that would turn a protect-list entry into a wildcard
# (rsync exclude patterns are glob semantics); newline would split one
# entry into two patterns.
GLOB_UNSAFE = set("*?[\n")

# Sync-face exclusions (watch mode; the only ones next to the gc
# delete-list). EMPTY: the single pattern this list used to carry
# (``*.pending`` = the receiver's two-phase-delivery in-flight ledger) lost
# its subject when that ledger moved into receiver process memory — nothing
# on disk is writer-local any more. See the module docstring
# ("Sync-face exclusions: NONE"). Match semantics are suffix/glob-only and
# unchanged, so re-adding a pattern is a one-line edit; there is still no
# runtime switch (safety semantics, not a performance knob).
WATCH_EXCLUDES: list = []

# Cycle cadence (see the module docstring "Cycle model"):
# + DEBOUNCE — first-event window before a detected change fires a cycle;
# + MIN_CYCLE — rate floor between two cycles (a whole-tree pair is ~1 s);
# + INTERVAL — forced cadence AND the completeness bound: a cycle runs at
#   least this often even on a tree with no events at all, which is what
#   re-anchors anything the detectors missed;
# + STATS_EVERY — cadence of the counter summary line.
DEFAULT_DEBOUNCE = 0.5
DEFAULT_MIN_CYCLE = 3.0
DEFAULT_INTERVAL = 30.0
DEFAULT_STATS_EVERY = 300.0

# Binary resolution: never trust the inherited PATH (a daemon started from
# an old session can resolve rsync to an ancient /bin/rsync, and --chown
# marking silently breaks with an old receiver). Prefer the known-good
# location.
PREFERRED_RSYNC = "/usr/local/bin/rsync"


def resolve_rsync_bin():
    """Local rsync binary: PREFERRED_RSYNC when present and executable,
    else PATH lookup, else the bare name (errors surface at first use)."""
    if os.path.isfile(PREFERRED_RSYNC) and os.access(PREFERRED_RSYNC,
                                                     os.X_OK):
        return PREFERRED_RSYNC
    return shutil.which("rsync") or "rsync"


RSYNC_BIN = resolve_rsync_bin()

# Remote-side rsync pinning for watch pipelines (--rsync-path). Set by
# watch() after the startup probe; empty = remote resolves via its own
# non-interactive PATH.
_WATCH_REMOTE_RSYNC_OPTS = []

# GC delete-list consumption: the hub-side GC tool (dsync/gc.py) publishes
# cumulative delete lists at <local>/gc/delete-list.*; the watch PULL
# side turns them into receive exclusions (listed paths are never
# pulled again) and deletes local copies after each transfer. This is
# the ONLY sync-layer change — push is untouched (nodes delete their
# own copies, so there is nothing left to push; the hub never pushes).
GC_DIR_NAME = "gc"
GC_LIST_PREFIX = "delete-list."
# Audited force bypass (dsync/gc.py add --force): exempt
# entries carry an inline audit marker '<path> #FORCED:<audit>'. The
# consumer applies the PATH part as-is and ignores the marker (the
# marker is hub-side audit, not a consumer gate — same trust model as
# the rest of the list). Kept verbatim in sync with gc.py FORCE_MARK.
GC_FORCE_MARK = " #FORCED:"
# Iron-rule shapes refused at CONSUMPTION too, not only at generation.
# ``dsync/gc.py``'s ``validate_path``/``BARE_FAMILY_ENTRIES``/
# ``PROTECTED_SYSTEM_PATHS`` are the authority; the tuples are mirrored
# here verbatim because this process must NOT import gc.py (as a script,
# sys.path[0] is dsync/, so `import gc` would shadow the stdlib module).
# Without the mirror, one legacy or forged entry naming a bare family
# container (``agents/``, ``task/`` ...) would rmtree a whole clan on
# every node — the consumer applies lists as-is, so the shape check has
# to live where the deletion happens.
GC_BARE_FAMILY_ENTRIES = ("agents", "task", "bot", "topic", "run")
GC_PROTECTED_PATHS = ("topic/dispatcher",)
# Refusal warnings are throttled: this is a resident process and one
# surviving junk entry would otherwise warn twice per cycle forever.
# A short-lived CLI (gc.py) reports per call instead.
GC_REFUSE_REPORT_GAP = 300.0
_gc_refuse_reported = [0.0, 0]     # [last report ts, suppressed since then]
# Iron rule enforcement is HUB-SIDE ONLY (dsync/gc.py validate_entry:
# add/read-compression/reap). Consumer-side iron-rule refusal existed
# (review finding B1) until
# (2026-08-31 user decision): the lists are generated by a single
# point (hub gc.py, validated at ingest), so consumer-side re-checking
# was a redundant layer — the consumer now trusts the list.


def read_gc_lists(local_dir):
    """Union of entries across all <local>/gc/delete-list.* files, in
    first-seen order (set semantics; duplicates absorbed). Unreadable
    files are skipped best-effort (lists are written atomically by
    dsync/gc.py; the next cycle retries)."""
    gcdir = os.path.join(local_dir, GC_DIR_NAME)
    try:
        names = sorted(f for f in os.listdir(gcdir)
                       if f.startswith(GC_LIST_PREFIX))
    except OSError:
        return []
    seen = set()
    out = []
    for fn in names:
        try:
            with open(os.path.join(gcdir, fn), encoding="utf-8") as fh:
                for line in fh:
                    e = line.strip()
                    if not e or e.startswith("#"):
                        continue
                    if GC_FORCE_MARK in e:
                        e = e.split(GC_FORCE_MARK, 1)[0]
                    if e not in seen:
                        seen.add(e)
                        out.append(e)
        except OSError:
            continue
    return out


def _gc_shape_refused(raw, is_dir):
    """Why a delete-list entry is refused at consumption, or None.

    Structural malformations (absolute path, ``..`` element, glob
    metacharacters) plus the hub-side iron-rule shapes: a bare family
    container names no participant (one such line would delete a whole
    clan, and because the lists are cumulative and append-only it would
    sit in the ledger forever), ``gc/`` internals are gc.py's own tree
    (the only legal entry is a compression entry
    ``gc/delete-list.NNNNNN``), and a PROTECTED system asset is refused
    even on the hub's audited bypass path."""
    parts = raw.split("/")
    if (not raw or raw.startswith("/")
            or any(p in ("", "..") for p in parts)):
        return "malformed path"
    if len(parts) == 1 and parts[0] in GC_BARE_FAMILY_ENTRIES:
        return "bare family container (names no participant)"
    for prot in GC_PROTECTED_PATHS:
        pp = prot.strip("/").split("/")
        if parts[:len(pp)] == pp:
            return "protected system asset"
    if parts[0] == GC_DIR_NAME and not (
            len(parts) == 2 and parts[1].startswith(GC_LIST_PREFIX)
            and not is_dir):
        return "gc/ self-tree (only list-compression entries are legal)"
    return None


def _gc_report_refusals(refused):
    """One summary line per GC_REFUSE_REPORT_GAP seconds (never one line
    per entry: every pull round reads every list)."""
    if not refused:
        return
    now = time.time()
    last, suppressed = _gc_refuse_reported
    if last and now - last < GC_REFUSE_REPORT_GAP:
        _gc_refuse_reported[1] = suppressed + len(refused)
        return
    extra = f" (+{suppressed} suppressed since the last report)" \
        if suppressed else ""
    _gc_refuse_reported[0] = now
    _gc_refuse_reported[1] = 0
    wlog(f"warning: gc: refused {len(refused)} delete-list entrie(s){extra} "
         f"(malformed, bare family container, gc/ self-tree or protected "
         f"asset — they would not delete until fixed; the shape authority "
         f"is dsync/gc.py): {refused[:5]}")


def gc_exclude_and_targets(local_dir):
    """Split delete-list entries into (exclude_patterns, targets
    refused). exclude_patterns are anchored rsync exclude patterns
    ('/rel' for files, '/rel/' for directory subtrees); targets are
    (relpath, is_dir) for local deletion. THE LIST IS TRUSTED: iron
    rules (bot/ immortality) are
    enforced hub-side ONLY by dsync/gc.py validate_entry (single
    point of generation and validation user
    decision removed the consumer-side duplicate). Entries are refused
    ONLY when structurally malformed (absolute paths, '..' elements
    glob metacharacters — same defense as the pull protect list, these
    would corrupt rsync pattern semantics) or when they touch ``gc/``
    internals (the sync layer's own gc/ tree is self-managed; the only
    legal gc/ entry is a compression entry
    ``gc/delete-list.NNNNNN``).

    Residual gap, informed-accepted: a list planted on this node that names a
    hub-side PROTECTED path is applied locally (no PROTECTED filter here).
    agents/ deletions do not propagate, so the loss is machine-local and
    recoverable by pulling from the hub. Adding a consumer-side filter was
    ruled out; re-open triggers =
    the workspace decision log."""
    excludes, targets, refused = [], [], []
    for e in read_gc_lists(local_dir):
        raw = e.rstrip("/")
        is_dir = e.endswith("/")
        if any(c in GLOB_UNSAFE for c in e) or _gc_shape_refused(raw, is_dir):
            refused.append(e)
            continue
        if is_dir:
            excludes.append("/" + raw + "/")
        else:
            excludes.append("/" + raw)
        targets.append((raw, is_dir))
    _gc_report_refusals(refused)
    return excludes, targets, refused


def apply_gc_deletions(local_dir):
    """Delete local copies of every delete-list entry (files, or whole
    subtrees for 'dir/' entries). Idempotent: missing paths are no-ops;
    per-path errors are logged and retried on the next cycle. Type
    mismatches converge instead of retrying forever: a file entry at a
    directory deletes the subtree, a dir entry at a symlink/file
    removes the link/file itself (review S2 + bug2). Returns the
    number of paths actually removed."""
    _excl, targets, refused = gc_exclude_and_targets(local_dir)
    deleted = 0
    for rel, is_dir in targets:
        p = os.path.join(local_dir, rel)
        try:
            if not os.path.lexists(p):
                continue
            if is_dir:
                if os.path.islink(p):
                    os.remove(p)  # consume the link; target survives
                    deleted += 1
                elif os.path.isdir(p):
                    shutil.rmtree(p)
                    deleted += 1
                else:
                    wlog(f"warning: gc: {rel}: directory entry at a "
                         f"plain file; removing the file")
                    os.remove(p)
                    deleted += 1
            else:
                if os.path.islink(p):
                    os.remove(p)
                elif os.path.isdir(p):
                    wlog(f"warning: gc: {rel}: file entry at a "
                         f"directory; removing the subtree")
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                deleted += 1
        except OSError as e:
            wlog(f"warning: gc: failed to delete {rel}: {e}")
    if deleted:
        wlog(f"pull: gc: deleted {deleted} locally-held delete-list "
             f"path(s)")
    return deleted

def die(msg):
    print(f"ssh-sync: error: {msg}", file=sys.stderr)
    sys.exit(1)


def ssh_config_path():
    cfg = os.environ.get("SSH_SYNC_CONFIG") or DEFAULT_SSH_CONFIG
    if not os.path.isfile(cfg):
        die(f"ssh config not found: {cfg}\n"
            f"       run `make env.materialize` in the workspace first "
            f"(or set SSH_SYNC_CONFIG).")
    return cfg


def parse_remote(spec):
    """Validate and split a <host>:<path> spec."""
    if ":" not in spec:
        die(f"invalid remote spec {spec!r}: expected <host>:<remote_dir>")
    host, _, path = spec.partition(":")
    if not host or not path:
        die(f"invalid remote spec {spec!r}: both <host> and <remote_dir> "
            f"must be non-empty")
    return host, path


def run(cmd, verbose, check=True, capture=False):
    if verbose:
        print("+ " + " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    r = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        text=True,
    )
    if check and r.returncode != 0:
        die(f"command failed (exit {r.returncode}): "
            + " ".join(shlex.quote(c) for c in cmd))
    return r


def preflight(cfg, host):
    print(f"preflight: checking ssh connectivity to {host!r} ...",
          file=sys.stderr)
    cmd = ["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS + [host, "true"]
    r = run(cmd, verbose=False, check=False)
    if r.returncode != 0:
        die(f"cannot reach host {host!r} (ssh exit {r.returncode}). "
            f"Check the host alias in {cfg}, network, and credentials.")


def rsync_cmd(cfg, args, verbose):
    """One-shot push/pull/both command: plain -a, no overwrite
    arbitration (watch mode builds its own group-gate pipelines)."""
    cmd = [RSYNC_BIN, "-a"]
    if args.delete:
        cmd.append("--delete")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.bwlimit is not None:
        cmd += ["--bwlimit", str(args.bwlimit)]
    for pat in args.exclude:
        cmd += ["--exclude", pat]
    if verbose:
        cmd.append("-v")
    cmd += ["-e", " ".join(["ssh", "-F", shlex.quote(cfg)] + SSH_TIMEOUT_OPTS)]
    return cmd


def do_push(cfg, host, local_dir, remote_dir, args):
    if not os.path.isdir(local_dir):
        # Same as pull: create the local dir (with parents) on demand so
        # the no-arg defaults work out of the box.
        os.makedirs(local_dir, exist_ok=True)
    # Ensure the remote directory exists (rsync only creates the last
    # path component; create parents explicitly).
    run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS
        + [host, "mkdir -p", "--", remote_dir],
        args.verbose, check=not args.dry_run)
    src = local_dir.rstrip("/") + "/"
    dst = f"{host}:{remote_dir}"
    print(f"push: {src} -> {dst}", file=sys.stderr)
    run(rsync_cmd(cfg, args, args.verbose) + [src, dst], args.verbose)


def do_pull(cfg, host, local_dir, remote_dir, args):
    os.makedirs(local_dir, exist_ok=True)
    src = f"{host}:{remote_dir.rstrip('/')}/"
    dst = local_dir.rstrip("/") + "/"
    print(f"pull: {src} -> {dst}", file=sys.stderr)
    run(rsync_cmd(cfg, args, args.verbose) + [src, dst], args.verbose)

# ---------------------------------------------------------------------------
# Group gate helpers
# ---------------------------------------------------------------------------

def resolve_replica_gid():
    """Resolve the local replica group gid; refuse to run without it."""
    try:
        return grp.getgrnam(REPLICA_GROUP).gr_gid
    except KeyError:
        die(f"group {REPLICA_GROUP!r} not found on this machine "
            f"(watch's group gate needs it). Fix: sudo groupadd "
            f"{REPLICA_GROUP} && sudo usermod -aG {REPLICA_GROUP} $USER")


def check_replica_membership(local_dir, replica_gid):
    """Non-fatal variant of the chgrp self-test: True = the running
    user can still chgrp to replica. Used by the runtime spot-check
    (A-2): membership
    revocation / group recreation (new gid) after startup was invisible
    before — rsync would silently stop marking. Best-effort: any probe
    error counts as failure, never raises."""
    probe = os.path.join(local_dir, ".ssh-sync-chown-probe")
    try:
        with open(probe, "w") as f:
            f.write("")
        os.chown(probe, -1, replica_gid)
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def probe_replica_membership(local_dir, replica_gid):
    """Functional self-test: chgrp a probe file to replica. Membership
    is required (chgrp to a non-member group is EPERM) and rsync would
    SILENTLY ignore failing --chown — so fail fast here instead of
    running a link that marks nothing."""
    if not check_replica_membership(local_dir, replica_gid):
        die(f"cannot chgrp to {REPLICA_GROUP!r} as the current user. "
            f"The running user must be a MEMBER of the group: "
            f"sudo usermod -aG {REPLICA_GROUP} $USER (then start watch "
            f"in a fresh login session).")


def probe_remote_replica_group(cfg, host):
    """The receiver of push resolves --chown=:replica by NAME locally;
    the group must exist there too. getent covers Linux; dscl covers
    macOS (no getent there)."""
    cmd = ["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS \
        + [host, f"getent group {REPLICA_GROUP} 2>/dev/null || "
                 f"dscl . -read /Groups/{REPLICA_GROUP} PrimaryGroupID "
                 f"2>/dev/null"]
    r = run(cmd, verbose=False, check=False, capture=True)
    if r.returncode != 0:
        die(f"group {REPLICA_GROUP!r} not found on remote {host!r} "
            f"(push's --chown=:replica resolves the name on the "
            f"receiver). Fix on {host}: sudo groupadd {REPLICA_GROUP} "
            f"&& sudo usermod -aG {REPLICA_GROUP} <user>")


def unmarked_files(local_dir, replica_gid, excludes=()):
    """(relpaths, ok): regular files of local_dir whose group is NOT
    replica — the local ORIGINALS. ok=False if the walk hit an error
    callers must abort the cycle rather than act on a partial list
    (a truncated pull protect list could expose originals).
    ``excludes`` drops sync-face-excluded names — the
    watch link passes WATCH_EXCLUDES (currently empty) so an excluded
    name is neither a push candidate nor a pull-protect entry (it is
    never received, so protecting it is moot)."""
    out = []
    errors = []

    def onerror(e):
        errors.append(e)

    for root, dirs, files in os.walk(local_dir, onerror=onerror):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, local_dir)
            if excludes and _excluded(rel, excludes):
                continue
            try:
                st = os.lstat(p)
            except OSError:
                continue  # raced with deletion: harmless either way
            if not statmod.S_ISREG(st.st_mode):
                continue
            if st.st_gid != replica_gid:
                out.append(rel)
    return out, not errors

# ---------------------------------------------------------------------------
# Watch mode — resident bidirectional sync daemon (group gate)
# ---------------------------------------------------------------------------

_STOP = threading.Event()          # set by SIGTERM/SIGINT
_REMOTE_STREAM_PROC = None         # ssh subprocess of the remote event stream


class ChangeSignal:
    """A detector's ONLY output: "something changed at T".

    FIRST-event window semantics — the timestamp is recorded when nothing
    is pending and later events only bump the count — so under continuous
    churn a cycle still fires within ``debounce`` of the FIRST pending
    event. A last-event window would never satisfy that condition on a
    busy tree and would silently starve the event path.

    No paths, no types, no completeness flags: a cycle is whole-tree, so
    the only thing a detector can contribute is WHEN to run one. Anything
    a detector cannot vouch for (an unreadable subtree, a dropped event
    queue, a dead stream, an unparseable line) is reported the same way as
    a change — one more reason to run a cycle — and the ``--interval``
    deadline bounds how long anything may stay unseen regardless.
    """

    def __init__(self, name):
        self.name = name
        self._lock = threading.Lock()
        self._ts = 0.0
        self._n = 0

    def note(self, n=1):
        with self._lock:
            if self._n == 0:
                self._ts = time.time()   # first pending event
            self._n += n

    def pending_since(self):
        """Timestamp of the oldest pending event, or None when idle."""
        with self._lock:
            return self._ts if self._n else None

    def take(self):
        """(timestamp, count) — consumes the pending events."""
        with self._lock:
            ts, n = self._ts, self._n
            self._n = 0                  # ts resets with the next first event
            return ts, n


_LOCAL_SIG = ChangeSignal("local")    # local detectors -> push side
_REMOTE_SIG = ChangeSignal("remote")  # remote event stream -> pull side


def cycle_trigger(now, next_forced, debounce, signals):
    """Why a cycle runs now, or None when nothing is due.

    The forced deadline is evaluated FIRST, before any pending-event
    check: it is the completeness bound, so a tree with no events at all
    (dead detector, no local backend, dropped event queue) still
    re-anchors every ``interval`` seconds. Evaluating it after a
    short-circuit would make that bound event luck instead of a time
    guarantee."""
    if now >= next_forced:
        return "interval deadline"
    for sig in signals:
        ts = sig.pending_since()
        if ts is not None and now - ts >= debounce:
            return f"{sig.name} change"
    return None

# Cycle counters, summarized once per --stats-every (a cycle that moves
# nothing logs nothing at all, so the counters are the link's heartbeat).
_STATS_LOCK = threading.Lock()
_STATS = {"push_cycles": 0, "push_files": 0, "push_idle": 0,
          "pull_cycles": 0, "pull_files": 0, "pull_idle": 0,
          "errors": 0}


def _bump(key, n=1):
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + n


def _stats_line():
    with _STATS_LOCK:
        s = dict(_STATS)
    return (f"push {s['push_cycles']} cycle(s)/{s['push_files']} file(s)"
            f"/{s['push_idle']} idle, pull {s['pull_cycles']}"
            f"/{s['pull_files']}/{s['pull_idle']}, errors {s['errors']}")

def wlog(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} ssh-sync[watch]: {msg}", file=sys.stderr, flush=True)


def _signal_handler(signum, _frame):
    # Only flip the flag. Writing to stderr from INSIDE a signal handler
    # can hit the buffered writer re-entrantly (RuntimeError: reentrant
    # call inside <_io.BufferedWriter>), which costs the process its
    # graceful exit and makes the supervisor escalate to SIGKILL. The
    # loop logs on its way out instead.
    _STOP.set()


def _excluded(relpath, patterns):
    """fnmatch filter kept for the trigger listings (watch ships with an
    empty pattern list; the CLI --exclude is one-shot-only)."""
    base = os.path.basename(relpath)
    for pat in patterns:
        if fnmatch.fnmatch(relpath, pat) or fnmatch.fnmatch(base, pat):
            return True
    return False

def _watch_ssh_e(cfg):
    return ["-e", " ".join(["ssh", "-F", shlex.quote(cfg)] + SSH_TIMEOUT_OPTS)]


def _watch_optional_flags(args):
    out = []
    if args.dry_run:
        out.append("--dry-run")
    if args.bwlimit is not None:
        out += ["--bwlimit", str(args.bwlimit)]
    return out

def _rsync_transferred(stdout):
    """(files, dirs) named in rsync -v's OWN output — the authoritative
    record of what actually moved (a file list diff would mislabel: it
    cannot tell a protected original that merely EXISTS on both sides from
    one that was overwritten).

    Header/summary/diagnostic lines are filtered by prefix; a name ending
    in '/' is a created directory. Parsing limits: a filename containing a
    newline would be split into two entries (the agents/ layout forbids
    newlines and glob characters in names — the same convention the glob
    defense relies on), and any stray line becomes a harmless lstat miss
    below."""
    files, dirs = set(), set()
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith("/"):
            dirs.add(line.rstrip("/"))
            continue
        if line.startswith(("sent ", "sending ", "received ", "total size",
                            "rsync", "building ", "delta-transmission ",
                            "Speedup", "cd++")):
            continue
        files.add(line)
    return files, dirs


def watch_rsync(cfg, host, local_dir, remote_dir, args, direction,
                replica_gid):
    """Run ONE whole-tree group-gate transfer. Returns ``(ok, n_moved)``.

    Both directions always negotiate the WHOLE tree — rsync's own content
    checksums (``-c``) decide what moves — so a cycle is correct whatever
    the detectors saw, and nothing here needs a candidate list:

    + push: whitelist = every local regular file NOT marked ``replica``
      (my originals) → ``--files-from=-``; lands marked on the receiver;
    + pull: blacklist = my unmarked originals (protect) + gc delete-list +
      ``WATCH_EXCLUDES`` (currently empty) → ``--exclude-from=-``;
      everything else arrives and is marked ``replica``.

    ``n_moved`` comes from rsync -v's own output, so a cycle that moved
    nothing is silent by construction. Any non-zero exit is a failure: the
    loop backs off and the next cycle re-negotiates the whole tree, so
    there is no partial state to repair or re-queue.
    """
    os.makedirs(local_dir, exist_ok=True)
    if direction == "push":
        # The hub directory is (re)created every cycle: it is one
        # multiplexed ssh exec, and it makes "the hub dir went away"
        # self-healing instead of a permanent loud failure.
        r = run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS
                + [host, "mkdir -p", "--", remote_dir],
                args.verbose, check=False)
        if r.returncode != 0:
            wlog(f"error: push: remote mkdir -p failed (ssh exit "
                 f"{r.returncode}); skipping this cycle")
            _bump("errors")
            return False, 0
        files, ok = unmarked_files(local_dir, replica_gid, WATCH_EXCLUDES)
        if not ok:
            wlog("error: push: local walk failed; skipping this cycle "
                 "(refusing to act on a partial whitelist)")
            _bump("errors")
            return False, 0
        if not files:
            _bump("push_cycles")
            _bump("push_idle")
            return True, 0     # nothing of mine here: no rsync at all
        src = local_dir.rstrip("/") + "/"
        dst = f"{host}:{remote_dir}"
        cmd = ([RSYNC_BIN, WATCH_RSYNC_FLAGS, "--files-from=-", "--from0",
                f"--chown=:{REPLICA_GROUP}"]
               + _WATCH_REMOTE_RSYNC_OPTS
               + _watch_optional_flags(args) + _watch_ssh_e(cfg)
               + [src, dst])
        stdin = "\0".join(sorted(files)) + "\0"
    else:
        src = f"{host}:{remote_dir.rstrip('/')}/"
        dst = local_dir.rstrip("/") + "/"
        # GC delete-list: listed paths are NEVER received (anchored
        # exclude patterns) and their local copies are deleted after the
        # transfer (below).
        gc_excludes, _gc_targets, _gc_refused = gc_exclude_and_targets(
            local_dir)
        # Protect list = my originals. A truncated one could expose them,
        # so a walk error aborts the cycle instead of pulling.
        unmarked, ok = unmarked_files(local_dir, replica_gid, WATCH_EXCLUDES)
        if not ok:
            wlog("error: pull: local walk failed; skipping this cycle "
                 "(a truncated protect list could expose originals)")
            _bump("errors")
            return False, 0
        protect, refused = [], []
        for rel in unmarked:
            if any(c in GLOB_UNSAFE for c in rel):
                refused.append(rel)  # pattern semantics would corrupt it
            else:
                # A leading '/' anchors the pattern at the transfer root
                # (an unanchored slashless entry would match on BASENAME
                # anywhere in the tree).
                protect.append("/" + rel)
        if refused:
            wlog(f"warning: glob defense: refused {len(refused)} path(s) "
                 f"with wildcard chars from the pull protect list (they "
                 f"would not sync until renamed): {refused[:5]}")
        cmd = ([RSYNC_BIN, WATCH_RSYNC_FLAGS, "--exclude-from=-",
                f"--chown=:{REPLICA_GROUP}"]
               + _WATCH_REMOTE_RSYNC_OPTS
               + _watch_optional_flags(args) + _watch_ssh_e(cfg)
               + [src, dst])
        # WATCH_EXCLUDES ride along as unanchored patterns (a pattern
        # matches the basename at any depth). The list is currently empty;
        # the slot stays so re-adding one is a single constant edit.
        stdin = "".join(p + "\n"
                        for p in protect + gc_excludes + WATCH_EXCLUDES)
        if args.verbose:
            wlog(f"pull: protecting {len(protect)} local original(s) from "
                 f"overwrite"
                 + (f" (e.g. {protect[:3]})" if protect else "")
                 + (f"; excluding {len(gc_excludes)} gc delete-list "
                    f"path(s)" if gc_excludes else "")
                 + f"; excluding {WATCH_EXCLUDES}")

    _bump(f"{direction}_cycles")
    try:
        r = subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        wlog(f"error: {direction}: rsync timed out after 600s")
        _bump("errors")
        return False, 0
    if r.returncode != 0:
        tail = (r.stdout or "").strip().splitlines()[-3:]
        wlog(f"error: {direction}: rsync failed (exit {r.returncode}): "
             + " | ".join(tail)[:300])
        _bump("errors")
        return False, 0

    moved, moved_dirs = _rsync_transferred(r.stdout)
    if direction == "pull":
        # Marking fix-up: --chown=:replica is SILENTLY IGNORED by some
        # rsync builds (observed: brew rsync 3.5.0 on macOS ignores both
        # named and numeric --chown), leaving pulled replicas UNMARKED. In
        # that state the push side would ship the whole tree back and the
        # pull side would protect the whole tree (existing files would
        # never update again) — the group gate would be effectively off. An
        # idempotent local chgrp of exactly the TRANSFERRED files is the
        # authoritative marking. On receivers where --chown works this walk
        # finds nothing to do.
        fixed = 0
        for rel in moved:
            p = os.path.join(local_dir, rel)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not statmod.S_ISREG(st.st_mode) or st.st_gid == replica_gid:
                continue
            try:
                os.chown(p, -1, replica_gid)
                fixed += 1
            except OSError:
                pass  # best-effort; the next cycle retries
        if fixed:
            wlog(f"pull: marked {fixed} pulled replica file(s) as "
                 f"{REPLICA_GROUP!r} (post-pull fix-up)")
        if sys.platform == "darwin" and moved_dirs:
            # Directory group undo: macOS new files INHERIT the parent
            # directory's group, so leaving RECEIVED DIRECTORIES tagged
            # replica would make this machine's new files born tagged →
            # skipped by the push whitelist → its outputs would never reach
            # the hub. Directories carry no gate state (the gate is
            # file-level), so chgrp the transferred dirs back to the
            # process primary group. Idempotent: only dirs currently tagged
            # replica are touched (and --dry-run tags nothing).
            dfixed = 0
            for rel in moved_dirs:
                p = os.path.join(local_dir, rel)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not statmod.S_ISDIR(st.st_mode) \
                        or st.st_gid != replica_gid:
                    continue
                try:
                    os.chown(p, -1, os.getgid())
                    dfixed += 1
                except OSError:
                    pass  # best-effort
            if dfixed:
                wlog(f"pull: restored {dfixed} directory group(s) to "
                     f"primary gid {os.getgid()} (darwin inherit undo)")
        # GC delete-list application: AFTER a successful transfer, re-read
        # the lists — a list that arrived in THIS transfer takes effect
        # immediately — and delete local copies of every listed path
        # (including unmarked node originals at listed paths: that closes
        # the only resurrection window). Every cycle runs a real transfer,
        # so gc converges every cycle, including one that moved nothing.
        # Idempotent/replayable; skipped under --dry-run.
        if not args.dry_run:
            apply_gc_deletions(local_dir)

    _bump(f"{direction}_files", len(moved))
    if not moved:
        _bump(f"{direction}_idle")
    return True, len(moved)

def local_inotify_thread(local_dir):
    """Recursive inotify watcher. Contract: report THAT something changed
    under ``local_dir`` — never what, and never what it means (a cycle is
    whole-tree, so a detector's only contribution is WHEN to run one).

    inotify is not recursive, so a directory CREATE/MOVED_TO still extends
    the watch set. ``WATCH_EXCLUDES`` names (currently none) are dropped
    here so an excluded name's churn cannot wake the loop. Everything the watcher cannot
    vouch for (an unwritable subtree, a read error, a dropped event queue)
    notes an event like any change: a cycle re-anchors, and the
    ``--interval`` deadline bounds the rest."""
    ino = INotify()
    mask = (iflags.CREATE | iflags.CLOSE_WRITE | iflags.MODIFY
            | iflags.DELETE | iflags.MOVED_FROM | iflags.MOVED_TO)
    wd2path = {}

    def add_tree(root):
        try:
            wd = ino.add_watch(root, mask)
            wd2path[wd] = root
        except OSError as e:
            wlog(f"warning: cannot watch {root}: {e}")
            _LOCAL_SIG.note()
            return
        try:
            for entry in os.scandir(root):
                if entry.is_dir(follow_symlinks=False):
                    add_tree(entry.path)
        except OSError:
            pass

    add_tree(local_dir)
    wlog(f"local watcher: inotify active ({len(wd2path)} initial watches)")
    while not _STOP.is_set():
        try:
            events = ino.read(timeout=1000)
        except OSError as e:
            if _STOP.is_set():
                break
            wlog(f"warning: local inotify read failed ({e}); the window is "
                 f"unobserved (a cycle re-anchors it)")
            _LOCAL_SIG.note()
            time.sleep(1)
            continue
        noted = 0
        for ev in events:
            if ev.mask & iflags.Q_OVERFLOW:
                noted += 1          # events dropped: let a cycle re-anchor
                continue
            parent = wd2path.get(ev.wd, local_dir)
            isdir = bool(ev.mask & iflags.ISDIR)
            if isdir and (ev.mask & (iflags.CREATE | iflags.MOVED_TO)) \
                    and ev.name:
                add_tree(os.path.join(parent, ev.name))
                noted += 1          # a new subtree may hold shippable files
                continue
            if _excluded(ev.name or "", WATCH_EXCLUDES):
                continue
            noted += 1
        if noted:
            _LOCAL_SIG.note(noted)
    try:
        ino.close()
    except OSError:
        pass


# Remote event stream: `ssh host python3 -c <this> <dir> <excludes>`.
# One `EV <path>` line per event; the reader only counts them (see
# ChangeSignal — no typing, no paths kept). An unwritable subtree and a
# dropped event queue are reported as an event too, so the reader cannot
# miss "the remote watcher could not vouch for completeness".
REMOTE_PY_INOTIFY_SCRIPT = r"""
import fnmatch, os, sys
from inotify_simple import INotify, flags as F
root = sys.argv[1]
pats = [p for p in (sys.argv[2].split(',') if len(sys.argv) > 2 else []) if p]
MASK = (F.CREATE | F.CLOSE_WRITE | F.MODIFY | F.DELETE
        | F.MOVED_FROM | F.MOVED_TO)
ino = INotify()
wd2p = {}
def out(line):
    sys.stdout.write(line + '\n')
    sys.stdout.flush()
def add(d):
    try:
        wd = ino.add_watch(d, MASK)
    except OSError:
        out('EV unwatchable %s' % d)
        return
    wd2p[wd] = d
    try:
        for e in os.scandir(d):
            if e.is_dir(follow_symlinks=False):
                add(e.path)
    except OSError:
        pass
add(root)
while True:
    for ev in ino.read():
        if ev.mask & F.Q_OVERFLOW:
            out('EV overflow')
            continue
        name = ev.name or ''
        if any(fnmatch.fnmatch(name, p) for p in pats):
            continue
        p = os.path.join(wd2p.get(ev.wd, root), name)
        if (ev.mask & F.ISDIR) and (ev.mask & (F.CREATE | F.MOVED_TO)):
            add(p)
        out('EV %s' % p)
"""


def local_watchdog_thread(local_dir):
    """Recursive watchdog watcher (FSEvents backend on macOS, where
    inotify_simple cannot exist). Same contract as local_inotify_thread:
    note that something changed, nothing else."""

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            src = getattr(event, "src_path", "") or ""
            if not src:
                _LOCAL_SIG.note()   # unusable event: let a cycle re-anchor
                return
            if _excluded(os.path.basename(src), WATCH_EXCLUDES):
                return
            _LOCAL_SIG.note()

    obs = Observer()
    obs.schedule(Handler(), local_dir, recursive=True)
    obs.start()
    wlog("local watcher: watchdog (FSEvents) active")
    try:
        while not _STOP.wait(1.0):
            pass
    finally:
        obs.stop()
        obs.join(timeout=5)


def remote_stream_thread(cfg, host, remote_dir):
    """Stream remote filesystem events over ssh and note them.

    A stream that ends is RESTARTED with backoff (5/10/30 s, reset after a
    60 s healthy run); every restart attempt also notes an event, so the
    blind window is closed by the next cycle instead of being left to the
    ``--interval`` deadline. An unparseable line counts as an event (never
    guess)."""
    global _REMOTE_STREAM_PROC
    excludes = ",".join(WATCH_EXCLUDES)
    watch_cmd = (f"python3 -c {shlex.quote(REMOTE_PY_INOTIFY_SCRIPT)} "
                 f"{shlex.quote(remote_dir)} {shlex.quote(excludes)}")
    cmd = ["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS + [host, watch_cmd]
    backoff = 5.0
    while not _STOP.is_set():
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            wlog(f"error: cannot start the remote event stream: {e}; "
                 f"retrying in {backoff:.0f}s")
            _REMOTE_SIG.note()
            if _STOP.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)
            continue
        _REMOTE_STREAM_PROC = proc
        wlog(f"remote watcher: event stream active on {host}")
        started = time.time()
        while not _STOP.is_set():
            line = proc.stdout.readline()
            if not line:
                break
            if os.environ.get("SSH_SYNC_DEBUG"):
                wlog(f"dbg: remote event: {line.strip()[:120]}")
            _REMOTE_SIG.note()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if _STOP.is_set():
            return
        ran = time.time() - started
        if ran > 60:
            backoff = 5.0   # healthy long run: reset the reconnect backoff
        wlog(f"warning: remote event stream ended after {ran:.0f}s; "
             f"reconnecting in {backoff:.0f}s")
        _REMOTE_SIG.note()   # blind window: let the next cycle re-anchor
        if _STOP.wait(backoff):
            return
        backoff = min(backoff * 2, 30.0)

def _rsync_version_tuple(out):
    """Parse `rsync --version` output ('rsync  version X.Y.Z ...') into an
    (X, Y, Z) int tuple; None if it cannot be parsed."""
    try:
        tok = out.split()
        v = tok[tok.index("version") + 1]
        return tuple(int(x) for x in v.split(".")[:3])
    except (ValueError, IndexError):
        return None


RSYNC_MIN_VERSION = (3, 5, 0)


def _diagnose_rsync_versions(cfg, host, remote_bin=None):
    """DIAGNOSTIC (runs only after the end-to-end --chown probe failed):
    return one line per endpoint whose rsync cannot be determined or is
    older than RSYNC_MIN_VERSION. ``--chown`` needs >= 3.1.0 as a hard
    floor; older builds negotiate it and then fail to apply it on the
    receiver, which lands replicas unmarked."""
    notes = []
    try:
        r = subprocess.run([RSYNC_BIN, "--version"], stdout=subprocess.PIPE,
                           text=True, timeout=15)
        lv = _rsync_version_tuple(r.stdout)
    except Exception:
        lv = None
    try:
        # What rsync-over-ssh actually invokes on the remote (its
        # non-interactive PATH, or the pinned --rsync-path binary) — the
        # same resolution the transfers use.
        r = subprocess.run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS
                           + [host, f"{remote_bin or 'rsync'} --version"],
                           stdout=subprocess.PIPE, text=True, timeout=30)
        rv = _rsync_version_tuple(r.stdout)
    except Exception:
        rv = None
    for side, v in ((f"local ({RSYNC_BIN})", lv), (f"remote {host}", rv)):
        if v is None:
            notes.append(f"cannot determine the rsync version on {side} "
                         f"(--chown needs >= 3.1.0, >= "
                         f"{'.'.join(map(str, RSYNC_MIN_VERSION))} "
                         f"recommended)")
        elif v < RSYNC_MIN_VERSION:
            notes.append(f"rsync on {side} is {'.'.join(map(str, v))} "
                         f"(< {'.'.join(map(str, RSYNC_MIN_VERSION))}): "
                         f"--chown=:replica marking is unsafe with older "
                         f"builds — upgrade rsync on {side}")
    return notes


def _diagnose_remote_group(cfg, host):
    """DIAGNOSTIC (runs only after the end-to-end --chown probe failed):
    return one line per failed receiver-side check — does the group exist
    there, and can the ssh user (the same user rsync's receiver runs as)
    actually chgrp a file to it?"""
    notes = []
    r = run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS
            + [host, f"getent group {REPLICA_GROUP} 2>/dev/null || "
                     f"dscl . -read /Groups/{REPLICA_GROUP} PrimaryGroupID "
                     f"2>/dev/null"],
            verbose=False, check=False, capture=True)
    if r.returncode != 0:
        notes.append(f"group {REPLICA_GROUP!r} does not exist on {host} "
                     f"(push's --chown=:replica resolves the name on the "
                     f"receiver). Fix on {host}: sudo groupadd "
                     f"{REPLICA_GROUP} && sudo usermod -aG {REPLICA_GROUP} "
                     f"<user>")
    script = (
        'f=$(mktemp) || exit 1; '
        f'if chgrp {REPLICA_GROUP} "$f" 2>/dev/null && '
        '[ "$(stat -c %G "$f" 2>/dev/null || stat -f %Sg "$f")" '
        f'= "{REPLICA_GROUP}" ]; then rc=0; else rc=1; fi; '
        'rm -f "$f"; exit $rc')
    r = run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS + [host, script],
            verbose=False, check=False, capture=True)
    if r.returncode != 0:
        notes.append(f"the ssh user on {host} cannot chgrp a file to "
                     f"{REPLICA_GROUP!r} (EPERM: not a member, or the group "
                     f"was recreated). Fix on {host}: sudo usermod -aG "
                     f"{REPLICA_GROUP} <user>, then restart watch from a "
                     f"FRESH login session")
    return notes


def probe_rsync_chown_pipeline(cfg, host):
    """Startup gate: END-TO-END proof that this machine's rsync can land a
    replica mark on the receiver — actually transfer a probe file with
    ``--chown=:replica`` into a remote scratch dir and stat the landed
    group. This is the only check that proves the whole pipeline (sender
    build + negotiation + receiver build + receiver membership), which is
    why the finer-grained checks are diagnostics rather than gates.

    Returns None on success, or a one-line failure reason; the caller runs
    ``diagnose_chown_failure`` and refuses to start."""
    probe_dir = tempfile.mkdtemp(prefix="ssh-sync-chown-probe-")
    probe = os.path.join(probe_dir, "probe")
    remote_dir = None
    try:
        with open(probe, "w"):
            pass
        r = run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS
                + [host, "mktemp -d"], verbose=False, check=False,
                capture=True)
        if r.returncode != 0 or not (r.stdout or "").strip():
            return (f"remote {host!r}: cannot create a scratch dir for the "
                    f"--chown pipeline probe (ssh exit {r.returncode})")
        remote_dir = r.stdout.strip()
        cmd = [RSYNC_BIN, "-c", f"--chown=:{REPLICA_GROUP}"] \
            + _WATCH_REMOTE_RSYNC_OPTS + _watch_ssh_e(cfg) \
            + [probe, f"{host}:{remote_dir}/"]
        r = run(cmd, verbose=False, check=False, capture=True)
        if r.returncode != 0:
            return (f"remote {host!r}: the --chown pipeline probe rsync "
                    f"failed (exit {r.returncode}); local rsync is "
                    f"{RSYNC_BIN} — a failing --chown transfer means "
                    f"replicas would land unmarked and the group gate "
                    f"breaks")
        r = run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS
                + [host, "stat -c %G -- "
                   f"{shlex.quote(remote_dir + '/probe')} 2>/dev/null || "
                   "stat -f %Sg -- "
                   f"{shlex.quote(remote_dir + '/probe')}"],
                verbose=False, check=False, capture=True)
        landed = (r.stdout or "").strip()
        if landed != REPLICA_GROUP:
            return (f"remote {host!r}: the --chown pipeline probe landed "
                    f"group {landed!r}, expected {REPLICA_GROUP!r} (local "
                    f"rsync {RSYNC_BIN}) — the sender/receiver pair "
                    f"negotiates --chown but the receiver cannot apply it, "
                    f"so every replica would land unmarked and the group "
                    f"gate breaks")
        return None
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        if remote_dir:
            run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS
                + [host, "rm -rf", "--", remote_dir],
                verbose=False, check=False, capture=True)


def diagnose_chown_failure(cfg, host, remote_bin=None):
    """Log one line per root-cause candidate for a failed end-to-end
    --chown probe (the fine-grained checks it subsumes)."""
    notes = (_diagnose_remote_group(cfg, host)
             + _diagnose_rsync_versions(cfg, host, remote_bin=remote_bin))
    for n in notes:
        wlog(f"diagnosis: {n}")
    if not notes:
        wlog("diagnosis: the fine-grained receiver checks all passed — "
             "suspect the rsync pair itself (rerun with -v to see the "
             "transfer)")

def watch(args, cfg, host, local_dir, remote_dir):
    interval = args.interval
    debounce = args.debounce
    min_cycle = args.min_cycle
    stats_every = args.stats_every

    # --- Group gate: three gates, the last one end-to-end ---------------
    replica_gid = resolve_replica_gid()
    wlog(f"group gate: local {REPLICA_GROUP!r} group resolved "
         f"(gid {replica_gid})")
    os.makedirs(local_dir, exist_ok=True)
    probe_replica_membership(local_dir, replica_gid)
    wlog(f"group gate: chown self-test passed (the running user can chgrp "
         f"to {REPLICA_GROUP})")

    # Pin the remote rsync when the known-good location exists there (this
    # covers the remote program in BOTH pipeline directions).
    global _WATCH_REMOTE_RSYNC_OPTS
    r = run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS
            + [host, f"test -x {PREFERRED_RSYNC}"],
            verbose=False, check=False)
    if r.returncode == 0:
        _WATCH_REMOTE_RSYNC_OPTS = ["--rsync-path", PREFERRED_RSYNC]
        wlog(f"group gate: remote rsync pinned to {PREFERRED_RSYNC} "
             f"(--rsync-path)")
    else:
        _WATCH_REMOTE_RSYNC_OPTS = []
        wlog(f"warning: {PREFERRED_RSYNC} not found on remote {host}; the "
             f"remote rsync resolves via its non-interactive PATH (an old "
             f"rsync there can break --chown marking)")

    why = probe_rsync_chown_pipeline(cfg, host)
    if why:
        diagnose_chown_failure(
            cfg, host,
            remote_bin=(_WATCH_REMOTE_RSYNC_OPTS[1]
                        if _WATCH_REMOTE_RSYNC_OPTS else None))
        die(f"group gate: {why}")
    wlog(f"group gate: end-to-end --chown pipeline probe passed (landed "
         f"group {REPLICA_GROUP!r} on {host}); local rsync {RSYNC_BIN}")

    if args.exclude:
        wlog(f"warning: watch mode ignores --exclude (the only sync-face "
             f"exclusions are the built-in {WATCH_EXCLUDES or '(none)'}); "
             f"got {args.exclude!r}")

    # --- Detectors ------------------------------------------------------
    if HAVE_INOTIFY:
        local_mode = "inotify"
    elif HAVE_WATCHDOG:
        local_mode = "watchdog"
    else:
        local_mode = "none"
    r = run(["ssh", "-F", cfg] + SSH_TIMEOUT_OPTS
            + [host, "python3 -c 'import inotify_simple'"],
            args.verbose, check=False)
    if r.returncode != 0:
        die(f"remote {host!r} has no python3 + inotify_simple, so the "
            f"remote event stream cannot run and this link would be blind "
            f"to hub-side changes between two --interval deadlines. Fix on "
            f"{host}: python3 -m pip install inotify_simple")

    wlog(f"starting watch: {local_dir} <-> {host}:{remote_dir} "
         f"(debounce={debounce}s min-cycle={min_cycle}s "
         f"interval={interval}s stats-every={stats_every:.0f}s)")
    wlog(f"local detection mode: {local_mode}"
         + ("" if HAVE_INOTIFY else
            (" (watchdog/FSEvents; Linux prefers `pip install "
             "inotify_simple`)" if HAVE_WATCHDOG else
             " (no inotify_simple/watchdog installed; the --interval "
             "deadline alone drives this link)")))
    wlog(f"remote detection mode: pyinotify (python3+inotify_simple "
         f"stream on {host})")
    wlog(f"cycle model: every cycle is whole-tree in both directions "
         f"(rsync -c arbitrates); sync-face excludes "
         f"{WATCH_EXCLUDES or '(none)'}; "
         f"gc delete-list consumed on pull; deletions never propagated "
         f"(no --delete)")

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    threads = []
    if local_mode == "inotify":
        t = threading.Thread(target=local_inotify_thread,
                             kwargs={"local_dir": local_dir}, daemon=True)
        t.start()
        threads.append(t)
    elif local_mode == "watchdog":
        t = threading.Thread(target=local_watchdog_thread,
                             kwargs={"local_dir": local_dir}, daemon=True)
        t.start()
        threads.append(t)
    t = threading.Thread(target=remote_stream_thread,
                         kwargs={"cfg": cfg, "host": host,
                                 "remote_dir": remote_dir}, daemon=True)
    t.start()
    threads.append(t)

    # --- Initial alignment: one whole-tree pair -------------------------
    # There is no baseline to take afterwards, so nothing written DURING
    # alignment can be swallowed: the first loop cycle re-negotiates the
    # whole tree and picks it up.
    wlog("initial alignment: push ...")
    ok_push, _n = watch_rsync(cfg, host, local_dir, remote_dir, args,
                              "push", replica_gid)
    wlog("initial alignment: pull ...")
    ok_pull, _n = watch_rsync(cfg, host, local_dir, remote_dir, args,
                              "pull", replica_gid)
    if not (ok_push and ok_pull):
        wlog("warning: initial alignment had errors; continuing anyway "
             "(loop errors are non-fatal)")

    # --- The single loop ------------------------------------------------
    wlog("watch loop running (one loop: whole-tree push + pull per cycle); "
         "Ctrl-C or SIGTERM to stop")
    next_allowed = time.time() + min_cycle
    next_forced = time.time() + interval
    next_stats = time.time() + stats_every
    while not _STOP.is_set():
        if _STOP.wait(0.1):
            break
        now = time.time()
        if now < next_allowed:
            continue        # rate floor
        # Pending signals are only consumed by a cycle that really runs,
        # so a change inside the rate floor is still pending afterwards.
        trigger = cycle_trigger(now, next_forced, debounce,
                                (_LOCAL_SIG, _REMOTE_SIG))
        if trigger is None:
            continue
        _, n_local = _LOCAL_SIG.take()
        _, n_remote = _REMOTE_SIG.take()
        t0 = time.time()
        ok_push, n_push = watch_rsync(cfg, host, local_dir, remote_dir,
                                      args, "push", replica_gid)
        ok_pull, n_pull = watch_rsync(cfg, host, local_dir, remote_dir,
                                      args, "pull", replica_gid)
        took = time.time() - t0
        now = time.time()
        next_forced = now + interval     # any cycle re-anchors everything
        next_allowed = now + min_cycle
        if not (ok_push and ok_pull):
            # Nothing to re-queue — a cycle is whole-tree, so the next one
            # re-negotiates everything. Back off to the safety-net cadence
            # instead of hot-looping on a hard failure (hub down, group
            # revoked): same shape as the supervisor's bounded backoff.
            next_allowed = now + max(min_cycle, interval)
        if n_push:
            wlog(f"push: {n_push} file(s) -> {host}:{remote_dir}")
        if n_pull:
            wlog(f"pull: {n_pull} file(s) <- {host}:{remote_dir}/")
        if os.environ.get("SSH_SYNC_DEBUG"):
            wlog(f"dbg: cycle ({trigger}; {n_local} local / {n_remote} "
                 f"remote event(s)) moved {n_push}+{n_pull} file(s) in "
                 f"{took:.2f}s")
        if now >= next_stats:
            next_stats = now + stats_every
            wlog(f"stats: {_stats_line()}")

    wlog("stopping (SIGTERM/SIGINT) ...")
    proc = _REMOTE_STREAM_PROC
    if proc is not None:
        proc.terminate()
    for t in threads:
        t.join(timeout=10)
    wlog("stopped.")

# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(
        description="Sync directories between local and a remote host "
                    "over rsync/ssh.",
        epilog="Remote specs look like <host>:<remote_dir>; hosts come "
               "from the ssh config (default: <workspace>/env/.live/"
               "ssh-hosts, override with SSH_SYNC_CONFIG). Positional "
               "push/pull/both REQUIRE explicit local_dir and "
               "host:remote_dir (the agents/ default link is owned by "
               "`watch` (resident agents/ link), so one-shot transfers must "
               "not silently target it). Only `watch` defaults to "
               f"local {DEFAULT_LOCAL_DIR} <-> {DEFAULT_REMOTE}.")
    sub = p.add_subparsers(dest="direction", required=True)

    def add_common(sp):
        sp.add_argument("--dry-run", action="store_true",
                        help="rsync -n: show what would change, do nothing")
        sp.add_argument("--bwlimit", type=int, metavar="KBPS", default=None,
                        help="bandwidth limit in KiB/s")
        sp.add_argument("--exclude", action="append", default=[],
                        metavar="PATTERN",
                        help="rsync exclude pattern (repeatable; one-shot "
                             "push/pull/both only — watch ignores it)")
        sp.add_argument("-v", "--verbose", action="store_true",
                        help="verbose: print commands, rsync -v")

    def add_delete(sp):
        # --delete 只属一次性 push/pull/both：watch 常驻链路永不删除是传输契约
        # （属组闸门，@agentd#sync-channel），故 watch 根本不认识该参数——argparse
        # 直接报错拒绝（接受但静默忽略是危险歧义）。
        sp.add_argument("--delete", action="store_true",
                        help="delete extraneous files at the destination "
                             "(default off; one-shot only — watch refuses it)")

    sp_push = sub.add_parser("push", help="local_dir -> host:remote_dir")
    sp_push.add_argument("local_dir",
                         help="local directory (required; the agents/ "
                              "default link is owned by the resident watch link, "
                              "pass dirs explicitly)")
    sp_push.add_argument("remote", metavar="host:remote_dir",
                         help="remote spec (required)")
    add_common(sp_push)
    add_delete(sp_push)

    sp_pull = sub.add_parser("pull", help="host:remote_dir -> local_dir")
    sp_pull.add_argument("remote", metavar="host:remote_dir",
                         help="remote spec (required)")
    sp_pull.add_argument("local_dir",
                         help="local directory (required; the agents/ "
                              "default link is owned by the resident watch link, "
                              "pass dirs explicitly)")
    add_common(sp_pull)
    add_delete(sp_pull)

    sp_both = sub.add_parser("both", help="push then pull (bidirectional)")
    sp_both.add_argument("local_dir",
                         help="local directory (required; the agents/ "
                              "default link is owned by the resident watch link, "
                              "pass dirs explicitly)")
    sp_both.add_argument("remote", metavar="host:remote_dir",
                         help="remote spec (required)")
    add_common(sp_both)
    add_delete(sp_both)

    sp_watch = sub.add_parser(
        "watch", help="resident bidirectional sync daemon (group gate; one "
                      "loop, whole-tree cycles)")
    sp_watch.add_argument("local_dir", nargs="?", default=DEFAULT_LOCAL_DIR,
                          help=f"local directory (default: {DEFAULT_LOCAL_DIR})")
    sp_watch.add_argument("remote", metavar="host:remote_dir", nargs="?",
                          default=DEFAULT_REMOTE,
                          help=f"remote spec (default: {DEFAULT_REMOTE})")
    # 注：watch 故意不加 add_delete——常驻链路永不删除是传输契约（见 add_delete 注释）。
    add_common(sp_watch)
    sp_watch.add_argument("--interval", type=float,
                          default=DEFAULT_INTERVAL, metavar="SEC",
                          help="forced cadence AND completeness bound: a "
                               "cycle runs at least every SEC seconds even "
                               "when no event was seen (default "
                               f"{DEFAULT_INTERVAL:.0f})")
    sp_watch.add_argument("--debounce", type=float,
                          default=DEFAULT_DEBOUNCE, metavar="SEC",
                          help="window after the FIRST pending detector "
                               "event before a cycle fires (default "
                               f"{DEFAULT_DEBOUNCE})")
    sp_watch.add_argument("--min-cycle", type=float,
                          default=DEFAULT_MIN_CYCLE, metavar="SEC",
                          help="rate floor between two cycles (default "
                               f"{DEFAULT_MIN_CYCLE})")
    sp_watch.add_argument("--stats-every", type=float,
                          default=DEFAULT_STATS_EVERY, metavar="SEC",
                          help="counter summary cadence (default "
                               f"{DEFAULT_STATS_EVERY:.0f})")

    args = p.parse_args()

    if shutil.which("rsync") is None:
        die("rsync not found in PATH; please install rsync first "
            "(e.g. `apt install rsync` / `yum install rsync`).")

    host, remote_dir = parse_remote(args.remote)
    cfg = ssh_config_path()
    preflight(cfg, host)

    if args.direction == "push":
        do_push(cfg, host, args.local_dir, remote_dir, args)
    elif args.direction == "pull":
        do_pull(cfg, host, args.local_dir, remote_dir, args)
    elif args.direction == "watch":
        watch(args, cfg, host, args.local_dir, remote_dir)
    else:  # both: push first, then pull
        do_push(cfg, host, args.local_dir, remote_dir, args)
        do_pull(cfg, host, args.local_dir, remote_dir, args)

    print("done.", file=sys.stderr)


if __name__ == "__main__":
    main()
