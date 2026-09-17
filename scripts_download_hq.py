import os, shutil
from huggingface_hub import hf_hub_download
M = "engine/models/diffusion_models"
items = [("QuantStack/Wan2.2-I2V-A14B-GGUF", "HighNoise/Wan2.2-I2V-A14B-HighNoise-Q6_K.gguf"),
         ("QuantStack/Wan2.2-I2V-A14B-GGUF", "LowNoise/Wan2.2-I2V-A14B-LowNoise-Q6_K.gguf"),
         ("QuantStack/Wan2.2-T2V-A14B-GGUF", "HighNoise/Wan2.2-T2V-A14B-HighNoise-Q6_K.gguf"),
         ("QuantStack/Wan2.2-T2V-A14B-GGUF", "LowNoise/Wan2.2-T2V-A14B-LowNoise-Q6_K.gguf")]
for repo, path in items:
    dst = os.path.join(M, os.path.basename(path))
    if os.path.exists(dst):
        print("have", dst, flush=True); continue
    p = hf_hub_download(repo, path, local_dir="dl_cache")
    shutil.move(p, dst); print("done", dst, flush=True)
print("ALL DONE", flush=True)
