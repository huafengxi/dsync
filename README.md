---
name: dsync
anchors:
  commands: "常用命令"
---

# dsync/ — 云端存储同步（PikPak / WebDAV）

本地镜像目录与云端存储之间的双向同步。（`agents/` 运行时树的跨机同步 over ssh 是另一回事，在独立子仓 `agents-sync/`。）

## 是什么

| 脚本 | 作用 |
|------|------|
| `pikpak.py` | PikPak 客户端（上传/下载/列表）；自带 CLI，凭据从 `env/pikpak.web`（enc1）读，缺失即 import 期拒启 |
| `dav-sync.py` | WebDAV 双向同步（默认 pikpak 后端）；服务 `dav-sync` = `make dav-sync.start`（`cwd: dsync`，`./dav-sync.py sync`） |

## 常用命令

```
make dav-sync.start/.stop        # WebDAV 双向同步常驻态（服务定义见工作区 services/）
dsync/pikpak.py login            # PikPak 登录、缓存 session（用法细节见 pikpak.md）
dsync/pikpak.py --help
```

## 指针

- PikPak 用法细节：`pikpak.md`
- WebDAV / PikPak 凭据：`env/webdav.env`、`env/pikpak.web`（enc1，`make env.show env/<file>`）
- `agents/` 跨机同步（ssh-sync watch + gc 删除传播 + replica-tag）：`agents-sync/` 子仓（`@agents-sync#watch-cost`、`@agents-sync#gc`）
