"""Frame-accurate video storage. Every video keeps its PNG frames so edits never re-encode twice."""
import glob
import json
import os
import shutil
import subprocess
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
VIDEOS = os.path.join(DATA, "videos")
UPLOADS = os.path.join(DATA, "uploads")
for d in (VIDEOS, UPLOADS):
    os.makedirs(d, exist_ok=True)


def ff(*args):
    p = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("ffmpeg: " + p.stderr[-600:])


def vdir(vid):
    return os.path.join(VIDEOS, vid)


def frames_of(vid) -> list[str]:
    return sorted(glob.glob(os.path.join(vdir(vid), "frames", "*.png")))


def meta(vid) -> dict:
    with open(os.path.join(vdir(vid), "meta.json")) as f:
        return json.load(f)


def save_meta(m):
    tmp = os.path.join(vdir(m["id"]), "meta.json.tmp")
    with open(tmp, "w") as f:
        json.dump(m, f, indent=1, ensure_ascii=False)
    os.replace(tmp, os.path.join(vdir(m["id"]), "meta.json"))


def list_videos() -> list[dict]:
    out = []
    for p in glob.glob(os.path.join(VIDEOS, "*", "meta.json")):
        try:
            with open(p) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            pass
    return sorted(out, key=lambda m: -m["created"])


def health(frame_paths: list[str]) -> dict:
    """Detect silent MPS output corruption (ComfyUI issue #15793): on some Apple Silicon
    chips Wan output degrades progressively along the clip with no error raised. Real
    symptom is a large MONOTONIC saturation climb; ordinary scene change is small and
    non-monotonic. Sampled, so this costs little."""
    import numpy as np
    from PIL import Image
    n = len(frame_paths)
    if n < 8:
        return {"drift": 0.0, "suspect": False}
    idx = [round(i * (n - 1) / 11) for i in range(12)]
    sat = []
    for i in idx:
        with Image.open(frame_paths[i]) as im:
            a = np.asarray(im.convert("RGB"), np.float32)
        mx, mn = a.max(2), a.min(2)
        sat.append(float(np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0).mean()) * 100)
    drift = (sat[-1] - sat[0]) / max(sat[0], 1e-6) * 100
    diffs = np.diff(sat)
    monotonic = float((diffs > 0).mean())          # 1.0 == climbs every single step
    return {"drift": round(drift, 1), "monotonic": round(monotonic, 2),
            "suspect": bool(drift > 25 and monotonic > 0.8)}


def create_video(frame_paths: list[str], fps: float, info: dict) -> dict:
    """Copy frames into a new video entry, encode mp4 + thumbnail, return meta."""
    from PIL import Image

    if not frame_paths:
        raise RuntimeError("no frames to save")
    vid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = vdir(vid)
    os.makedirs(os.path.join(d, "frames"))
    for i, src in enumerate(frame_paths):
        shutil.copyfile(src, os.path.join(d, "frames", f"{i:06d}.png"))
    with Image.open(frame_paths[0]) as im:
        w, h = im.size
    encode(vid, fps)
    Image.open(frame_paths[0]).convert("RGB").resize((320, max(2, int(320 * h / w)))).save(os.path.join(d, "thumb.jpg"), quality=85)
    m = {"id": vid, "created": time.time(), "fps": fps, "width": w, "height": h, "frames": len(frame_paths),
         "seconds": round(len(frame_paths) / fps, 3), "health": health(frame_paths), **info}
    m.setdefault("name", (info.get("prompt") or "video")[:60])
    save_meta(m)
    return m


def encode(vid, fps):
    d = vdir(vid)
    ff("-framerate", str(fps), "-i", os.path.join(d, "frames", "%06d.png"),
       # all-intra-ish short GOP so the browser can seek to exact frames in the editor
       "-c:v", "libx264", "-crf", "14", "-preset", "slow", "-g", "8", "-pix_fmt", "yuv420p",
       "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", os.path.join(d, "video.mp4"))


def delete_video(vid):
    shutil.rmtree(vdir(vid), ignore_errors=True)


def filtered_copy(vid, vf: str, fps_out: float, tmpdir: str) -> list[str]:
    """Run frames through an ffmpeg filter, return new frame paths."""
    os.makedirs(tmpdir, exist_ok=True)
    m = meta(vid)
    ff("-framerate", str(m["fps"]), "-i", os.path.join(vdir(vid), "frames", "%06d.png"),
       "-vf", vf, "-r", str(fps_out), os.path.join(tmpdir, "%06d.png"))
    return sorted(glob.glob(os.path.join(tmpdir, "*.png")))


def fit_image(src: str, w: int, h: int, dst: str):
    """Center-crop + resize a frame to w x h (used when joining clips of different sizes)."""
    from PIL import Image, ImageOps
    ImageOps.fit(Image.open(src).convert("RGB"), (w, h), Image.LANCZOS).save(dst)
