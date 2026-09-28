# mac-rsync-patch — brew rsync 3.5.0 补丁（macOS）

两期补丁：
- 一期：getgrouplist 修复。
- 二期：--chown/--groupmap 只标常规文件、目录不做组变更。

## 根因（一句话）

macOS 内核凭据只缓存 `kern.ngroups=16` 个补充组（编译期只读常量），而本机用户
在 Directory Services 有 18 个组——`getgroups(2)` 拿到的进程组表被截断，恰好
丢掉 `replica(502)`。rsync 接收端非 root 时 `uidlist.c` 的 `is_in_group` 用
`getgroups` 预检目标组，判否即 `FLAG_SKIP_GROUP` **静默跳过 chgrp**——于是
`--chown=:replica` / `-g` 全部失效（agents-sync 属组闸门的打标路径被打穿）。
而 `chown(2)` 的内核鉴权走 OpenDirectory 全量组列表，所以 `chgrp`/`os.chown`
一直可用。

## 补丁内容（一期）

`rsync-getgrouplist.patch`（正统 unified diff，p1，只改 `uidlist.c`
`is_in_group` 一处）：

- `#if defined(__APPLE__) && defined(HAVE_GETGROUPLIST)` 分支改用
  `getgrouplist(3)` 取 Directory Services **全量**组成员（与内核 `chown(2)`
  鉴权同一数据源）；探测数组不足时倍增重试（macOS 的 getgrouplist 失败时
  **不**回填所需数量，与 BSD 语义不同，上游 `getallgroups` 的扩容逻辑在
  macOS 上其实失效，本补丁未照抄）。
- 非 Apple 构建（Linux 等）**逐字不变**：仍走 `getgroups`（Linux
  `NGROUPS_MAX=65536`，无截断问题）。
- 不改任何其它语义：非成员组仍照旧静默跳过（已实测）。

补丁同时内联在 `rsync.rb` 的 `__END__` 数据段（`patch:DATA`），安装零外网
依赖；`rsync-getgrouplist.patch` 是同一内容的独立工件，供审阅/rebase。

## 补丁内容（二期）

`rsync-chown-dirs-skip.patch`（正统 unified diff，p1，基于**一期补丁已应用**
的树；两个 hunk，覆盖接收端组映射的两条路径）：

- `flist.c` `recv_file_entry`（inc_recurse 默认路径）与 `uidlist.c`
  `recv_id_list`（老协议全量路径）：在组映射落 `file->flags` 处，若
  `groupmap` 非空（即用户用了 --chown 组部分/--groupmap）且条目非正规文件，
  置 `FLAG_SKIP_GROUP`。
- 效果复用上游既有抑制机制：`rsync.c` `set_file_attrs` 的 change_gid 与
  `generator.c` 的组差异比较都检查该标志——目录/符号链接等**既不 chgrp、也
  不会被生成器每轮报「组有变化」**（无周期拉锯噪音）。
- 动机：一期修复让 --chown 真正生效后，macOS 目录组继承副作用暴露——目录被
  标 replica 后，其内**新建**文件继承该组 → 被闸门误判为他机副本不回传。
  agents-sync 闸门语义中目录本就不承载状态位（白/黑名单只作用常规文件），故
  「--chown 不作用目录」不破坏任何语义。
- 行为矩阵：--chown 常规文件照常打标；目录/符号链接/设备保持接收进程创建时
  的默认组；裸 `-g`（无 --chown/--groupmap）语义逐字不变（groupmap 为 NULL
  不触发）；不影响一期 getgrouplist 修复；非 Apple 构建同样生效但仅在使用
  --chown/--groupmap 时才有行为差异。
- 独立工件与 `rsync.rb` `__END__` 数据段（追加在一期 hunk 之后，同一 `patch:DATA`
  流顺序应用）为同一内容。

## 安装

```
./install.sh
```

脚本动作（幂等可重跑）：

