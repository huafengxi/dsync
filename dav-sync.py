#!/usr/bin/env python3
"""
dav-sync.py — Sync files between local mirror and remote storage.

Directory layout: only ONE level of subdirectories under the root is
synced; top-level files (direct children of the root) are ignored on
both download and upload, and never cleaned. Deeper nesting is also
ignored.

Remote ``.done`` semantics (cleanup protocol): renaming a remote
subdirectory ``<base>`` to ``<base>.done`` marks that requirement dir
as finished/void and drives local cleanup through three rules:

  1. Download never pulls remote ``<...>.done`` subdirectories.
  2. Upload skips a local subdir ``<base>.<tag>`` entirely while remote
     ``<base>.done`` exists (tag = last dot segment of the local dir
     name; a dir name without a dot matches on its full name).
  3. clean_local deletes local ``<base>.<tag>`` while remote
     ``<base>.done`` exists — even if a live remote ``<base>.<tag>``
     also exists (``.done`` wins).

Name fuzzing (double fuzz): the WHOLE file stem is fuzzed, extension
kept (``_fuzz_str``, symmetric: fuzz==defuzz). Subdirectory names are
NOT fuzzed: local subdirectory names are identical to the remote
names. ``*.done`` remote subdirectories are skipped (checked on the
remote name).

The watcher names its conversion products ``<src-base>.<fuzz(req-type)>
<ext>`` (i.e. the req-type tag is fuzzed locally, see
``watcher/convert-file.py``). Because both upload and below fuzz the
entire stem, symmetry restores the tag to readable plaintext on the
remote: local ``y0853.xyXHybx-OFkxR.mp4`` uploads as
``n0853.enhance-video.mp4`` and downloads back to the identical local
name, so existing files dedup naturally (``Skip (exists)``) — the
former download-naming asymmetry loop cannot occur.

Upload: for a local subdir ``<x>.<req-type>``, files whose stem ends
with ``.<fuzz(req-type)>`` (watcher conversion products,
``<src-base>.<fuzz(req-type)><ext>``) are uploaded with
remote_name = fuzz(whole stem) + ext. Plaintext legacy product names
(``<src-base>.<req-type><ext>``) do NOT match the filter and are not
uploaded; anything already on the remote stays untouched. The former
``*.restored.mp4`` special branch is retired (the demosaic handler
was removed; existing remote restored files still download normally).

Upload .part skip: any name carrying a ``.part`` segment (``.part.``
or a trailing ``.part``) is never uploaded — this covers dav downloads
in progress (``<name>.part``) and watcher conversions in progress
(``<src-base>.<fuzz(req-type)>.part.<ext>``). The size-stability guard below
remains as a fallback for partials from other sources.

Upload size-stability guard: a file can match the upload filter while
it is still being written. Uploads therefore require the file size to
be unchanged since the previous upload cycle
(an in-memory ``{path: last-seen size}`` record; a fresh process starts
with an empty record, so its first cycle only records sizes and never
uploads). Records are dropped after a successful upload and pruned when
the file disappears.

Backends (DAV_BACKEND env, default pikpak):
    webdav  — direct WebDAV (WEBDAV_ENDPOINT_URL; the former alist-mount route was removed)
    pikpak  — direct PikPak API via pikpak.py (same directory). The pikpak
              backend forces direct downloads only
              (pikpak.set_download_no_proxy(True)); uploads (and list/move
              etc. API calls) resolve proxies per PIKPAK_PROXY /
              *_PROXY env config. The standalone ./pikpak.py CLI keeps its
              own proxy resolution.

Defaults: all positional arguments are optional — remote defaults to
``shared`` and the local mirror defaults to ``<workspace>/run/temp/shared``
(resolved relative to this script, independent of the cwd).

Usage:
    ./dav-sync.py download [remote_dir] [local_mirror]
    ./dav-sync.py upload [local_mirror] [remote_dir]
    ./dav-sync.py clean_local [local_dir] [remote_dir]
    ./dav-sync.py sync [remote_dir] [local_mirror] [--interval 30] [--skip-upload]
"""

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

