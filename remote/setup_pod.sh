#!/usr/bin/env bash
# Video Studio — prepare a rented GPU to act as the render engine.
#
# Runs ON THE POD, fed over ssh stdin:  ssh root@POD 'bash -s' full
#
#   full   ~120 GB  Wan 2.2 unquantised fp16 — best quality, Apache-2.0, needs >=48 GB VRAM
#   fast    ~40 GB  Wan 2.2 Q8 quantised — good, fits a 24 GB card
#   h3      ~43 GB  MiniMax H3 — strongest model, but commercial use of its output
#                   needs a licence from Comfy. See REMOTE.md.
#
# ComfyUI is left bound to 127.0.0.1. Nothing is exposed to the internet; the Mac
# reaches it through an SSH tunnel.
set -uo pipefail

MODE="${1:-full}"
echo "==> Video Studio pod setup (mode: $MODE)"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "!! no GPU visible"

# ---------------------------------------------------------------- locate ComfyUI
# The RunPod ComfyUI image already ships one. Use it rather than cloning a rival
# copy that then fights it for port 8188.
COMFY=""
for c in /workspace/runpod-slim/ComfyUI /workspace/ComfyUI /ComfyUI /root/ComfyUI; do
  [ -f "$c/main.py" ] && COMFY="$c" && break
done
if [ -z "$COMFY" ]; then
  COMFY=/workspace/ComfyUI
  echo "==> no existing ComfyUI found, cloning into $COMFY"
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFY" || exit 1
  pip install -q -r "$COMFY/requirements.txt"
fi
echo "==> using ComfyUI at $COMFY"
PY_BIN="$(command -v python3 || command -v python)"

# ---------------------------------------------------------------- custom nodes
if [ ! -d "$COMFY/custom_nodes/ComfyUI-GGUF" ]; then
  git clone --depth 1 https://github.com/city96/ComfyUI-GGUF "$COMFY/custom_nodes/ComfyUI-GGUF" \
    && pip install -q -r "$COMFY/custom_nodes/ComfyUI-GGUF/requirements.txt"
fi
pip install -q "huggingface_hub>=0.34" hf_transfer 2>/dev/null

# same contact-sheet preview patch we run locally, so previews show the whole clip
mkdir -p "$COMFY/custom_nodes/vs_preview"
cat > "$COMFY/custom_nodes/vs_preview/__init__.py" <<'NODE'
import math
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
COLS, MAX_TILES = 4, 12
try:
    import latent_preview
    from PIL import Image

    def _tile(images):
        if len(images) == 1:
            return images[0]
        cols = min(COLS, len(images)); rows = math.ceil(len(images) / cols)
        w, h = images[0].size
        sheet = Image.new("RGB", (w * cols, h * rows), (12, 14, 20))
        for i, im in enumerate(images):
            sheet.paste(im, ((i % cols) * w, (i // cols) * h))
        return sheet

    def _patch(cls):
        original = cls.decode_latent_to_preview
        def decode_latent_to_preview(self, x0):
            if getattr(x0, "ndim", 0) != 5 or x0.shape[2] < 2:
                return original(self, x0)
            t = x0.shape[2]; n = min(t, MAX_TILES)
            idx = [round(i * (t - 1) / max(1, n - 1)) for i in range(n)]
            try:
                return _tile([original(self, x0[:, :, k:k + 1]) for k in idx])
            except Exception:
                return original(self, x0)
        cls.decode_latent_to_preview = decode_latent_to_preview

    for _n in ("Latent2RGBPreviewer", "TAESDPreviewerImpl"):
        _c = getattr(latent_preview, _n, None)
        if _c is not None and hasattr(_c, "decode_latent_to_preview"):
            _patch(_c)
    print("[video-studio] preview patched", flush=True)
except Exception as _e:
    print(f"[video-studio] preview patch skipped: {_e}", flush=True)
NODE

# ---------------------------------------------------------------- models
echo "==> downloading models into $COMFY/models (skips what is already there)"
HF_HUB_ENABLE_HF_TRANSFER=1 "$PY_BIN" - "$MODE" "$COMFY" <<'PY'
import os, shutil, sys
from huggingface_hub import hf_hub_download

mode, comfy = sys.argv[1], sys.argv[2]
C = "Comfy-Org/Wan_2.2_ComfyUI_Repackaged"
QI, QT = "QuantStack/Wan2.2-I2V-A14B-GGUF", "QuantStack/Wan2.2-T2V-A14B-GGUF"
H3 = "Comfy-Org/MiniMax-H3"

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
h3 = [
    (H3, "split_files/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors", "diffusion_models"),
    (H3, "split_files/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "text_encoders"),
    (H3, "split_files/vae/minimax_h3_video_vae_fp16.safetensors", "vae"),
    (H3, "split_files/vae/minimax_h3_audio_vae_fp32.safetensors", "vae"),
]
items = common + {"full": full, "fast": fast, "h3": h3}.get(mode, full)
dl = os.path.join(comfy, "_dl")
for repo, path, sub in items:
    dst = os.path.join(comfy, "models", sub, os.path.basename(path))
    if os.path.exists(dst):
        print("  have", os.path.basename(path), flush=True); continue
    print("  fetching", os.path.basename(path), flush=True)
    try:
        p = hf_hub_download(repo, path, local_dir=dl)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(p, dst)
    except Exception as e:
        print(f"  !! FAILED {os.path.basename(path)}: {str(e)[:160]}", flush=True)
shutil.rmtree(dl, ignore_errors=True)
print("  models ready", flush=True)
PY

# ---------------------------------------------------------------- (re)start
# Restart so the new models and the preview patch are picked up. Bound to
# loopback: unreachable from the internet, we tunnel in over ssh.
echo "==> restarting ComfyUI on 127.0.0.1:8188"
pkill -f "ComfyUI/main.py" 2>/dev/null
pkill -f "main.py --listen" 2>/dev/null
sleep 3
cd "$COMFY"
nohup "$PY_BIN" main.py --listen 127.0.0.1 --port 8188 \
      --preview-method auto --preview-size 1024 --disable-auto-launch \
      > /workspace/comfy.log 2>&1 &

for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1 && break
  sleep 2
done
echo "================================================================"
if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then
  echo " ComfyUI is UP on 127.0.0.1:8188"
  "$PY_BIN" -c "import json,urllib.request;d=json.load(urllib.request.urlopen('http://127.0.0.1:8188/system_stats'));print('  GPU:',d['devices'][0]['name'],round(d['devices'][0]['vram_total']/1e9,1),'GB')" 2>/dev/null
  ls -1 "$COMFY/models/diffusion_models" 2>/dev/null | sed 's/^/  model: /'
else
  echo " !! ComfyUI did not come up — see /workspace/comfy.log"
  tail -20 /workspace/comfy.log 2>/dev/null
fi
echo "================================================================"
