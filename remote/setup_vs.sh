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
# Models are NOT moved: ComfyUI is pointed at the existing models directory.
set -uo pipefail
VS=/workspace/vs
VER="${COMFY_VERSION:-v0.36.0}"
MODELS=/workspace/runpod-slim/ComfyUI/models
PY="$VS/venv/bin/python"
log(){ echo "==> $*"; }

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

# ---- point at the models already on the volume (no copying 66 GB) ------------------
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
