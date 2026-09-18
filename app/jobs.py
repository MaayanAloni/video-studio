"""Job queue: one worker thread, jobs composed from the single render_clip primitive."""
import json
import os
import random
import shutil
import tempfile
import threading
import time
import traceback
import uuid

import comfy
import media
import settings
import workflows as wf

JOBS_FILE = os.path.join(media.DATA, "jobs.json")
PERF_FILE = os.path.join(media.DATA, "perf.json")
PREVIEW_DIR = os.path.join(media.DATA, "previews")
os.makedirs(PREVIEW_DIR, exist_ok=True)
MAX_SECONDS = 5.0  # longest single Wan pass; anything longer is chained


def model_shape(quality: str):
    """(fps, grid, max_seconds_per_pass, snap_fn) — H3 has its own frame grid and can
    do ~15 s in one pass, where Wan tops out around 5 s."""
    if wf.QUALITY[quality]["model"] == "h3":
        return wf.H3["fps"], wf.H3["grid"], wf.H3_MAX_FRAMES / wf.H3["fps"], wf.snap_h3
    m = wf.MODELS[wf.QUALITY[quality]["model"]]
    return m["fps"], m["grid"], MAX_SECONDS, wf.snap_len

ASPECTS = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:3": (4, 3), "3:4": (3, 4), "21:9": (21, 9)}
RES_PIXELS = {"360p": 640 * 368, "480p": 832 * 480, "580p": 1024 * 576, "720p": 1280 * 704}

CAMERA = {
    "": "",
    "static": "static locked-off camera, no camera movement",
    "zoom_in": "slow cinematic camera zoom in, dolly in",
    "zoom_out": "slow camera zoom out, dolly out, revealing the scene",
    "pan_left": "camera pans left smoothly",
    "pan_right": "camera pans right smoothly",
    "tilt_up": "camera tilts up",
    "tilt_down": "camera tilts down",
    "orbit": "camera orbits around the subject, arc shot",
    "tracking": "tracking shot following the subject",
    "handheld": "handheld camera, subtle natural shake",
    "crane_up": "crane shot, camera rises up",
    "fpv": "fast FPV drone shot flying through the scene",
}

_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_order: list[str] = []
_cancel: set[str] = set()
_wake = threading.Event()


def _load():
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE) as f:
                for j in json.load(f):
                    if j["status"] in ("queued", "running"):
                        j["status"], j["error"] = "error", "interrupted by restart"
                    _jobs[j["id"]] = j
                    _order.append(j["id"])
        except (OSError, ValueError):
            pass


def _persist():
    with _lock:
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump([_jobs[i] for i in _order[-200:]], f, ensure_ascii=False)
        os.replace(tmp, JOBS_FILE)


