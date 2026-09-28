#!/usr/bin/env python3
"""test_gc.py — unit tests for the GC delete-list mechanism
().

Covers:
+ dsync/gc.py: entry sanitization & iron rules (FORBIDDEN_LAYOUT_DIRS
  cleanup-exemption, empty since), cumulative list union, list
  compression, fixed-delay scheduling/reap, state-loss recovery.
+ dsync/ssh-sync.py pull side: delete-list parsing/sanitization,
  anchored exclude patterns, local deletion application (files +
  directory subtrees), idempotent replay, malformed-entry refusal.
+ fixes (): compression
  non-ingest of forged entries, S1 reap full sanitization vs
  '..'/absolute paths, S2 symlink convergence on both sides, bug2
  dir-entry normalization + type-mismatch convergence.
+ (2026-08-31 user decision): consumer-side iron rules
  REMOVED — the ssh-sync consumer trusts the delete list and applies
  it as-is (forged-list defense lives hub-side only: gc.py
  add/read-compression/reap); consumer still refuses structurally
  malformed entries and gc/ internals.
+ (2026-08-31 user decision): the AUDITED FORCE BYPASS —
  ``add --force`` accepts cleanup-exempt entries with an inline
  '#FORCED:' audit marker; default behavior unchanged (iron rule
  regression), the marker lifts ONLY the exemption rules, marked
  entries survive compression/reap, consumer strips the marker.
+ the schedule is a CACHE and ``add`` is its only writer: a lost or
  reset state deletes nothing (the surface never widens), ``list``
  reports the listed-but-unscheduled residue read-only, and an explicit
  ``add`` of those paths re-plans them; ``next_seq`` still resumes past
  the surviving lists; a forged list can never schedule a deletion by
  itself; ``add`` honors ``--delay`` (the propagation grace window).
+ hardening: a deletion must name a CONCRETE PARTICIPANT directory (bare
  family containers ``agents/ task/ bot/ topic/ run/`` refused even with
  ``--force``; legacy junk in an old list self-heals at the next
  compression, append-only intact); illegal-entry warnings are one
  summary line per read.
+ (2026-09-06 user decision): the bot/ IMMORTALITY IRON
  RULE REMOVED — bot/ subtrees are deletable through the gc channel
  like task/topic (no --force, no audit marker); FORBIDDEN_LAYOUT_DIRS
  is empty (mechanism + audited --force bypass kept for future exempt
  clans, covered here with a hypothetical clan); PROTECTED_SYSTEM_PATHS
  (topic/dispatcher) protection unchanged.

Self-contained: every case runs in a temp workspace. Run with plain
python3 (no pytest needed): `python3 dsync/test_gc.py`.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gc = _load("gc", os.path.join(_HERE, "gc.py"))
ss = _load("ssh_sync", os.path.join(_HERE, "ssh-sync.py"))

PASS = 0
FAIL = []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def mkws():
    """Fresh temp workspace with agents/ and run/."""
    ws = tempfile.mkdtemp(prefix="gc-test-")
    os.makedirs(os.path.join(ws, "agents"))
    os.makedirs(os.path.join(ws, "run"))
    return ws


def touch(agents_dir, rel, body="x"):
    p = os.path.join(agents_dir, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)
    return p



# ---------------------------------------------------------------- gc.py

print("== sanitize_entry: iron rules & validation ==")
for bad in ["/abs/path", "a/../b", "a//b", ".", "",
            "wild*card", "q[1]", "nl\npath"]:
    try:
        gc.sanitize_entry(bad)
        check(f"reject {bad!r}", False, "was accepted")
    except gc.GcError:
        check(f"reject {bad!r}", True)
check("accept plain file", gc.sanitize_entry("a/b.txt") == "a/b.txt")
check("accept dir w/ slash", gc.sanitize_entry("task-x/") == "task-x/")
check("accept .msg", gc.sanitize_entry("dispatcher/inbox/x.msg")
      == "dispatcher/inbox/x.msg")
check("accept ticket-ish names (no longer an exempt clan)",
      gc.sanitize_entry("xticket.1/f") == "xticket.1/f"
      and gc.sanitize_entry("ticket.0830-1741-wao2/anything")
      == "ticket.0830-1741-wao2/anything"
      and gc.sanitize_entry("ticket.x") == "ticket.x"
      and gc.sanitize_entry("ticket/abc123/x") == "ticket/abc123/x"
      and gc.sanitize_entry("ticket/") == "ticket/"
      and gc.sanitize_entry("myticket/notes") == "myticket/notes")
check("accept epic-ish paths (no longer an exempt clan)",
      gc.sanitize_entry("my-epic.1/f") == "my-epic.1/f"
      and gc.sanitize_entry("epic.big/x") == "epic.big/x"
      and gc.sanitize_entry("epic.") == "epic.")
check("accept task/ subtree (ephemeral task dirs stay deletable)",
      gc.sanitize_entry("task/abc123/") == "task/abc123/")
check("accept bot/ subtree (immortality iron rule removed, task)",
      gc.sanitize_entry("bot/dispatcher/") == "bot/dispatcher/"
      and gc.sanitize_entry("bot/dispatcher/inbox/x.msg")
      == "bot/dispatcher/inbox/x.msg"
      and gc.sanitize_entry("bot/c1/watcher/alice")
      == "bot/c1/watcher/alice")
# the BARE clan container is a different thing (task): it names no
# participant, so one entry would delete the whole clan
try:
    gc.sanitize_entry("bot/")
    _bare_refused = False
except gc.GcError:
    _bare_refused = True
check("refuse the bare bot/ clan container (name a participant instead)",
      _bare_refused and gc.validate_path("bot/") is not None)
check("accept agent/ subtree (clan dropped from exemption, task)",
      gc.sanitize_entry("agent/dispatcher/") == "agent/dispatcher/")

print("== add: list creation, cumulativity, compression ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "victim/f1")
touch(ad, "victim/sub/f2")
touch(ad, "keepme.txt")

rc = gc.cmd_add(ws, ad, ["victim/"], delay=999, wait=False)
check("add rc=0", rc == 0)
lists = gc.existing_lists(ad)
check("one list written", len(lists) == 1 and
      lists[0] == "gc/delete-list.000001", str(lists))
ents = gc.read_entries(ad)
check("list holds dir entry", ents == ["victim/"], str(ents))

touch(ad, "victim2/g1")
rc = gc.cmd_add(ws, ad, ["victim2/g1"], delay=999, wait=False)
check("second add rc=0", rc == 0)
lists = gc.existing_lists(ad)
check("two lists now", len(lists) == 2, str(lists))
ents = gc.read_entries(ad)
check("new list is cumulative (old entry kept)",
      "victim/" in ents and "victim2/g1" in ents, str(ents))
check("new list compresses old list",
      "gc/delete-list.000001" in ents, str(ents))
st = gc.load_state(ws)
check("schedule holds 3 due entries",
      set(st["schedule"]) == {"victim/", "victim2/g1",
                              "gc/delete-list.000001"},
      str(st["schedule"]))
check("seq monotone", st["seq"] == 2)

print("== reap: fixed delay honored ==")
deleted, missed = gc.reap(ws, ad, verbose=False)
check("nothing due yet -> nothing deleted", deleted == [] and missed == [])
check("hub file survives before due", os.path.exists(
    os.path.join(ad, "victim/f1")))
# force dues into the past
st = gc.load_state(ws)
for k in st["schedule"]:
    st["schedule"][k] = time.time() - 1
gc.save_state(ws, st)
deleted, missed = gc.reap(ws, ad, verbose=False)
check("reap deletes at due", missed == [] and len(deleted) == 3,
      f"{deleted} {missed}")
check("subtree gone", not os.path.exists(os.path.join(ad, "victim")))
check("old list file gone (compression)",
      not os.path.exists(os.path.join(ad, "gc/delete-list.000001")))
check("latest list survives", os.path.exists(
    os.path.join(ad, "gc/delete-list.000002")))
check("unlisted file untouched",
      os.path.exists(os.path.join(ad, "keepme.txt")))
check("schedule drained", gc.load_state(ws)["schedule"] == {})

print("== add --wait path (delay ~0) ==")
touch(ad, "quick/f")
rc = gc.cmd_add(ws, ad, ["quick/"], delay=0, wait=True)
check("add --wait rc=0", rc == 0)
check("--wait deleted hub copy", not os.path.exists(
    os.path.join(ad, "quick")))

print("== add refuses forbidden paths (no side effects) ==")
before = gc.existing_lists(ad)
rc = gc.cmd_add(ws, ad, ["gc/whatever"], delay=60, wait=False)
check("gc/ self rejected rc=1", rc == 1)
rc = gc.cmd_add(ws, ad, ["topic/dispatcher/"], delay=60, wait=False)
check("protected system asset rejected rc=1 (task)", rc == 1)
check("no list written on rejection", gc.existing_lists(ad) == before)
# ticket./epic./ticket/ clans no longer exempt: acceptance proven on
# the delete-side section below (is_forbidden_at_delete negative
# cases), keeping this section side-effect-free.

print("== delete-side iron rule (defense in depth) ==")
check("is_forbidden_at_delete gc internals", gc.is_forbidden_at_delete(
    "gc/inbox/req"))
check("is_forbidden_at_delete NOT bot/ (iron rule removed,)",
      not gc.is_forbidden_at_delete("bot/p1/inbox/x.msg")
      and not gc.is_forbidden_at_delete("bot/c1/watcher/alice")
      and not gc.is_forbidden_at_delete("bot/c1/")
      and not gc.is_forbidden_at_delete("bot/dispatcher/"))
check("is_forbidden_at_delete NOT ticket-ish (clan no longer exempt)",
      not gc.is_forbidden_at_delete("ticket.0101-0000-aaaa/x")
      and not gc.is_forbidden_at_delete("epic.e/x")
      and not gc.is_forbidden_at_delete("ticket/abc123/status.json"))
check("agent/ clan gone: no longer exempt (dropped, task)",
      not gc.is_forbidden_at_delete("agent/dispatcher/"))
check("agent/ clan gone: session file no longer exempt (task)",
      not gc.is_forbidden_at_delete("agent/dispatcher/session.jsonl"))
check("task/ layout subtree allowed (ephemeral)",
      not gc.is_forbidden_at_delete("task/abc123/"))
check("participant/ clan gone: no longer exempt (merged into bot/)",
      not gc.is_forbidden_at_delete("participant/p1/"))
check("channel/ clan gone: no longer exempt (merged into bot/)",
      not gc.is_forbidden_at_delete("channel/c1/"))
check("gc list file allowed (compression)",
      not gc.is_forbidden_at_delete("gc/delete-list.000001"))
check("normal path allowed",
      not gc.is_forbidden_at_delete("0828-0000-aaaa/report.md"))

print("== state loss recovery ==")
os.remove(gc.state_path(ws))
touch(ad, "recover/f")
rc = gc.cmd_add(ws, ad, ["recover/"], delay=999, wait=False)
check("add after state loss rc=0", rc == 0)
st = gc.load_state(ws)
check("seq resumes past surviving lists", st["seq"] >= 3, str(st["seq"]))
shutil.rmtree(ws)

# ------------------------------------------------------- ssh-sync pull

print("== ssh-sync: read_gc_lists / gc_exclude_and_targets ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "gc/delete-list.000001", "a/b.txt\ndir1/\n\n# comment\n")
touch(ad, "gc/delete-list.000002", "a/b.txt\ndir2/\n/bad\n../evil\nw*ld\n")
ex, tg, ref = ss.gc_exclude_and_targets(ad)
check("union dedup", ex.count("/a/b.txt") == 1, str(ex))
check("anchored file pattern", "/a/b.txt" in ex)
check("anchored dir pattern", "/dir1/" in ex and "/dir2/" in ex)
check("malformed refused", set(ref) == {"/bad", "../evil", "w*ld"},
      str(ref))
check("targets split", ("a/b.txt", False) in tg and ("dir1", True) in tg)

print("== ssh-sync: apply_gc_deletions ==")
touch(ad, "a/b.txt")
touch(ad, "dir1/x/y.txt")
touch(ad, "dir2/z.txt")
touch(ad, "unrelated.txt")
n = ss.apply_gc_deletions(ad)
check("deleted 3 listed paths", n == 3, f"n={n}")
check("file gone", not os.path.exists(os.path.join(ad, "a/b.txt")))
check("subtree gone", not os.path.exists(os.path.join(ad, "dir1")))
check("unrelated survives",
      os.path.exists(os.path.join(ad, "unrelated.txt")))
n = ss.apply_gc_deletions(ad)
check("idempotent replay deletes nothing", n == 0, f"n={n}")

print("== ssh-sync: no gc dir / empty ==")
ws2 = mkws()
ad2 = os.path.join(ws2, "agents")
check("no gc dir -> empty", ss.read_gc_lists(ad2) == [])
ex, tg, ref = ss.gc_exclude_and_targets(ad2)
check("no gc dir -> no excludes", ex == [] and tg == [] and ref == [])
shutil.rmtree(ws2)
shutil.rmtree(ws)

# ---------------------- fix coverage -------------

print("== consumer trusts the list (iron rules hub-side only) ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "gc/delete-list.000001",
      "ticket.0830-1741-wao2/x\nepic.big/y\ngc/inbox/req\ngc/\n"
      "gc\nnormal/f\ngc/delete-list.000000\n")
ex, tg, ref = ss.gc_exclude_and_targets(ad)
check("consumer ACCEPTS ticket.* (list is trusted)",
      "/ticket.0830-1741-wao2/x" in ex and
      ("ticket.0830-1741-wao2/x", False) in tg, f"{ex} {tg}")
check("consumer ACCEPTS epic.* (list is trusted)",
      "/epic.big/y" in ex, str(ex))
check("consumer still refuses gc/ internals", "gc/inbox/req" in ref
      and "gc/" in ref and "gc" in ref, str(ref))
check("gc compression entry allowed", "/gc/delete-list.000000" in ex,
      str(ex))
check("normal entry still allowed", "/normal/f" in ex and
      ("normal/f", False) in tg, f"{ex} {tg}")
check("no gc/ entry in targets",
      not any(r == "gc" or r.startswith("gc/")
              for r, _ in tg if r != "gc/delete-list.000000"),
      str(tg))
shutil.rmtree(ws)

print("== delete-list E2E — consumer applies the list as-is ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "ticket.0830-1741-wao2/ticket.json", "{}")
touch(ad, "epic.big/plan.md", "x")
touch(ad, "doomed/f", "x")
touch(ad, "gc/delete-list.000002",
      "ticket.0830-1741-wao2/\nepic.big/\ndoomed/\n")
touch(ad, "normal/f")
n = ss.apply_gc_deletions(ad)
check("consumer E2E: ALL listed entries deleted (list is trusted)",
      n == 3, f"n={n}")
check("consumer E2E: doomed subtree gone",
      not os.path.exists(os.path.join(ad, "doomed")))
check("consumer E2E: ticket.* deleted per list",
      not os.path.exists(os.path.join(ad, "ticket.0830-1741-wao2")))
check("consumer E2E: epic.* deleted per list",
      not os.path.exists(os.path.join(ad, "epic.big")))
n = ss.apply_gc_deletions(ad)
check("consumer E2E: replay converges (no retries, no deletes)",
      n == 0, f"n={n}")
# Defense lives hub-side: ticket./epic. clans are no longer exempt,
# so hub add now ACCEPTS them (consumer still applies lists as-is)
rc = gc.cmd_add(ws, ad, ["ticket.0830-1741-wao2/"], delay=60, wait=False)
check("hub add accepts ticket.* (no longer exempt)", rc == 0)
rc = gc.cmd_add(ws, ad, ["epic.big/"], delay=60, wait=False)
check("hub add accepts epic.* (no longer exempt)", rc == 0)
shutil.rmtree(ws)

print("== layout-subtree E2E — consumer applies the list ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "ticket/abc123/ticket.json", "{}")
touch(ad, "ticket/abc123/status.json", "{}")
touch(ad, "bot/pal/inbox/x.msg", "{}")
touch(ad, "bot/pal/keep.json", "{}")     # unlisted: the clan must survive
touch(ad, "task/doomed/f", "x")
touch(ad, "gc/delete-list.000001",
      "ticket/\nticket/abc123/\nbot/pal/inbox/x.msg\n"
      "bot/\ntask/doomed/\nnormal/f\n")
touch(ad, "normal/f")
ex, tg, ref = ss.gc_exclude_and_targets(ad)
check("legacy clan + participant entries ACCEPTED by consumer",
      set(["/ticket/", "/ticket/abc123/", "/bot/pal/inbox/x.msg"]).
      issubset(set(ex)), str(ex))
check("INVARIANT the consumer REFUSES a bare family container (one such "
      "line would delete a whole clan on every node)",
      "/bot/" not in ex and "bot/" in ref, (ex, ref))
check("task/ + normal entries still legal",
      "/task/doomed/" in ex and "/normal/f" in ex, str(ex))
n = ss.apply_gc_deletions(ad)
check("layout E2E: every accepted entry deleted, the refused one kept",
      n == 4, f"n={n}")
check("layout E2E: task subtree deleted",
      not os.path.exists(os.path.join(ad, "task/doomed")))
check("layout E2E: ticket/ subtree deleted per list",
      not os.path.exists(os.path.join(ad, "ticket")))
check("layout E2E: a listed FILE inside bot/ is still deleted",
      not os.path.exists(os.path.join(ad, "bot/pal/inbox/x.msg")))
check("layout E2E: the bot/ CLAN survives the bare container entry",
      os.path.isdir(os.path.join(ad, "bot"))
      and os.path.exists(os.path.join(ad, "bot/pal/keep.json")))
n = ss.apply_gc_deletions(ad)
check("layout E2E: replay converges", n == 0, f"n={n}")
# Defense lives hub-side: add refuses layout subtree entries at ingest
rc = gc.cmd_add(ws, ad, ["ticket/abc123/"], delay=60, wait=False)
check("hub add accepts ticket/ subtree (no longer exempt)", rc == 0)
rc = gc.cmd_add(ws, ad, ["bot/pal/"], delay=60, wait=False)
check("hub add accepts bot/ subtree (iron rule removed,)",
      rc == 0)
shutil.rmtree(ws)

print("== B1: compression never ingests forged entries ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "gc/delete-list.000001",
      "/abs/evil\nwild*card\ngc/evil\n../evil\nok/f\n")
touch(ad, "newpath/g")
rc = gc.cmd_add(ws, ad, ["newpath/g"], delay=999, wait=False)
check("add over forged list rc=0", rc == 0)
ents = gc.read_entries(ad)
check("forged absolute/glob/gc/traversal entries filtered on read",
      not any(e.startswith("/abs") for e in ents)
      and not any("wild" in e for e in ents)
      and "gc/evil" not in ents and "../evil" not in ents, str(ents))
check("legal entries kept through compression",
      "ok/f" in ents and "newpath/g" in ents
      and "gc/delete-list.000001" in ents, str(ents))
with open(os.path.join(ad, "gc", "delete-list.000002")) as fh:
    new_list = fh.read()
check("new authoritative list carries no forged entry",
      "/abs/evil" not in new_list and "wild*card" not in new_list
      and "gc/evil" not in new_list, new_list)
shutil.rmtree(ws)

print("== S1: reap full sanitization (tampered state.json) ==")
ws = mkws()
ad = os.path.join(ws, "agents")
outside = os.path.join(ws, "OUTSIDE.txt")
with open(outside, "w") as fh:
    fh.write("x")
touch(ad, "gc/evil/f")
st = gc.load_state(ws)
st["schedule"] = {"../OUTSIDE.txt": 0, "/etc/hostname": 0,
                    "gc/evil/": 0}
gc.save_state(ws, st)
deleted, missed = gc.reap(ws, ad, verbose=False)
check("tampered schedule: nothing deleted", deleted == [] and
      missed == [], f"{deleted} {missed}")
check("outside-agents file survives",
      os.path.exists(outside))
check("gc/ self-tree survives reap",
      os.path.exists(os.path.join(ad, "gc/evil/f")))
check("tampered entries dropped from schedule (no retry storm)",
      gc.load_state(ws)["schedule"] == {},
      str(gc.load_state(ws)["schedule"]))
shutil.rmtree(ws)

print("== S2: symlink entries converge on both sides ==")
# ssh-sync consumer side
ws = mkws()
ad = os.path.join(ws, "agents")
os.makedirs(os.path.join(ad, "realdir"))
touch(ad, "realdir/keep")
os.symlink(os.path.join(ad, "realdir"), os.path.join(ad, "linkdir"))
touch(ad, "gc/delete-list.000001", "linkdir/\n")
n = ss.apply_gc_deletions(ad)
check("consumer: symlinked dir entry deletes the LINK", n == 1,
      f"n={n}")
check("consumer: link gone, target subtree survives",
      not os.path.lexists(os.path.join(ad, "linkdir"))
      and os.path.exists(os.path.join(ad, "realdir/keep")))
n = ss.apply_gc_deletions(ad)
check("consumer: symlink entry converged (replay = 0)", n == 0,
      f"n={n}")
shutil.rmtree(ws)
# gc.py reap side
ws = mkws()
ad = os.path.join(ws, "agents")
os.makedirs(os.path.join(ad, "target"))
touch(ad, "target/keep")
os.symlink(os.path.join(ad, "target"), os.path.join(ad, "lnk"))
st = gc.load_state(ws)
st["schedule"] = {"lnk/": 0}
gc.save_state(ws, st)
deleted, missed = gc.reap(ws, ad, verbose=False)
check("reap: symlinked dir entry deletes the LINK (no missed)",
      deleted == ["lnk/"] and missed == [], f"{deleted} {missed}")
check("reap: link gone, target survives",
      not os.path.lexists(os.path.join(ad, "lnk"))
      and os.path.exists(os.path.join(ad, "target/keep")))
check("reap: schedule drained (converged)",
      gc.load_state(ws)["schedule"] == {})
shutil.rmtree(ws)

print("== bug2: add auto-normalizes existing dir path ==")
ws = mkws()
ad = os.path.join(ws, "agents")
os.makedirs(os.path.join(ad, "somedir"))
touch(ad, "somedir/f")
touch(ad, "plain.txt")
check("existing dir w/o slash -> dir entry",
      gc.sanitize_entry("somedir", agents_dir=ad) == "somedir/")
check("missing path stays a file entry",
      gc.sanitize_entry("noexist", agents_dir=ad) == "noexist")
check("plain file stays a file entry",
      gc.sanitize_entry("plain.txt", agents_dir=ad) == "plain.txt")
rc = gc.cmd_add(ws, ad, ["somedir"], delay=999, wait=False)
check("add dir w/o slash rc=0", rc == 0)
ents = gc.read_entries(ad)
check("list holds NORMALIZED dir entry", ents == ["somedir/"],
      str(ents))
st = gc.load_state(ws)
check("schedule key is the normalized entry",
      set(st["schedule"]) == {"somedir/"},
      str(st["schedule"]))
shutil.rmtree(ws)

print("== bug2: type mismatches converge ==")
# reap: file entry at a real directory -> subtree deleted
ws = mkws()
ad = os.path.join(ws, "agents")
os.makedirs(os.path.join(ad, "dirX"))
touch(ad, "dirX/f")
st = gc.load_state(ws)
st["schedule"] = {"dirX": 0}
gc.save_state(ws, st)
deleted, missed = gc.reap(ws, ad, verbose=False)
check("reap: file entry at dir deletes subtree",
      deleted == ["dirX"] and missed == [], f"{deleted} {missed}")
check("reap: subtree gone", not os.path.exists(os.path.join(ad, "dirX")))
shutil.rmtree(ws)
# consumer: file entry at a real directory -> subtree deleted
ws = mkws()
ad = os.path.join(ws, "agents")
os.makedirs(os.path.join(ad, "dirY"))
touch(ad, "dirY/f")
touch(ad, "gc/delete-list.000001", "dirY\n")
n = ss.apply_gc_deletions(ad)
check("consumer: file entry at dir deletes subtree", n == 1, f"n={n}")
check("consumer: subtree gone",
      not os.path.exists(os.path.join(ad, "dirY")))
n = ss.apply_gc_deletions(ad)
check("consumer: converged (replay = 0)", n == 0, f"n={n}")
shutil.rmtree(ws)

print("== --force audited bypass of the cleanup exemption ==")
# FORBIDDEN_LAYOUT_DIRS is empty (bot/ immortality
# removed); the exemption + audited-bypass MECHANISM stays and is
# covered here against a hypothetical exempt clan.
_SAVED_FORBIDDEN = gc.FORBIDDEN_LAYOUT_DIRS
gc.FORBIDDEN_LAYOUT_DIRS = ("zzz",)
try:
    ws = mkws()
    ad = os.path.join(ws, "agents")
    # 1. default behavior unchanged: exempt entries still REJECTED
    rc = gc.cmd_add(ws, ad, ["zzz/abc123/"], delay=60, wait=False)
    check("default: exempt entry REJECTED (iron rule regression)", rc == 1)
    check("default: no list written on rejection",
          gc.existing_lists(ad) == [])
    # 2. sanitize: force lifts ONLY the exemption rule
    check("force: sanitize accepts exempt subtree",
          gc.sanitize_entry("zzz/abc/", force=True) == "zzz/abc/")
    check("force: sanitize accepts exempt at any depth",
          gc.sanitize_entry("zzz/pal/inbox/x.msg", force=True)
          == "zzz/pal/inbox/x.msg")
    for bad in ["/abs/path", "a/../b", "wild*card", "gc/evil", "gc/",
                ".."]:
        try:
            gc.sanitize_entry(bad, force=True)
            check(f"force does NOT skip other checks ({bad!r})", False,
                  "was accepted")
        except gc.GcError:
            check(f"force does NOT skip other checks ({bad!r})", True)
    # 3. forced add writes the entry WITH the audit marker
    os.makedirs(os.path.join(ad, "zzz/abc123"))
    touch(ad, "zzz/abc123/inbox/x.msg", "{}")
    rc = gc.cmd_add(ws, ad, ["zzz/abc123/"], delay=999, wait=False,
                    force=True, forced_by="tester")
    check("forced add rc=0", rc == 0)
    ents = gc.read_entries(ad)
    check("forced entry carries the #FORCED: audit marker",
          len(ents) == 1 and
          ents[0].startswith("zzz/abc123/ #FORCED:by=tester,"),
          str(ents))
    check("audit marker records who/where/when",
          "by=tester" in ents[0] and "host=" in ents[0]
          and "ts=" in ents[0], str(ents))
    # 4. regular entries in a forced batch stay UNMARKED; the marked
    #    entry survives list compression
    os.makedirs(os.path.join(ad, "normal"))
    touch(ad, "normal/f")
    rc = gc.cmd_add(ws, ad, ["normal/f"], delay=999, wait=False,
                    force=True, forced_by="tester")
    check("mixed forced batch rc=0", rc == 0)
    ents = gc.read_entries(ad)
    check("regular entry stays unmarked under --force",
          "normal/f" in ents, str(ents))
    check("marked entry survives compression into the new list",
          any(e.startswith("zzz/abc123/ #FORCED:") for e in ents),
          str(ents))
    with open(os.path.join(ad, "gc", "delete-list.000002")) as fh:
        body = fh.read()
    check("authoritative list file holds the marker verbatim",
          "zzz/abc123/ #FORCED:by=tester" in body, body)
    # 5. forged UNMARKED exempt entries are still filtered at read time
    touch(ad, "gc/delete-list.000090", "zzz/FORGED/x\n")
    os.makedirs(os.path.join(ad, "another"))
    touch(ad, "another/f")
    rc = gc.cmd_add(ws, ad, ["another/f"], delay=999, wait=False)
    check("add over forged list rc=0", rc == 0)
    ents = gc.read_entries(ad)
    check("forged UNMARKED exempt entry filtered on compression",
          not any("zzz/FORGED" in e for e in ents), str(ents))
    check("legit marked entry still carried",
          any(e.startswith("zzz/abc123/ #FORCED:") for e in ents),
          str(ents))
    # 6. reap + --wait: marked entries pass delete-time validation
    st = gc.load_state(ws)
    for k in st["schedule"]:  # drain earlier batches -> no long --wait sleep
        st["schedule"][k] = time.time() - 1
    gc.save_state(ws, st)
    os.makedirs(os.path.join(ad, "zzz/def456"))
    touch(ad, "zzz/def456/x")
    rc = gc.cmd_add(ws, ad, ["zzz/def456/"], delay=0, wait=True,
                    force=True, forced_by="tester")
    check("forced add --wait rc=0 (reap accepts marked entry)", rc == 0)
    check("forced reap deleted the exempt subtree",
          not os.path.exists(os.path.join(ad, "zzz/def456")))
    # 7. the delete-line refuses UNMARKED exempt paths exactly as before
    check("is_forbidden_at_delete still refuses UNMARKED exempt",
          gc.is_forbidden_at_delete("zzz/abc123/"))
    check("marked exempt entry passes validation",
          gc.validate_entry(
              "zzz/abc123/ #FORCED:by=u,host=h,ts=2026-08-31T00:00:00Z")
          is None)
    check("empty audit marker refused (no silent bypass)",
          gc.validate_entry("zzz/x/ #FORCED:") is not None)
    shutil.rmtree(ws)
finally:
    gc.FORBIDDEN_LAYOUT_DIRS = _SAVED_FORBIDDEN

print("== consumer strips the marker and applies the path ==")
ws = mkws()
ad = os.path.join(ws, "agents")
os.makedirs(os.path.join(ad, "ticket/abc"))
touch(ad, "ticket/abc/ticket.json", "{}")
touch(ad, "gc/delete-list.000001",
      "ticket/abc/ #FORCED:by=tester,host=h,ts=2026-08-31T12:00:00Z\n"
      "plain/f\n")
touch(ad, "plain/f")
ex, tg, ref = ss.gc_exclude_and_targets(ad)
check("consumer strips marker -> clean exclude pattern",
      "/ticket/abc/" in ex and ref == [], f"{ex} {ref}")
check("consumer target is the bare path",
      ("ticket/abc", True) in tg, str(tg))
n = ss.apply_gc_deletions(ad)
check("consumer deletes the forced entry path", n == 2, f"n={n}")
check("forced subtree gone",
      not os.path.exists(os.path.join(ad, "ticket/abc")))
n = ss.apply_gc_deletions(ad)
check("replay converges", n == 0, f"n={n}")
shutil.rmtree(ws)

print("== bot/ immortality iron rule REMOVED ==")
check("FORBIDDEN_LAYOUT_DIRS is empty (user decision 2026-09-06)",
      gc.FORBIDDEN_LAYOUT_DIRS == (), str(gc.FORBIDDEN_LAYOUT_DIRS))
# 1. any bot/ path passes validation WITHOUT force at all three
#    iron-rule enforcement points (add sanitize / read-compression
#    validate_entry / reap is_forbidden_at_delete)
for good in ("bot/dispatcher", "bot/dispatcher/",
             "bot/dispatcher/inbox/x.msg",
             "bot/dispatcher/watcher/dev-dispatcher",
             "bot/pal/", "bot/c1/watcher/alice"):
    check(f"bot/ accepted (no force): {good}",
          gc.validate_path(good) is None)
    check(f"bot/ accepted via validate_entry: {good}",
          gc.validate_entry(good) is None)
    check(f"bot/ NOT forbidden at delete: {good}",
          not gc.is_forbidden_at_delete(good))
# ... but the BARE clan container is refused at all three points (task
#): deletable clan != deletable clan CONTAINER
for bare in ("bot/", "bot"):
    check(f"bare bot/ clan container refused: {bare}",
          gc.validate_path(bare) is not None)
    check(f"bare bot/ refused via validate_entry: {bare}",
          gc.validate_entry(bare) is not None)
    check(f"bare bot/ forbidden at delete: {bare}",
          gc.is_forbidden_at_delete(bare))
# 2. cmd_add: bot/dispatcher lands in the list UNMARKED (no bypass
#    involved), reap converges the subtree, consumer applies it
ws = mkws()
ad = os.path.join(ws, "agents")
os.makedirs(os.path.join(ad, "bot/dispatcher/inbox"))
touch(ad, "bot/dispatcher/inbox/x.msg", "{}")
touch(ad, "bot/dispatcher/pid.json", "{}")
rc = gc.cmd_add(ws, ad, ["bot/dispatcher/"], delay=0, wait=False)
check("cmd_add accepts bot/dispatcher/ without force", rc == 0,
      f"rc={rc}")
ents = gc.read_entries(ad)
check("bot/ entry stored UNMARKED (no #FORCED)",
      "bot/dispatcher/" in ents
      and not any("#FORCED" in e for e in ents), str(ents))
deleted, missed = gc.reap(ws, ad)
check("reap deletes the bot/ subtree",
      not os.path.exists(os.path.join(ad, "bot/dispatcher"))
      and not missed, f"{deleted} {missed}")
os.makedirs(os.path.join(ad, "bot/dispatcher/inbox"))
touch(ad, "bot/dispatcher/inbox/y.msg", "{}")
n = ss.apply_gc_deletions(ad)
check("consumer applies the bot/ entry (list is trusted)", n >= 1
      and not os.path.exists(os.path.join(ad, "bot/dispatcher")),
      f"n={n}")
shutil.rmtree(ws)
# 3. protected system asset regression: topic/dispatcher still refused
#    EVEN with --force (full coverage in the PROTECTED_SYSTEM_PATHS section below)
check("topic/dispatcher still refused (incl. force)",
      gc.validate_path("topic/dispatcher/") is not None
      and gc.validate_path("topic/dispatcher/", force=True) is not None)

print("== PROTECTED_SYSTEM_PATHS (dispatcher position mailbox) ==")
# The position mailbox moved into the deletable topic family
# (bot/dispatcher/ -> topic/dispatcher/); it is critical infrastructure,
# so gc.py refuses it at any depth EVEN under the audited --force bypass.
check("protected list covers the position mailbox",
      gc.PROTECTED_SYSTEM_PATHS == ("topic/dispatcher",),
      str(gc.PROTECTED_SYSTEM_PATHS))
for bad in ("topic/dispatcher", "topic/dispatcher/",
            "topic/dispatcher/inbox/x.msg",
            "topic/dispatcher/watcher/dev-dispatcher",
            "topic/dispatcher/topic.md"):
    check(f"protected refused (no force): {bad}",
          gc.validate_path(bad) is not None)
    check(f"protected refused EVEN with force: {bad}",
          gc.validate_path(bad, force=True) is not None)
    check(f"protected refused via validate_entry+marker: {bad}",
          gc.validate_entry(
              bad + " #FORCED:by=u,host=h,ts=2026-09-05T00:00:00Z")
          is not None)
    try:
        gc.sanitize_entry(bad, force=True)
        check(f"sanitize_entry(force) refuses protected: {bad}", False,
              "was accepted")
    except gc.GcError:
        check(f"sanitize_entry(force) refuses protected: {bad}", True)
# siblings stay deletable (topic family is ephemeral by design)
for good in ("topic/mtg-x/", "topic/mtg-x/inbox/a.msg", "task/abc123/"):
    check(f"non-protected topic/task still deletable: {good}",
          gc.validate_path(good) is None)
# a name that merely STARTS with the protected string is not protected
check("prefix match is element-wise, not string-wise",
      gc.validate_path("topic/dispatcher-2/") is None)
ws = mkws()
ad = os.path.join(ws, "agents")
os.makedirs(os.path.join(ad, "topic/dispatcher/inbox"))
touch(ad, "topic/dispatcher/inbox/a.msg", "{}")
rc = gc.cmd_add(ws, ad, ["topic/dispatcher/"], delay=60, wait=False,
                force=True, forced_by="tester")
check("cmd_add --force refuses the protected system asset", rc == 1,
      f"rc={rc}")
check("no delete-list written for the refused path",
      gc.existing_lists(ad) == [])
rc = gc.cmd_add(ws, ad, ["topic/dispatcher/inbox/a.msg"], delay=60,
                wait=False)
check("cmd_add (no force) refuses a protected file entry", rc == 1,
      f"rc={rc}")
check("still no delete-list", gc.existing_lists(ad) == [])
check("protected tree still on disk",
      os.path.exists(os.path.join(ad, "topic/dispatcher/inbox/a.msg")))
shutil.rmtree(ws)

print("== cross-machine idempotency (hub + 4 nodes, consumer side) ==")
# The same cumulative list applied independently on five trees, each
# holding a different subset: every tree converges to the same terminal
# state, deleting exactly what IT held, and a second run is a no-op.
# Converging a LISTED path is the consumer's job (the watch pull side,
# dsync/ssh-sync.py apply_gc_deletions); the hub's own copies go through
# `add`'s schedule, so this walks the consumer on each tree.
LIST_BODY = "bot/gone/\ntask/aaa/report.md\ntopic/mtg/\n"
HELD = {
    "hub": ["bot/gone/inbox/x.msg", "task/aaa/report.md", "keep/out.txt"],
    "node1": ["task/aaa/report.md", "keep/out.txt"],
    "node2": ["topic/mtg/inbox/a.msg", "keep/out.txt"],
    "node3": ["keep/out.txt"],
    "node4": ["bot/gone/inbox/y.msg", "topic/mtg/inbox/b.msg",
              "keep/out.txt"],
}
# entries of LIST_BODY that this tree actually holds
EXPECTED = {"hub": 2, "node1": 1, "node2": 1, "node3": 0, "node4": 2}
for name, files in HELD.items():
    ws = mkws()
    ad = os.path.join(ws, "agents")
    for rel in files:
        touch(ad, rel, "{}")
    lp = touch(ad, "gc/delete-list.000042", LIST_BODY)
    aged = time.time() - 9999   # propagated long ago
    os.utime(lp, (aged, aged))
    n1 = ss.apply_gc_deletions(ad)
    n2 = ss.apply_gc_deletions(ad)
    check(f"idem[{name}]: listed paths gone, unlisted survivor kept",
          not os.path.exists(os.path.join(ad, "bot/gone"))
          and not os.path.exists(os.path.join(ad, "task/aaa/report.md"))
          and not os.path.exists(os.path.join(ad, "topic/mtg"))
          and os.path.exists(os.path.join(ad, "keep/out.txt")))
    check(f"idem[{name}]: 1st run deleted exactly what this tree held",
          n1 == EXPECTED[name], f"{n1} want {EXPECTED[name]}")
    check(f"idem[{name}]: 2nd run is a no-op (idempotent)", n2 == 0, n2)
    check(f"idem[{name}]: the list itself survives (replayable)",
          os.path.exists(lp))
    shutil.rmtree(ws)

print("== add's authorized compression path still retires old lists ==")
ws = mkws()
ad = os.path.join(ws, "agents")
aged = time.time() - 99999
old = touch(ad, "gc/delete-list.000041", "task/old/\n")
os.utime(old, (aged, aged))
gc.save_state(ws, {"seq": 41, "schedule": {}})
touch(ad, "task/new/f")
rc = gc.cmd_add(ws, ad, ["task/new/"], delay=0, wait=False)
check("T2-3 add rc=0", rc == 0, str(rc))
newest = [lp for lp in gc.existing_lists(ad) if lp != "gc/delete-list.000041"]
check("T2-3 the new list names the OLD one (compression entry)",
      len(newest) == 1
      and "gc/delete-list.000041" in open(os.path.join(ad, newest[0]),
                                         encoding="utf-8").read().split(),
      str(newest))
d, m = gc.reap(ws, ad, verbose=False)
check("T2-3 the OLD list is retired via add's authorized schedule",
      not os.path.exists(old), f"{d} {m}")
check("T2-3 the NEW authoritative list survives",
      all(os.path.exists(os.path.join(ad, lp)) for lp in newest), str(newest))
check("T2-3 the ledger entry carried forward (cumulative)",
      "task/old/" in gc.read_entries(ad), str(gc.read_entries(ad)))
shutil.rmtree(ws)

print("== bare family containers are refused ==")
for bare in ("agents/", "task/", "bot/", "topic/", "run/"):
    check(f"T3-1 refused: {bare}", gc.validate_path(bare) is not None)
    check(f"T3-1 refused even with --force: {bare}",
          gc.validate_path(bare, force=True) is not None)
    check(f"T3-1 refused via validate_entry: {bare}",
          gc.validate_entry(bare) is not None)
    check(f"T3-1 forbidden at delete: {bare}",
          gc.is_forbidden_at_delete(bare))
    try:
        gc.sanitize_entry(bare)
        check(f"T3-1 sanitize_entry refuses: {bare}", False, "was accepted")
    except gc.GcError:
        check(f"T3-1 sanitize_entry refuses: {bare}", True)
for bare in ("agents", "task", "bot", "topic", "run"):
    check(f"T3-1 refused unslashed: {bare}",
          gc.validate_path(bare) is not None)
for good in ("task/abc123/", "bot/foo/", "topic/mtg-1/",
             "bot/foo/inbox/x.msg", "task/abc123/report.md",
             "topic/mtg-1/inbox", "agent/dispatcher/", "ticket/",
             "ticket/abc123/x", "task-x/", "run/gc/state.json",
             "topic/dispatcher-2/", "agents/x/y", "epic."):
    check(f"T3-2 concrete participant path still accepted: {good}",
          gc.validate_path(good) is None)
check("T3-2 the authorized surface still deletes a concrete participant",
      gc.sanitize_entry("task/abc123/") == "task/abc123/"
      and gc.sanitize_entry("bot/foo/") == "bot/foo/"
      and gc.sanitize_entry("topic/mtg-1/") == "topic/mtg-1/")
# the pre-existing refusal surface is unchanged
for bad in ("gc/", "gc/evil", ".", "/abs/path", "task/../bot/", "wild*card",
            "q[1]", "", "  ", "topic/dispatcher/",
            "topic/dispatcher/inbox/x.msg"):
    check(f"T3-5 still refused: {bad!r}", gc.validate_path(bad) is not None)
    check(f"T3-5 still refused with --force: {bad!r}",
          gc.validate_path(bad, force=True) is not None)

print("== a bare family container cannot be added via the CLI ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "task/aaa/report.md")
touch(ad, "bot/live/spec.json")
lists_before = gc.existing_lists(ad)
rc = gc.cmd_add(ws, ad, ["task/"], delay=0, wait=False)
check("T3-4 add of a bare clan container is REJECTED (rc != 0)", rc != 0,
      str(rc))
check("T3-4 no list was written", gc.existing_lists(ad) == lists_before,
      str(gc.existing_lists(ad)))
check("T3-4 both clans are intact",
      os.path.exists(os.path.join(ad, "task/aaa/report.md"))
      and os.path.exists(os.path.join(ad, "bot/live/spec.json")))
rc = gc.cmd_add(ws, ad, ["agents/"], delay=0, wait=False)
check("T3-4 add of the tree-root-isomorphic agents/ is REJECTED", rc != 0,
      str(rc))
check("T3-4 the tree is intact", os.path.isdir(os.path.join(ad, "task")))
shutil.rmtree(ws)

print("== legacy junk self-heals at the next compression ==")
ws = mkws()
ad = os.path.join(ws, "agents")
os.makedirs(os.path.join(ad, "agents"))  # the tree-root-isomorphic dir exists
touch(ad, "agents/oops.txt")
touch(ad, "task/aaa/report.md")
old = touch(ad, "gc/delete-list.000041",
            "agents/\ntask/\ntask/aaa/\n")  # junk + a legit entry
body_before = open(old, encoding="utf-8").read()
gc.save_state(ws, {"seq": 41, "schedule": {}})
union_before = gc.read_entries(ad)
check("T3-3 read_entries filters the bare family containers",
      "agents/" not in union_before and "task/" not in union_before
      and "task/aaa/" in union_before, str(union_before))
rc = gc.cmd_add(ws, ad, ["task/bbb/"], delay=0, wait=False)
check("T3-3 add rc=0 with the junk filtered at read time", rc == 0, str(rc))
newest = [lp for lp in gc.existing_lists(ad) if lp != "gc/delete-list.000041"]
new_body = open(os.path.join(ad, newest[0]), encoding="utf-8").read().split()
check("T3-3 the NEW list carries no bare family container",
      "agents/" not in new_body and "task/" not in new_body, str(new_body))
check("T3-3 the legit entries carried forward",
      "task/aaa/" in new_body and "task/bbb/" in new_body, str(new_body))
check("T3-3 the OLD list file is byte-identical (append-only)",
      open(old, encoding="utf-8").read() == body_before)
d, m = gc.reap(ws, ad, verbose=False)
check("T3-3 the tree-root-isomorphic agents/ dir was NOT deleted",
      os.path.exists(os.path.join(ad, "agents/oops.txt")), f"{d} {m}")
check("T3-3 a carried-forward entry stays as hub residue (only `add` "
      "writes the plan; reap never widens the surface)",
      os.path.exists(os.path.join(ad, "task/aaa")), f"{d} {m}")
check("T3-3 the consumer still converges it (the LIST is the authority)",
      ss.apply_gc_deletions(ad) >= 1
      and not os.path.exists(os.path.join(ad, "task/aaa")))
check("T3-3 the retired list took the junk with it (compression is the "
      "self-heal; the consumer never saw a bare container)",
      not os.path.exists(old)
      and os.path.exists(os.path.join(ad, "agents/oops.txt")))
shutil.rmtree(ws)

print("== illegal-entry warnings are one summary line per read ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "gc/delete-list.000042",
      "agents/\ntask/\nbot/\ntopic/dispatcher/\ntask/ok/\n")
err = io.StringIO()
with contextlib.redirect_stderr(err):
    entries = gc.read_entries(ad)
lines = [l for l in err.getvalue().splitlines() if l.strip()]
check("T5-1 the legit entry survives the read filter",
      entries == ["task/ok/"], str(entries))
check("T5-1 ONE summary line per read (not one per illegal entry)",
      len(lines) == 1, f"{len(lines)} lines: {lines[:6]}")
check("T5-1 the summary reports the filtered count",
      "4" in (lines[0] if lines else ""), str(lines[:1]))
err2 = io.StringIO()
with contextlib.redirect_stderr(err2):
    gc.read_entries(ad)
lines2 = [l for l in err2.getvalue().splitlines() if l.strip()]
check("T5-1 the same pollution is reported ONCE per invocation",
      lines2 == [], f"{len(lines2)} lines: {lines2[:3]}")
# a CHANGED pollution set is a new signature and reports again
touch(ad, "gc/delete-list.000043", "run/\n")
err3 = io.StringIO()
with contextlib.redirect_stderr(err3):
    gc.read_entries(ad)
lines3 = [l for l in err3.getvalue().splitlines() if l.strip()]
check("T5-1 a changed pollution set reports again (one line)",
      len(lines3) == 1, f"{len(lines3)} lines: {lines3[:3]}")
check("T5-1 the new summary counts all five illegal entries",
      bool(lines3) and "5" in lines3[0], str(lines3[:1]))
shutil.rmtree(ws)

# ------------------------------------------- the plan is a cache (L8)
# `add` is the ONLY writer of the schedule, so a lost or reset state
# drops the plan and nothing else: the deletion surface never widens,
# and the operator re-plans the residue with an explicit `add`.

print("== state loss: the plan is a cache, the lists stay the authority ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "task/gone/f")
rc = gc.cmd_add(ws, ad, ["task/gone/"], delay=999, wait=False)
check("L8 baseline add rc=0", rc == 0)
seq_before = gc.load_state(ws)["seq"]
os.remove(gc.state_path(ws))                       # lose the plan
d, m = gc.reap(ws, ad, verbose=False)
check("L8 reap after a state loss deletes nothing (no plan, no surface)",
      d == [] and m == [], (d, m))
check("L8 the listed path is still on disk (residue, not data loss)",
      os.path.exists(os.path.join(ad, "task/gone/f")))
st = gc.load_state(ws)
check("L8 the rebuilt state has an empty schedule",
      st["schedule"] == {}, str(st))
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = gc.cmd_list(ws, ad)
listed = out.getvalue()
check("L8 list reports the residue as unscheduled (rc=0)", rc == 0
      and "unscheduled" in listed and "task/gone/" in listed, listed[-300:])
st_after = gc.load_state(ws)
check("L8 the report is READ-ONLY (state untouched, no grace clock)",
      st_after["schedule"] == {} and st_after["seq"] == 0, str(st_after))
rc = gc.cmd_add(ws, ad, ["task/gone/"], delay=0, wait=True)
check("L8 re-adding the residue re-plans it and --wait converges",
      rc == 0 and not os.path.exists(os.path.join(ad, "task/gone")))
check("L8 seq resumes past the surviving lists (no collision)",
      gc.load_state(ws)["seq"] > seq_before, str(gc.load_state(ws)["seq"]))
shutil.rmtree(ws)

print("== a forged list can never schedule a deletion by itself ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "task/mine/f")
auth = touch(ad, "gc/delete-list.000042", "task/mine/\n")
forged = touch(ad, "gc/delete-list.000010",
               "gc/delete-list.000042\ntask/mine/\n")
aged = time.time() - 99999
os.utime(auth, (aged, aged))
os.utime(forged, (aged, aged))
d, m = gc.reap(ws, ad, verbose=False)
check("L8 reap with no plan deletes nothing, whatever the lists claim",
      d == [] and m == [], (d, m))
check("L8 the authority list and the listed path both survive",
      os.path.exists(auth) and os.path.exists(os.path.join(ad, "task/mine/f")))
rc = gc.cmd_add(ws, ad, ["task/other/"], delay=0, wait=True)
sched_after = gc.load_state(ws)["schedule"]
check("L8 an unrelated add never schedules a forged list's entries",
      rc == 0 and os.path.exists(os.path.join(ad, "task/mine/f"))
      and not any("task/mine" in k for k in sched_after), str(sched_after))
check("L8 add's compression still retires BOTH old list files (ledger op)",
      not os.path.exists(auth) and not os.path.exists(forged))
shutil.rmtree(ws)

print("== add honors --delay (the propagation grace window) ==")
ws = mkws()
ad = os.path.join(ws, "agents")
touch(ad, "task/slow/f")
rc = gc.cmd_add(ws, ad, ["task/slow/"], delay=999, wait=False)
check("L8 --delay 999: scheduled, still on disk", rc == 0
      and os.path.exists(os.path.join(ad, "task/slow/f")),
      str(gc.load_state(ws)["schedule"]))
d, m = gc.reap(ws, ad, verbose=False)
check("L8 reap before the deadline deletes nothing", d == [] and m == [],
      (d, m))
rc = gc.cmd_add(ws, ad, ["task/fast/"], delay=0, wait=True)
touch(ad, "task/slow/f")
check("L8 --delay 0 + --wait converges the batch", rc == 0
      and not os.path.exists(os.path.join(ad, "task/fast")), str(rc))
shutil.rmtree(ws)

print()
print(f"{PASS} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL OK")
