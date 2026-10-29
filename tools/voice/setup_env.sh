#!/usr/bin/env bash
# 人声美化运行环境：独立 venv（torch CPU）+ MossFormer2_SE_48K 权重。
#
#   tools/voice/setup_env.sh              # 首次安装 / 校验
#   YUANCUT_VOICE_ROOT=/data/vc tools/voice/setup_env.sh
#
# 为什么单独建 venv：clearvoice 依赖 numpy<2，而 ComfyUI 主环境是 numpy 2.x，
# 装在一起会互相拆台。
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=${YUANCUT_VOICE_ROOT:-$HOME/.local/share/yuancut/voice}
MODEL=MossFormer2_SE_48K
REPO=alibabasglab/$MODEL
BASE_URL=https://www.modelscope.cn/api/v1/models/$REPO/repo

# 优先复用 ComfyUI 自带的 CPython（3.13），退回系统 python3
PYTHON_BIN=${YUANCUT_VOICE_PYTHON:-}
if [ -z "$PYTHON_BIN" ]; then
  for candidate in \
    "$HOME/ComfyUI-Installs/ComfyUI/standalone-env/bin/python3" \
    "$(command -v python3.13 || true)" \
    "$(command -v python3 || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then PYTHON_BIN=$candidate; break; fi
  done
fi
[ -n "$PYTHON_BIN" ] || { echo "找不到 python3" >&2; exit 1; }

echo "==> 环境：$ROOT（python: $PYTHON_BIN）"
mkdir -p "$ROOT/venv" "$ROOT/checkpoints/$MODEL"

if [ ! -x "$ROOT/venv/bin/python" ]; then
  echo "==> 创建 venv"
  "$PYTHON_BIN" -m venv "$ROOT/venv"
fi

VENV_PY=$ROOT/venv/bin/python
"$VENV_PY" -m pip install -q --upgrade pip

echo "==> 安装 torch（CPU 版）"
"$VENV_PY" -c "import torch" 2>/dev/null || \
  "$VENV_PY" -m pip install -q torch torchaudio --index-url https://download.pytorch.org/whl/cpu

echo "==> 安装 clearvoice（语音增强推理）"
"$VENV_PY" -c "import clearvoice" 2>/dev/null || \
  "$VENV_PY" -m pip install -q clearvoice

echo "==> 准备模型权重 $MODEL"
MARKER=$ROOT/checkpoints/$MODEL/last_best_checkpoint
WEIGHTS=$ROOT/checkpoints/$MODEL/last_best_checkpoint.pt
if [ ! -s "$WEIGHTS" ]; then
  echo "    下载 last_best_checkpoint.pt（约 211 MB，ModelScope）"
  curl -fL --retry 3 -o "$WEIGHTS.part" "$BASE_URL?Revision=master&FilePath=last_best_checkpoint.pt"
  mv "$WEIGHTS.part" "$WEIGHTS"
fi
if [ ! -s "$MARKER" ]; then
  curl -fL --retry 3 -o "$MARKER" "$BASE_URL?Revision=master&FilePath=last_best_checkpoint"
fi
# 权重里必须能读到模型名，否则 clearvoice 会去 huggingface 下载（内网不可达）
grep -q "$MODEL" "$MARKER" || printf '%s' "last_best_checkpoint.pt" > "$MARKER"

echo "==> 自检"
VOICE_ROOT=$ROOT "$VENV_PY" "$HERE/beautify_worker.py" --help > /dev/null
"$VENV_PY" - <<PY
from clearvoice import ClearVoice  # noqa: F401  (仅验证依赖完整)
print("    clearvoice ok")
PY
echo "==> 完成：$ROOT"