def _perf():
    try:
        with open(PERF_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def submit(jtype: str, params: dict) -> dict:
    validate(jtype, params)
    j = {"id": uuid.uuid4().hex[:12], "type": jtype, "params": params, "status": "queued", "progress": 0.0,
         "stage": "queued", "created": time.time(), "video": None, "error": None, "eta": None}
    with _lock:
        _jobs[j["id"]] = j
        _order.append(j["id"])
    _persist()
    _wake.set()
    return j


def all_jobs():
    with _lock:
        return [dict(_jobs[i]) for i in reversed(_order)]


def cancel(jid):
    with _lock:
        j = _jobs.get(jid)
        if not j:
            return
        if j["status"] == "queued":
            j["status"] = "cancelled"
        elif j["status"] == "running":
            _cancel.add(jid)
            comfy.interrupt()
    _persist()


def remove(jid):
    with _lock:
        j = _jobs.get(jid)
        if j and j["status"] not in ("queued", "running"):
            _jobs.pop(jid)
            _order.remove(jid)
    _persist()


# ---------------------------------------------------------------- validation

def validate(jtype, p):
    if p.get("compute") == "remote":
        settings.engine_for("remote")   # raises if not configured/enabled
    if jtype == "generate":
        if not (p.get("prompt") or "").strip() and not p.get("segments"):
            raise ValueError("prompt is required")
        if p.get("quality", "standard") not in wf.QUALITY:
            raise ValueError("unknown quality")
        if p.get("quality") in wf.REMOTE_ONLY and p.get("compute") != "remote":
            raise ValueError(f"{p['quality'].title()} quality needs the rented GPU (~40 GB VRAM)")
        if p.get("end_image") and wf.QUALITY[p.get("quality", "standard")]["model"] != "14b":
            raise ValueError("an end frame needs Standard/Pro/Master quality (14B model)")
        for k in ("start_image", "end_image"):
            if p.get(k) and not os.path.exists(_upload_path(p[k])):
                raise ValueError(f"{k} not found")
        refs = p.get("ref_images") or []
        if refs:
            if wf.QUALITY[p.get("quality", "standard")]["model"] != "h3":
                raise ValueError("reference images are a MiniMax H3 feature — pick H3 Turbo or H3 Max")
            if len(refs) > wf.H3["max_refs"]:
                raise ValueError(f"at most {wf.H3['max_refs']} reference images")
            for r in refs:
                if not os.path.exists(_upload_path(r)):
                    raise ValueError("a reference image is missing")
        qn = p.get("quality", "standard")
        if wf.QUALITY[qn]["model"] == "14b":
            w, h = dims(p.get("aspect", "16:9"), p.get("resolution", "480p"), 16)
            secs = min(float(p.get("seconds") or 5), MAX_SECONDS)
            cfg = p.get("cfg") or wf.QUALITY[qn]["cfg"]
            check_fits(w, h, wf.snap_len(round(secs * 16)), cfg, qn, p.get("force"), p.get("compute"))
    elif jtype in ("extend", "edit_range", "interpolate", "upscale", "trim", "join", "reverse"):
        if not os.path.exists(media.vdir(p.get("video", "?"))):
            raise ValueError("video not found")
        if jtype == "edit_range":
            m = media.meta(p["video"])
            f0, f1 = int(p["f0"]), int(p["f1"])
            if not (0 <= f0 < f1 < m["frames"]) or f1 - f0 < 4:
                raise ValueError("range must span at least 5 frames inside the video")
            if not (p.get("prompt") or "").strip():
                raise ValueError("prompt is required")
        if jtype == "join" and not os.path.exists(media.vdir(p.get("other", "?"))):
            raise ValueError("second video not found")
        if jtype == "extend":
            q = p.get("quality") or media.meta(p["video"]).get("quality") or "standard"
            if p.get("ref_images") and wf.QUALITY.get(q, {}).get("model") != "h3":
                raise ValueError("reference images need an H3 tier — pick H3 Turbo or H3 Max")
            if q in wf.REMOTE_ONLY and p.get("compute") != "remote":
                raise ValueError(f"{q} needs the rented GPU")
            for r in (p.get("ref_images") or []):
                if not os.path.exists(_upload_path(r)):
                    raise ValueError("a reference image is missing")
    else:
        raise ValueError("unknown job type")


# Calibrated on THIS machine (M4 Pro 24 GB). cost = pixels x frames x cfg_batches x expert_GB.
#   1.3e8  480p / 33f / cfg1   / Q4  -> completed, 19.8 min          (measured)
#   3.2e8  480p / 81f / cfg1   / Q4  -> slow but progressing         (measured)
#   5.7e8  720p / 33f / cfg3.5 / Q4  -> page-thrashed, 0 steps/30min (measured)
MEM_BUDGET = 4.0e8


def mem_cost(w, h, frames, cfg, expert_gb=9.65):
    return w * h * frames * (2 if cfg and cfg > 1.05 else 1) * expert_gb


def check_fits(w, h, frames, cfg, quality, force=False, compute="local"):
    """Refuse configurations that will page-thrash instead of computing (local Mac only)."""
    if force or compute == "remote" or wf.QUALITY[quality]["model"] != "14b":
        return
    mode = wf.QUALITY[quality].get("quant", "fast")
    cost = mem_cost(w, h, frames, cfg, wf.expert_gb(wf.MODELS["14b"]["high"], mode))
    if cost <= MEM_BUDGET:
        return
    over = cost / MEM_BUDGET
    tips = []
    if w * h > 700_000:
        tips.append("drop to 480p")
    if cfg and cfg > 1.05:
        tips.append("use Pro instead of Master (8 steps, no doubled CFG batch)")
    if frames > 33:
        tips.append("shorten the clip")
    raise ValueError(
        f"This needs roughly {over:.1f}x more memory than this Mac can hold, so it would "
        f"page to disk instead of computing (a 720p Master job stalled for 30 min on step 1). "
        f"Try: {'; '.join(tips) or 'a smaller clip'}. Add \"force\": true to run it anyway."
    )


def _upload_path(name):
    return os.path.join(media.UPLOADS, os.path.basename(name))


# ---------------------------------------------------------------- helpers

def dims(aspect, resolution, grid):
    a, b = ASPECTS.get(aspect, (16, 9))
    px = RES_PIXELS.get(resolution, RES_PIXELS["480p"])
    h = wf.snap_dim(int((px * b / a) ** 0.5), grid)
    return wf.snap_dim(int(px / h), grid), h


def split_seconds(total, max_s=MAX_SECONDS):
    n = max(1, int(-(-total // max_s)))
    return [total / n] * n


def full_prompt(prompt, camera, style=""):
    parts = [prompt.strip(), CAMERA.get(camera or "", ""), (style or "").strip()]
    return ", ".join(x for x in parts if x)


class Ctx:
    def __init__(self, job, total_clips):
        self.job, self.total, self.done = job, max(1, total_clips), 0
        self.engine = settings.engine_for(job["params"].get("compute"))
        self.available = self.available_clip = self.available_loras = None
        self.has_addguide = None
        self.audio = None
        self.audio_parts = []      # H3 emits audio per pass; a long clip has several
        if not self.engine.local:   # remote box has its own model set
            self.available = self.engine.unet_options()
            self.available_clip = self.engine.clip_options()
            self.available_loras = self.engine.loader_options("LoraLoaderModelOnly", "lora_name")
            self.has_addguide = self.engine.has_node("MiniMaxH3AddGuide")
            if not self.available:
                raise comfy.EngineError("remote engine reported no loadable models")
        self.tmp = tempfile.mkdtemp(prefix="vs_", dir=media.DATA)
        job["tmpdir"] = self.tmp        # lets the UI browse finished frames mid-render
        job["frames_done"] = 0
        self.t_clip = None

    def cancelled(self):
        return self.job["id"] in _cancel

    def stage(self, s):
        self.job["stage"] = s

    def preview(self, img: bytes):
        """Newest in-progress frame, written atomically so the UI never reads a torn file."""
        p = os.path.join(PREVIEW_DIR, self.job["id"] + ".jpg")
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(img)
        os.replace(tmp, p)
        self.job["preview_at"] = time.time()

    def progress(self, frac, node):
        if frac is None:
            return
        j = self.job
        j["progress"] = round((self.done + frac) / self.total, 4)
        if frac > 0.02 and self.t_clip:
            per_clip = (time.time() - self.t_clip) / frac
            j["eta"] = int(per_clip * (self.total - self.done - frac))


def render_clip(ctx: Ctx, clip: dict, label: str) -> list[str]:
    """Render one model pass, return frame paths inside ctx.tmp."""
    ctx.stage(label)
    clip = dict(clip, prefix=f"vs/{ctx.job['id']}_{ctx.done:03d}", available=ctx.available,
                available_clip=ctx.available_clip, available_loras=ctx.available_loras,
                has_addguide=ctx.has_addguide)
    eng = ctx.engine
    for k in ("start_image", "end_image"):
        if clip.get(k):
            clip[k] = eng.upload_image(clip[k])
    if clip.get("ref_images"):
        clip["ref_images"] = [eng.upload_image(p) for p in clip["ref_images"]]
    dst = os.path.join(ctx.tmp, f"clip{ctx.done:03d}")
    ctx.t_clip = time.time()
    try:
        out = eng.run(wf.build(clip), dst, ctx.progress, ctx.cancelled, on_preview=ctx.preview)
    except (comfy.Cancelled, comfy.EngineError):
        raise
    except Exception as e:  # noqa: BLE001 - connection dropped: engine died (usually out of memory)
        if ctx.cancelled():
            raise comfy.Cancelled()
        ctx.stage("engine unreachable - waiting, will retry once")
        for _ in range(90):
            if eng.alive():
                break
            time.sleep(2)
        else:
            raise comfy.EngineError(f"engine went away and did not come back ({e})")
        ctx.stage(label + " (retry)")
        ctx.t_clip = time.time()
        shutil.rmtree(dst, ignore_errors=True)
        out = eng.run(wf.build(clip), dst, ctx.progress, ctx.cancelled, on_preview=ctx.preview)
    perf = _perf()
    perf[clip["quality"]] = {"sec": time.time() - ctx.t_clip, "frames": len(out), "px": clip["width"] * clip["height"]}
    with open(PERF_FILE, "w") as f:
        json.dump(perf, f)
    a = glob_audio(dst)
    if a:
        ctx.audio_parts.append(a)
        ctx.audio = a
    ctx.done += 1
    ctx.job["progress"] = round(ctx.done / ctx.total, 4)
    ctx.job["frames_done"] = ctx.job.get("frames_done", 0) + len(out)
    return out


def glob_audio(d):
    import glob as _g
    return next(iter(sorted(_g.glob(os.path.join(d, "audio.*")))), None)


def chain(ctx, segs, base, start_image, end_image, fps, max_frames):
    """Render consecutive segments; each starts on the previous one's last frame.
    segs: [{prompt, frames}] -> (all frames, [{start,end,prompt}])"""
    frames, marks = [], []
    for i, s in enumerate(segs):
        clip = dict(base, prompt=s["prompt"], frames=min(s["frames"], max_frames), seed=base["seed"] + i,
                    start_image=start_image if i == 0 else frames[-1],
                    end_image=end_image if i == len(segs) - 1 else None)
        got = render_clip(ctx, clip, f"segment {i + 1}/{len(segs)}")
        a = len(frames)
        frames += got if i == 0 or not frames else got[1:]
        if i == 0 and start_image is None:
            pass
        marks.append({"start": a, "end": len(frames) - 1, "prompt": s["prompt"]})
    return frames, marks


def t2v14_ready():
    m = wf.MODELS["14b"]
    base = os.path.join(media.ROOT, "engine", "models")
    unets = [os.path.join(base, "diffusion_models", wf.pick_gguf(m[k], "max")) for k in ("t2v_high", "t2v_low")]
    loras = [os.path.join(base, "loras", m[k]) for k in ("t2v_lora_high", "t2v_lora_low")]
    return all(os.path.exists(p) for p in unets + loras)


def seed_of(p):
    s = p.get("seed")
    return int(s) if s not in (None, "", -1, "-1") else random.randint(1, 2**31 - 1)


# ---------------------------------------------------------------- job types

def job_generate(job):
    p = job["params"]
    quality = p.get("quality", "standard")
    q = wf.QUALITY[quality]
    fps, grid, max_s, snap = model_shape(quality)
    w, h = dims(p.get("aspect", "16:9"), p.get("resolution", "480p"), grid)
    style = p.get("style", "")
    if p.get("segments"):
        segs = []
        for s in p["segments"]:
            for sec in split_seconds(float(s.get("seconds") or 5), max_s):
                segs.append({"prompt": full_prompt(s["prompt"], s.get("camera") or p.get("camera"), style),
                             "frames": snap(round(sec * fps))})
    else:
        segs = [{"prompt": full_prompt(p["prompt"], p.get("camera"), style), "frames": snap(round(sec * fps))}
                for sec in split_seconds(float(p.get("seconds") or 5), max_s)]
    start = _upload_path(p["start_image"]) if p.get("start_image") else None
    end = _upload_path(p["end_image"]) if p.get("end_image") else None
    needs_still = q["model"] == "14b" and not start and not (len(segs) == 1 and end) and not t2v14_ready()
    ctx = Ctx(job, len(segs) + (1 if needs_still else 0))
    try:
        base = {"negative": p.get("negative"), "width": w, "height": h, "seed": seed_of(p), "quality": quality,
                "cfg": p.get("cfg"), "steps": p.get("steps"),
                "ref_images": [_upload_path(r) for r in (p.get("ref_images") or [])],
                "ref_size": p.get("ref_size")}
        if needs_still:
            # 14B is image->video: paint the opening frame from text with the 5B model first
            sw, sh = dims(p.get("aspect", "16:9"), "720p", 32)
            still = render_clip(ctx, dict(base, prompt=segs[0]["prompt"], width=sw, height=sh, frames=17,
                                          quality="draft", steps=24, cfg=5.0, start_image=None,
                                          end_image=None, ref_images=None),
                                "painting opening frame")
            start = still[-1]  # the 5B model paints poor single stills; the end of a short clip is clean
        frames, marks = chain(ctx, segs, base, start, end, fps, snap(int(max_s * fps)))
        ctx.stage("encoding")
        info = {"op": "generate", "prompt": p.get("prompt") or segs[0]["prompt"], "params": p, "seed": base["seed"],
                "segments": marks, "parent": None, "quality": quality}
        if p.get("name"):
            info["name"] = p["name"]
        return media.create_video(frames, fps, info, audio=media.join_audio(ctx.audio_parts, ctx.tmp))
    finally:
        shutil.rmtree(ctx.tmp, ignore_errors=True)


def _edit_quality(p):
    q = p.get("quality", "standard")
    return q if wf.QUALITY[q]["model"] == "14b" else "standard"


def extend_quality(p, source_meta):
    """Pick the tier for an extension. Keep the source video's model family where we can —
    silently swapping an H3 clip to Wan would change the frame rate and drop the audio."""
    want = p.get("quality")
    if want and want in wf.QUALITY:
        return want
    src_q = source_meta.get("quality")
    if src_q in wf.QUALITY:
        return src_q
    return "standard"


def job_extend(job):
    """Continue an existing video: its last frame becomes the first frame of new
    footage, with a fresh prompt and its own reference images."""
    p = job["params"]
    m = media.meta(p["video"])
    src = media.frames_of(p["video"])
    quality = extend_quality(p, m)
    fps, grid, max_s, snap = model_shape(quality)
    out_fps = m["fps"]                      # the existing video sets the frame rate
    secs = split_seconds(float(p.get("seconds") or 5), max_s)
    ctx = Ctx(job, len(secs))
    try:
        base = {"negative": p.get("negative"), "width": m["width"], "height": m["height"],
                "seed": seed_of(p), "quality": quality, "cfg": p.get("cfg"),
                "ref_images": [_upload_path(r) for r in (p.get("ref_images") or [])],
                "ref_size": p.get("ref_size")}
        prompt = full_prompt(p.get("prompt") or m.get("prompt", ""), p.get("camera"))
        segs = [{"prompt": prompt, "frames": snap(round(s * fps))} for s in secs]
        end = _upload_path(p["end_image"]) if p.get("end_image") else None
        new, marks = chain(ctx, segs, base, src[-1], end, fps, snap(int(max_s * fps)))
        new = new[1:]                       # first frame is the source's last frame

        # a 24 fps model extending a 16 fps clip must be re-timed, or it plays in slow motion
        if abs(fps - out_fps) > 0.01:
            ctx.stage("matching frame rate")
            new = media.resample_frames(new, fps, out_fps, os.path.join(ctx.tmp, "retimed"))

        audio = None
        if ctx.audio_parts or media.audio_of(m["id"]):
            ctx.stage("joining audio")
            head = media.audio_of(m["id"]) or media.silence(len(src) / out_fps,
                                                            os.path.join(ctx.tmp, "head.wav"))
            tail = ctx.audio_parts or [media.silence(len(new) / out_fps,
                                                     os.path.join(ctx.tmp, "tail.wav"))]
            audio = media.join_audio([head] + tail, ctx.tmp)

        off = len(src)
        marks = [{"start": max(k["start"] + off - 1, off), "end": min(k["end"] + off - 1, off + len(new) - 1),
                  "prompt": k["prompt"]} for k in marks]
        ctx.stage("encoding")
        return media.create_video(src + new, out_fps, {
            "op": "extend", "prompt": p.get("prompt") or m.get("prompt"), "params": p,
            "parent": m["id"], "name": m.get("name"),
            "segments": (m.get("segments") or []) + marks, "quality": quality}, audio=audio)
    finally:
        shutil.rmtree(ctx.tmp, ignore_errors=True)


def plan_range(f0, f1, total, max_len=81):
    """Snap [f0,f1] so its length is 4k+1 and split it into chunks that share boundary frames."""
    n = wf.snap_len(f1 - f0 + 1)
    while n > total:
        n -= 4
    if f0 + n > total:
        f0 = total - n
    f1 = f0 + n - 1
    steps = (n - 1) // 4                      # each chunk is 4*s+1 frames; chunks overlap by one frame
    k = -(-steps // ((max_len - 1) // 4))
    chunks = [4 * (steps // k + (1 if i < steps % k else 0)) + 1 for i in range(k)]
    return f0, f1, chunks


def job_edit_range(job):
    p = job["params"]
    m = media.meta(p["video"])
    src = media.frames_of(p["video"])
    f0, f1, chunks = plan_range(int(p["f0"]), int(p["f1"]), len(src))
    quality = _edit_quality(p)
    ctx = Ctx(job, len(chunks))
    try:
        base = {"negative": p.get("negative"), "width": m["width"], "height": m["height"], "seed": seed_of(p),
                "quality": quality, "cfg": p.get("cfg")}
        prompt = full_prompt(p["prompt"], p.get("camera"))
        gen = []
        for i, c in enumerate(chunks):
            last = i == len(chunks) - 1
            clip = dict(base, prompt=prompt, frames=c, seed=base["seed"] + i,
                        start_image=src[f0] if i == 0 else gen[-1], end_image=src[f1] if last else None)
            got = render_clip(ctx, clip, f"regenerating range, part {i + 1}/{len(chunks)}")
            gen += got if i == 0 else got[1:]
        # keep the ORIGINAL boundary frames so the cut points are pixel-identical to the untouched footage
        interior = gen[1:len(gen) - 1]
        want = f1 - f0 - 1
        if len(interior) != want:  # defensive: resample to fit exactly
            interior = [interior[round(i * (len(interior) - 1) / max(1, want - 1))] for i in range(want)]
        frames = src[:f0 + 1] + interior + src[f1:]
        ctx.stage("encoding")
        segs = [s for s in (m.get("segments") or [])]
        return media.create_video(frames, m["fps"], {
            "op": "edit_range", "prompt": m.get("prompt"), "params": p, "parent": m["id"], "name": m.get("name"),
            "segments": segs, "edits": (m.get("edits") or []) + [{"start": f0, "end": f1, "prompt": p["prompt"]}],
            "quality": quality})
    finally:
        shutil.rmtree(ctx.tmp, ignore_errors=True)


def job_simple(job):
    """ffmpeg-only operations."""
    p, t = job["params"], job["type"]
    m = media.meta(p["video"])
    src = media.frames_of(p["video"])
    ctx = Ctx(job, 1)
    try:
        fps, frames = m["fps"], src
        ctx.stage(t)
        if t == "interpolate":
            fps = m["fps"] * 2
            frames = media.filtered_copy(m["id"], f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1", fps, ctx.tmp + "/o")
        elif t == "upscale":
            s = 2 if m["width"] * 2 <= 2600 else 1.5
            frames = media.filtered_copy(m["id"], f"scale=trunc(iw*{s}/2)*2:trunc(ih*{s}/2)*2:flags=lanczos,unsharp=5:5:0.6", fps, ctx.tmp + "/o")
        elif t == "trim":
            a, b = max(0, int(p["f0"])), min(len(src) - 1, int(p["f1"]))
            frames = src[:a] + src[b + 1:] if p.get("mode") == "cut" else src[a:b + 1]
            if len(frames) < 2:
                raise ValueError("nothing left after trim")
        elif t == "reverse":
            frames = src[::-1]
        elif t == "join":
            o = media.meta(p["other"])
            other = media.frames_of(p["other"])
            if (o["width"], o["height"]) != (m["width"], m["height"]):
                os.makedirs(ctx.tmp + "/o")
                fit = []
                for i, f in enumerate(other):
                    d = f"{ctx.tmp}/o/{i:06d}.png"
                    media.fit_image(f, m["width"], m["height"], d)
                    fit.append(d)
                other = fit
            frames = src + other
        return media.create_video(frames, fps, {"op": t, "prompt": m.get("prompt"), "params": p, "parent": m["id"],
                                                "name": m.get("name"), "quality": m.get("quality")})
    finally:
        shutil.rmtree(ctx.tmp, ignore_errors=True)


RUNNERS = {"generate": job_generate, "extend": job_extend, "edit_range": job_edit_range}


def _worker():
    while True:
        _wake.wait(timeout=2)
        _wake.clear()
        while True:
            with _lock:
                job = next((_jobs[i] for i in _order if _jobs[i]["status"] == "queued"), None)
                if job:
                    job["status"], job["started"] = "running", time.time()
            if not job:
                break
            try:
                n = max(1, int(job["params"].get("variations") or 1)) if job["type"] == "generate" else 1
                vids = []
                for v in range(n):
                    if n > 1:
                        job["params"] = dict(job["params"], seed=seed_of(job["params"]) + v * 1000)
                    vids.append(RUNNERS.get(job["type"], job_simple)(job)["id"])
                job.update(status="done", progress=1.0, stage="done", video=vids[-1], videos=vids, eta=0)
            except comfy.Cancelled:
                job.update(status="cancelled", stage="cancelled")
            except Exception as e:  # noqa: BLE001 - surface every failure to the UI
                traceback.print_exc()
                job.update(status="error", stage="error", error=str(e)[:800])
            finally:
                _cancel.discard(job["id"])
                try:
                    os.remove(os.path.join(PREVIEW_DIR, job["id"] + ".jpg"))
                except OSError:
                    pass
                job["finished"] = time.time()
                _persist()


def start():
    import glob
    for d in glob.glob(os.path.join(media.DATA, "vs_*")) + [os.path.join(comfy.ENGINE_DIR, "output", "vs")]:
        shutil.rmtree(d, ignore_errors=True)  # leftovers from a crash / force-quit
    _load()
    threading.Thread(target=_worker, daemon=True).start()
