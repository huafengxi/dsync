#!/usr/bin/env python3
"""
replica-tag.py — one-shot group tagging for ssh-sync.py's group gate
(participant/ & ticket/ layout rules added after the 2026-08-31 agents/ layout split, tasks tu7z3q /
9ejgm4; legacy flat-layout rules dropped when the
flat residue was GC-cleaned and dual-layout compat retired; bot/ &
topic/ clan rules + namespaced-ack ownership + path-style writer ids
added after the three-clan layout split, following the
topic push unification).

PURE MANUAL TOOL: nothing in the agents-sync service call chain
invokes this script (svc/agents-sync-loop.sh and dsync/ssh-sync.py
only mention it in comments) — it is run by hand per
bootstrap/SETUP.md §「agents/ 跨机同步」, so changing it needs no
service version bump and no restart.

The watch link marks every file it lands as a replica (filesystem
group ``replica``); UNMARKED = local original = the only file this
machine may push and the only file pull must never overwrite. Before
the first gated watch run on a machine the EXISTING tree must be
classified once: files whose single writer is THIS machine stay
unmarked; files whose writer is ANOTHER machine get the replica group.

Ownership rules (owner = canonical host name, per agentd single-writer
discipline; env/host-id maps hostname -> canonical name):

+ ``run/agentd.<H>.lock`` (and the legacy   -> owner <H> (embedded in
  tree-root ``agentd.<H>.lock``)               the file name; each
  machine writes its own liveness lock; locks moved under ``run/`` in)
+ ``gc/**``                                 -> hub (dsync/gc.py runs
  ONLY on the hub and publishes the delete lists there)
+ ``participant/<name>/**`` (the dispatcher participant lives at
  ``participant/dispatcher/`` since the legacy top-level dir was
  retired and GC-cleaned):
  - ``inbox/**`` except ``inbox/ack/**``    -> writer = envelope
    ``from`` resolved to a host (see writer resolution below);
    unparseable envelope -> UNKNOWN
  - ``inbox/ack/**``                        -> the READER writes acks;
    the reader is the participant session itself -> participant host
  - anything else (session jsonl, .pi files, ...)
                                            -> participant host
+ ``bot/<name>/**`` (resident session / process carriers; the clan
  that replaced ``participant/`` in the three-clan layout split —
  rules mirror the task-dir rules below):
  - ``spec.json``, ``prompt.md``            -> C = spec.createdByHost
    (registration/declaration side; fallback spec.host)
  - ``enable.json``                         -> the SCHEDULING side (see
    the enable.json rule below)
  - ``inbox/**`` except ``inbox/ack/**``:
    - ``*.msg``                             -> writer = envelope
      ``from`` resolved to a host; resolution fails -> UNKNOWN (a bot
      mailbox may be written from ANY machine, so there is no
      registry-side fallback here)
    - anything else                         -> C
  - ``control/**`` except ``control/ack/**``->   C (controller side)
  - ``inbox/ack/**``, ``control/ack/**``    -> see the ack rule below
    (flat = H = spec.host, the reader/supervising machine; namespaced
    = the SUBSCRIBER's host)
  - anything else (pid.json, pid.log, session/**, loose files)
                                            -> H (single writer = the
    machine whose runner supervises this bot)
+ ``topic/<id>/**`` (shared multi-subscriber mailbox —
  includes the position mailbox ``topic/dispatcher``).
  A topic is a NON-process participant: it has no spec.host, so only
  per-file writers are resolvable and everything else stays UNKNOWN:
  - ``inbox/*.msg``                         -> writer = envelope
    ``from`` resolved to a host; fails -> UNKNOWN
  - ``inbox/ack/<subscriber>/<id>``         -> the SUBSCRIBER's host
    (see the ack rule below) — NOT the topic's own machine
  - ``inbox/ack/<id>`` (flat)               -> UNKNOWN (protocol §4.6
    forbids flat acks in a shared mailbox; don't guess)
  - ``topic.md``, ``minutes/**``            -> UNKNOWN (single writer =
    the moderator, but no machine-readable authority: ``subscribes``
    declarers are not necessarily the moderator)
  - ``watcher/*``                           -> UNKNOWN (subscription
    registry entries: written by whoever subscribes/unsubscribes)
  - anything else                           -> UNKNOWN
+ ``ticket/<id>/**``:
  - ``ticket.json``                         -> writer = ``createdBy``
    resolved to a host
  - ``status.json``, ``context.md``         -> writer = ticket
    ``owner`` resolved to a host (owner maintains status, per the
    ticket system design)
  - ``events/*.ev``                         -> writer = event body
    ``by`` resolved to a host
  - ``inbox/**`` except ``inbox/ack/**``    -> writer = envelope
    ``from`` resolved to a host
  - ``inbox/ack/**``                        -> reader = ticket owner
    -> owner host
  - anything else                           -> UNKNOWN
+ inside a task dir <T> — ``task/<T>/`` (spec.json cached once per
  task):
  - ``spec.json``, ``prompt.md``            -> C = spec.createdByHost
  - ``enable.json``                         -> the SCHEDULING side:
    ``by`` (protocol §14.2) resolvable -> that writer's host;
    ``agentd-scheduler`` / unresolvable -> hub. The scheduler is a
    single global instance pinned to the hub host (env/services.yml
    ``scheduler.hosts``) and releases tasks on EVERY host
    (``--all-hosts``), so attributing enable.json to createdByHost
    tagged the hub's own originals as replica for every task
    registered elsewhere — they would never be pushed and the release
    would never reach the target machine
  - ``inbox/**`` except ``inbox/ack/**``:
    - ``*.msg``                             -> writer = envelope
      ``from`` resolved to a host (same resolution as participant
      inbox); resolution fails -> C (registry-side
      fallback); no spec -> UNKNOWN
    - anything else                         -> C (registry side writes)
  - ``control/**`` except ``control/ack/**``->   registry side writes
  - ``inbox/ack/**``, ``control/ack/**``    -> see the ack rule below
    (flat = H = spec.host, the READER/executor; namespaced = the
    SUBSCRIBER's host)
  - ``pid.json``, ``pid.log``, ``result.md``, ``report.md``,
    ``progress.md``, ``plan.md``, ``session/**``
                                            -> H (executor products)
  - anything else inside <T>                -> UNKNOWN
+ anything else                             -> UNKNOWN

Ack ownership (protocol §4.6 two-state acks): segments after ``ack/`` decide —
+ ``ack/<id>`` (flat, single-subscriber mailbox: ``task/``, ``bot/``)
  -> the READER = the mailbox's own session -> that participant's host
+ ``ack/<subscriber>/<id>`` (namespaced, shared multi-subscriber
  mailbox: ``topic/``) -> the WRITER is the SUBSCRIBER's session, so
  ownership follows the SUBSCRIBER's ``spec.host``, NOT the mailbox's
  owning participant (t-3yp9③). The ack file's own ``subscriber``
  field is authoritative (protocol §2.2: content beats file names);
  the namespace directory segment is the fallback. The segment is
  ``fsSafeId(participantId)`` (``/`` -> ``.``) and is NOT inverted by
  splitting on dots (names may contain dots, e.g. ``bot/svc.web``):
  existing ``task/`` + ``bot/`` directory names are forward-transformed
  once into an index and matched exactly. Unresolvable -> UNKNOWN.

Writer resolution: a writer id from an envelope/event/
ticket field maps to a host as follows, in order:
+ a PATH-STYLE participant id ``<family>/<name>`` (protocol §2.2, the
  only form written since the layout split):
  - ``task/<id>``                           -> ``host`` of that task's
    spec.json; no spec -> unknown
  - ``bot/<name>``                          -> ``host`` of
    ``bot/<name>/spec.json``; no spec / no usable host -> unknown
    (deliberately NO hub fallback: guessing the hub for a bot hosted
    elsewhere would tag that bot's originals as replica — the one
    mistake the group gate cannot undo)
  - ``topic/<id>``                          -> unknown (a topic is a
    position/shared identity with no host of its own)
+ ``dispatcher``, ``user``, ``user-im``    -> hub (the dispatcher,
  user and IM-bridge sessions all run on the hub)
+ ``agentd``                               -> NOT a fixed host: runner
  terminal notifications (agentd/runner.py notify_tick) are written
  by the runner on the TASK's host, with the the JSON
  body — resolve via that spec.host; unparseable -> unknown
+ a participant name (dir exists under ``participant/``)
                                            -> participant host via
  the DECLARATION scan (phase 2; recursive
  over the whole assistant/ subtree since D8 host-named agents live
  in subdirectories): scan ALL ``.agent`` files under
  ``<workspace>/assistant/`` (any depth) for a ``participant`` field
  declaring this name; exactly one declarer -> read its ``host``;
  multiple declarers -> conflict warning + fall back to the legacy
  same-name file (see below); no declarer -> fall back to reading the
  ``host`` field of the same-name ``<workspace>/assistant/<name>
  .agent`` (legacy path; still correct while every
  holder's .agent file is named after the participant).
  The host value is a canonical machine name (env/host-id aliases
  normalized through the same table self_host uses). Participant
  sessions may run on ANY machine (mac-worker runs on mac);
  mis-tagging an original as replica lets pull overwrite it. No
  usable host either way -> fall back to hub + warning (legacy
  behavior for participants that never got an .agent file).
+ a BARE name that is a ``bot/`` directory name
                                            -> that bot's host (same
  resolution as ``bot/<name>`` above; pre-split envelopes and a few
  tools still write bare session names)
+ a task id (``task-`` prefix tolerated)   -> ``host`` of that task's
  spec.json (``task/<id>/spec.json``); no spec -> unknown
+ anything else                            -> unknown

``hub`` = canonical name ``dev`` — the machine hosting the neutral hub
directory ``dev:/data/shared/agents`` (svc/agents-sync-loop.sh
``REMOTE``), the dispatcher/resident sessions and the single global
scheduler (env/services.yml ``scheduler.hosts``). It was ``nv1`` until
the hub migration (agents-sync v7 → v8).

UNKNOWN files stay UNMARKED (failure is asymmetric: worst case a
stale replica lingers, never data loss).

Behavior:
+ default = DRY RUN: prints the classification summary (counts per
  class + samples) and the list of files that WOULD be tagged.
+ ``--apply`` performs ``chown(-1, replica_gid)`` on exactly those
  files. Idempotent (already-tagged files are skipped). The script
  only ever TAGS; it never untags (untagging would invent a writer).
+ Requires the ``replica`` group on this machine and the running user
  to be a member.

Usage:
    ./replica-tag.py                 # dry run on <workspace>/agents
    ./replica-tag.py --apply
    ./replica-tag.py --root <dir> --self <canonical-host> --apply
"""




