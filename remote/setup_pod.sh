#!/usr/bin/env bash
# Video Studio — set up a rented GPU to act as the render engine.
#
# Run this ON THE POD (paste into its web terminal or over SSH):
#   curl -sL <this file> -o setup_pod.sh && bash setup_pod.sh full
#
#   full   ~120 GB  Wan 2.2 unquantised fp16 — best quality, Apache-2.0, needs >=48 GB VRAM
#   fast    ~40 GB  Wan 2.2 Q8 quantised — good, fits a 24 GB card
#   h3      ~43 GB  MiniMax H3 (pruned int8 + nvfp4) — strongest model, but commercial
#                   use of its output needs a licence from Comfy. Read REMOTE.md first.
#
# By default ComfyUI binds to 127.0.0.1 only, so nothing is exposed to the
# internet; you reach it through an SSH tunnel. Pass a token as $2 to instead
# expose it behind a token check (see auth_proxy.py).
set -euo pipefail

MODE="${1:-full}"
TOKEN="${2:-}"
ROOT="${WORKSPACE:-/workspace}"          # RunPod network volumes mount at /workspace
COMFY="$ROOT/ComfyUI"
export HF_HUB_ENABLE_HF_TRANSFER=1

echo "==> Video Studio pod setup  (mode: $MODE, root: $ROOT)"
mkdir -p "$ROOT"
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || echo "!! no nvidia-smi — is this a GPU pod?"

# ---------------------------------------------------------------- ComfyUI
if [ ! -d "$COMFY" ]; then
  echo "==> cloning ComfyUI"
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFY"
fi
if [ ! -d "$COMFY/custom_nodes/ComfyUI-GGUF" ]; then
  git clone --depth 1 https://github.com/city96/ComfyUI-GGUF "$COMFY/custom_nodes/ComfyUI-GGUF"
fi
echo "==> installing python deps (a few minutes)"
pip install -q --upgrade pip
pip install -q -r "$COMFY/requirements.txt"
pip install -q -r "$COMFY/custom_nodes/ComfyUI-GGUF/requirements.txt"
pip install -q "huggingface_hub>=0.34" hf_transfer aiohttp

# ---------------------------------------------------------------- models
echo "==> downloading models (skips anything already present)"
python - "$MODE" "$COMFY" <<'PY'
import os, shutil, sys
from huggingface_hub import hf_hub_download

mode, comfy = sys.argv[1], sys.argv[2]
C = "Comfy-Org/Wan_2.2_ComfyUI_Repackaged"
QI, QT = "QuantStack/Wan2.2-I2V-A14B-GGUF", "QuantStack/Wan2.2-T2V-A14B-GGUF"

common = [
    (C, "split_files/vae/wan_2.1_vae.safetensors", "vae"),
    (C, "split_files/vae/wan2.2_vae.safetensors", "vae"),
    (C, "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors", "diffusion_models"),
    (C, "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors", "loras"),
    (C, "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors", "loras"),
    (C, "split_files/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors", "loras"),
    (C, "split_files/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors", "loras"),
    ("Kijai/WanVideo_comfy", "taew2_1.safetensors", "vae_approx"),
]
full = [
    (C, "split_files/text_encoders/umt5_xxl_fp16.safetensors", "text_encoders"),
    (C, "split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp16.safetensors", "diffusion_models"),
    (C, "split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp16.safetensors", "diffusion_models"),
    (C, "split_files/diffusion_models/wan2.2_t2v_high_noise_14B_fp16.safetensors", "diffusion_models"),
    (C, "split_files/diffusion_models/wan2.2_t2v_low_noise_14B_fp16.safetensors", "diffusion_models"),
]
fast = [
    ("city96/umt5-xxl-encoder-gguf", "umt5-xxl-encoder-Q8_0.gguf", "text_encoders"),
    (QI, "HighNoise/Wan2.2-I2V-A14B-HighNoise-Q8_0.gguf", "diffusion_models"),
    (QI, "LowNoise/Wan2.2-I2V-A14B-LowNoise-Q8_0.gguf", "diffusion_models"),
    (QT, "HighNoise/Wan2.2-T2V-A14B-HighNoise-Q8_0.gguf", "diffusion_models"),
    (QT, "LowNoise/Wan2.2-T2V-A14B-LowNoise-Q8_0.gguf", "diffusion_models"),
]
H3 = "Comfy-Org/MiniMax-H3"
h3 = [
    (H3, "split_files/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors", "diffusion_models"),
    (H3, "split_files/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "text_encoders"),
    (H3, "split_files/vae/minimax_h3_video_vae_fp16.safetensors", "vae"),
    (H3, "split_files/vae/minimax_h3_audio_vae_fp32.safetensors", "vae"),
    (H3, "split_files/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors", "loras"),
]
items = common + {"full": full, "fast": fast, "h3": h3}.get(mode, full)
for repo, path, sub in items:
    dst = os.path.join(comfy, "models", sub, os.path.basename(path))
    if os.path.exists(dst):
        print("  have", os.path.basename(path), flush=True); continue
    print("  fetching", os.path.basename(path), flush=True)
    p = hf_hub_download(repo, path, local_dir=os.path.join(comfy, "_dl"))
    os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.move(p, dst)
shutil.rmtree(os.path.join(comfy, "_dl"), ignore_errors=True)
print("  models ready")
PY

# ---------------------------------------------------------------- launch
cd "$COMFY"
pkill -f "main.py --port 818" 2>/dev/null || true
if [ -n "$TOKEN" ]; then
  echo "==> starting ComfyUI on 127.0.0.1:8189 behind token proxy on 0.0.0.0:8188"
  nohup python main.py --listen 127.0.0.1 --port 8189 --preview-method auto --preview-size 512 \
      --disable-auto-launch > "$ROOT/comfy.log" 2>&1 &
  nohup python "$ROOT/auth_proxy.py" --token "$TOKEN" --listen 0.0.0.0:8188 --upstream http://127.0.0.1:8189 \
      > "$ROOT/proxy.log" 2>&1 &
else
  echo "==> starting ComfyUI on 127.0.0.1:8188  (reach it with an SSH tunnel)"
  nohup python main.py --listen 127.0.0.1 --port 8188 --preview-method auto --preview-size 512 \
      --disable-auto-launch > "$ROOT/comfy.log" 2>&1 &
fi

for i in $(seq 1 90); do
  curl -sf http://127.0.0.1:${TOKEN:+8189}${TOKEN:-8188}/system_stats >/dev/null 2>&1 && break || sleep 2
done
echo
echo "================================================================"
echo " ComfyUI is up.  Log: $ROOT/comfy.log"
if [ -n "$TOKEN" ]; then
  echo " Point Video Studio at:  https://<POD_ID>-8188.proxy.runpod.net"
  echo " Token:                  $TOKEN"
else
  echo " On your Mac, open a tunnel:"
  echo "   ssh -N -L 8189:127.0.0.1:8188 root@<POD_IP> -p <POD_SSH_PORT> -i ~/.ssh/id_ed25519"
  echo " Then point Video Studio at:  http://127.0.0.1:8189"
fi
echo "================================================================"
