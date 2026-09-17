"""ComfyUI API-format workflow builders for Wan 2.2."""
import os

__doc__ = """ComfyUI API-format workflow builders for Wan 2.2.

One primitive: build(clip) -> graph that renders a single clip (<= ~5s) to mp4.
Everything else (long videos, extend, range edits) is composed from it in jobs.py.
"""

NEG_DEFAULT = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，"
    "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，"
    "手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)

MODELS = {
    # 5B dense model: text->video and start-image->video, 24 fps, fast enough for drafts
    "5b": {
        "fps": 24,
        "unet": "wan2.2_ti2v_5B_fp16.safetensors",
        "vae": "wan2.2_vae.safetensors",
        "grid": 32,
    },
    # 14B MoE image->video (GGUF Q4): best quality, supports start AND end frames, 16 fps
    "14b": {
        "fps": 16,
        "high": "Wan2.2-I2V-A14B-HighNoise",
        "low": "Wan2.2-I2V-A14B-LowNoise",
        "lora_high": "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
        "lora_low": "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors",
        "vae": "wan_2.1_vae.safetensors",
        "grid": 16,
        # native text->video experts, used when a 14B clip has no start/end image
        "t2v_high": "Wan2.2-T2V-A14B-HighNoise",
        "t2v_low": "Wan2.2-T2V-A14B-LowNoise",
        "t2v_lora_high": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors",
        "t2v_lora_low": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors",
    },
}
TEXT_ENCODER = "umt5-xxl-encoder-Q8_0.gguf"

# GGUF precision, best first. "max" walks this list and takes the highest one on disk;
# "fast" pins Q4 so the distilled tiers stay quick.
QUANT_ORDER = ["Q8_0", "Q6_K", "Q5_K_M", "Q5_K_S", "Q4_K_M"]
_MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine", "models", "diffusion_models")


def pick_gguf(stem: str, mode: str = "max") -> str:
    """stem e.g. 'Wan2.2-I2V-A14B-HighNoise' -> best available '<stem>-Q8_0.gguf'."""
    order = QUANT_ORDER if mode == "max" else list(reversed(QUANT_ORDER))
    for q in order:
        name = f"{stem}-{q}.gguf"
        if os.path.exists(os.path.join(_MODEL_DIR, name)):
            return name
    return f"{stem}-Q4_K_M.gguf"


def expert_gb(stem: str, mode: str = "fast") -> float:
    """Size on disk of the weights that will actually be loaded, in GB."""
    p = os.path.join(_MODEL_DIR, pick_gguf(stem, mode))
    return (os.path.getsize(p) / 1e9) if os.path.exists(p) else 9.65


def available_quants(stem: str) -> list:
    return [q for q in QUANT_ORDER if os.path.exists(os.path.join(_MODEL_DIR, f"{stem}-{q}.gguf"))]

# quality presets (Kling: Standard / Professional / Master-ish)
QUALITY = {
    "draft":    {"model": "5b",  "steps": 12, "cfg": 5.0},
    "standard": {"model": "14b", "steps": 4,  "cfg": 1.0, "fast": True},
    "pro":      {"model": "14b", "steps": 8,  "cfg": 1.0, "fast": True},
    "master":   {"model": "14b", "steps": 20, "cfg": 3.5, "fast": False, "quant": "max"},
}