import argparse
import grp
import json
import os
import re
import socket
import stat as statmod
import sys

REPLICA_GROUP = "replica"
_WORKSPACE = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
# Hub = the machine hosting the neutral hub directory
# dev:/data/shared/agents (svc/agents-sync-loop.sh REMOTE), the
# dispatcher/resident sessions, the IM bridge sessions and the single
# global agentd scheduler (env/services.yml scheduler.hosts). It was
# nv1 until the hub migration — see the writer
# resolution rules in the docstring.
HUB = "dev"
# enable.json writer id of the scheduling side (agentd/scheduler.py
# SCHEDULER_ID, protocol §14.2).
SCHEDULER_ID = "agentd-scheduler"
LOCK_RE = re.compile(r"agentd\.(.+)\.lock$")
# Executor-written products inside a task dir (owner = spec.host).
EXECUTOR_FILES = {"pid.json", "pid.log", "result.md", "report.md",
                  "progress.md", "plan.md"}
# Registry-written files inside a task/bot dir (owner =
# spec.createdByHost). enable.json is NOT in this set: it is written by
# the SCHEDULING side (a single global scheduler pinned to the hub
# host, releasing tasks on every host), not by the registering side —
# see classify_enable().
REGISTRY_FILES = {"spec.json", "prompt.md"}
# Writer ids that unambiguously live on the hub regardless of layout.
HUB_WRITERS = {"dispatcher", "user", "user-im"}
SAMPLE_CAP = 5       # samples per class in the dry-run summary
LIST_CAP = 200       # max entries of the would-tag list printed