1. 物化本地 tap 到 `~/homebrew-taps/mac-rsync/`（Formula/rsync.rb，自动
   `git init`——brew 6 要求 tap 是 git 仓库；该目录在 ~/m 之外，不入 ~/m 仓库）。
2. `brew tap local/mac-rsync`（已 tap 则跳过）。
3. 卸载官方 rsync bottle（`brew uninstall --ignore-dependencies rsync`）。
4. `brew install --build-from-source alice/mac-rsync/rsync`（源码编译进
   `/opt/homebrew/Cellar/rsync/3.5.0/`，与官方包同一 Cellar 路径，
   `/opt/homebrew/bin/rsync` 链接关系不变）。
5. `brew pin rsync`（防止未来 `brew upgrade` 用官方 bottle 覆盖；升级见下）。
6. 冒烟验证 `--chown=:replica`（文件 gid=502；目录不标，见下）。

安装后核对：`brew info rsync` 应显示 `From: <tap 路径>`；
`rsync --debug=OWN3` 传输时应打印 `process has 18 gids: ... 502 ...`；
二期行为：传输含目录的树后 `ls -ln` 目标——常规文件 gid=502、目录/符号链接
保持接收进程默认组（如 ~/ 下新建为 20/staff）；对同一目标重复 `rsync -ai`
应零输出（目录无组差异条目）。

## 升级维护（新版 rsync 发布时）

1. 改 `rsync.rb`：`url`/`mirror`/`sha256` 指向新版源码包。
2. 在新版源码上按序试套两期补丁：
   `patch -p1 --dry-run < rsync-getgrouplist.patch && patch -p1 --dry-run < rsync-chown-dirs-skip.patch`
   - 干净套用 → 直接用（必要时重新生成 `patch:DATA` 段，内容以本目录两个
     `.patch` 文件为唯一事实源且保持一期在前、二期在后，`install.sh` 会自动
     同步进 tap）。
   - 套不上 → 一期看上游 `uidlist.c` `is_in_group` 是否已改用
     `getgrouplist`（本补丁的意图上游可能已自行修复）；已修 → 删掉补丁块；
     未修 → 手工 rebase。二期看上游接收端组映射点（`flist.c`
     `recv_file_entry` 的 preserve_gid 块、`uidlist.c` `recv_id_list` 循环）
     是否已变化；rebase 后更新对应 `.patch` 与 `__END__` 数据段。
3. 重跑 `./install.sh`，再跑 README 里的验证命令。

## 回退官方版

```
brew unpin rsync
brew untap local/mac-rsync
brew uninstall rsync
brew install rsync        # 官方 bottle
```

## 验证命令（任意机器复现）

```
mkdir -p /tmp/rs/src /tmp/rs/dst && echo hi > /tmp/rs/src/f.txt && chgrp replica /tmp/rs/src/f.txt
rsync -a --chown=:replica /tmp/rs/src/ /tmp/rs/dst/ --debug=OWN3
stat -f 'gid=%g group=%Sg' /tmp/rs/dst/f.txt      # 期望 gid=502 group=replica
```

一期补丁前（官方包）：`process has 16 gids`（无 502），落盘 `gid=0`；
一期补丁后：`process has 18 gids`（含 502），落盘 `gid=502`。

二期验证（在 $HOME 下建夹具，避开 /tmp 新建目录落 wheel 组的系统怪癖）：

```
mkdir -p ~/tmp/rs2/src/sub ~/tmp/rs2/dst && echo hi > ~/tmp/rs2/src/f.txt && echo x > ~/tmp/rs2/src/sub/g.txt
rsync -a --chown=:replica ~/tmp/rs2/src/ ~/tmp/rs2/dst/
ls -lnR ~/tmp/rs2/dst    # 期望：文件列 502 502；目录/符号链接列 502 20（默认组）
rsync -ai --chown=:replica ~/tmp/rs2/src/ ~/tmp/rs2/dst/   # 期望零输出（无组拉锯）
```
