#!/usr/bin/env bash
# install.sh — 安装补丁版 rsync 3.5.0（macOS）。
# 幂等可重跑；细节与回退见同目录 README.md。
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
TAP_USER="${TAP_USER:-local}"   # 本地 tap 命名空间（tap 由本脚本从 $TAP_DIR 物化，不是 GitHub tap）
TAP_REPO="mac-rsync"
TAP_DIR="$HOME/homebrew-taps/$TAP_REPO"

echo "[1/6] 物化 tap -> $TAP_DIR"
mkdir -p "$TAP_DIR/Formula"
cp "$DIR/rsync.rb" "$TAP_DIR/Formula/rsync.rb"
if [ ! -d "$TAP_DIR/.git" ]; then
  git -C "$TAP_DIR" init -q
fi
git -C "$TAP_DIR" add -A
git -C "$TAP_DIR" diff --cached --quiet || \
  git -C "$TAP_DIR" commit -qm "rsync.rb: getgrouplist patch (mac-rsync-patch)"

echo "[2/6] brew tap $TAP_USER/$TAP_REPO"
if brew tap | grep -qx "$TAP_USER/$TAP_REPO"; then
  echo "  already tapped"
else
  brew tap "$TAP_USER/$TAP_REPO" "$TAP_DIR"
fi

echo "[3/6] 卸载已装 rsync（官方或旧补丁版；先 unpin）"
brew unpin rsync 2>/dev/null || true
if brew list --versions rsync >/dev/null 2>&1; then
  brew uninstall --ignore-dependencies rsync
else
  echo "  not installed"
fi

echo "[4/6] 源码编译安装补丁版"
brew install --build-from-source "$TAP_USER/$TAP_REPO/rsync"

echo "[5/6] brew pin rsync（防 brew upgrade 覆盖）"
brew pin rsync || true

echo "[6/6] 冒烟验证 --chown=:replica"
rm -rf /tmp/rs-mac-patch-smoke
mkdir -p /tmp/rs-mac-patch-smoke/src /tmp/rs-mac-patch-smoke/dst
echo smoke > /tmp/rs-mac-patch-smoke/src/f.txt
chgrp replica /tmp/rs-mac-patch-smoke/src/f.txt
rsync -a --chown=:replica /tmp/rs-mac-patch-smoke/src/ /tmp/rs-mac-patch-smoke/dst/
GID="$(stat -f %g /tmp/rs-mac-patch-smoke/dst/f.txt)"
echo "dst gid=$GID (期望 502)"
[ "$GID" = 502 ] || { echo "FAIL: --chown 未生效"; exit 1; }
echo "OK: 补丁版 rsync 已安装并验证"