def fs_safe_id(pid_):
    """`/` -> `.` (protocol §2.2 single-point transform; mirrors
    agentd/proto.py fs_safe_id / core.ts fsSafeId). Not invertible when
    a name contains dots — hence subscriber segments are matched by
    forward-transforming real directory names (Ctx.subscriber_index)."""
    return pid_.replace("/", ".")


def die(msg):
    print(f"replica-tag: error: {msg}", file=sys.stderr)
    sys.exit(1)


def read_host_id_table(host_id_file):
    """env/host-id as a hostname -> canonical-name dict (same file
    format/convention as agentd/agentctl.py)."""
    table = {}
    try:
        with open(host_id_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    table[parts[0]] = parts[1]
    except OSError:
        pass
    return table


def self_host(host_id_file):
    """Canonical name of this machine via env/host-id (same convention
    as agentd/agentctl.py); falls back to the raw hostname."""
    hn = socket.gethostname()
    table = read_host_id_table(host_id_file)
    if hn in table:
        return table[hn]
    print(f"replica-tag: warning: no env/host-id entry for {hn!r}; "
          f"falling back to hostname as canonical name", file=sys.stderr)
    return hn


def load_json(path):
    """JSON dict from path, or {} if missing/unparseable."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class Ctx:
    """Classification context: tree root plus per-entity caches."""

    def __init__(self, root, host_id_file):
        self.root = root
        # Workspace = parent of the agents tree; <ws>/assistant/ .agent
        # participant specs and env/host-id live next to it. (With a
        # synthetic --root the same layout is expected around it.)
        self.ws = os.path.dirname(root)
        self.host_id_table = read_host_id_table(host_id_file)
        self.spec_cache = {}      # specdir relpath -> spec dict
        self.task_host_cache = {} # task id -> host or None
        self.bot_host_cache = {}  # bot name -> host or None
        self.sub_host_cache = {}  # ack subscriber segment -> host or None
        self.envelope_cache = {}  # relpath -> writer id or None
        self.ticket_cache = {}    # ticket id -> ticket.json dict
        self.agent_host_cache = {}  # participant name -> host
        self.agent_warned = set()   # names already warned about
        self.warned = set()         # one-shot warning keys (generic)
        # fsSafeId(participantId) -> (family, name) index of existing
        # task/ + bot/ dirs (lazy, built once): ack namespace segments
        # are matched by FORWARD transform, never by splitting on dots
        # (name grammar allows dots, protocol §2.2).
        self.sub_index = None
        # Declaration scan (phase 2): participant name ->
        # list of (agent file relpath under assistant/, host field or
        # None) for every .agent declaring that participant. Lazy,
        # built once on first use.
        self.decl_scan = None
        # Participant names = dirs under participant/ (dispatcher
        # included: participant/dispatcher/ is its home since the
        # legacy top-level dir was retired).
        self.participants = set()
        pdir = os.path.join(root, "participant")
        try:
            for name in os.listdir(pdir):
                if os.path.isdir(os.path.join(pdir, name)):
                    self.participants.add(name)
        except OSError:
            pass
        # bot/ clan = the resident session & process carriers that
        # replaced participant/ in the three-clan layout split.
        self.bots = set()
        bdir = os.path.join(root, "bot")
        try:
            for name in os.listdir(bdir):
                if os.path.isdir(os.path.join(bdir, name)):
                    self.bots.add(name)
        except OSError:
            pass

    def warn_once(self, key, msg):
        if key not in self.warned:
            self.warned.add(key)
            print(f"replica-tag: warning: {msg}", file=sys.stderr)

    def spec(self, specdir_rel):
        if specdir_rel not in self.spec_cache:
            self.spec_cache[specdir_rel] = load_json(
                os.path.join(self.root, specdir_rel, "spec.json"))
        return self.spec_cache[specdir_rel]

    def ticket(self, tid):
        if tid not in self.ticket_cache:
            self.ticket_cache[tid] = load_json(
                os.path.join(self.root, "ticket", tid, "ticket.json"))
        return self.ticket_cache[tid]

    def envelope_writer(self, rel):
        """Writer id recorded in a protocol envelope (from) — the file
        CONTENT is authoritative, not the file name (timestamp formats
        vary)."""
        if rel not in self.envelope_cache:
            d = load_json(os.path.join(self.root, rel))
            w = d.get("from")
            self.envelope_cache[rel] = w if isinstance(w, str) and w \
                else None
        return self.envelope_cache[rel]

    def envelope_owner(self, rel):
        """Host that wrote the envelope at rel, or None = unknown.
        from=agentd needs special care: runner terminal notifications
        are written by the runner ON THE TASK'S HOST (agentd/runner.py
        notify_tick), with the taskId in the JSON body — so they must
        resolve through that task's spec.host, not be pinned to any
        fixed machine (tagging a runner original as replica would keep
        it out of the push whitelist and it might never reach the
        hub)."""
        w = self.envelope_writer(rel)
        if not w:
            return None
        if w == "agentd" or w.startswith("agentd-"):
            d = load_json(os.path.join(self.root, rel))
            b = d.get("body")
            try:
                pl = json.loads(b) if isinstance(b, str) else b
            except ValueError:
                pl = None
            if isinstance(pl, dict):
                tid = pl.get("taskId")
                if isinstance(tid, str) and tid:
                    if tid.startswith("task-"):
                        tid = tid[5:]
                    h = self.task_host(tid)
                    if h:
                        return h
            return None  # agentd envelope without a provable taskId
        return self.resolve_writer(w)

    def bot_host(self, name):
        """Host of a bot/ participant (resident session or process
        carrier), via its own spec.json — the authoritative single
        writer statement (spec.host = the machine whose runner
        supervises it). No spec / no usable host -> None (UNKNOWN):
        deliberately NO hub fallback, since guessing the hub for a bot
        hosted elsewhere would tag that bot's ORIGINALS as replica
        there (the one mistake the group gate cannot undo)."""
        if name in self.bot_host_cache:
            return self.bot_host_cache[name]
        spec = self.spec(os.path.join("bot", name))
        host = spec.get("host") or spec.get("createdByHost")
        host = self.normalize_host(host) \
            if isinstance(host, str) and host else None
        if not host:
            self.warn_once("bot:" + name,
                           f"bot {name!r} has no bot/{name}/spec.json "
                           f"with a usable host; its files stay "
                           f"UNMARKED (owner UNKNOWN)")
        self.bot_host_cache[name] = host
        return host

    def subscriber_index(self):
        """{fsSafeId(participantId) -> (family, name)} over the existing
        task/ + bot/ directories, built once. Used to resolve ack
        namespace segments exactly (no dot-splitting inverse)."""
        if self.sub_index is None:
            idx = {}
            for family in ("task", "bot"):
                fdir = os.path.join(self.root, family)
                try:
                    names = os.listdir(fdir)
                except OSError:
                    continue
                for n in names:
                    if os.path.isdir(os.path.join(fdir, n)):
                        idx.setdefault(fs_safe_id(family + "/" + n),
                                       (family, n))
            self.sub_index = idx
        return self.sub_index

    def subscriber_host(self, seg):
        """Host of the subscriber named by an ack namespace segment /
        ``subscriber`` field: bot/<name> -> that bot's spec.host,
        task/<id> -> that task's spec.host, anything else (unknown
        segment, topic/<id> = a non-process participant) -> None."""
        if not isinstance(seg, str) or not seg:
            return None
        if seg in self.sub_host_cache:
            return self.sub_host_cache[seg]
        host = None
        ent = self.subscriber_index().get(seg)
        if ent:
            family, name = ent
            host = self.bot_host(name) if family == "bot" \
                else self.task_host(name)
        if not host:
            self.warn_once("sub:" + seg,
                           f"ack subscriber {seg!r} resolves to no "
                           f"usable host (no such task/bot directory, "
                           f"or its spec.json has no host); the ack "
                           f"stays UNMARKED")
        self.sub_host_cache[seg] = host
        return host

    def ack_owner(self, rel, ack_parts, flat_owner):
        """Owner of an ack file. ``ack_parts`` = the path segments after
        ``ack/``: [<id>] = FLAT ack of a single-subscriber mailbox (the
        reader = the mailbox's own session -> ``flat_owner``);
        [<subscriber>, <id>] = NAMESPACED ack of a shared
        multi-subscriber mailbox (protocol §4.6) — the
        writer is the SUBSCRIBER's session, so ownership follows the
        subscriber's spec.host, NOT the mailbox's owning participant.
        The ack's own ``subscriber`` field is authoritative (protocol
        §2.2: file content beats file names); the directory segment is
        the fallback. Unresolvable -> None (UNKNOWN)."""
        if len(ack_parts) < 2:
            return flat_owner
        d = load_json(os.path.join(self.root, rel))
        for cand in (d.get("subscriber"), ack_parts[0]):
            if isinstance(cand, str) and cand:
                host = self.subscriber_host(cand)
                if host:
                    return host
        return None

    def task_host(self, taskid):
        """Host a task runs on, via its spec.json (single layout:
        task/<id>)."""
        if taskid in self.task_host_cache:
            return self.task_host_cache[taskid]
        host = None
        spec = load_json(os.path.join(self.root, "task", taskid,
                                      "spec.json"))
        if spec:
            host = spec.get("host") or spec.get("createdByHost")
        self.task_host_cache[taskid] = host
        return host

    def normalize_host(self, host):
        """Normalize a machine name to its canonical form through the
        env/host-id table (hostname alias -> canonical name); values
        already canonical pass through."""
        if host in self.host_id_table:
            return self.host_id_table[host]  # hostname alias
        return host                          # assume canonical

    def declarations(self):
        """Scan the <workspace>/assistant/ subtree once (
        phase 2; recursive over ALL depths since the D8
        host-named agent files moved into subdirectories): return {participant name -> [(agent file relpath
        host field or None)]} built from every .agent's
        ``participant`` declaration. Unparseable files / non-string
        declarations are skipped."""

        if self.decl_scan is not None:
            return self.decl_scan
        scan = {}
        adir = os.path.join(self.ws, "assistant")
        for dirpath, dirs, files in os.walk(adir):
            dirs.sort()          # deterministic scan order
            for fn in sorted(files):
                if not fn.endswith(".agent"):
                    continue
                spec = load_json(os.path.join(dirpath, fn))
                p = spec.get("participant")
                if not isinstance(p, str) or not p:
                    continue
                h = spec.get("host")
                rel = os.path.relpath(os.path.join(dirpath, fn), adir)
                scan.setdefault(p, []).append(
                    (rel[:-len(".agent")],
                     h if isinstance(h, str) and h else None))
        self.decl_scan = scan
        return scan

    def participant_host(self, name):
        """Host of a participant session (data-driven mapping —
        participant sessions may run on any machine, e.g. mac-worker
        on mac). Epic.f0j2a1 phase 2: the holder is located by
        scanning ALL .agent files under the assistant/ subtree for a
        ``participant`` DECLARATION of this name (agent-side
        declaration, design §6).
        Exactly one declarer -> its host. Zero or multiple declarers
        -> fall back to the legacy same-name assistant/<name>.agent
        read (multiple declarations = conflict, warned
        once). No usable host either way -> HUB with a one-shot
        warning (legacy behavior)."""
        if name in self.agent_host_cache:
            return self.agent_host_cache[name]
        host = None
        declarers = self.declarations().get(name, [])
        if len(declarers) == 1:
            host = declarers[0][1]
        elif len(declarers) > 1:
            if name not in self.agent_warned:
                self.agent_warned.add(name)
                print(f"replica-tag: warning: participant {name!r} is "
                      f"declared by MULTIPLE .agent files "
                      f"({', '.join(d[0] for d in declarers)}); "
                      f"falling back to the same-name file",
                      file=sys.stderr)
        if not host:
            spec = load_json(os.path.join(self.ws, "assistant",
                                          name + ".agent"))
            h = spec.get("host")
            host = h if isinstance(h, str) and h else None
        if host:
            host = self.normalize_host(host)
        else:
            if name not in self.agent_warned:
                self.agent_warned.add(name)
                print(f"replica-tag: warning: participant {name!r} has "
                      f"no assistant/{name}.agent with a usable host; "
                      f"falling back to hub ({HUB!r})", file=sys.stderr)
            host = HUB
        self.agent_host_cache[name] = host
        return host

    def resolve_writer(self, name):
        """Map a writer id (envelope from / event by / ticket
        createdBy/owner) to a canonical host, or None = unknown. See
        the module docstring for the mapping and its caveats."""
        if not name:
            return None
        if "/" in name:
            # Path-style participant id (protocol §2.2) — the only form
            # written since the layout split.
            family, _, wname = name.partition("/")
            if family == "task":
                return self.task_host(wname)
            if family == "bot":
                return self.bot_host(wname)
            # topic/<id> as a writer = a shared/position identity (e.g.
            # the dispatcher writing as topic/dispatcher): no host of
            # its own -> don't guess.
            return None
        if name in HUB_WRITERS:
            return HUB
        if name in self.bots:
            return self.bot_host(name)
        if name in self.participants:
            return self.participant_host(name)
        taskid = name[5:] if name.startswith("task-") else name
        return self.task_host(taskid)


def classify_enable(rel, spec, ctx):
    """enable.json = the scheduling side's release record (protocol
    §14.2). The scheduler is a single global instance pinned to the hub
    host (env/services.yml ``scheduler.hosts``) and releases tasks on
    EVERY host (``--all-hosts``), so the writer is normally the hub —
    NOT spec.createdByHost (that mis-attribution tagged the hub's own
    originals as replica for every task registered on another machine:
    they would never be pushed and the release would never arrive).
    ``by`` names the actual writer: a resolvable participant id (manual
    ``agentctl enable --by ...``) wins; ``agentd-scheduler`` or
    anything unresolvable -> hub."""
    d = load_json(os.path.join(ctx.root, rel))
    by = d.get("by")
    if isinstance(by, str) and by and by != SCHEDULER_ID:
        host = ctx.resolve_writer(by)
        if host:
            return host, "enable-by"
    return HUB, "enable-scheduler"


def classify_participant(rel, name, sub_parts, ctx):
    """Files under participant/<name>/: single writer is the participant
    session's host, except inbox messages, which anyone may send."""
    sub = os.sep.join(sub_parts)
    host = ctx.participant_host(name)
    if sub.startswith("inbox" + os.sep):
        rest = sub_parts[1:]
        if rest[0] == "ack":
            # reader = session itself (flat); namespaced = subscriber
            return ctx.ack_owner(rel, rest[1:], host), "participant-ack"
        if sub_parts[-1].endswith(".msg"):
            return ctx.envelope_owner(rel), "participant-inbox-msg"
        return None, "participant-inbox-other"
    return host, "participant-session"


