"""Video Studio server: REST API + static GUI. Supervises the local ComfyUI engine."""
import os
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from PIL import Image  # noqa: E402

import comfy  # noqa: E402
import jobs  # noqa: E402
import media  # noqa: E402
import workflows as wf  # noqa: E402

ROOT = media.ROOT
app = FastAPI(title="Video Studio")
_engine_proc = None


@app.exception_handler(FileNotFoundError)
async def _not_found(request, exc):
    return JSONResponse({"error": "not found"}, status_code=404)


def ensure_engine():
    """Start ComfyUI if it is not running; called at boot and by a watchdog."""
    global _engine_proc
    if comfy.alive():
        return
    if _engine_proc and _engine_proc.poll() is None:
        return  # still booting
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    env = dict(os.environ, PYTORCH_ENABLE_MPS_FALLBACK="1", PYTORCH_MPS_HIGH_WATERMARK_RATIO="0.0")
    log = open(os.path.join(ROOT, "logs", "engine.log"), "a")
    _engine_proc = subprocess.Popen(
        [sys.executable, "main.py", "--port", "8188", "--listen", "127.0.0.1", "--disable-auto-launch"],
        cwd=os.path.join(ROOT, "engine"), env=env, stdout=log, stderr=subprocess.STDOUT)


def _watchdog():
    while True:
        try:
            ensure_engine()
        except Exception as e:  # noqa: BLE001
            print("engine watchdog:", e)
        time.sleep(10)


@app.on_event("startup")
def _startup():
    threading.Thread(target=_watchdog, daemon=True).start()
    jobs.start()


def model_status():
    base = os.path.join(ROOT, "engine", "models")
    need = {
        "text": ("text_encoders", wf.TEXT_ENCODER),
        "5b": ("diffusion_models", wf.MODELS["5b"]["unet"]), "5b_vae": ("vae", wf.MODELS["5b"]["vae"]),
        "14b_high": ("diffusion_models", wf.MODELS["14b"]["high"]), "14b_low": ("diffusion_models", wf.MODELS["14b"]["low"]),
        "14b_vae": ("vae", wf.MODELS["14b"]["vae"]),
        "lora_high": ("loras", wf.MODELS["14b"]["lora_high"]), "lora_low": ("loras", wf.MODELS["14b"]["lora_low"]),
    }
    have = {k: os.path.exists(os.path.join(base, *v)) for k, v in need.items()}
    return {"draft": have["text"] and have["5b"] and have["5b_vae"],
            "hq": all(have[k] for k in ("text", "14b_high", "14b_low", "14b_vae", "lora_high", "lora_low")),
            "files": have}


@app.get("/api/status")
def status():
    free = os.statvfs(ROOT)
    return {"engine": comfy.alive(), "models": model_status(), "perf": jobs._perf(),
            "disk_free_gb": round(free.f_bavail * free.f_frsize / 1e9, 1),
            "cameras": list(jobs.CAMERA), "aspects": list(jobs.ASPECTS), "resolutions": list(jobs.RES_PIXELS),
            "quality": {k: {"model": v["model"], "steps": v["steps"], "fps": wf.MODELS[v["model"]]["fps"]} for k, v in wf.QUALITY.items()}}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 60 * 1024 * 1024:
        raise HTTPException(413, "image too large")
    name = uuid.uuid4().hex[:12] + ".png"
    path = os.path.join(media.UPLOADS, name)
    tmp = path + ".src"
    with open(tmp, "wb") as f:
        f.write(raw)
    try:
        from PIL import ImageOps
        im = ImageOps.exif_transpose(Image.open(tmp)).convert("RGB")
        im.save(path)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "not a readable image")
    finally:
        os.remove(tmp)
    return {"name": name, "url": f"/uploads/{name}", "width": im.width, "height": im.height}


@app.post("/api/videos/{vid}/frame/{n}/as_upload")
def frame_as_upload(vid: str, n: int):
    """Copy a frame of a video into uploads so it can be used as a start/end reference."""
    fr = media.frames_of(os.path.basename(vid))
    if not fr:
        raise HTTPException(404)
    n = max(0, min(len(fr) - 1, n))
    name = uuid.uuid4().hex[:12] + ".png"
    import shutil
    shutil.copyfile(fr[n], os.path.join(media.UPLOADS, name))
    return {"name": name, "url": f"/uploads/{name}"}


@app.get("/api/jobs")
def get_jobs():
    return jobs.all_jobs()


@app.post("/api/jobs")
def post_job(body: dict):
    try:
        return jobs.submit(body.get("type", ""), body.get("params") or {})
    except (ValueError, KeyError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    jobs.cancel(jid)
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    jobs.remove(jid)
    return {"ok": True}


@app.get("/api/videos")
def videos():
    return media.list_videos()


@app.get("/api/videos/{vid}")
def video(vid: str):
    try:
        return media.meta(os.path.basename(vid))
    except OSError:
        raise HTTPException(404)


@app.patch("/api/videos/{vid}")
def rename(vid: str, body: dict):
    m = media.meta(os.path.basename(vid))
    if "name" in body:
        m["name"] = str(body["name"])[:120]
    if "favorite" in body:
        m["favorite"] = bool(body["favorite"])
    media.save_meta(m)
    return m


@app.delete("/api/videos/{vid}")
def delete(vid: str):
    media.delete_video(os.path.basename(vid))
    return {"ok": True}


@app.get("/api/videos/{vid}/frame/{n}")
def frame(vid: str, n: int, w: int = 0):
    fr = media.frames_of(os.path.basename(vid))
    if not fr:
        raise HTTPException(404)
    path = fr[max(0, min(len(fr) - 1, n))]
    if w:  # small jpeg for the timeline filmstrip
        import io
        from fastapi.responses import Response
        im = Image.open(path).convert("RGB")
        im.thumbnail((max(32, min(w, 640)), 640))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
    return FileResponse(path, media_type="image/png")


@app.get("/api/videos/{vid}/download")
def download(vid: str):
    vid = os.path.basename(vid)
    m = media.meta(vid)
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in m.get("name", vid))[:50] or vid
    return FileResponse(os.path.join(media.vdir(vid), "video.mp4"), filename=f"{safe}.mp4", media_type="video/mp4")


app.mount("/videos", StaticFiles(directory=media.VIDEOS), name="videos")
app.mount("/uploads", StaticFiles(directory=media.UPLOADS), name="uploads")
app.mount("/", StaticFiles(directory=os.path.join(ROOT, "app", "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 7860)), log_level="warning")