# ---------------------------------------------------------------------------
# Defaults (resolve the workspace from this script's location, not the cwd)
# ---------------------------------------------------------------------------

_WORKSPACE = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEFAULT_REMOTE = "shared"
DEFAULT_MIRROR = os.path.join(_WORKSPACE, "run", "temp", "shared")

# ---------------------------------------------------------------------------
# Name fuzz (same rule as fsu.py nfuzz) — symmetric, fuzz==defuzz
# ---------------------------------------------------------------------------

_CHAR_MAP = 'iFXhbcNYDuUgsjrIMJwTpPAqnyvOfSxeEzWBkdtQmlZCoRVKLGHa'

def _fuzz_str(s):
    """Fuzz a string using the nfuzz rule. Symmetric: _fuzz_str(_fuzz_str(x)) == x."""
    def _translate(c):
        i = _CHAR_MAP.find(c)
        return _CHAR_MAP[i ^ 1] if i >= 0 else c
    return ''.join(map(_translate, s))


# ---------------------------------------------------------------------------
# WebDAV helpers
# ---------------------------------------------------------------------------

def _dec_secret(cipher):
    """Decrypt an enc1: cipher value via encrypt/envdec.py (in-memory only)."""
    envdec = os.path.join(os.path.dirname(__file__), "..", "encrypt", "envdec.py")
    return subprocess.run(
        [sys.executable, envdec, "--value", cipher],
        capture_output=True, text=True, check=True,
    ).stdout


def _load_webdav_env():
    """Load WebDAV credentials from env files."""
    env = {}
    env_files = [
        os.path.join(os.path.dirname(__file__), "..", "env", "webdav.env"),
        os.path.join(os.path.dirname(__file__), "..", "dav.env"),
        os.path.expanduser("~/m/env/webdav.env"),
        os.path.expanduser("~/m/dav.env"),
    ]
    for f in env_files:
        if os.path.isfile(f):
            with open(f) as fh:
                for line in fh:
                    m = re.match(r"^\s*(?:export\s+)?([A-Za-z_]\w*)\s*=\s*(.*)\s*$", line)
                    if not m:
                        continue
                    val = m.group(2).strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                        val = val[1:-1]
                    if val.startswith("enc1:"):
                        val = _dec_secret(val[5:])
                    env[m.group(1)] = val
            break

    # Direct WebDAV only (the alist-mount route was removed with alist).
    url = env.get("WEBDAV_ENDPOINT_URL") or env.get("WEBDAV_URL")
    if url:
        p = urllib.parse.urlsplit(url)
        return {
            "hostname": f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else ""),
            "username": p.username or env.get("WEBDAV_USERNAME", ""),
            "password": p.password or env.get("WEBDAV_PASSWORD", ""),
            "root": env.get("WEBDAV_ROOT", "/"),
        }
    return {
        "hostname": env.get("WEBDAV_HOSTNAME", ""),
        "username": env.get("WEBDAV_USERNAME", ""),
        "password": env.get("WEBDAV_PASSWORD", ""),
        "root": env.get("WEBDAV_ROOT", "/"),
    }


def _get_webdav_client():
    """Create a webdav4 Client from env config."""
    from webdav4.client import Client
    creds = _load_webdav_env()
    hostname = creds["hostname"].rstrip("/")
    root = "/" + creds["root"].strip("/")
    base_url = f"{hostname}{root}"
    return Client(
        base_url,
        auth=(creds["username"], creds["password"]),
        verify=False,
        follow_redirects=True,
        timeout=120.0,
        trust_env=False,
    )


def _list_webdav_entries(dav, path):
    """List a WebDAV directory, returning (file_names, dir_names).

    Returns ``(None, None)`` when the directory cannot be listed (e.g. remote
    is unreachable).  Callers must NOT interpret ``None`` as an empty
    directory, otherwise destructive steps would run against an unknown
    remote state.
    """
    import os as _os
    try:
        items = dav.ls(path, detail=True)
    except Exception as e:
        print(f"Error listing {path}: {e}", file=sys.stderr)
        return None, None

    files, dirs = [], []
    for item in items:
        name = item.get("name", "")
        name = _os.path.split(name)[1]
        if not name:
            continue
        is_dir = (
            item.get("isdir") or
            item.get("href", "").endswith("/") or
            item.get("content_type") in ("httpd/unix-directory", "directory")
        )
        (dirs if is_dir else files).append(name)
    return files, dirs