def classify_bot(rel, name, sub_parts, ctx):
    """Files under bot/<name>/ (resident session / process carrier):
    single writer = the machine whose runner supervises it (spec.host),
    except inbox envelopes (any machine may send), the registry-side
    files and the release record. Mirrors the task-dir rules."""
    spec = ctx.spec(os.path.join("bot", name))
    sub = os.sep.join(sub_parts)
    h_host = ctx.bot_host(name)      # None (+ one-shot warning) w/o spec
    c_host = spec.get("createdByHost") or h_host
    if sub == "enable.json":
        return classify_enable(rel, spec, ctx)
    if sub in REGISTRY_FILES:
        return c_host, "bot-registry"
    if sub.startswith("inbox" + os.sep):
        rest = sub_parts[1:]
        if rest[0] == "ack":
            return ctx.ack_owner(rel, rest[1:], h_host), \
                ("ack-ns" if len(rest) >= 3 else "ack")
        if sub_parts[-1].endswith(".msg"):
            # a bot mailbox may be written from any machine; no
            # registry-side fallback (unlike task inboxes)
            return ctx.envelope_owner(rel), "bot-inbox-msg"
        return c_host, "bot-inbox-other"
    if sub.startswith("control" + os.sep):
        rest = sub_parts[1:]
        if rest[0] == "ack":
            return ctx.ack_owner(rel, rest[1:], h_host), "ack"
        return c_host, "bot-control"
    return h_host, "bot-session"


