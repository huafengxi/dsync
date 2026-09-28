#!/usr/bin/env python3
"""test_replica_tag.py — unit tests for the replica-tag ownership rules
( the tool itself is).

Covers:
+ topic/ clan (shared multi-subscriber mailbox — including
  the position mailbox topic/dispatcher, task): envelopes via
  ``from``, namespaced acks via the SUBSCRIBER's host, everything else
  UNKNOWN (a topic has no spec.host).
+ namespaced ack ownership (protocol §4.6 two-state acks, t-3yp9③):
  ``ack/<subscriber>/<id>`` follows the subscriber, NOT the mailbox's
  owning participant — in every clan (topic/task/bot/participant/
  ticket); the ack's own ``subscriber`` field beats the directory
  segment; segments are matched by FORWARD fsSafeId transform of real
  directory names (never by splitting on dots — names may contain dots,
  e.g. bot/svc.web).
+ path-style writer ids (protocol §2.2): ``task/<id>`` -> that task's
  spec.host, ``bot/<name>`` -> that bot's spec.host, ``topic/<id>`` ->
  UNKNOWN; bare names that are bot directory names resolve too.
+ bot/ clan rules (mirror of the task-dir rules), including the
  no-hub-fallback rule for bots without a usable spec.host.
+ enable.json = the SCHEDULING side's record (``by`` wins; the single
  global scheduler is pinned to the hub host) — regression for the
  mis-attribution that tagged the hub's own release records as replica
  for every task registered on another machine.
+ hub = dev (was stale nv1), run/agentd.<H>.lock (locks moved under
  run/ in task), gc/** -> hub.
+ unchanged legacy behavior (task-dir classes, participant/ & ticket/
  pre-split layouts, from=agentd via body taskId, UNKNOWN conservatism).
+ end-to-end --apply in a temp tree (skipped when this machine has no
  ``replica`` group): only owner != self files are chgrped; owner ==
  self and UNKNOWN stay unmarked; the re-run is idempotent.

Self-contained: every case runs in a temp workspace and nothing outside
it is ever chowned. Run with plain python3 (no pytest needed):
`python3 dsync/test_replica_tag.py`.
"""

import contextlib
import grp
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rt = _load("replica_tag", os.path.join(_HERE, "replica-tag.py"))

PASS = 0
FAIL = []