def _download_from_webdav(dav, remote_path, local_path):
    """Download a file from WebDAV to local path via a .part temp file.

    The final file only appears after the download completes, so watchers
    (handler scripts) never see a partial file.
    """
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    part_path = local_path + ".part"
    try:
        with open(part_path, "wb") as f:
            dav.download_fileobj(remote_path, f)
        os.replace(part_path, local_path)
        return True
    except Exception as e:
        print(f"Download error {remote_path}: {e}", file=sys.stderr)
        try:
            os.remove(part_path)
        except OSError:
            pass
        return False


def _upload_to_webdav(dav, local_path, remote_path):
    """Upload a local file to WebDAV."""
    try:
        with open(local_path, "rb") as f:
            data = f.read()
        buffer = io.BytesIO(data)
        dav.upload_fileobj(buffer, remote_path, overwrite=True)
        return True
    except Exception as e:
        print(f"Upload error {remote_path}: {e}", file=sys.stderr)
        return False


def _move_webdav(dav, src_path, dst_path):
    """Move/rename a file on WebDAV."""
    try:
        dav.move(src_path, dst_path, overwrite=True)
        return True
    except Exception as e:
        print(f"Move error {src_path} -> {dst_path}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Backends — uniform interface: list / download / upload / move.
# list() returns None when the remote is unreachable/unknown (callers must
# NOT treat that as an empty dir).
# ---------------------------------------------------------------------------

class WebDAVBackend:
    name = "webdav"

    def __init__(self):
        self.dav = _get_webdav_client()

    def list(self, path):
        files, _ = _list_webdav_entries(self.dav, path)
        return files

    def list_dirs(self, path):
        _, dirs = _list_webdav_entries(self.dav, path)
        return dirs

    def download(self, remote_path, local_path):
        return _download_from_webdav(self.dav, remote_path, local_path)

    def upload(self, local_path, remote_path):
        return _upload_to_webdav(self.dav, local_path, remote_path)

    def move(self, src_path, dst_path):
        return _move_webdav(self.dav, src_path, dst_path)


class PikPakBackend:
    """Direct PikPak API backend (dsync/pikpak.py)."""
    name = "pikpak"

    def __init__(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pikpak
        # Force direct (no proxy) downloads for the dav-sync pikpak
        # backend: the dl CDN is reachable from CN egress and downloads
        # are faster without the proxy. Uploads and API calls keep the
        # normal proxy resolution (PIKPAK_PROXY / *_PROXY env). Only
        # affects this process; the standalone ./pikpak.py CLI keeps its
        # own proxy behaviour.
        pikpak.set_download_no_proxy(True)
        self.pk = pikpak

    @staticmethod
    def _p(path):
        return "/" + path.strip("/") if path.strip("/") else "/"

    def _list_entries(self, path):
        """Return (files, dirs) for a remote path, or (None, None) on error."""
        p = self._p(path)
        try:
            parent = "" if p == "/" else self.pk._get_folder_id(p)
            files, dirs = [], []
            page_token = None
            while True:
                result = self.pk.list_files(parent_id=parent,
                                            page_token=page_token)
                for f in result["files"]:
                    (dirs if f["kind"] == "drive#folder" else files) \
                        .append(f["name"])
                page_token = result.get("next_page_token")
                if not page_token:
                    break
            return files, dirs
        except Exception as e:
            print(f"Error listing {p}: {e}", file=sys.stderr)
            return None, None

    def list(self, path):
        files, _ = self._list_entries(path)
        return files

    def list_dirs(self, path):
        _, dirs = self._list_entries(path)
        return dirs

    def download(self, remote_path, local_path):
        """Download via .part temp file (watchers never see partials)."""
        part_path = local_path + ".part"
        try:
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            self.pk.download_file(self._p(remote_path), part_path)
            os.replace(part_path, local_path)
            return True
        except Exception as e:
            print(f"Download error {remote_path}: {e}", file=sys.stderr)
            try:
                os.remove(part_path)
            except OSError:
                pass
            return False

    def upload(self, local_path, remote_path):
        try:
            self.pk.upload_file(local_path, self._p(remote_path))
            return True
        except Exception as e:
            print(f"Upload error {remote_path}: {e}", file=sys.stderr)
            return False

    def move(self, src_path, dst_path):
        try:
            src = self._p(src_path)
            dst = self._p(dst_path)
            src_parent = self.pk._get_folder_id(os.path.dirname(src))
            f = self.pk._find_child(src_parent, os.path.basename(src))
            if not f:
                print(f"Move error: {src_path} not found", file=sys.stderr)
                return False
            dst_parent = self.pk._get_folder_id(os.path.dirname(dst),
                                                create=True)
            self.pk._api_request(
                "POST", "/drive/v1/files:batchMove",
                json={"ids": [f["id"]], "to": {"parent_id": dst_parent}})
            return True
        except Exception as e:
            print(f"Move error {src_path} -> {dst_path}: {e}",
                  file=sys.stderr)
            return False


def _done_bases(remote_dirs):
    """Set of bases having a ``<base>.done`` marker dir in remote_dirs.

    ``remote_dirs`` must be a list of remote subdir names (never None).
    """
    return {d[:-len(".done")] for d in remote_dirs if d.endswith(".done")}


def _local_dir_base(name):
    """Base of a local subdir name for ``.done`` matching.

    ``<base>.<tag>`` -> ``<base>`` (tag = last dot segment, same
    rpartition rule as the upload req-type parsing). Names without a
    dot match on the full name.
    """
    base, dot, _tag = name.rpartition(".")
    return base if (dot and base) else name


def _get_backend():
    name = os.environ.get("DAV_BACKEND", "pikpak").lower()
    if name == "pikpak":
        return PikPakBackend()
    return WebDAVBackend()


def _clean_part_files(local_dir, remote_files):
    """清理孤儿 .part：目标文件已存在，或远端已不再有该文件"""
    expected_finals = set()
    for fname in remote_files:
        base, ext = os.path.splitext(fname)
        expected_finals.add(_fuzz_str(base) + ext)
    for entry in os.listdir(local_dir):
        if not entry.endswith(".part"):
            continue
        target = entry[:-len(".part")]
        in_remote = target in expected_finals
        final_exists = os.path.isfile(os.path.join(local_dir, target))
        if in_remote and not final_exists:
            continue  # 正在下载/待下载的临时文件，保留
        try:
            os.remove(os.path.join(local_dir, entry))
            print(f"  Clean .part: {entry}", file=sys.stderr)
        except OSError:
            pass


def _download_dir(backend, remote_dir, local_dir, remote_files):
    """Download the (fuzzed) files of one remote dir into one local dir."""
    _clean_part_files(local_dir, remote_files)

    for fname in remote_files:
        base, ext = os.path.splitext(fname)
        local_name = _fuzz_str(base) + ext
        local_path = os.path.join(local_dir, local_name)
        if os.path.isfile(local_path):
            print(f"  Skip (exists): {fname} -> {local_name}", file=sys.stderr)
            continue

        remote_path = f"{remote_dir}/{fname}"
        print(f"  Download: {fname} -> {local_name}", file=sys.stderr)
        if backend.download(remote_path, local_path):
            print(f"    OK", file=sys.stderr)
        else:
            print(f"    FAILED", file=sys.stderr)


# ---------------------------------------------------------------------------
# Upload size-stability guard (see module docstring)
# ---------------------------------------------------------------------------

_UPLOAD_SEEN_SIZES = {}  # {local_path: size seen in the previous upload cycle}


def _prune_upload_size_records():
    """Drop size records whose file no longer exists (file/dir deleted)."""
    for path in list(_UPLOAD_SEEN_SIZES):
        if not os.path.isfile(path):
            del _UPLOAD_SEEN_SIZES[path]


def _upload_dir(backend, local_dir, remote_dir, local_sub_name):
    """Upload conversion products of one local dir into one remote dir.

    Upload rule (double fuzz): for a local dir named ``<x>.<req-type>``,
    any file whose stem ends with ``.<fuzz(req-type)>`` is uploaded —
    the watcher product naming ``<src-base>.<fuzz(req-type)><ext>``
    (see ``watcher/convert-file.py``). The remote name is
    ``_fuzz_str(whole stem) + ext``; by symmetry the fuzzed tag is
    restored to readable plaintext on the remote
    (``y0853.xyXHybx-OFkxR.mp4`` -> ``n0853.enhance-video.mp4``).

    Source files (fuzzed originals, no ``.<fuzz(req-type)>`` suffix)
    and plaintext legacy products (``<src-base>.<req-type><ext>``) do
    not match the filter and are never (re-)uploaded. The former
    ``*.restored.mp4`` special branch is retired.
    """
    remote_files = backend.list(remote_dir)
    if remote_files is None:
        print(f"SKIP upload: remote /{remote_dir} unreachable", file=sys.stderr)
        return
    remote_set = set(remote_files)

    _base, dot, req_type = local_sub_name.rpartition(".")
    req_type = req_type if (dot and _base) else None
    fuzz_tag = "." + _fuzz_str(req_type) if req_type is not None else None

    for entry in sorted(os.listdir(local_dir)):
        local_path = os.path.join(local_dir, entry)
        # Skip anything with a .part segment: downloads in progress
        # (<name>.part) and conversions in progress
        # (<x>.<fuzz(req)>.part.<ext>).
        if (not os.path.isfile(local_path) or ".part." in entry
                or entry.endswith(".part")):
            continue

        if fuzz_tag is None:
            continue  # dir name without req-type: nothing to upload
        stem, ext = os.path.splitext(entry)
        if not stem.endswith(fuzz_tag) or len(stem) <= len(fuzz_tag):
            continue  # not a conversion product for this req-type
        remote_name = _fuzz_str(stem) + ext
        if remote_name in remote_set:
            print(f"  Skip (exists): {entry} -> {remote_name}", file=sys.stderr)
            continue

        # Size-stability guard: only upload once the size has been
        # unchanged across a full cycle (first sight just records).
        try:
            size = os.path.getsize(local_path)
        except OSError:
            continue
        prev_size = _UPLOAD_SEEN_SIZES.get(local_path)
        if prev_size is None:
            _UPLOAD_SEEN_SIZES[local_path] = size
            print(f"  Skip (first seen, size recorded {size}): {entry}", file=sys.stderr)
            continue
        if size != prev_size:
            _UPLOAD_SEEN_SIZES[local_path] = size
            print(f"  Skip (size changing {prev_size} -> {size}, still being written): {entry}", file=sys.stderr)
            continue

        remote_path = f"{remote_dir}/{remote_name}"
        print(f"  Upload: {entry} -> {remote_name}", file=sys.stderr)
        if backend.upload(local_path, remote_path):
            print(f"    OK", file=sys.stderr)
            _UPLOAD_SEEN_SIZES.pop(local_path, None)  # done; free the record
        else:
            print(f"    FAILED", file=sys.stderr)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_download(args):
    """Download files from the remote SUBDIRECTORIES (one level) into
    local_mirror. Remote top-level files are ignored. Remote subdirectories
    named ``<name>.done`` are skipped entirely."""
    backend = _get_backend()
    remote_dir = args.remote_dir.strip("/")
    local_mirror = args.local_mirror

    os.makedirs(local_mirror, exist_ok=True)

    remote_files = backend.list(remote_dir)
    remote_dirs = backend.list_dirs(remote_dir)
    if remote_files is None or remote_dirs is None:
        print(f"SKIP download: remote /{remote_dir} unreachable", file=sys.stderr)
        return
    print(f"Remote: {len(remote_files)} top-level file(s) ignored, {len(remote_dirs)} dirs in /{remote_dir}", file=sys.stderr)
    print(f"Local: {local_mirror}", file=sys.stderr)

    for dname in sorted(remote_dirs):
        if dname.endswith(".done"):
            print(f"  Skip .done dir: {dname}", file=sys.stderr)
            continue
        sub_files = backend.list(f"{remote_dir}/{dname}")
        if sub_files is None:
            print(f"  Skip dir (unreachable): {dname}", file=sys.stderr)
            continue
        local_sub = os.path.join(local_mirror, dname)
        os.makedirs(local_sub, exist_ok=True)
        print(f"Dir: {dname} ({len(sub_files)} files) -> {local_sub}", file=sys.stderr)
        _download_dir(backend, f"{remote_dir}/{dname}", local_sub, sub_files)

    print("Download complete.", file=sys.stderr)


def cmd_upload(args):
    """Upload conversion products (``<base>.<fuzz(req-type)><ext>`` per
    subdir req-type) from local_mirror SUBDIRECTORIES (one level) to
    remote_dir. Top-level local files are ignored. A local subdir
    ``<base>.<tag>`` is skipped entirely while remote ``<base>.done``
    exists (.done cleanup protocol, rule 2)."""
    backend = _get_backend()
    local_mirror = args.local_mirror
    remote_dir = args.remote_dir.strip("/")
    _prune_upload_size_records()

    # .done rule 2: list remote root dirs once; skip uploading any
    # local <base>.<tag> whose remote <base>.done marker exists.
    remote_root_dirs = backend.list_dirs(remote_dir)
    if remote_root_dirs is None:
        print(f"SKIP upload: remote /{remote_dir} unreachable", file=sys.stderr)
        return
    done_bases = _done_bases(remote_root_dirs)

    print(f"Local: {local_mirror}", file=sys.stderr)
    print(f"Remote: /{remote_dir}", file=sys.stderr)

    for entry in sorted(os.listdir(local_mirror)):
        local_sub = os.path.join(local_mirror, entry)
        if not os.path.isdir(local_sub):
            print(f"  Skip top-level file: {entry}", file=sys.stderr)
            continue
        base = _local_dir_base(entry)
        if base in done_bases:
            print(f"  Skip dir ({base}.done on remote): {entry}", file=sys.stderr)
            continue
        # Local dir name is the remote dir name (no fuzzing)
        remote_sub = f"{remote_dir}/{entry}"
        print(f"Dir: {entry} -> /{remote_sub}", file=sys.stderr)
        _upload_dir(backend, local_sub, remote_sub, entry)

    print("Upload complete.", file=sys.stderr)


def cmd_clean_local(args):
    """Align the local mirror with the remote at subdirectory level.

    Destructive: local subdirectories whose corresponding remote
    subdirectory no longer exists are deleted entirely (list printed
    before deleting). Additionally, local ``<base>.<tag>`` is deleted
    while remote ``<base>.done`` exists (.done cleanup protocol,
    rule 3; takes precedence over a live remote ``<base>.<tag>``).
    """
    backend = _get_backend()
    local_dir = args.local_dir
    remote_dir = args.remote_dir.strip("/")

    remote_dirs = backend.list_dirs(remote_dir)
    if remote_dirs is None:
        print(f"SKIP clean local: remote /{remote_dir} unreachable", file=sys.stderr)
        return
    # Local dir names equal remote dir names directly
    remote_set = set(remote_dirs)
    # .done rule 3: remote <base>.done deletes local <base>.<tag> even
    # if a live remote <base>.<tag> also exists (.done wins).
    done_bases = _done_bases(remote_dirs)
    print(f"Remote: {len(remote_dirs)} dirs in /{remote_dir}", file=sys.stderr)
    print(f"Local: {local_dir}", file=sys.stderr)

    doomed = []  # (entry, reason)
    for entry in sorted(os.listdir(local_dir)):
        local_path = os.path.join(local_dir, entry)
        if not os.path.isdir(local_path):
            continue
        base = _local_dir_base(entry)
        if base in done_bases:
            doomed.append((entry, f"remote {base}.done"))
        elif entry not in remote_set:
            doomed.append((entry, "no matching remote dir"))

    if not doomed:
        print("Clean local complete. (nothing to remove)", file=sys.stderr)
        return

    print(f"Will delete {len(doomed)} local dir(s):", file=sys.stderr)
    for entry, reason in doomed:
        print(f"  {entry} ({reason})", file=sys.stderr)

    for entry, reason in doomed:
        local_path = os.path.join(local_dir, entry)
        print(f"  Delete: {entry} ({reason})", file=sys.stderr)
        try:
            shutil.rmtree(local_path)
            print(f"    OK", file=sys.stderr)
        except OSError as e:
            print(f"    FAILED: {e}", file=sys.stderr)

    print("Clean local complete.", file=sys.stderr)


def cmd_sync(args):
    """Run download, upload, clean_local in a loop."""
    from argparse import Namespace
    remote_dir = args.remote_dir
    local_mirror = args.local_mirror
    interval = args.interval
    skip_upload = args.skip_upload or os.environ.get("DAV_SKIP_UPLOAD") == "1"

    print(f"dav-sync loop started", file=sys.stderr)
    print(f"  remote: {remote_dir}", file=sys.stderr)
    print(f"  local:  {local_mirror}", file=sys.stderr)
    print(f"  interval: {interval}s", file=sys.stderr)
    print(f"  upload:  {'SKIP' if skip_upload else 'enabled'}", file=sys.stderr)

    os.makedirs(local_mirror, exist_ok=True)

    while True:
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{ts}] --- download ---", file=sys.stderr)
            cmd_download(Namespace(remote_dir=remote_dir, local_mirror=local_mirror))

            if skip_upload:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] --- upload --- SKIP", file=sys.stderr)
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] --- upload ---", file=sys.stderr)
                cmd_upload(Namespace(local_mirror=local_mirror, remote_dir=remote_dir))

            print(f"[{datetime.now().strftime('%H:%M:%S')}] --- clean local ---", file=sys.stderr)
            cmd_clean_local(Namespace(
                local_dir=local_mirror, remote_dir=remote_dir))

        except KeyboardInterrupt:
            print("\nStopping dav-sync...", file=sys.stderr)
            break
        except Exception as e:
            print(f"Sync loop error: {e}", file=sys.stderr)

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync files between local mirror and WebDAV remote."
    )
    sub = parser.add_subparsers(dest="command", help="Commands")

    # download
    def _remote_arg(p):
        p.add_argument("remote_dir", nargs="?", default=DEFAULT_REMOTE,
                       help=f"Remote directory (default: {DEFAULT_REMOTE})")

    def _mirror_arg(p, name="local_mirror"):
        p.add_argument(name, nargs="?", default=DEFAULT_MIRROR,
                       help=f"Local mirror directory (default: {DEFAULT_MIRROR})")

    p_dl = sub.add_parser("download", help="Download files from remote to local mirror")
    _remote_arg(p_dl)
    _mirror_arg(p_dl)

    # upload
    p_ul = sub.add_parser("upload", help="Upload conversion products (<base>.<fuzz(req-type)><ext>) to remote")
    _mirror_arg(p_ul)
    _remote_arg(p_ul)

    # clean_local
    p_cl = sub.add_parser("clean_local",
                          help="Delete local subdirs with no matching remote subdir")
    _mirror_arg(p_cl, name="local_dir")
    _remote_arg(p_cl)

    # sync (loop)
    p_sync = sub.add_parser("sync",
                            help="Run download/upload/clean_local in a loop")
    _remote_arg(p_sync)
    _mirror_arg(p_sync)
    p_sync.add_argument("--interval", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    p_sync.add_argument("--skip-upload", action="store_true",
                        help="Skip the upload step (also honours DAV_SKIP_UPLOAD=1)")

    args = parser.parse_args()

    if args.command == "download":
        cmd_download(args)
    elif args.command == "upload":
        cmd_upload(args)
    elif args.command == "clean_local":
        cmd_clean_local(args)
    elif args.command == "sync":
        cmd_sync(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()