def classify_topic(rel, tid, sub_parts, ctx):
    """Files under topic/<id>/ — a shared multi-subscriber mailbox, including the position mailbox topic/dispatcher. A topic is a NON-process participant with no spec.host, so
    only per-file writers are resolvable; everything else stays UNKNOWN
    (conservative: an unmarked file is never overwritten by pull)."""


    sub = os.sep.join(sub_parts)
    if sub.startswith("inbox" + os.sep):
        rest = sub_parts[1:]
        if rest[0] == "ack":
            if len(rest) >= 3:
                # namespaced: the SUBSCRIBER's session wrote it
                return ctx.ack_owner(rel, rest[1:], None), "topic-ack-ns"
            # flat ack in a shared mailbox: protocol §4.6 forbids it
            # (implementations refuse the binding instead) -> don't guess
            return None, "topic-ack-flat"
        if sub_parts[-1].endswith(".msg"):
            return ctx.envelope_owner(rel), "topic-inbox-msg"
        return None, "topic-inbox-other"
    if sub == "topic.md" or sub.startswith("minutes" + os.sep):
        return None, "topic-curated"
    if sub.startswith("watcher" + os.sep):
        return None, "topic-watcher"
    return None, "topic-other"


def classify_ticket(rel, tid, sub_parts, ctx):
    """Files under ticket/<id>/: owner-maintained files resolve via
    ticket.json; envelopes/events resolve via their recorded writer."""
    sub = os.sep.join(sub_parts)
    tj = ctx.ticket(tid)
    owner = tj.get("owner") if isinstance(tj.get("owner"), str) else None
    if sub == "ticket.json":
        created = tj.get("createdBy")
        return ctx.resolve_writer(created), "ticket-json"
    if sub in ("status.json", "context.md"):
        return ctx.resolve_writer(owner), "ticket-owner-file"
    if (sub.startswith("events" + os.sep)
            and sub_parts[-1].endswith(".ev")):
        ev = load_json(os.path.join(ctx.root, rel))
        by = ev.get("by")
        return ctx.resolve_writer(by if isinstance(by, str) else None), \
            "ticket-event"
    if sub.startswith("inbox" + os.sep):
        rest = sub_parts[1:]
        if rest[0] == "ack":
            return ctx.ack_owner(rel, rest[1:],
                                 ctx.resolve_writer(owner)), "ticket-ack"
        if sub_parts[-1].endswith(".msg"):
            return ctx.envelope_owner(rel), "ticket-inbox-msg"
        return None, "ticket-inbox-other"
    return None, "ticket-other"


