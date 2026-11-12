#!/usr/bin/env bash
# Video Studio — self-contained engine on the network volume.
#
# Why: the RunPod image bundles a ComfyUI that is welded to the image's system
# libraries (comfy_kitchen, torch). Touching either side breaks the pairing, and the
# broken half persists on the volume while the fix evaporates with the container.
# So we stop using it. Everything below lives in /workspace/vs: its own Python env,
# its own ComfyUI at a pinned version, its own dependencies. It persists across pods
# and cannot drift from the image because it does not depend on the image.
#
# Runs ON THE POD, fed over ssh stdin:   ssh root@POD 'bash -s' h3
#
#   h3       ~66 GB  MiniMax H3 — strongest downloadable model, audio + reference images (default)
#   full    ~120 GB  Wan 2.2 unquantised fp16 — the Ultra tier, Apache-2.0
#   fast     ~40 GB  Wan 2.2 Q8 quantised — enough for Standard/Pro/Master
#   full+h3 ~163 GB  both
#
# Models are downloaded onto the network volume once and reused by every later pod.
set -uo pipefail
MODE="${1:-h3}"
VS=/workspace/vs
VER="${COMFY_VERSION:-v0.36.0}"
# Sticky models directory: whichever already exists on this volume wins, so an upgrade
# never "loses" 60 GB of weights. New volumes get /workspace/vs/models.
if   [ -d /workspace/vs/models ];                   then MODELS=/workspace/vs/models
elif [ -d /workspace/runpod-slim/ComfyUI/models ]; then MODELS=/workspace/runpod-slim/ComfyUI/models
else MODELS=/workspace/vs/models; fi
mkdir -p "$MODELS"
PY="$VS/venv/bin/python"
log(){ echo "==> $*"; }
log "Video Studio pod setup (models: $MODE, dir: $MODELS)"

mkdir -p "$VS"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "!! no GPU visible"

# ---- python env (once) ------------------------------------------------------------
if [ ! -x "$PY" ]; then
  log "creating python env on the volume"
  python3 -m venv "$VS/venv" || exit 1
  "$PY" -m pip install -q --upgrade pip
  log "installing torch (cu128) into the env — one-time, ~2.5 GB"
  "$PY" -m pip install -q torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128 \
    || { echo "!! torch install failed"; exit 1; }
fi

# ---- ComfyUI at a pinned version (once) -------------------------------------------
if [ ! -f "$VS/ComfyUI/main.py" ]; then
  log "cloning ComfyUI $VER"
  git clone -q --branch "$VER" --depth 1 https://github.com/comfyanonymous/ComfyUI "$VS/ComfyUI" || exit 1
fi
CUR="$(cd "$VS/ComfyUI" && git describe --tags --abbrev=0 2>/dev/null || echo ?)"
log "ComfyUI $CUR — installing its pinned requirements (incl. matching comfy-kitchen)"
"$PY" -m pip install -q -r "$VS/ComfyUI/requirements.txt" || { echo "!! requirements failed"; exit 1; }

"$PY" -m pip install -q "huggingface_hub>=0.34" hf_transfer

if [ ! -d "$VS/ComfyUI/custom_nodes/ComfyUI-GGUF" ]; then
  git clone -q --depth 1 https://github.com/city96/ComfyUI-GGUF "$VS/ComfyUI/custom_nodes/ComfyUI-GGUF" \
    && "$PY" -m pip install -q -r "$VS/ComfyUI/custom_nodes/ComfyUI-GGUF/requirements.txt"
fi

# contact-sheet previews across the whole clip (same patch as the local engine)
mkdir -p "$VS/ComfyUI/custom_nodes/vs_preview"
cat > "$VS/ComfyUI/custom_nodes/vs_preview/__init__.py" <<'NODE'
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

# ---- models onto the volume (once; skips what is already there) --------------------
log "downloading models ($MODE) into $MODELS"
HF_HUB_ENABLE_HF_TRANSFER=1 "$PY" - "$MODE" "$MODELS" <<'PYDL'
import os, shutil, sys
from huggingface_hub import hf_hub_download

mode, models = sys.argv[1], sys.argv[2]
C = "Comfy-Org/Wan_2.2_ComfyUI_Repackaged"
QI, QT = "QuantStack/Wan2.2-I2V-A14B-GGUF", "QuantStack/Wan2.2-T2V-A14B-GGUF"
H3 = "Comfy-Org/MiniMax-H3"
# int8_convrot needs torch built against CUDA 13; fp8_scaled is the documented fallback
try:
    import torch
    H3Q = "int8_convrot" if (torch.version.cuda or "0").split(".")[0] >= "13" else "fp8_scaled"
