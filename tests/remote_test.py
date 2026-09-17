"""Drive the remote-engine code path against a fake ComfyUI, so the whole
remote flow (auth, queue, progress, previews, output download) is proven
before any money is spent on a real GPU.

    python tests/remote_test.py
"""
import asyncio
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from PIL import Image

import comfy

PORT = 8399
TOKEN = "s3cret-token"
FRAMES = 5
seen = {"auth": [], "prompt": None, "uploads": []}

app = FastAPI()


def _authed(req) -> bool:
    seen["auth"].append(req.headers.get("authorization"))
    return req.headers.get("authorization") == f"Bearer {TOKEN}"


def _png(i: int) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (32, 16), (i * 40 % 256, 90, 140)).save(b, "PNG")
    return b.getvalue()


@app.get("/system_stats")
async def stats(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"system": {"comfyui_version": "0.36.0-fake"},
            "devices": [{"name": "NVIDIA H100 80GB HBM3", "vram_total": 85_520_000_000}]}


@app.get("/models/{folder}")
async def models(folder: str, request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return ["Wan2.2-I2V-A14B-HighNoise-Q8_0.gguf", "Wan2.2-I2V-A14B-LowNoise-Q8_0.gguf",
            "Wan2.2-T2V-A14B-HighNoise-Q8_0.gguf", "Wan2.2-T2V-A14B-LowNoise-Q8_0.gguf"]


@app.get("/object_info/{node}")
async def object_info(node: str, request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    files = {"UNETLoader": ["wan2.2_i2v_high_noise_14B_fp16.safetensors", "wan2.2_i2v_low_noise_14B_fp16.safetensors"],
             "UnetLoaderGGUF": ["Wan2.2-I2V-A14B-HighNoise-Q8_0.gguf", "Wan2.2-I2V-A14B-LowNoise-Q8_0.gguf"],
             "CLIPLoader": ["umt5_xxl_fp16.safetensors"],
             "CLIPLoaderGGUF": ["umt5-xxl-encoder-Q8_0.gguf"]}
    if node not in files:
        return {}
    field = "clip_name" if "CLIP" in node else "unet_name"
    return {node: {"input": {"required": {field: [files[node], {}]}}}}


@app.post("/upload/image")
async def upload(request: Request):
    form = await request.form()
    f = form["image"]
    seen["uploads"].append(f.filename)
    return {"name": f.filename, "subfolder": "", "type": "input"}


@app.post("/prompt")
async def prompt(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    seen["prompt"] = await request.json()
    return {"prompt_id": "fake-1"}


@app.get("/history/{pid}")
async def history(pid: str):
    return {pid: {"status": {"completed": True, "status_str": "success"},
                  "outputs": {"save": {"images": [{"filename": f"f_{i:05d}.png", "subfolder": "vs", "type": "output"}
                                                  for i in range(FRAMES)]}}}}


@app.get("/view")
async def view(filename: str, subfolder: str = "", type: str = "output"):
    return Response(_png(int(filename.split("_")[1].split(".")[0])), media_type="image/png")


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    await asyncio.sleep(0.4)                      # let /prompt land first
    for step in range(1, 4):
        await sock.send_text(json.dumps({"type": "progress",
                                         "data": {"value": step, "max": 3, "node": "ks1", "prompt_id": "fake-1"}}))
        # binary preview frame, same wire format ComfyUI uses
        await sock.send_bytes(struct.pack(">I", 1) + struct.pack(">I", 2) + _png(step))
        await asyncio.sleep(0.2)
    await sock.send_text(json.dumps({"type": "executing", "data": {"node": None, "prompt_id": "fake-1"}}))
    await asyncio.sleep(2)


fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, info)
    if not cond:
        fails.append(name)


def main():
    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    url = f"http://127.0.0.1:{PORT}"
    eng = comfy.Engine(url, TOKEN, local=False, name="fake-h100")
    bad = comfy.Engine(url, "wrong-token", local=False)

    check("remote engine reachable", eng.alive())
    check("wrong token is rejected", not bad.alive())
    st = eng.stats()
    check("reads GPU identity", "H100" in st["devices"][0]["name"], st["devices"][0]["name"])
    check("lists remote models", len(eng.models("diffusion_models")) == 4)
    opts = eng.unet_options()
    check("sees BOTH safetensors and gguf weights", len(opts) == 4 and
          any(n.endswith(".gguf") for n in opts) and any(n.endswith(".safetensors") for n in opts), str(len(opts)))
    check("sees remote text encoders", len(eng.clip_options()) == 2)

    # picks the remote box's Q8 weights, not this Mac's Q4
    import workflows as wf
    avail = eng.unet_options()
    name = wf.pick_gguf("Wan2.2-I2V-A14B-HighNoise", "max", avail)
    check("selects remote Q8 over local Q4", name.endswith("Q8_0.gguf"), name)
    g = wf.build(dict(prompt="x", negative=None, width=1280, height=704, frames=81, seed=1, prefix="t",
                      start_image="a.png", quality="ultra", available=avail, available_clip=eng.clip_options()))
    check("ultra picks unquantised fp16 weights", g["u_high"]["class_type"] == "UNETLoader" and
          g["u_high"]["inputs"]["unet_name"].endswith("_fp16.safetensors"), g["u_high"]["inputs"]["unet_name"])
    check("ultra uses fp16 text encoder", g["clip"]["inputs"]["clip_name"] == "umt5_xxl_fp16.safetensors")
    check("ultra skips the distillation lora", "l_high" not in g)

    with tempfile.TemporaryDirectory() as td:
        img = os.path.join(td, "ref.png")
        Image.new("RGB", (8, 8)).save(img)
        up = eng.upload_image(img)
        check("uploads reference image", up in seen["uploads"], up)

        progress, previews = [], []
        dest = os.path.join(td, "out")
        files = eng.run({"ks1": {"class_type": "KSampler", "inputs": {}},
                         "save": {"class_type": "SaveImage", "inputs": {}}},
                        dest, on_progress=lambda f, n: progress.append(f),
                        on_preview=previews.append, timeout=60)
        check("progress reported", len([p for p in progress if p]) >= 3, str([round(p, 2) for p in progress if p]))
        check("live previews received over ws", len(previews) >= 3, f"{len(previews)} frames")
        check("previews are real images", all(p[:4] == b"\x89PNG" for p in previews))
        check("all frames downloaded", len(files) == FRAMES, f"{len(files)} files")
        check("frames landed locally", all(os.path.exists(f) and f.startswith(dest) for f in files))
        check("downloaded frames are valid", Image.open(files[0]).size == (32, 16))
        check("frames ordered", files == sorted(files))
        check("graph was sent", seen["prompt"] and "ks1" in seen["prompt"]["prompt"])

    # cancellation must reach the remote box, not hang
    t0 = time.time()
    try:
        eng.run({"ks1": {}}, tempfile.mkdtemp(), cancelled=lambda: True, timeout=30)
        check("cancel raises", False)
    except comfy.Cancelled:
        check("cancel raises promptly", time.time() - t0 < 5, f"{time.time() - t0:.1f}s")

    # settings layer must refuse to send work off-machine unless explicitly enabled
    import media
    import settings
    settings.FILE = os.path.join(tempfile.mkdtemp(), "settings.json")
    try:
        settings.engine_for("remote")
        check("remote refused when disabled", False)
    except ValueError:
        check("remote refused when disabled", True)
    settings.save({"remote": {"enabled": True, "url": url, "token": TOKEN, "label": "fake"}})
    e2 = settings.engine_for("remote")
    check("remote used once enabled", e2 and not e2.local and e2.url == url)
    check("local still default", settings.engine_for("local").local)
    check("token never echoed to UI", settings.public()["remote"]["token"] == "********")
    _ = media

    print()
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