def classify_task_sub(rel, sub, sub_parts, spec, ctx):
    """Task-dir rules (shared by everything under task/<id>/)."""
    c_host = spec.get("createdByHost") or spec.get("host")
    h_host = spec.get("host") or spec.get("createdByHost")
    if not spec:
        return None, "no-spec"
    if sub == "enable.json":
        return classify_enable(rel, spec, ctx)
    if sub in REGISTRY_FILES:
        return c_host, "registry"
    if sub.startswith("inbox" + os.sep):
        rest = sub_parts[1:]
        if rest[0] == "ack":
            # flat = the executor (reader) writes it; namespaced = the
            # SUBSCRIBER's host, not this task's
            return ctx.ack_owner(rel, rest[1:], h_host), \
                ("ack-ns" if len(rest) >= 3 else "ack")
        return c_host, "inbox-other"
    if sub.startswith("control" + os.sep):
        rest = sub_parts[1:]
        if rest[0] == "ack":
            return ctx.ack_owner(rel, rest[1:], h_host), "ack"
        return c_host, "control-msg"
    if sub in EXECUTOR_FILES or sub.startswith("session" + os.sep):
        return h_host, "executor"
    return None, "task-other"


def classify(rel, ctx):
    """Return (owner, class) for a relpath; owner None = UNKNOWN."""
    parts = rel.split(os.sep)
    top = parts[0]
    if len(parts) == 1:
        m = LOCK_RE.match(parts[0])
        if m:
            return m.group(1), "lock"
        return None, "top-level-other"
    if top == "gc":
        return HUB, "gc"
    if top == "bot":
        if len(parts) >= 3:
            return classify_bot(rel, parts[1], parts[2:], ctx)
        return None, "bot-other"
    if top == "topic":
        if len(parts) >= 3:
            return classify_topic(rel, parts[1], parts[2:], ctx)
        return None, "topic-other"
    if top == "run":
        # liveness locks moved under run/ in each machine
        # writes its own (host embedded in the file name)
        if len(parts) == 2:
            m = LOCK_RE.match(parts[1])
            if m:
                return m.group(1), "lock"
        return None, "run-other"
    if top == "participant":
        if len(parts) >= 3:
            return classify_participant(rel, parts[1], parts[2:], ctx)
        return None, "participant-other"
    if top == "ticket":
        if len(parts) >= 3:
            return classify_ticket(rel, parts[1], parts[2:], ctx)
        return None, "ticket-other"
    # Task dirs: single layout task/<id>/... (the legacy
    # flat task dirs were GC-cleaned). Any other top-level dir is not a
    # recognized container -> UNKNOWN (top-level-other below).
    if top == "task" and len(parts) >= 3:
        specdir, sub = os.path.join("task", parts[1]), \
            os.sep.join(parts[2:])
    else:
        return None, "top-level-other"
    spec = ctx.spec(specdir)
    # Task inbox envelopes are written by whoever sent them (any
    # machine), not just the registry side: resolve via the envelope
    # from field like participant inbox does; fall back
    # to createdByHost, then UNKNOWN.
    if (sub.startswith("inbox" + os.sep)
            and not sub.startswith("inbox" + os.sep + "ack" + os.sep)
            and parts[-1].endswith(".msg")):
        owner = ctx.envelope_owner(rel)
        if owner:
            return owner, "task-inbox-msg"
        return (spec.get("createdByHost") or spec.get("host")), \
            "task-inbox-msg-fallback"
    return classify_task_sub(rel, sub, parts[2:], spec, ctx)


