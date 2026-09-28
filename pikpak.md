# pikpak cli

Self-contained PikPak CLI (`./pikpak.py`, direct PikPak API — no WebDAV/intermediary).

```bash
./pikpak.py login                       # login once; session is persisted
./pikpak.py upload <local> <remote>     # e.g. ./pikpak.py upload out.tar /backup/out.tar
./pikpak.py list [path]                 # list a directory (default: /)
./pikpak.py download <remote> [local]   # download a file (parallel, direct)
./pikpak.py download <remote> --threads 16
./pikpak.py link <remote>               # print ~24h direct link (aria2c-ready)
./pikpak.py delete <remote>             # delete a file or folder
./pikpak.py mkdir <path>                # create a folder
./pikpak.py logout                      # clear cached session
```

Zero-config: creds and proxy are read automatically from `$WS/env/pikpak.web`
(`PIKPAK_USERNAME` / `PIKPAK_PASSWORD` / `PIKPAK_PROXY`, plus the per-platform
client credentials `PIKPAK_{WEB,ANDROID}_CLIENT_ID` / `_CLIENT_SECRET`);
env vars override. `enc1:` inline-encrypted values are decrypted in memory.
Nothing credential-shaped lives in `pikpak.py`: a missing client credential is a
hard error at import time that names the key and this file.

## Session

- `./pikpak.py login` — the session should be persistent, avoid repeat login.
  Tokens are cached in `~/.pikpak_token.json` (access + refresh + captcha token).
- All other commands reuse the cached session; on token expiry they
  auto-refresh and retry. Run `login` explicitly only once, or to force a
  fresh login.
- PikPak **disabled the plain password grant** ("currently not supported"),
  so login goes through the shield-captcha flow (`/v1/shield/captcha/init` +
  `/v1/auth/signin`). Normally init hands back a captcha_token directly; if
  it returns a slider URL instead, solve it in a browser, then:

  ```bash
  ./pikpak.py login --captcha-token <TOKEN>
  ```

  Also available: `./pikpak.py login --refresh-token <TOKEN>` to seed a
  session from an existing refresh token.

## Proxy (split routing)

PikPak API (`user.*` / `api-drive.*.mypikpak.net`) is geo-blocked from CN
egress (`PROHIBITED:CN`), but the download CDN (`dl-*.mypikpak.net`) is
reachable directly. So the CLI splits traffic:

- **API calls** go through `PIKPAK_PROXY` (in `env/pikpak.web`, currently
  `http://127.0.0.1:7897`; mihomo pins PikPak domains to the SG node).
  `SOCKS_PROXY`/`HTTPS_PROXY` env vars work
  too, but `PIKPAK_PROXY` takes precedence.
- **Content downloads** go **direct** (bypasses proxy env vars), saving
  proxy bandwidth; measured ~8x faster than tunneling from this host.
  Direct falls back to proxy automatically on failure; set
  `PIKPAK_DL_PROXY=1` to force content through the proxy.

## Download speed

- `link`/`download` use **CACHE mode** (`usage=CACHE` media link) instead of
  FETCH: links stay valid **~24h** (vs ~5-10 min), so multi-GB downloads
  can't die mid-transfer. The `expire=` in the URL confirms this.
- `download` fetches with parallel HTTP Range chunks
  (`--threads N`, default 8, `$PIKPAK_THREADS`).
- Reality check from this host: the CDN caps at ~0.8 MB/s total from this
  egress, so threads don't help here — but the ~24h `link` output can be
  fed to `aria2c -x 16 -s 16 <url>` from a faster machine.
- Fixed device_id (derived from hostname) and the web platform client keep
  auth stable; no risk-control invalidation observed.

## Upload

- `upload <local> <remote>`: the basename of `<remote>` becomes the remote
  file name; missing parent folders are created automatically.
- Uses PikPak's resumable upload: sha1 dedup ("instant" when the content
  already exists), then a signed PUT to the Aliyun OSS bucket PikPak
  returns. Files above `$PIKPAK_MULTIPART_MB` (default 100) use hand-rolled
  OSS **multipart** upload (32 MiB parts, 3 retries/part, ~312 GiB max), so
  the old 5 GB single-PUT limit is gone.

## Platform

`PIKPAK_PLATFORM=android` (env or `env/pikpak.web`) switches to Android
client emulation (client_id, custom UA with devicesign, android
captcha-sign algorithms — ported from the open-source PikPak driver). Default is `web`. Tokens are
per-client: switching platform requires a fresh `login`. Verified: login +
list work on both platforms.
- Drive API calls carry `X-Device-ID` + `X-Captcha-Token`; the captcha token
  is re-signed on demand (`captcha_sign` over the web-platform algorithm
  chain, same as the open-source PikPak driver).

## Open items

See `./todo` — all former "not adopted" items are now implemented.

## Notes

- `make dav-sync.start` uses this CLI directly (`DAV_BACKEND=pikpak` default in
  `dsync/dav-sync.py`); no WebDAV mount is on the sync path.
- If PikPak ever demands a human slider for *post-login* actions too, the
  CLI prints the verification URL to solve in a browser.