def check(desc, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(desc + (f"  [{detail}]" if detail else ""))
        print(f"  FAIL: {desc}" + (f"  [{detail}]" if detail else ""))


def w(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content if isinstance(content, str) else json.dumps(content))


def msg(mid, from_, body="b"):
    return json.dumps({"id": mid, "from": from_,
                       "ts": "2026-09-06T10:00:00+08:00",
                       "type": "inform", "body": body})


def ack(mid, subscriber=None):
    d = {"id": mid, "ts": "2026-09-06T10:00:00+08:00"}
    if subscriber:
        d["subscriber"] = subscriber
    return json.dumps(d)


def spec(host=None, createdByHost=None):
    d = {}
    if host is not None:
        d["host"] = host
    if createdByHost is not None:
        d["createdByHost"] = createdByHost
    return json.dumps(d)


def mkws():
    """Synthetic workspace: <ws>/agents (the tree under test) plus
    <ws>/env/host-id and <ws>/assistant/ (legacy declaration-scan root).
    Returns (ws, agents_dir)."""
    ws = tempfile.mkdtemp(prefix="rt-test-")
    ad = os.path.join(ws, "agents")
    w(os.path.join(ws, "env", "host-id"),
      "node-a.example.com\tnv1\nhub.example.com\tdev\nnode-b.example.com\tnv2\n"
      "workstation.local\tmac\n")
    # legacy participant declaration (pre-split layout): host nv1, so it
    # differs from every subscriber host used below
    w(os.path.join(ws, "assistant", "legacy.agent"),
      json.dumps({"participant": "legacy", "host": "nv1"}))
    os.makedirs(os.path.join(ws, "assistant"), exist_ok=True)

    # ---- bots (resident sessions / process carriers) ----
    w(f"{ad}/bot/dev-dispatcher/spec.json", spec("dev", "dev"))
    w(f"{ad}/bot/mac-dispatcher/spec.json", spec("mac", "dev"))
    w(f"{ad}/bot/svc.web/spec.json", spec("dev", "dev"))   # dotted name
    w(f"{ad}/bot/mac-dispatcher/pid.json", "{}")
    w(f"{ad}/bot/mac-dispatcher/prompt.md", "p")
    w(f"{ad}/bot/mac-dispatcher/enable.json", json.dumps(
        {"by": "agentd-scheduler"}))
    w(f"{ad}/bot/mac-dispatcher/session/session.jsonl", "{}")
    w(f"{ad}/bot/mac-dispatcher/loose.jsonl", "x")
    w(f"{ad}/bot/mac-dispatcher/control/r1.req",
      json.dumps({"from": "operator"}))
    w(f"{ad}/bot/mac-dispatcher/control/ack/r1", ack("r1"))
    w(f"{ad}/bot/mac-dispatcher/inbox/from-dev.msg",
      msg("from-dev", "bot/dev-dispatcher"))
    w(f"{ad}/bot/mac-dispatcher/inbox/unparseable.msg", "{oops")
    w(f"{ad}/bot/mac-dispatcher/inbox/ack/y2", ack("y2"))
    w(f"{ad}/bot/mac-dispatcher/inbox/ack/bot.dev-dispatcher/y1",
      ack("y1", "bot.dev-dispatcher"))
    w(f"{ad}/bot/nospec/loose.txt", "x")
    w(f"{ad}/bot/nospec/inbox/whatever.msg", msg("whatever", "task/tdev"))

    # ---- tasks ----
    w(f"{ad}/task/tdev/spec.json", spec("dev", "dev"))
    w(f"{ad}/task/tnv1/spec.json", spec("nv1", "dev"))
    w(f"{ad}/task/tmac/spec.json", spec("mac", "mac"))
    w(f"{ad}/task/tmac/prompt.md", "p")
    w(f"{ad}/task/tmac/report.md", "r")
    w(f"{ad}/task/tmac/enable.json", json.dumps({"by": "agentd-scheduler"}))
    w(f"{ad}/task/tmac/inbox/rel.msg", msg("rel", "task/tdev"))
    w(f"{ad}/task/tmac/inbox/unk.msg", msg("unk", "no-such-writer"))
    w(f"{ad}/task/tmac/inbox/ack/x2", ack("x2"))
    w(f"{ad}/task/tmac/inbox/ack/bot.dev-dispatcher/x1",
      ack("x1", "bot.dev-dispatcher"))
    w(f"{ad}/task/tmac/control/ack/bot.dev-dispatcher/c1",
      ack("c1", "bot.dev-dispatcher"))
    w(f"{ad}/task/byhand/spec.json", spec("dev", "dev"))
    w(f"{ad}/task/byhand/enable.json", json.dumps({"by": "task/tmac"}))

    # ---- a shared topic mailbox: dev + mac + dotted-name subscribers ----
    ti = f"{ad}/topic/mtg-x/inbox"
    w(f"{ti}/m-from-tdev.msg", msg("m-from-tdev", "task/tdev"))
    w(f"{ti}/m-from-tmac.msg", msg("m-from-tmac", "task/tmac"))
    w(f"{ti}/m-from-bdev.msg", msg("m-from-bdev", "bot/dev-dispatcher"))
    w(f"{ti}/m-from-bmac.msg", msg("m-from-bmac", "bot/mac-dispatcher"))
    w(f"{ti}/m-from-topic.msg", msg("m-from-topic", "topic/dispatcher"))
    w(f"{ti}/m-from-agentd.msg", msg("m-from-agentd", "agentd", body=json.dumps(
        {"event": "task_done", "taskId": "tmac"})))
    w(f"{ti}/m-from-agentd-nopayload.msg",
      msg("m-from-agentd-nopayload", "agentd", body="not json"))
    w(f"{ti}/m-unparseable.msg", "{ not json")
    w(f"{ti}/notes.txt", "not an envelope")
    # the same envelope acked by three subscribers on two machines
    w(f"{ti}/ack/bot.dev-dispatcher/m-from-tdev",
      ack("m-from-tdev", "bot.dev-dispatcher"))
    w(f"{ti}/ack/bot.mac-dispatcher/m-from-tdev",
      ack("m-from-tdev", "bot.mac-dispatcher"))
    w(f"{ti}/ack/bot.svc.web/m-from-tdev", ack("m-from-tdev", "bot.svc.web"))
    w(f"{ti}/ack/bot.mac-dispatcher/m-from-bdev", ack("m-from-bdev"))
    w(f"{ti}/ack/bot.dev-dispatcher/m-content-wins",
      ack("m-content-wins", "bot.mac-dispatcher"))
    w(f"{ti}/ack/task.tmac/m-from-bdev", ack("m-from-bdev", "task.tmac"))
    w(f"{ti}/ack/bot.gone/m-from-bdev", ack("m-from-bdev", "bot.gone"))
    w(f"{ti}/ack/topic.mtg-y/m-from-bdev", ack("m-from-bdev", "topic.mtg-y"))
    w(f"{ti}/ack/m-flat", ack("m-flat"))     # forbidden flat ack (§4.6)
    w(f"{ad}/topic/mtg-x/topic.md", "# curated")
    w(f"{ad}/topic/mtg-x/minutes/2026-09-06.md", "# minutes")
    w(f"{ad}/topic/mtg-x/watcher/dev-dispatcher", "")

    # ---- legacy pre-split clans ----
    w(f"{ad}/participant/legacy/session/s.jsonl", "{}")
    w(f"{ad}/participant/legacy/inbox/a.msg", msg("a", "task/tdev"))
    w(f"{ad}/participant/legacy/inbox/ack/a", ack("a"))
    w(f"{ad}/participant/legacy/inbox/ack/bot.mac-dispatcher/z1",
      ack("z1", "bot.mac-dispatcher"))
    w(f"{ad}/ticket/tk1/ticket.json",
      json.dumps({"createdBy": "task/tdev", "owner": "legacy"}))
    w(f"{ad}/ticket/tk1/status.json", "{}")
    w(f"{ad}/ticket/tk1/events/e1.ev", json.dumps({"by": "task/tmac"}))
    w(f"{ad}/ticket/tk1/inbox/b.msg", msg("b", "bot/mac-dispatcher"))
    w(f"{ad}/ticket/tk1/inbox/ack/b", ack("b"))

    # ---- hub-only + per-machine singletons ----
    w(f"{ad}/gc/delete-list.000001", "task/tmac/\n")
    w(f"{ad}/run/agentd.dev.lock", "{}")
    w(f"{ad}/run/agentd.mac.lock", "{}")
    w(f"{ad}/run/other.txt", "x")
    w(f"{ad}/agentd.nv2.lock", "{}")          # legacy tree-root form
    w(f"{ad}/whatever/x.msg", msg("x", "task/tdev"))
    return ws, ad


def ctx_of(ws, ad):
    return rt.Ctx(ad, os.path.join(ws, "env", "host-id"))


def owner(rel, ctx):
    return rt.classify(rel, ctx)[0]


def klass(rel, ctx):
    return rt.classify(rel, ctx)[1]


ws, ad = mkws()
ctx = ctx_of(ws, ad)
NS = "topic/mtg-x/inbox/ack"

# =====================================================================
print("== 1. shared mailbox: dev/mac subscriber acks belong to their own "
      "machine ==")
check("topic ack of the dev subscriber -> dev",
      owner(f"{NS}/bot.dev-dispatcher/m-from-tdev", ctx) == "dev",
      owner(f"{NS}/bot.dev-dispatcher/m-from-tdev", ctx))
check("topic ack of the mac subscriber -> mac",
      owner(f"{NS}/bot.mac-dispatcher/m-from-tdev", ctx) == "mac",
      owner(f"{NS}/bot.mac-dispatcher/m-from-tdev", ctx))
check("both acks share one mailbox and one class (topic-ack-ns)",
      klass(f"{NS}/bot.dev-dispatcher/m-from-tdev", ctx) == "topic-ack-ns"
      and klass(f"{NS}/bot.mac-dispatcher/m-from-tdev", ctx)
      == "topic-ack-ns")
# the tagging decision main() derives from owner-vs-self, on both sides
for me, mine, theirs in (("dev", "bot.dev-dispatcher", "bot.mac-dispatcher"),
                         ("mac", "bot.mac-dispatcher", "bot.dev-dispatcher")):
    o_mine = owner(f"{NS}/{mine}/m-from-tdev", ctx)
    o_theirs = owner(f"{NS}/{theirs}/m-from-tdev", ctx)
    check(f"self={me}: own-subscriber ack stays UNMARKED (pushable)",
          o_mine == me, f"owner={o_mine}")
    check(f"self={me}: other-subscriber ack WOULD BE TAGGED replica",
          o_theirs is not None and o_theirs != me, f"owner={o_theirs}")
check("dotted bot name namespace (bot.svc.web) resolves exactly",
      owner(f"{NS}/bot.svc.web/m-from-tdev", ctx) == "dev",
      owner(f"{NS}/bot.svc.web/m-from-tdev", ctx))
check("ack content `subscriber` beats a diverging directory segment",
      owner(f"{NS}/bot.dev-dispatcher/m-content-wins", ctx) == "mac",
      owner(f"{NS}/bot.dev-dispatcher/m-content-wins", ctx))
check("directory segment authoritative when content has no subscriber",
      owner(f"{NS}/bot.mac-dispatcher/m-from-bdev", ctx) == "mac")
check("task subscriber -> that task's spec.host",
      owner(f"{NS}/task.tmac/m-from-bdev", ctx) == "mac")
check("unknown subscriber segment -> UNKNOWN (no guessing)",
      owner(f"{NS}/bot.gone/m-from-bdev", ctx) is None)
check("topic subscriber (non-process clan, no spec.host) -> UNKNOWN",
      owner(f"{NS}/topic.mtg-y/m-from-bdev", ctx) is None)
check("flat ack inside a shared mailbox -> UNKNOWN",
      owner(f"{NS}/m-flat", ctx) is None
      and klass(f"{NS}/m-flat", ctx) == "topic-ack-flat")

print("== 2. topic envelopes: writer = envelope `from` ==")
check("from=task/<id> -> that task's spec.host (dev task)",
      owner("topic/mtg-x/inbox/m-from-tdev.msg", ctx) == "dev")
check("from=task/<id> -> that task's spec.host (mac task)",
      owner("topic/mtg-x/inbox/m-from-tmac.msg", ctx) == "mac")
check("from=bot/<name> -> that bot's spec.host (dev bot)",
      owner("topic/mtg-x/inbox/m-from-bdev.msg", ctx) == "dev")
check("from=bot/<name> -> that bot's spec.host (mac bot)",
      owner("topic/mtg-x/inbox/m-from-bmac.msg", ctx) == "mac")
check("from=agentd -> resolved via body taskId (runner writes on the "
      "task's host)",
      owner("topic/mtg-x/inbox/m-from-agentd.msg", ctx) == "mac")
check("from=agentd without a provable taskId -> UNKNOWN",
      owner("topic/mtg-x/inbox/m-from-agentd-nopayload.msg", ctx) is None)
check("from=topic/<id> (position identity, no host) -> UNKNOWN",
      owner("topic/mtg-x/inbox/m-from-topic.msg", ctx) is None)
check("unparseable envelope -> UNKNOWN",
      owner("topic/mtg-x/inbox/m-unparseable.msg", ctx) is None)
check("non-.msg file in a topic inbox -> UNKNOWN",
      owner("topic/mtg-x/inbox/notes.txt", ctx) is None
      and klass("topic/mtg-x/inbox/notes.txt", ctx) == "topic-inbox-other")

print("== 3. topic non-mailbox files stay UNKNOWN (no host authority) ==")
check("topic.md -> UNKNOWN", owner("topic/mtg-x/topic.md", ctx) is None
      and klass("topic/mtg-x/topic.md", ctx) == "topic-curated")
check("minutes/** -> UNKNOWN",
      owner("topic/mtg-x/minutes/2026-09-06.md", ctx) is None)
check("watcher/* -> UNKNOWN",
      owner("topic/mtg-x/watcher/dev-dispatcher", ctx) is None
      and klass("topic/mtg-x/watcher/dev-dispatcher", ctx) == "topic-watcher")
check("topic/<id> with no sub path -> UNKNOWN",
      owner("topic/mtg-x", ctx) is None)

print("== 4. namespaced acks follow the subscriber in EVERY clan "
      "(t-3yp9③) ==")
check("task mailbox: namespaced ack -> subscriber host, NOT the task's",
      owner("task/tmac/inbox/ack/bot.dev-dispatcher/x1", ctx) == "dev",
      owner("task/tmac/inbox/ack/bot.dev-dispatcher/x1", ctx))
check("task mailbox: namespaced ack class = ack-ns",
      klass("task/tmac/inbox/ack/bot.dev-dispatcher/x1", ctx) == "ack-ns")
check("task mailbox: flat ack still -> spec.host (executor/reader)",
      owner("task/tmac/inbox/ack/x2", ctx) == "mac"
      and klass("task/tmac/inbox/ack/x2", ctx) == "ack")
check("task control: namespaced ack -> subscriber host",
      owner("task/tmac/control/ack/bot.dev-dispatcher/c1", ctx) == "dev")
check("bot mailbox: namespaced ack -> subscriber host, NOT the bot's",
      owner("bot/mac-dispatcher/inbox/ack/bot.dev-dispatcher/y1", ctx)
      == "dev")
check("bot mailbox: flat ack -> the bot's own host (reader)",
      owner("bot/mac-dispatcher/inbox/ack/y2", ctx) == "mac")
check("legacy participant mailbox: namespaced ack -> subscriber host "
      "(mac), not the participant's declared host (nv1)",
      owner("participant/legacy/inbox/ack/bot.mac-dispatcher/z1", ctx)
      == "mac",
      owner("participant/legacy/inbox/ack/bot.mac-dispatcher/z1", ctx))

print("== 5. bot/ clan rules ==")
check("bot spec.json -> createdByHost (declaration side)",
      owner("bot/mac-dispatcher/spec.json", ctx) == "dev"
      and klass("bot/mac-dispatcher/spec.json", ctx) == "bot-registry")
check("bot prompt.md -> createdByHost",
      owner("bot/mac-dispatcher/prompt.md", ctx) == "dev")
check("bot pid.json / session/** / loose files -> bot host",
      owner("bot/mac-dispatcher/pid.json", ctx) == "mac"
      and owner("bot/mac-dispatcher/session/session.jsonl", ctx) == "mac"
      and owner("bot/mac-dispatcher/loose.jsonl", ctx) == "mac")
check("bot control/*.req -> createdByHost (controller side)",
      owner("bot/mac-dispatcher/control/r1.req", ctx) == "dev"
      and klass("bot/mac-dispatcher/control/r1.req", ctx) == "bot-control")
check("bot control/ack -> bot host (its runner writes receipts)",
      owner("bot/mac-dispatcher/control/ack/r1", ctx) == "mac")
check("bot inbox envelope -> writer host",
      owner("bot/mac-dispatcher/inbox/from-dev.msg", ctx) == "dev"
      and klass("bot/mac-dispatcher/inbox/from-dev.msg", ctx)
      == "bot-inbox-msg")
check("bot inbox unparseable envelope -> UNKNOWN (no registry fallback)",
      owner("bot/mac-dispatcher/inbox/unparseable.msg", ctx) is None)
check("bot without spec.json: own files -> UNKNOWN, NO hub fallback",
      owner("bot/nospec/loose.txt", ctx) is None
      and klass("bot/nospec/loose.txt", ctx) == "bot-session")
check("bot without spec.json: its inbox envelope still resolves via from",
      owner("bot/nospec/inbox/whatever.msg", ctx) == "dev")

print("== 6. enable.json = scheduling side; hub = dev; run/ locks ==")
check("enable.json (by=agentd-scheduler, task registered on mac) -> hub "
      "= dev, NOT createdByHost",
      owner("task/tmac/enable.json", ctx) == rt.HUB == "dev",
      f"owner={owner('task/tmac/enable.json', ctx)} hub={rt.HUB}")
check("enable.json class = enable-scheduler",
      klass("task/tmac/enable.json", ctx) == "enable-scheduler")
check("enable.json with a resolvable `by` -> that writer's host",
      owner("task/byhand/enable.json", ctx) == "mac"
      and klass("task/byhand/enable.json", ctx) == "enable-by")
check("bot enable.json follows the same rule",
      owner("bot/mac-dispatcher/enable.json", ctx) == "dev")
check("spec.json / prompt.md still -> createdByHost",
      owner("task/tmac/spec.json", ctx) == "mac"
      and klass("task/tmac/spec.json", ctx) == "registry"
      and owner("task/tmac/prompt.md", ctx) == "mac")
check("executor products still -> spec.host",
      owner("task/tmac/report.md", ctx) == "mac"
      and klass("task/tmac/report.md", ctx) == "executor")
check("task inbox envelope -> writer host; unresolvable -> createdByHost "
      "fallback",
      owner("task/tmac/inbox/rel.msg", ctx) == "dev"
      and klass("task/tmac/inbox/rel.msg", ctx) == "task-inbox-msg"
      and owner("task/tmac/inbox/unk.msg", ctx) == "mac"
      and klass("task/tmac/inbox/unk.msg", ctx) == "task-inbox-msg-fallback")
check("gc/** -> hub = dev", owner("gc/delete-list.000001", ctx) == "dev"
      and rt.HUB == "dev")
check("run/agentd.<H>.lock -> <H> (own lock stays unmarked on dev)",
      owner("run/agentd.dev.lock", ctx) == "dev"
      and owner("run/agentd.mac.lock", ctx) == "mac"
      and klass("run/agentd.mac.lock", ctx) == "lock")
check("legacy tree-root lock form still works",
      owner("agentd.nv2.lock", ctx) == "nv2")
check("other run/ files -> UNKNOWN", owner("run/other.txt", ctx) is None
      and klass("run/other.txt", ctx) == "run-other")
check("hub-pinned legacy writer names resolve to dev",
      ctx.resolve_writer("dispatcher") == "dev"
      and ctx.resolve_writer("user") == "dev"
      and ctx.resolve_writer("user-im") == "dev")
check("bare name that is a bot dir -> that bot's host",
      ctx.resolve_writer("mac-dispatcher") == "mac"
      and ctx.resolve_writer("dev-dispatcher") == "dev")
check("legacy bare task id -> that task's host; task- prefix tolerated",
      ctx.resolve_writer("tmac") == "mac"
      and ctx.resolve_writer("task-tmac") == "mac")
check("unknown writer id -> None", ctx.resolve_writer("no-such") is None
      and ctx.resolve_writer("") is None)
check("hostname aliases normalize through env/host-id",
      ctx.normalize_host("hub.example.com") == "dev"
      and ctx.normalize_host("workstation.local") == "mac")

print("== 7. legacy clans unchanged (participant/ & ticket/) ==")
check("participant session/flat ack -> declared host (assistant/*.agent)",
      owner("participant/legacy/session/s.jsonl", ctx) == "nv1"
      and owner("participant/legacy/inbox/ack/a", ctx) == "nv1")
check("participant inbox envelope -> writer host",
      owner("participant/legacy/inbox/a.msg", ctx) == "dev")
check("ticket.json -> createdBy host; status.json -> owner host",
      owner("ticket/tk1/ticket.json", ctx) == "dev"
      and owner("ticket/tk1/status.json", ctx) == "nv1")
check("ticket event -> `by` host; ticket inbox msg -> `from` host",
      owner("ticket/tk1/events/e1.ev", ctx) == "mac"
      and owner("ticket/tk1/inbox/b.msg", ctx) == "mac")
check("ticket flat ack -> owner host",
      owner("ticket/tk1/inbox/ack/b", ctx) == "nv1")
check("unrecognized top-level dir -> UNKNOWN",
      owner("whatever/x.msg", ctx) is None
      and klass("whatever/x.msg", ctx) == "top-level-other")

# =====================================================================
print("== 8. end-to-end --apply in a temp tree (tagging decision) ==")


def _chgrp_capable(gid):
    """section 8 does real ``chown(-1, gid)``; probe the capability first.

    A process whose credentials lack the group (a shell spawned before the group
    was granted, a service/container context — ``id`` then differs from
    ``/etc/group``) fails EVERY tag, and ``replica-tag --apply`` exits 1 on
    ``failed>0``. That is an environment fact, not a regression, so it must read
    as SKIP — and the SystemExit must never be allowed to kill this suite
    silently (see run_apply below).
    """
    d = tempfile.mkdtemp(prefix="rt-gid-probe-")
    try:
        p = os.path.join(d, "probe")
        with open(p, "w"):
            pass
        os.chown(p, -1, gid)
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


SKIP_WHY = None
try:
    gid = grp.getgrnam(rt.REPLICA_GROUP).gr_gid
except KeyError:
    gid, SKIP_WHY = None, f"no {rt.REPLICA_GROUP!r} group on this machine"
if gid is not None and not _chgrp_capable(gid):
    gid, SKIP_WHY = None, (
        f"group {rt.REPLICA_GROUP!r} exists (gid {gid}) but THIS process cannot "
        "chgrp to it — its supplementary groups lack it (`id` differs from "
        "/etc/group); section 8 needs a login shell that is in that group")
if gid is None:
    print("  SKIP: " + SKIP_WHY)
else:
    ws2, ad2 = mkws()
    argv = sys.argv

    def run_apply():
        buf = io.StringIO()
        try:
            sys.argv = ["replica-tag.py", "--root", ad2, "--self", "dev",
                        "--apply"]
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    rt.main()
                except SystemExit as e:
                    # 工具级拒启/失败退出（如 apply 的 failed>0 → rc=1）不得静默带走
                    # 整套件：记下来，让下面的 check 带着诊断信息 FAIL。
                    print("  replica-tag main() exited rc=%s" % e.code, file=sys.stderr)
        finally:
            sys.argv = argv
        return buf.getvalue()

    def grp_of(rel):
        return os.lstat(os.path.join(ad2, rel)).st_gid

    out = run_apply()
    check("dev-owned topic ack stayed UNMARKED (self original)",
          grp_of(f"{NS}/bot.dev-dispatcher/m-from-tdev") != gid)
    check("mac-owned topic ack in the SAME mailbox was TAGGED replica",
          grp_of(f"{NS}/bot.mac-dispatcher/m-from-tdev") == gid)
    check("mac-written topic envelope TAGGED; dev-written one not",
          grp_of("topic/mtg-x/inbox/m-from-tmac.msg") == gid
          and grp_of("topic/mtg-x/inbox/m-from-tdev.msg") != gid)
    check("UNKNOWN files stay UNMARKED (conservative)",
          grp_of("topic/mtg-x/topic.md") != gid
          and grp_of(f"{NS}/m-flat") != gid
          and grp_of("bot/nospec/loose.txt") != gid)
    check("mac bot session file TAGGED; hub-owned gc list not (self=dev)",
          grp_of("bot/mac-dispatcher/session/session.jsonl") == gid
          and grp_of("gc/delete-list.000001") != gid)
    check("mac task enable.json NOT tagged on dev (scheduler = hub = dev)",
          grp_of("task/tmac/enable.json") != gid)
    check("run locks: own unmarked, foreign tagged",
          grp_of("run/agentd.dev.lock") != gid
          and grp_of("run/agentd.mac.lock") == gid)
    check("APPLY summary reports zero failures", "failed=0" in out,
          out[-200:])
    check("first run tagged exactly the foreign-owned files",
          re.search(r"unmarked, owned by others\s+: (\d+)", out)
          and int(re.search(r"unmarked, owned by others\s+: (\d+)",
                            out).group(1))
          == int(re.search(r"APPLY done: tagged=(\d+)", out).group(1)),
          out[-300:])
    out2 = run_apply()
    m2 = re.search(r"unmarked, owned by others\s+: (\d+)", out2)
    check("re-run is idempotent (nothing left to tag, marks survive, zero "
          "failures)",
          m2 and int(m2.group(1)) == 0
          and "APPLY done: tagged=0" in out2 and "failed=0" in out2
          and grp_of(f"{NS}/bot.mac-dispatcher/m-from-tdev") == gid,
          out2[-300:])
    shutil.rmtree(ws2)

shutil.rmtree(ws)

print()
print(f"{PASS} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL OK")
