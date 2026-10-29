#!/usr/bin/env bash
# 把 Sonic（Apache-2.0，剪映里 "sonic" 后端的同款库）编译成 wasm32。
#
#   tools/sonic/build_sonic_wasm.sh
#
# 产物：tools/sonic/sonic.wasm（约 15 KB，无任何 import 依赖）
# 需要 clang + wasm-ld（clang 自带 wasm32 目标即可，不需要 emscripten）。
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

for bin in clang wasm-ld; do
  command -v "$bin" >/dev/null || { echo "missing required tool: $bin" >&2; exit 1; }
done

clang \
  --target=wasm32 -O2 -nostdlib -I"$HERE/inc" \
  -DSONIC_NO_MALLOC -DSONIC_MAX_MEMORY=2097152 \
  -Wl,--no-entry \
  -Wl,--export-all \
  -Wl,--export=malloc \
  -Wl,--export=free \
  -Wl,--allow-undefined \
  -Wl,--initial-memory=4194304 \
  -o "$HERE/sonic.wasm" \
  "$HERE/sonic.c" "$HERE/shim.c"

echo "==> built $HERE/sonic.wasm ($(du -h "$HERE/sonic.wasm" | cut -f1))"
