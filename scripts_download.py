import os, shutil
from huggingface_hub import hf_hub_download
M = "engine/models"
C = "Comfy-Org/Wan_2.2_ComfyUI_Repackaged"
Q = "QuantStack/Wan2.2-I2V-A14B-GGUF"
items = [
 ("city96/umt5-xxl-encoder-gguf", "umt5-xxl-encoder-Q8_0.gguf", "text_encoders"),
 (C, "split_files/vae/wan2.2_vae.safetensors", "vae"),
 (C, "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors", "diffusion_models"),
 (C, "split_files/vae/wan_2.1_vae.safetensors", "vae"),
 (C, "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors", "loras"),
 (C, "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors", "loras"),
 (Q, "HighNoise/Wan2.2-I2V-A14B-HighNoise-Q4_K_M.gguf", "diffusion_models"),
 (Q, "LowNoise/Wan2.2-I2V-A14B-LowNoise-Q4_K_M.gguf", "diffusion_models"),
]
for repo, path, sub in items:
    dst = os.path.join(M, sub, os.path.basename(path))
    if os.path.exists(dst): print("have", dst, flush=True); continue
    p = hf_hub_download(repo, path, local_dir="dl_cache")
    os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.move(p, dst)
    print("done", dst, flush=True)
print("ALL DONE", flush=True)