except Exception:
    H3Q = "fp8_scaled"
print("   H3 quantisation:", H3Q, flush=True)

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
    (H3, "diffusion_models/minimax_h3_fl2va_pruned_%s.safetensors" % H3Q, "diffusion_models"),
    (H3, "diffusion_models/minimax_h3_ref2va_pruned_%s.safetensors" % H3Q, "diffusion_models"),
    (H3, "loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "loras"),
    (H3, "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "text_encoders"),
    (H3, "vae/minimax_h3_video_vae_fp16.safetensors", "vae"),
    (H3, "vae/minimax_h3_audio_vae_fp32.safetensors", "vae"),
    (H3, "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors", "loras"),
    (H3, "loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors", "loras"),
]
items = common + {"full": full, "fast": fast, "h3": h3, "full+h3": full + h3}.get(mode, h3)
dl = os.path.join(models, "_dl")
failed = 0
for repo, path, sub in items:
    dst = os.path.join(models, sub, os.path.basename(path))
    if os.path.exists(dst):
        print("   have", os.path.basename(path), flush=True); continue
    print("   fetching", os.path.basename(path), flush=True)
    try:
        p = hf_hub_download(repo, path, local_dir=dl)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(p, dst)
    except Exception as e:
        failed += 1
        print(f"   !! FAILED {os.path.basename(path)}: {str(e)[:160]}", flush=True)
shutil.rmtree(dl, ignore_errors=True)
print("   models ready" if not failed else f"   !! {failed} model file(s) failed — re-run setup", flush=True)
PYDL

# ---- point ComfyUI at the models directory on the volume -----------------------------
cat > "$VS/ComfyUI/extra_model_paths.yaml" <<YAML
volume_models:
  base_path: $MODELS
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
  vae_approx: vae_approx
  loras: loras
YAML

# ---- verify the pairing BEFORE trusting it --------------------------------------
if ! (cd "$VS/ComfyUI" && "$PY" -c "import comfy.ldm.modules.attention" 2>/tmp/imp.err); then
  echo "!! ComfyUI cannot import in its own env:"; tail -5 /tmp/imp.err; exit 1
fi
"$PY" - <<'PY'
import comfy_kitchen, importlib.metadata as m
print(f"   comfy-kitchen {m.version('comfy_kitchen')}  int8_attn={hasattr(comfy_kitchen,'int8_attention_is_available')}")
from comfy_kitchen.tensor import AsymW4A8Int8Layout
print("   fp8/fp4 support OK")
PY

# ---- launch on loopback (reached through the ssh tunnel) --------------------------
log "starting the engine"
# port 8199: the image's own ComfyUI may hold 8188 and killing it stops the container
pkill -f "vs/ComfyUI/main.py" 2>/dev/null; sleep 2
cd "$VS/ComfyUI"
nohup "$PY" main.py --listen 127.0.0.1 --port 8199 --preview-method auto --preview-size 1024 \
  --disable-auto-launch > /var/log/vs-comfy.log 2>&1 &
for _ in $(seq 1 90); do
  curl -sf http://127.0.0.1:8199/system_stats >/dev/null 2>&1 && break; sleep 2
done
if curl -sf http://127.0.0.1:8199/system_stats >/dev/null 2>&1; then
  echo "================================================================"
  echo " ENGINE UP — ComfyUI $CUR in its own env on the volume"
  "$PY" - <<'PY'
import json, urllib.request
u = lambda p: json.load(urllib.request.urlopen("http://127.0.0.1:8199" + p))
d = u("/system_stats")["devices"][0]; print(f"   GPU: {d['name']} {d['vram_total']/1e9:.0f} GB")
un = u("/object_info/UNETLoader")["UNETLoader"]["input"]["required"]["unet_name"][0]
print("   H3 models:", [n for n in un if "minimax" in n])
for n in ("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo", "MiniMaxH3AddGuide"):
    print(f"   {n}: {'YES' if u('/object_info/'+n) else 'MISSING'}")
PY
  echo "================================================================"
else
  echo "!! engine did not come up:"; tail -15 /var/log/vs-comfy.log
  exit 1
fi
