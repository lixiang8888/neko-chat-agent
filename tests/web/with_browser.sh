#!/usr/bin/env bash
# 在裸 WSL / 精简容器里跑 Playwright 的包装器。
#
# 问题：`playwright install chromium` 下下来的 chromium 依赖 libnss3 / libnspr4，
# 而这两个库在很多 WSL 发行版里没有预装，装上它们又要 root。
#
# 办法：`apt-get download` 只要网络不要 root。把 .deb 解到用户目录，
# 用 LD_LIBRARY_PATH 让动态链接器找到它们。**不改动系统**，删掉目录就还原。
#
#     bash tests/web/with_browser.sh python tests/web/screenshots.py
#
# 已经有 root 或系统里本来就有这两个库的话，直接跑 python 就行，不必经过这里。
set -euo pipefail

DEPS="${NEKO_BROWSER_DEPS:-$HOME/.local/lib/neko-browser-deps}"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

if [ ! -e "$DEPS/libnss3.so" ]; then
  echo "浏览器运行库不在 $DEPS，去取一份（apt-get download，不需要 root）..." >&2
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  ( cd "$tmp" && apt-get download libnss3 libnspr4 >&2 )
  mkdir -p "$tmp/x" "$DEPS"
  for f in "$tmp"/*.deb; do dpkg-deb -x "$f" "$tmp/x"; done
  cp -a "$tmp"/x/usr/lib/*/libnss*.so* "$tmp"/x/usr/lib/*/libnspr4.so \
        "$tmp"/x/usr/lib/*/libsmime3.so "$tmp"/x/usr/lib/*/libssl3.so \
        "$tmp"/x/usr/lib/*/libplds4.so "$tmp"/x/usr/lib/*/libplc4.so \
        "$DEPS/" 2>/dev/null || cp -a "$tmp"/x/usr/lib/*/*.so* "$DEPS/"
  echo "好了：$DEPS" >&2
fi

export LD_LIBRARY_PATH="$DEPS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$@"
