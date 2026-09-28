#!/usr/bin/env python3
"""
pikpak.py — self-contained PikPak CLI (direct API, no WebDAV).

Credentials
-----------
Reads PIKPAK_USERNAME / PIKPAK_PASSWORD from the environment, or from
$WS/env/pikpak.web automatically (so `source env/pikpak.web` is optional).

Session
-------
Login state is persisted in ~/.pikpak_token.json (access_token +
refresh_token). Commands reuse the cached session and only re-login
automatically when the token is rejected (401). Run `login` explicitly
only once, or to force a fresh login.

Proxy
-----
PikPak API is geo-blocked from CN egress. Set a proxy if needed:
    export HTTPS_PROXY=http://127.0.0.1:7897
    export SOCKS_PROXY=socks5://127.0.0.1:7897

Usage
-----
    ./pikpak.py login                          # (re)login, cache session
    ./pikpak.py login --captcha-token <TOKEN>  # finish login after solving slider
    ./pikpak.py login --refresh-token <TOKEN>  # seed session from a refresh token
    ./pikpak.py upload <local> <remote>        # upload a file
    ./pikpak.py list [path]                    # list a directory
    ./pikpak.py download <remote> [local]      # download a file
    ./pikpak.py link <remote>                  # print ~24h direct link
    ./pikpak.py delete <remote>                # delete a file
    ./pikpak.py mkdir <path>                   # create a folder
    ./pikpak.py logout                         # clear cached session

Platform
--------
PIKPAK_PLATFORM=android (env or env/pikpak.web) switches to the Android
client emulation (client_id/UA/devicesign/algorithms). Default is web.
Tokens are per-client: switching platform requires a fresh `login`.

The per-platform client credentials are NOT in this file: they are read from
the credential face like every other secret (process env wins over
env/pikpak.web, `enc1:` inline encryption supported) under
PIKPAK_WEB_CLIENT_ID / PIKPAK_WEB_CLIENT_SECRET and
PIKPAK_ANDROID_CLIENT_ID / PIKPAK_ANDROID_CLIENT_SECRET. A missing key is a
hard error at import time (it names the key and the file to put it in).

Uploads above $PIKPAK_MULTIPART_MB (default 100) use OSS multipart.

Note: PikPak disabled plain password grant ("currently not supported").
Login now goes through the shield-captcha flow (/v1/shield/captcha/init +
/v1/auth/signin). Usually init hands back a captcha_token directly; if it
returns a slider URL instead, solve it in a browser and re-run with
--captcha-token.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

API_HOST = "https://api-drive.mypikpak.net"
AUTH_HOST = "https://user.mypikpak.net"

TOKEN_FILE = os.path.expanduser("~/.pikpak_token.json")
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR.parent / "env" / "pikpak.web"
REDIRECT_URI = "xlaccsdk01://xbase.cloud/callback?state=harbor"


# ---------------------------------------------------------------------------
# Env / credentials
# ---------------------------------------------------------------------------

def _dec_secret(cipher):
    """Decrypt an enc1: cipher value via encrypt/envdec.py (in-memory only)."""
    envdec = SCRIPT_DIR.parent / "encrypt" / "envdec.py"
    return subprocess.run(
        [sys.executable, str(envdec), "--value", cipher],
        capture_output=True, text=True, check=True,
    ).stdout


def _load_env_file(path):
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path) as fh:
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
    return env


def _creds():
    username = os.environ.get("PIKPAK_USERNAME", "")
    password = os.environ.get("PIKPAK_PASSWORD", "")
    if not username or not password:
        env = _load_env_file(ENV_FILE)
        username = username or env.get("PIKPAK_USERNAME", "")
        password = password or env.get("PIKPAK_PASSWORD", "")
    return username, password


# ---------------------------------------------------------------------------
# Platform (web | android) — constants from the open-source PikPak driver (util.go).
# Tokens are per-client: switching platform requires a fresh login.
# ---------------------------------------------------------------------------

_PLATFORMS = {
    "web": {
        # client_id / client_secret are CREDENTIALS: they come from the
        # deployment's credential face (process env, or ENV_FILE with `enc1:`
        # inline encryption) and are NEVER shipped in this file. The keys below
        # name the variables; everything else in this table is a protocol
        # constant of the vendor's own client (version, package, signature
        # salts), not a credential.
        "client_id_key": "PIKPAK_WEB_CLIENT_ID",
        "client_secret_key": "PIKPAK_WEB_CLIENT_SECRET",
        "client_version": "2.0.0",
        "package_name": "mypikpak.com",
        "algorithms": [
            "C9qPpZLN8ucRTaTiUMWYS9cQvWOE", "+r6CQVxjzJV6LCV", "F", "pFJRC",
            "9WXYIDGrwTCz2OiVlgZa90qpECPD6olt", "/750aCr4lm/Sly/c",
            "RB+DT/gZCrbV", "", "CyLsf7hdkIRxRm215hl", "7xHvLi2tOYP0Y92b",
            "ZGTXXxu8E/MIWaEDB+Sm/", "1UI3",
            "E7fP5Pfijd+7K+t6Tg/NhuLq0eEUVChpJSkrKxpO",
            "ihtqpG6FMt65+Xk+tWUH2", "NhXXU9rg4XXdzo7u5o",
        ],
    },
    "android": {
        "client_id_key": "PIKPAK_ANDROID_CLIENT_ID",
        "client_secret_key": "PIKPAK_ANDROID_CLIENT_SECRET",
        "client_version": "1.53.2",
        "package_name": "com.pikcloud.pikpak",
        "sdk_version": "2.0.6.206003",
        "algorithms": [
            "SOP04dGzk0TNO7t7t9ekDbAmx+eq0OI1ovEx",
            "nVBjhYiND4hZ2NCGyV5beamIr7k6ifAsAbl",
            "Ddjpt5B/Cit6EDq2a6cXgxY9lkEIOw4yC1GDF28KrA",
            "VVCogcmSNIVvgV6U+AochorydiSymi68YVNGiz",
            "u5ujk5sM62gpJOsB/1Gu/zsfgfZO",
            "dXYIiBOAHZgzSruaQ2Nhrqc2im",
            "z5jUTBSIpBN9g4qSJGlidNAutX6",
            "KJE2oveZ34du/g1tiimm",
        ],
    },
}


def _env_conf(key, default=""):
    return os.environ.get(key) or _load_env_file(ENV_FILE).get(key, default) \
        or default


def _require_conf(key):
    """A credential with NO default: missing = hard error naming the key.

    Failing loudly at import time is the point — a silent empty client_secret
    would surface much later as an opaque auth error from the API.
    """
    val = _env_conf(key)
    if not val:
        raise SystemExit(
            "pikpak: %s is not set. Put it in the credential face (%s, `enc1:` "
            "inline-encrypted) or export it in the environment; this file ships "
            "no client credentials." % (key, ENV_FILE))
    return val


PLATFORM = _env_conf("PIKPAK_PLATFORM", "web").lower()
_PLAT = _PLATFORMS.get(PLATFORM, _PLATFORMS["web"])
CLIENT_ID = _require_conf(_PLAT["client_id_key"])
CLIENT_SECRET = _require_conf(_PLAT["client_secret_key"])
CLIENT_VERSION = _PLAT["client_version"]
PACKAGE_NAME = _PLAT["package_name"]
ALGORITHMS = _PLAT["algorithms"]


# ---------------------------------------------------------------------------
# Proxy / session
# ---------------------------------------------------------------------------

# Programmatic proxy switch for library consumers (e.g. dav-sync). When
# enabled via set_download_no_proxy(True), ONLY the download path goes
# direct (_content_session() ignores PIKPAK_DL_PROXY and download_file
# drops the proxy fallback); API sessions and uploads (incl. OSS
# multipart) keep the original proxy resolution (_get_proxies()). The
# CLI default is unchanged: False, so everything follows _get_proxies().
_DOWNLOAD_NO_PROXY = False


def set_download_no_proxy(enabled=True):
    """Force direct connections (no proxy) for downloads only. API and
    upload requests still resolve proxies via _get_proxies(). Never
    called by the CLI itself, so running ./pikpak.py standalone keeps
    the original proxy behaviour."""
    global _DOWNLOAD_NO_PROXY
    _DOWNLOAD_NO_PROXY = bool(enabled)


def _direct_proxies():
    """Explicit no-proxy marker: keys present with None values block
    requests from merging *_PROXY environment variables in."""
    return {"http": None, "https": None}


def _get_proxies():
    # PIKPAK_PROXY (env or env/pikpak.web) takes precedence.
    pikpak_proxy = os.environ.get("PIKPAK_PROXY") or \
        _load_env_file(ENV_FILE).get("PIKPAK_PROXY", "")
    if pikpak_proxy:
        return {"http": pikpak_proxy, "https": pikpak_proxy}
    proxies = {}
    for var in ["SOCKS_PROXY", "socks_proxy", "HTTPS_PROXY", "https_proxy",
                "HTTP_PROXY", "http_proxy"]:
        val = os.environ.get(var)
        if not val:
            continue
        low = var.lower()
        if low.startswith("socks"):
            proxies["http"] = val
            proxies["https"] = val
        elif low.startswith("https"):
            proxies["https"] = val
        else:
            proxies["http"] = val
    return proxies if proxies else None


def _get_session():
    s = requests.Session()
    proxies = _get_proxies()
    if proxies:
        s.proxies.update(proxies)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    })
    return s


def _device_id():
    return hashlib.md5(f"pikpak-{os.uname().nodename}".encode()).hexdigest()


def _with_retry(fn, attempts=3, what="request"):
    """Retry transient network errors (the proxy tunnel drops occasionally)."""
    for i in range(attempts):
        try:
            return fn()
        except requests.RequestException as e:
            if i + 1 >= attempts:
                raise RuntimeError(f"{what} failed after {attempts} attempts: "
                                   f"{type(e).__name__}: {str(e)[:150]}")
            print(f"  {what}: {type(e).__name__}; retry {i + 2}/{attempts}",
                  file=sys.stderr)
            time.sleep(2 * (i + 1))


def _user_agent():
    if PLATFORM == "android":
        # Custom Android UA with devicesign (BuildCustomUserAgent).
        dev = _device_id()
        base = f"{dev}{PACKAGE_NAME}1appkey"
        sha1 = hashlib.sha1(base.encode()).hexdigest()
        md5 = hashlib.md5(sha1.encode()).hexdigest()
        sign = f"div101.{dev}{md5}"
        return (f"ANDROID-{PACKAGE_NAME}/{CLIENT_VERSION} "
                f"protocolVersion/200 accesstype/ clientid/{CLIENT_ID} "
                f"clientversion/{CLIENT_VERSION} action_type/ "
                f"networktype/WIFI sessionid/ deviceid/{dev} "
                f"providername/NONE devicesign/{sign} refresh_token/ "
                f"sdkversion/{_PLAT.get('sdk_version', '')} "
                f"datetime/{int(time.time() * 1000)} userno/ "
                f"appname/android-{PACKAGE_NAME} session_origin/ grant_type/ "
                f"appid/ clientip/ devicename/Xiaomi_M2004j7ac osversion/13 "
                f"platformversion/10 accessmode/ devicemodel/M2004J7AC ")
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/117.0.0.0 Safari/537.36")


USER_AGENT = _user_agent()


# ---------------------------------------------------------------------------
# Auth / persistent session
# ---------------------------------------------------------------------------

def _load_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_token(data):
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)


def _refresh_grant(refresh_token):
    """Exchange a refresh_token for a fresh token set. None on failure."""
    try:
        resp = _with_retry(lambda: _get_session().post(
            f"{AUTH_HOST}/v1/auth/token",
            params={"client_id": CLIENT_ID},
            json={
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
            },
            timeout=60,
        ), what="token refresh")
        if resp.status_code == 200:
            return resp.json()
    except RuntimeError:
        pass
    return None


def _captcha_sign():
    """Compute (timestamp, captcha_sign) for post-login captcha init."""
    ts = str(int(time.time() * 1000))
    s = f"{CLIENT_ID}{CLIENT_VERSION}{PACKAGE_NAME}{_device_id()}{ts}"
    for alg in ALGORITHMS:
        s = hashlib.md5((s + alg).encode()).hexdigest()
    return ts, "1." + s


def _captcha_init(action, meta=None):
    """POST /v1/shield/captcha/init. Returns (captcha_token, url)."""
    device_id = _device_id()
    resp = _with_retry(lambda: _get_session().post(
        f"{AUTH_HOST}/v1/shield/captcha/init",
        params={"client_id": CLIENT_ID},
        headers={"X-Device-ID": device_id, "X-Captcha-Token": ""},
        json={
            "action": action,
            "captcha_token": "",
            "client_id": CLIENT_ID,
            "device_id": device_id,
            "meta": meta or {},
            "redirect_uri": REDIRECT_URI,
        },
        timeout=60,
    ), what="captcha init")
    if resp.status_code != 200:
        raise RuntimeError(f"captcha init failed: {resp.status_code} {resp.text[:300]}")
    d = resp.json()
    return d.get("captcha_token", ""), d.get("url", "")


def _refresh_captcha(action):
    """Post-login captcha token for a drive API action (e.g. 'GET:/drive/v1/files').
    Persists the new token and returns it."""
    cached = _load_token()
    ts, sign = _captcha_sign()
    meta = {
        "client_version": CLIENT_VERSION,
        "package_name": PACKAGE_NAME,
        "user_id": cached.get("sub", ""),
        "timestamp": ts,
        "captcha_sign": sign,
    }
    ct, url = _captcha_init(action, meta)
    if url:
        raise RuntimeError(
            "PikPak requires a slider captcha for this action. Open this "
            f"URL in a browser, solve it, then re-run:\n  {url}")
    cached["captcha_token"] = ct
    _save_token(cached)
    return ct


def _signin(username, password, captcha_token):
    """POST /v1/auth/signin with a captcha_token -> token set."""
    device_id = _device_id()
    resp = _with_retry(lambda: _get_session().post(
        f"{AUTH_HOST}/v1/auth/signin",
        params={"client_id": CLIENT_ID},
        headers={"X-Device-ID": device_id, "X-Captcha-Token": captcha_token},
        json={
            "captcha_token": captcha_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": username,
            "password": password,
        },
        timeout=60,
    ), what="signin")
    if resp.status_code != 200:
        raise RuntimeError(f"signin failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def login(force=False, captcha_token=None, refresh_token=None):
    """Login and persist the session.

    Order: --refresh-token > cached refresh_token > --captcha-token >
    captcha-init + signin. PikPak no longer supports the plain password
    grant, so full login goes through the shield-captcha flow.
    """
    device_id = _device_id()

    if refresh_token:
        data = _refresh_grant(refresh_token)
        if not data:
            raise RuntimeError("Provided refresh_token was rejected.")
        data["device_id"] = device_id
        _save_token(data)
        return data

    cached = {} if force else _load_token()
    if cached.get("refresh_token"):
        data = _refresh_grant(cached["refresh_token"])
        if data:
            data["device_id"] = device_id
            _save_token(data)
            return data

    username, password = _creds()
    if not username or not password:
        raise RuntimeError(
            "No cached session and no PIKPAK_USERNAME/PIKPAK_PASSWORD "
            f"(env or {ENV_FILE}).")

    if not captcha_token:
        captcha_token, url = _captcha_init("POST:/v1/auth/signin",
                                           {"email": username})
        if url:
            raise RuntimeError(
                "PikPak requires a slider captcha. Open this URL in a "
                f"browser, solve it, then re-run:\n  {url}\n"
                "  ./pikpak.py login --captcha-token <TOKEN>")

    data = _signin(username, password, captcha_token)
    data["device_id"] = device_id
    data["captcha_token"] = captcha_token
    _save_token(data)
    return data


def get_access_token():
    token_data = _load_token()
    if token_data.get("access_token"):
        return token_data["access_token"]
    return login()["access_token"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _err_code(resp):
    try:
        return resp.json().get("error_code", 0)
    except Exception:
        return 0


def _api_request(method, path, **kwargs):
    token = get_access_token()
    cached = _load_token()
    s = _get_session()
    s.headers["Authorization"] = f"Bearer {token}"
    s.headers["X-Device-ID"] = _device_id()
    if cached.get("captcha_token"):
        s.headers["X-Captcha-Token"] = cached["captcha_token"]

    url = f"{API_HOST}{path}"
    resp = _with_retry(lambda: s.request(method, url, timeout=120, **kwargs),
                       what=f"API {method} {path}")

    if resp.status_code == 401 or _err_code(resp) in (4121, 4122, 16):
        # Access token rejected/expired -> re-login and retry once.
        data = login()
        s.headers["Authorization"] = f"Bearer {data['access_token']}"
        if data.get("captcha_token"):
            s.headers["X-Captcha-Token"] = data["captcha_token"]
        resp = _with_retry(
            lambda: s.request(method, url, timeout=120, **kwargs),
            what=f"API retry {method} {path}")

    if _err_code(resp) == 9:
        # Captcha token invalid/expired -> refresh for this action, retry.
        ct = _refresh_captcha(f"{method}:{path}")
        s.headers["X-Captcha-Token"] = ct
        resp = _with_retry(
            lambda: s.request(method, url, timeout=120, **kwargs),
            what=f"API retry {method} {path}")

    if resp.status_code != 200:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def list_files(parent_id="", limit=100, page_token=None):
    params = {
        "parent_id": parent_id,
        "page_token": page_token or "",
        "limit": limit,
        "thumbnail_size": "SIZE_LARGE",
        "with_audit": "true",
        "filters": json.dumps({
            "phase": {"eq": "PHASE_TYPE_COMPLETE"},
            "trashed": {"eq": False},
        }),
    }
    result = _api_request("GET", "/drive/v1/files", params=params)
    files = [{
        "id": f.get("id"),
        "name": f.get("name"),
        "kind": f.get("kind"),
        "size": int(f.get("size", 0)),
        "modified_time": f.get("modified_time"),
        "md5_checksum": f.get("md5_checksum"),
    } for f in result.get("files", [])]
    return {"files": files, "next_page_token": result.get("next_page_token")}


def _find_child(parent_id, name, kind=None):
    page_token = None
    while True:
        result = list_files(parent_id=parent_id, page_token=page_token)
        for f in result["files"]:
            if f["name"] == name and (kind is None or f["kind"] == kind):
                return f
        page_token = result.get("next_page_token")
        if not page_token:
            break
    return None


def _get_folder_id(path, create=False):
    """Resolve /a/b/c to a folder id. Optionally create missing folders."""
    if path in ("", "/"):
        return ""
    parts = [p for p in path.strip("/").split("/") if p]
    parent = ""
    for part in parts:
        found = _find_child(parent, part, kind="drive#folder")
        if found:
            parent = found["id"]
            continue
        if not create:
            raise RuntimeError(f"Folder not found: '{part}' in path '{path}'")
        parent = _api_request("POST", "/drive/v1/files", json={
            "kind": "drive#folder",
            "name": part,
            "parent_id": parent,
        })["file"]["id"]
    return parent


# ---------------------------------------------------------------------------
# File ops
# ---------------------------------------------------------------------------

def _oss_sign(params, verb, resource, content_type="", date=None):
    """Sign an OSS request (V1 signature). Returns headers dict."""
    import base64
    import hmac
    from email.utils import formatdate

    date = date or formatdate(usegmt=True)
    token = params.get("security_token", "")
    string_to_sign = (f"{verb}\n\n{content_type}\n{date}\n"
                      f"x-oss-security-token:{token}\n{resource}")
    sig = base64.b64encode(
        hmac.new(params["access_key_secret"].encode(),
                 string_to_sign.encode(), hashlib.sha1).digest()).decode()
    return {
        "Authorization": f"OSS {params['access_key_id']}:{sig}",
        "Date": date,
        "X-OSS-Security-Token": token,
    }


def _oss_put(params, local_path, file_size):
    """Single-PUT upload to the Aliyun OSS bucket PikPak hands out."""
    bucket, key = params["bucket"], params["key"]
    endpoint = params["endpoint"].removeprefix("https://")
    url = f"https://{endpoint}/{key}"  # cname mode
    content_type = "application/octet-stream"
    headers = _oss_sign(params, "PUT", f"/{bucket}/{key}", content_type)
    headers["Content-Type"] = content_type

    with open(local_path, "rb") as f:
        up = requests.put(
            url, data=f, headers=headers, timeout=(30, 3600))
    if up.status_code not in (200, 201, 204):
        raise RuntimeError(f"OSS upload failed: {up.status_code} {up.text[:300]}")


UPLOAD_STATE_FILE = os.path.expanduser("~/.pikpak_upload_state.json")
PART_RETRIES = 10


def _load_upload_state():
    try:
        with open(UPLOAD_STATE_FILE) as fh:
            return json.load(fh)
    except (IOError, json.JSONDecodeError):
        return {}


def _save_upload_state(state):
    with open(UPLOAD_STATE_FILE, "w") as fh:
        json.dump(state, fh)


def _clear_upload_state():
    if os.path.exists(UPLOAD_STATE_FILE):
        os.remove(UPLOAD_STATE_FILE)


def _state_matches(state, local_path, file_size, mtime, name, parent_id,
                    file_hash):
    return (state.get("local_path") == str(local_path)
            and state.get("size") == file_size
            and state.get("mtime") == mtime
            and state.get("name") == name
            and state.get("parent_id") == parent_id
            and state.get("hash") == file_hash
            and state.get("upload_id")
            and state.get("expiration", "") > time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime()))


def _oss_multipart(params, local_path, file_size, state=None):
    """OSS multipart upload (no SDK), resumable via `state`.
    Max 10000 parts; 32 MiB chunks cover ~312 GiB."""
    import re as _re

    bucket, key = params["bucket"], params["key"]
    endpoint = params["endpoint"].removeprefix("https://")
    url = f"https://{endpoint}/{key}"
    chunk = 32 * 1024 * 1024
    total_parts = (file_size + chunk - 1) // chunk
    state = state or {}
    done = state.get("etags", {})  # {"part": etag}

    # 1. Initiate (or resume an existing upload).
    upload_id = state.get("upload_id")
    if not upload_id:
        headers = _oss_sign(params, "POST", f"/{bucket}/{key}?uploads")
        r = requests.post(
            f"{url}?uploads", headers=headers, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(
                f"OSS init failed: {r.status_code} {r.text[:300]}")
        m = _re.search(r"<UploadId>(.+?)</UploadId>", r.text)
        if not m:
            raise RuntimeError(f"No UploadId in response: {r.text[:300]}")
        upload_id = m.group(1)
        state["upload_id"] = upload_id
        state["params"] = params
        state["chunk"] = chunk
        state["etags"] = done
        state["expiration"] = params.get("expiration", "")
        _save_upload_state(state)
    if done:
        print(f"  resuming: {len(done)}/{total_parts} parts done",
              file=sys.stderr)

    # 2. Upload parts (skipping completed ones).
    t0 = time.time()
    base_bytes = sum(1 for _ in done) * chunk
    with open(local_path, "rb") as f:
        for i in range(1, total_parts + 1):
            data = f.read(chunk)
            if str(i) in done:
                continue
            resource = (f"/{bucket}/{key}"
                        f"?partNumber={i}&uploadId={upload_id}")
            last_err = None
            for attempt in range(PART_RETRIES):
                try:
                    headers = _oss_sign(
                        params, "PUT", resource,
                        content_type="application/octet-stream")
                    headers["Content-Type"] = "application/octet-stream"
                    r = requests.put(
                        url,
                        params={"partNumber": i, "uploadId": upload_id},
                        data=data, headers=headers, timeout=(30, 3600))
                    if r.status_code != 200:
                        raise RuntimeError(
                            f"HTTP {r.status_code}: {r.text[:200]}")
                    done[str(i)] = r.headers["ETag"]
                    state["etags"] = done
                    _save_upload_state(state)
                    last_err = None
                    break
                except (requests.RequestException, RuntimeError) as e:
                    last_err = e
                    wait = min(5 * (attempt + 1), 30)
                    print(f"\n  part {i} attempt {attempt + 1}/"
                          f"{PART_RETRIES} failed ({str(e)[:100]}); "
                          f"retry in {wait}s", file=sys.stderr)
                    time.sleep(wait)
            if last_err:
                raise RuntimeError(
                    f"OSS part {i} failed after {PART_RETRIES} retries "
                    f"(state saved; re-run to resume): {last_err}")
            done_bytes = min(base_bytes + i * chunk, file_size)
            mb_s = ((done_bytes - base_bytes) / 1e6 /
                    max(time.time() - t0, 0.1))
            print(f"\r  part {i}/{total_parts} "
                  f"({done_bytes / 1e6:.0f}/{file_size / 1e6:.0f} MB, "
                  f"{mb_s:.2f} MB/s)" + " " * 20,
                  end="", file=sys.stderr)
    print(file=sys.stderr)

    # 3. Complete.
    etags = [done[str(i)] for i in range(1, total_parts + 1)]
    xml = ("<CompleteMultipartUpload>" +
           "".join(f"<Part><PartNumber>{i + 1}</PartNumber>"
                   f"<ETag>{e}</ETag></Part>"
                   for i, e in enumerate(etags)) +
           "</CompleteMultipartUpload>")
    resource = f"/{bucket}/{key}?uploadId={upload_id}"
    headers = _oss_sign(params, "POST", resource,
                        content_type="application/xml")
    headers["Content-Type"] = "application/xml"
    r = requests.post(
        url, params={"uploadId": upload_id},
        data=xml.encode(), headers=headers, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(
            f"OSS complete failed: {r.status_code} {r.text[:300]}")
    _clear_upload_state()


def upload_file(local_path, remote_path):
    local_path = Path(local_path)
    if not local_path.is_file():
        raise RuntimeError(f"File not found: {local_path}")

    file_size = local_path.stat().st_size

    # Remote path is <dir>/<name>: basename becomes the remote file name.
    remote_path = remote_path.rstrip("/")
    file_name = Path(remote_path).name or local_path.name
    remote_parent = str(Path(remote_path).parent)
    parent_id = _get_folder_id(remote_parent, create=True)

    sha1 = hashlib.sha1()
    with open(local_path, "rb") as f:
        sha1.update(f.read(1024 * 1024))
    file_hash = sha1.hexdigest()
    mtime = local_path.stat().st_mtime

    # Resume an interrupted multipart upload if state matches.
    state = _load_upload_state()
    if _state_matches(state, local_path, file_size, mtime, file_name,
                      parent_id, file_hash):
        print("Resuming interrupted multipart upload ...", file=sys.stderr)
        _oss_multipart(state["params"], local_path, file_size, state)
        print(f"Upload complete: {file_name} -> {remote_path}",
              file=sys.stderr)
        return state.get("file_id")

    upload_req = {
        "kind": "drive#file",
        "name": file_name,
        "size": file_size,
        "hash": file_hash,
        "upload_type": "UPLOAD_TYPE_RESUMABLE",
        "parent_id": parent_id,
    }
    resp = _api_request("POST", "/drive/v1/files",
                        params={"upload_type": "UPLOAD_TYPE_RESUMABLE"},
                        json=upload_req)

    fid = resp.get("id") or resp.get("file", {}).get("id")
    resumable = resp.get("resumable") or {}
    oss_params = resumable.get("params") or {}
    upload_url = resp.get("upload_url") or resumable.get("upload_url")

    multipart_mb = int(_env_conf("PIKPAK_MULTIPART_MB", "100"))
    if oss_params.get("access_key_id"):
        print(f"Uploading {file_name} ({file_size} bytes) ...",
              file=sys.stderr)
        if file_size > multipart_mb * 1024 * 1024:
            new_state = {
                "local_path": str(local_path),
                "size": file_size,
                "mtime": mtime,
                "name": file_name,
                "parent_id": parent_id,
                "hash": file_hash,
                "file_id": fid,
            }
            _oss_multipart(oss_params, local_path, file_size, new_state)
        else:
            _oss_put(oss_params, local_path, file_size)
        _clear_upload_state()
    elif upload_url:
        print(f"Uploading {file_name} ({file_size} bytes) ...",
              file=sys.stderr)
        with open(local_path, "rb") as f:
            up = _get_session().put(
                upload_url, data=f,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(file_size)},
                timeout=3600,
            )
        if up.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Upload failed: {up.status_code} {up.text[:300]}")
    elif fid:
        # Instant-upload: sha1 matched an existing file.
        print(f"Upload complete (instant): {file_name} -> {remote_path}",
              file=sys.stderr)
        return fid
    else:
        raise RuntimeError(f"No upload target in response: {resp}")

    print(f"Upload complete: {file_name} -> {remote_path}", file=sys.stderr)
    return fid


def _resolve_file(remote_path):
    parent_id = _get_folder_id(str(Path(remote_path).parent))
    f = _find_child(parent_id, Path(remote_path).name, kind="drive#file")
    if not f:
        raise RuntimeError(f"File not found: {remote_path}")
    return f


def get_download_url(remote_path):
    """Return a long-lived (CACHE-mode media) direct link for a file.

    CACHE links live ~24h (vs ~5-10 min for FETCH), so they survive large
    downloads and can be handed to aria2c. Falls back to FETCH.
    """
    f = _resolve_file(remote_path)
    for usage in ("CACHE", "FETCH"):
        info = _api_request("GET", f"/drive/v1/files/{f['id']}",
                            params={"_magic": "2021", "usage": usage,
                                    "thumbnail_size": "SIZE_LARGE"})
        medias = info.get("medias") or []
        url = ""
        if medias:
            url = medias[0].get("link", {}).get("url", "")
        url = url or info.get("web_content_link") or info.get("download_url")
        if url:
            return url
    raise RuntimeError("No download URL in PikPak response")


def _content_session():
    """Session for content (dl-*) links. The dl CDN is reachable from CN
    egress, so download direct by default (saves proxy bandwidth and is
    much faster); PIKPAK_DL_PROXY=1 forces the proxy."""
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    if _DOWNLOAD_NO_PROXY:
        # Forced direct: ignore PIKPAK_DL_PROXY and *_PROXY env vars.
        s.trust_env = False
        s.proxies.update(_direct_proxies())
    elif os.environ.get("PIKPAK_DL_PROXY") == "1":
        proxies = _get_proxies()
        if proxies:
            s.proxies.update(proxies)
    else:
        s.trust_env = False  # ignore HTTP(S)_PROXY env vars
    return s


def download_file(remote_path, local_path=None, threads=None):
    f = _resolve_file(remote_path)
    url = get_download_url(remote_path)

    local_path = Path(local_path or Path(remote_path).name)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    file_size = f["size"]
    threads = threads or int(os.environ.get("PIKPAK_THREADS", "8"))

    sessions = [_content_session()]
    if not _DOWNLOAD_NO_PROXY:
        proxies = _get_proxies()
        if proxies:  # fallback: retry through proxy if direct fails
            sessions.append(_get_session())

    last_err = None
    for i, sess in enumerate(sessions):
        try:
            _download_stream(sess, url, local_path, file_size, f["name"],
                             threads)
            print(f"\nDownload complete: {local_path}", file=sys.stderr)
            return str(local_path)
        except Exception as e:
            last_err = e
            if i + 1 < len(sessions):
                print(f"\nDirect download failed ({e}); retrying via proxy",
                      file=sys.stderr)
    raise RuntimeError(f"Download failed: {last_err}")


def _download_stream(sess, url, local_path, file_size, name, threads):
    """Download url to local_path, parallel Range chunks if supported."""
    import threading

    # Probe Range support.
    probe = sess.get(url, headers={"Range": "bytes=0-0"},
                     timeout=(15, 120), stream=True)
    range_ok = probe.status_code == 206
    probe.close()

    if not range_ok or file_size <= 16 * 1024 * 1024 or threads <= 1:
        print(f"Downloading {name} ({file_size} bytes) -> {local_path}",
              file=sys.stderr)
        resp = sess.get(url, timeout=(10, 3600), stream=True)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        downloaded = 0
        with open(local_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                downloaded += len(chunk)
                if file_size:
                    print(f"\r  {downloaded / 1e6:.1f}/{file_size / 1e6:.1f} MB "
                          f"({downloaded * 100 // file_size}%)",
                          end="", file=sys.stderr)
        return

    print(f"Downloading {name} ({file_size / 1e6:.1f} MB, {threads} threads) "
          f"-> {local_path}", file=sys.stderr)

    chunk = max(8 * 1024 * 1024, (file_size + threads * 4 - 1) // (threads * 4))
    ranges = [(off, min(off + chunk, file_size) - 1)
              for off in range(0, file_size, chunk)]
    remaining = list(ranges)
    lock = threading.Lock()
    counter = {"done": 0}
    errors = []
    t0 = time.time()

    with open(local_path, "wb") as fh:
        fh.truncate(file_size)

        def progress():
            done = counter["done"]
            mb_s = done / 1e6 / max(time.time() - t0, 0.1)
            print(f"\r  {done / 1e6:.1f}/{file_size / 1e6:.1f} MB "
                  f"({done * 100 // file_size}%) {mb_s:.1f} MB/s",
                  end="", file=sys.stderr)

        def worker():
            while True:
                with lock:
                    if not remaining or errors:
                        return
                    start, end = remaining.pop(0)
                last_err = None
                for attempt in range(3):
                    try:
                        r = sess.get(
                            url,
                            headers={"Range": f"bytes={start}-{end}"},
                            timeout=(15, 600), stream=True)
                        if r.status_code not in (200, 206):
                            raise RuntimeError(f"HTTP {r.status_code}")
                        buf = bytearray()
                        for c in r.iter_content(chunk_size=1 << 20):
                            buf += c
                        with lock:
                            fh.seek(start)
                            fh.write(buf)
                            counter["done"] += len(buf)
                            progress()
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        time.sleep(2 * (attempt + 1))
                if last_err:
                    errors.append(last_err)
                    return

        ths = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()

    if errors:
        raise RuntimeError(f"{len(errors)} chunk(s) failed: {errors[0]}")


def delete_file(remote_path):
    parent_id = _get_folder_id(str(Path(remote_path).parent))
    f = _find_child(parent_id, Path(remote_path).name)
    if not f:
        raise RuntimeError(f"File not found: {remote_path}")
    _api_request("DELETE", f"/drive/v1/files/{f['id']}")
    print(f"Deleted: {remote_path}", file=sys.stderr)


def create_folder(path):
    parent_id = _get_folder_id(str(Path(path).parent), create=True)
    _api_request("POST", "/drive/v1/files", json={
        "kind": "drive#folder",
        "name": Path(path).name,
        "parent_id": parent_id,
    })
    print(f"Created folder: {path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_size(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def cmd_list(args):
    path = args.path or "/"
    parent_id = "" if path == "/" else _get_folder_id(path)
    page_token = None
    total = 0
    print(f"{'Type':<6} {'Size':>10} {'Modified':<20} Name")
    print("-" * 70)
    while True:
        result = list_files(parent_id=parent_id, page_token=page_token)
        for f in result["files"]:
            kind = "DIR" if f["kind"] == "drive#folder" else "FILE"
            size = "-" if kind == "DIR" else _format_size(f["size"])
            mtime = (f.get("modified_time") or "")[:19].replace("T", " ")
            print(f"{kind:<6} {size:>10} {mtime:<20} {f['name']}")
            total += 1
        page_token = result.get("next_page_token")
        if not page_token:
            break
    sys.stdout.flush()
    print(f"\n{total} items", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="PikPak CLI (direct API)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("login", help="Login and cache session")
    p.add_argument("--captcha-token", help="Solved slider-captcha token")
    p.add_argument("--refresh-token", help="Seed session from a refresh token")

    sub.add_parser("logout", help="Clear cached session")

    p = sub.add_parser("upload", help="Upload a file")
    p.add_argument("local_file")
    p.add_argument("remote_path")

    p = sub.add_parser("list", help="List a directory")
    p.add_argument("path", nargs="?", default="/")

    p = sub.add_parser("download", help="Download a file")
    p.add_argument("remote_path")
    p.add_argument("local_path", nargs="?")
    p.add_argument("--threads", type=int, help="Parallel download threads "
                   "(default: $PIKPAK_THREADS or 8)")

    p = sub.add_parser("link", help="Print long-lived (~24h) direct link")
    p.add_argument("remote_path")

    p = sub.add_parser("delete", help="Delete a file")
    p.add_argument("remote_path")

    p = sub.add_parser("mkdir", help="Create a folder")
    p.add_argument("path")

    args = parser.parse_args()

    try:
        if args.command == "login":
            if args.refresh_token:
                login(refresh_token=args.refresh_token)
            elif args.captcha_token:
                login(force=True, captcha_token=args.captcha_token)
            else:
                login(force=True)
            print("Login OK. Session saved to ~/.pikpak_token.json",
                  file=sys.stderr)
        elif args.command == "logout":
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            print("Session cleared.", file=sys.stderr)
        elif args.command == "upload":
            upload_file(args.local_file, args.remote_path)
        elif args.command == "list":
            cmd_list(args)
        elif args.command == "download":
            download_file(args.remote_path, args.local_path,
                          threads=args.threads)
        elif args.command == "link":
            print(get_download_url(args.remote_path))
        elif args.command == "delete":
            delete_file(args.remote_path)
        elif args.command == "mkdir":
            create_folder(args.path)
        else:
            parser.print_help()
            return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if "PROHIBITED" in str(e) or "not available in your region" in str(e):
            print("  -> PikPak is geo-blocked from this egress; set a proxy: "
                  "export HTTPS_PROXY=http://127.0.0.1:7897", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