def main():
    p = argparse.ArgumentParser(
        description="One-shot replica-group tagging for the ssh-sync "
                    "group gate (dry run unless --apply).")
    p.add_argument("--root", default=os.path.join(_WORKSPACE, "agents"),
                   help="tree root to classify (default: <workspace>/agents)")
    p.add_argument("--self", default=None, metavar="HOST",
                   help="canonical name of this machine (default: "
                        "env/host-id lookup by hostname)")
    p.add_argument("--apply", action="store_true",
                   help="actually chown (default: dry run)")
    args = p.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        die(f"root not a directory: {root}")
    try:
        gid = grp.getgrnam(REPLICA_GROUP).gr_gid
    except KeyError:
        die(f"group {REPLICA_GROUP!r} not found on this machine "
            f"(sudo groupadd {REPLICA_GROUP} && sudo usermod -aG "
            f"{REPLICA_GROUP} $USER)")
    host_id_file = os.path.join(_WORKSPACE, "env", "host-id")
    me = args.self or self_host(host_id_file)
    print(f"replica-tag: root={root} self={me!r} "
          f"replica gid={gid} mode={'APPLY' if args.apply else 'DRY RUN'}")

    ctx = Ctx(root, host_id_file)
    buckets = {}          # class -> list of (rel, owner)
    already_tagged = []
    own = []              # unmarked, owner == self -> stays unmarked
    to_tag = []           # unmarked, owner == other -> tag
    unknown = []          # unmarked, owner unknown -> stays (conservative)

    for dirpath, dirs, files in os.walk(root):
        for fn in files:
            pth = os.path.join(dirpath, fn)
            try:
                st = os.lstat(pth)
            except OSError:
                continue
            if not statmod.S_ISREG(st.st_mode):
                continue
            rel = os.path.relpath(pth, root)
            if st.st_gid == gid:
                already_tagged.append(rel)
                continue
            owner, cls = classify(rel, ctx)
            buckets.setdefault(cls, []).append((rel, owner))
            if owner is None:
                unknown.append(rel)
            elif owner == me:
                own.append(rel)
            else:
                to_tag.append((rel, owner))

    total = len(already_tagged) + len(own) + len(to_tag) + len(unknown)
    print(f"\n== classification ({total} files) ==")
    print(f"  already tagged (replica)      : {len(already_tagged)}")
    print(f"  unmarked, owned by self       : {len(own)}  (stay unmarked)")
    print(f"  unmarked, owned by others     : {len(to_tag)}  "
          f"({'WILL TAG' if args.apply else 'would tag'})")
    print(f"  unmarked, owner UNKNOWN       : {len(unknown)}  "
          f"(stay unmarked, conservative)")
    print(f"\n== by class ==")
    for cls in sorted(buckets):
        items = buckets[cls]
        print(f"  {cls:22s}: {len(items)}")
        for rel, owner in items[:SAMPLE_CAP]:
            print(f"      {rel}  (owner={owner})")
        if len(items) > SAMPLE_CAP:
            print(f"      ... {len(items) - SAMPLE_CAP} more")
    if unknown:
        print(f"\n== UNKNOWN (stay unmarked; sample) ==")
        for rel in unknown[:SAMPLE_CAP]:
            print(f"  {rel}")
        if len(unknown) > SAMPLE_CAP:
            print(f"  ... {len(unknown) - SAMPLE_CAP} more")
    if to_tag:
        print(f"\n== {'tagging' if args.apply else 'would tag'} "
              f"{len(to_tag)} file(s) ==")
        for rel, owner in to_tag[:LIST_CAP]:
            print(f"  {rel}  (owner={owner})")
        if len(to_tag) > LIST_CAP:
            print(f"  ... {len(to_tag) - LIST_CAP} more (full list "
                  f"suppressed)")

    if not args.apply:
        print("\nreplica-tag: dry run done; re-run with --apply to chown.")
        return
    ok = fail = skipped = 0
    for rel, owner in to_tag:
        pth = os.path.join(root, rel)
        try:
            st = os.lstat(pth)
            if st.st_gid == gid:
                skipped += 1  # raced / re-run idempotency
                continue
            os.chown(pth, -1, gid)
            ok += 1
        except OSError as e:
            fail += 1
            print(f"replica-tag: chown failed: {rel}: {e}", file=sys.stderr)
    print(f"\nreplica-tag: APPLY done: tagged={ok} already-tagged={skipped} "
          f"failed={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
