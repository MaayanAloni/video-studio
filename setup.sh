#!/bin/zsh
# Video Studio — one-time install on a Mac with Apple Silicon.
#
#   ./setup.sh          # everything: tools, python env, ComfyUI engine, ~55 GB of models
#   ./setup.sh --hq     # also fetch the Q6_K 14B experts (+48 GB) used by the Master tier
#
# Safe to re-run: every step skips what is already there. Afterwards: ./start.sh
set -e
cd "$(dirname "$0")"

# Pinned to the exact ComfyUI commit this app was built and tested against (v0.36.0 line).
COMFY_SHA="${COMFY_SHA:-387f98aa2822f684b8597959a52a467d88cc4806}"
GGUF_SHA="${GGUF_SHA:-6ea2651}"
HQ=0; [[ "${1:-}" == "--hq" ]] && HQ=1

say() { print -P "%F{cyan}==>%f $*"; }
die() { print -P "%F{red}!!%f $*" >&2; exit 1; }

# ---------------------------------------------------------------- 0. sanity
[[ "$(uname -s)" == "Darwin" ]] || die "this installer is for macOS. The engine runs on Apple's Metal GPU (MPS)."
[[ "$(uname -m)" == "arm64" ]]  || die "Apple Silicon (M1 or newer) is required; Intel Macs have no usable GPU path."
avail_gb=$(df -g . | awk 'NR==2{print $4}')
need_gb=$(( HQ ? 115 : 65 ))
(( avail_gb >= need_gb )) || die "need about ${need_gb} GB free on this disk, have ${avail_gb} GB."

# ---------------------------------------------------------------- 1. tools
if ! command -v brew >/dev/null; then
  die "Homebrew is missing. Install it from https://brew.sh (one command), then re-run ./setup.sh"
fi
for pkg in ffmpeg python@3.12 git; do
  if ! brew list --versions "$pkg" >/dev/null 2>&1; then say "installing $pkg"; brew install "$pkg"; fi
done
PY312="$(brew --prefix python@3.12)/bin/python3.12"
[[ -x "$PY312" ]] || die "python3.12 not found at $PY312"
command -v ffmpeg >/dev/null || die "ffmpeg not on PATH after install; open a new terminal and re-run"
command -v ssh >/dev/null || die "ssh missing (it ships with macOS; something is very wrong)"

# ---------------------------------------------------------------- 2. python env
if [[ ! -x .venv/bin/python ]]; then
  say "creating python 3.12 virtualenv in .venv"
  "$PY312" -m venv .venv
fi
source .venv/bin/activate
python -m pip install -q --upgrade pip

# ---------------------------------------------------------------- 3. engine (ComfyUI, pinned)
if [[ ! -f engine/main.py ]]; then
  say "cloning ComfyUI at $COMFY_SHA into engine/"
  rm -rf engine
  git init -q engine
  git -C engine remote add origin https://github.com/comfyanonymous/ComfyUI
  git -C engine fetch -q --depth 1 origin "$COMFY_SHA"
  git -C engine checkout -q FETCH_HEAD
fi
if [[ ! -d engine/custom_nodes/ComfyUI-GGUF ]]; then
  say "adding ComfyUI-GGUF (loads the quantised 14B experts)"
  git clone -q https://github.com/city96/ComfyUI-GGUF engine/custom_nodes/ComfyUI-GGUF
  git -C engine/custom_nodes/ComfyUI-GGUF checkout -q "$GGUF_SHA" 2>/dev/null || true
fi
say "installing python packages (torch for Metal, ComfyUI, GGUF, the app) — a few minutes the first time"
pip install -q -r engine/requirements.txt
pip install -q -r engine/custom_nodes/ComfyUI-GGUF/requirements.txt
pip install -q -r requirements.txt

# whole-clip contact-sheet previews while sampling (same patch the rented GPU gets)
mkdir -p engine/custom_nodes/vs_preview
cp engine_patches/vs_preview/__init__.py engine/custom_nodes/vs_preview/__init__.py

python - <<'PY'
import torch
assert torch.backends.mps.is_available(), "PyTorch cannot see the Metal GPU (MPS). Update macOS and re-run."
print(f"   torch {torch.__version__}, Metal GPU available")
PY

# ---------------------------------------------------------------- 4. models (~55 GB, resumable)
say "downloading models into engine/models (skips files already present; re-run if interrupted)"
HF_HUB_ENABLE_HF_TRANSFER=1 python scripts_download.py
if (( HQ )); then
  say "downloading the Q6_K experts for the Master tier (+48 GB)"
  HF_HUB_ENABLE_HF_TRANSFER=1 python scripts_download_hq.py
fi
# tiny approximate decoder used for live previews
python - <<'PY'
import os, shutil
from huggingface_hub import hf_hub_download
dst = "engine/models/vae_approx/taew2_1.safetensors"
if not os.path.exists(dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(hf_hub_download("Kijai/WanVideo_comfy", "taew2_1.safetensors", local_dir="dl_cache"), dst)
print("   previews decoder ready")
PY
rm -rf dl_cache

# ---------------------------------------------------------------- 5. done
mkdir -p data logs
chmod +x start.sh stop.sh logs.sh
print
print -P "%F{green}Video Studio is installed.%f"
print "  ./start.sh      opens http://127.0.0.1:7860"
print "  ./stop.sh       stops the server and the engine"
print "  ./logs.sh       tails the logs"
print "  Renting a GPU is optional — see SETUP.md, part 2."