def snap_len(frames: int) -> int:
    """Wan needs 4k+1 frames (1 = single still)."""
    if frames <= 1:
        return 1
    return max(5, ((max(frames, 5) - 1 + 2) // 4) * 4 + 1)


def snap_dim(v: int, grid: int) -> int:
    return max(grid * 4, int(round(v / grid)) * grid)


def build(clip: dict) -> dict:
    """clip keys: prompt, negative, width, height, frames, seed, quality,
    cfg (optional override), start_image, end_image (ComfyUI input filenames), prefix"""
    q = dict(QUALITY[clip["quality"]])
    m = MODELS[q["model"]]
    cfg = float(clip.get("cfg") or q["cfg"])
    if q.get("fast"):
        cfg = 1.0  # distilled LoRA requires cfg 1
    steps = int(clip.get("steps") or q["steps"])
    w, h = snap_dim(clip["width"], m["grid"]), snap_dim(clip["height"], m["grid"])
    n = snap_len(clip["frames"])
    seed = int(clip["seed"])
    g = {}
    g["clip"] = {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": TEXT_ENCODER, "type": "wan"}}
    g["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": clip["prompt"]}}
    g["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": clip.get("negative") or NEG_DEFAULT}}
    g["vae"] = {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}}

    def image(key, fname):
        g[key + "_load"] = {"class_type": "LoadImage", "inputs": {"image": fname}}
        g[key] = {"class_type": "ImageScale", "inputs": {"image": [key + "_load", 0], "upscale_method": "lanczos",
                                                         "width": w, "height": h, "crop": "center"}}
        return [key, 0]

    if q["model"] == "5b":
        if clip.get("end_image"):
            raise ValueError("end frame requires a 14B quality preset")
        g["unet"] = {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}}
        g["ms"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": ["unet", 0], "shift": 8.0}}
        lat = {"vae": ["vae", 0], "width": w, "height": h, "length": n, "batch_size": 1}
        if clip.get("start_image"):
            lat["start_image"] = image("img0", clip["start_image"])
        g["lat"] = {"class_type": "Wan22ImageToVideoLatent", "inputs": lat}
        g["ks"] = {"class_type": "KSampler", "inputs": {
            "model": ["ms", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0],
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}}
        out = ["ks", 0]
    else:
        t2v = not clip.get("start_image") and not clip.get("end_image")
        pre = "t2v_" if t2v else ""
        if t2v:
            g["fl"] = {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": w, "height": h, "length": n, "batch_size": 1}}
            cond_pos, cond_neg, latent = ["pos", 0], ["neg", 0], ["fl", 0]
        else:
            fl = {"positive": ["pos", 0], "negative": ["neg", 0], "vae": ["vae", 0],
                  "width": w, "height": h, "length": n, "batch_size": 1}
            if clip.get("start_image"):
                fl["start_image"] = image("img0", clip["start_image"])
            if clip.get("end_image"):
                fl["end_image"] = image("img1", clip["end_image"])
            g["fl"] = {"class_type": "WanFirstLastFrameToVideo", "inputs": fl}
            cond_pos, cond_neg, latent = ["fl", 0], ["fl", 1], ["fl", 2]
        for k in ("high", "low"):
            g["u_" + k] = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": pick_gguf(m[pre + k], clip.get("quant") or q.get("quant", "fast"))}}
            src = ["u_" + k, 0]
            if q.get("fast"):
                g["l_" + k] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                    "model": src, "lora_name": m[pre + "lora_" + k], "strength_model": 1.0}}
                src = ["l_" + k, 0]
            g["ms_" + k] = {"class_type": "ModelSamplingSD3", "inputs": {"model": src, "shift": 5.0 if q.get("fast") else 8.0}}
        half = max(1, steps // 2)
        common = {"positive": cond_pos, "negative": cond_neg, "cfg": cfg, "sampler_name": "euler",
                  "scheduler": "simple", "steps": steps, "noise_seed": seed}
        g["ks1"] = {"class_type": "KSamplerAdvanced", "inputs": {
            **common, "model": ["ms_high", 0], "latent_image": latent, "add_noise": "enable",
            "start_at_step": 0, "end_at_step": half, "return_with_leftover_noise": "enable"}}
        g["ks2"] = {"class_type": "KSamplerAdvanced", "inputs": {
            **common, "model": ["ms_low", 0], "latent_image": ["ks1", 0], "add_noise": "disable",
            "start_at_step": half, "end_at_step": 10000, "return_with_leftover_noise": "disable"}}
        out = ["ks2", 0]

    g["dec"] = {"class_type": "VAEDecodeTiled", "inputs": {
        "samples": out, "vae": ["vae", 0], "tile_size": 512 if w * h <= 640_000 else 384, "overlap": 64, "temporal_size": 64, "temporal_overlap": 8}}
    # lossless-ish PNG frames: all downstream splicing works on frames, never on re-encoded video
    g["save"] = {"class_type": "SaveImage", "inputs": {"images": ["dec", 0], "filename_prefix": clip["prefix"]}}
    return g
