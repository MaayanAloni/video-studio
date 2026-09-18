"""Video Studio server: REST API + static GUI. Supervises the local ComfyUI engine."""
import glob
import os
import re
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
import runpod as rp  # noqa: E402
import settings as settings_mod  # noqa: E402
import media  # noqa: E402
import workflows as wf  # noqa: E402

ROOT = media.ROOT
app = FastAPI(title="Video Studio")
_engine_proc = None


@app.exception_handler(FileNotFoundError)
async def _not_found(request, exc):
    return JSONResponse({"error": "not found"}, status_code=404)


_unhealthy = 0


def ensure_engine():
    """Start ComfyUI if it is not running; called at boot and by a watchdog."""
    global _engine_proc, _unhealthy
    if comfy.alive():
        _unhealthy = 0
        return
    if _engine_proc and _engine_proc.poll() is None:
        return  # still booting
    if comfy.port_busy():
        # Something IS listening, it is just too busy to answer (heavy sampling, swapping).
        # Never spawn a duplicate over a live port; only give up after a long stall.
        _unhealthy += 1
        if _unhealthy < 30:  # ~5 minutes at the 10s watchdog interval
            return
        print("engine unresponsive for ~5 min but port is held; not spawning a duplicate")
        _unhealthy = 0
        return
    _unhealthy = 0
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    env = dict(os.environ, PYTORCH_ENABLE_MPS_FALLBACK="1", PYTORCH_MPS_HIGH_WATERMARK_RATIO="0.0")
    log = open(os.path.join(ROOT, "logs", "engine.log"), "a")
    _engine_proc = subprocess.Popen(
        [sys.executable, "main.py", "--port", "8188", "--listen", "127.0.0.1", "--disable-auto-launch",
         "--preview-method", "auto", "--preview-size", "512"],
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
    m14 = wf.MODELS["14b"]
    g = lambda stem: wf.pick_gguf(stem, "max")  # stems resolve to whichever quant is on disk
    need = {
        "text": ("text_encoders", wf.TEXT_ENCODER),
        "5b": ("diffusion_models", wf.MODELS["5b"]["unet"]), "5b_vae": ("vae", wf.MODELS["5b"]["vae"]),
        "14b_high": ("diffusion_models", g(m14["high"])), "14b_low": ("diffusion_models", g(m14["low"])),
        "14b_vae": ("vae", m14["vae"]),
        "lora_high": ("loras", m14["lora_high"]), "lora_low": ("loras", m14["lora_low"]),
        "t2v_high": ("diffusion_models", g(m14["t2v_high"])), "t2v_low": ("diffusion_models", g(m14["t2v_low"])),
        "t2v_lora_high": ("loras", m14["t2v_lora_high"]), "t2v_lora_low": ("loras", m14["t2v_lora_low"]),
    }
    have = {k: os.path.exists(os.path.join(base, *v)) for k, v in need.items()}
    return {"draft": have["text"] and have["5b"] and have["5b_vae"],
            "hq": all(have[k] for k in ("text", "14b_high", "14b_low", "14b_vae", "lora_high", "lora_low")),
            "t2v_hq": jobs.t2v14_ready(),
            "files": have}


@app.get("/api/status")
def status():
    free = os.statvfs(ROOT)
    st = settings_mod.load()["remote"]
    return {"engine": comfy.alive(), "models": model_status(), "perf": jobs._perf(),
            "remote_models": _remote_models(),
            "remote": {"enabled": bool(st.get("enabled") and st.get("url")), "label": st.get("label", ""),
                       "default_for_new": bool(st.get("default_for_new"))},
            "disk_free_gb": round(free.f_bavail * free.f_frsize / 1e9, 1),
            "cameras": list(jobs.CAMERA), "aspects": list(jobs.ASPECTS), "resolutions": list(jobs.RES_PIXELS),
            "quality": {k: {"model": v["model"], "steps": v["steps"],
                            "fps": wf.H3["fps"] if v["model"] == "h3" else wf.MODELS[v["model"]]["fps"],
                            "remote_only": k in wf.REMOTE_ONLY}
                        for k, v in wf.QUALITY.items()}}


@app.get("/api/settings")
def get_settings():
    return settings_mod.public()


@app.put("/api/settings")
def put_settings(body: dict):
    p = dict(body.get("runpod") or {})
    if p.get("api_key") == "********":
        p.pop("api_key")
    if p.get("api_key"):
        old = settings_mod.load()["runpod"].get("api_key", "")
        if old and p["api_key"] != old:
            # a different account cannot see the previous account's pod or volume
            p.setdefault("pod_id", "")
            p.setdefault("volume_id", "")
            rp.close_tunnel()
            settings_mod.save({"remote": {"enabled": False, "url": "", "label": ""}})
    if p:
        settings_mod.save({"runpod": p})
    r = dict(body.get("remote") or {})
    if r.get("token") == "********":      # unchanged placeholder from the UI
        r.pop("token")
    if r.get("url"):
        u = str(r["url"]).strip().rstrip("/")
        if not u.startswith(("http://", "https://")):
            u = "http://" + u
        r["url"] = u
    return settings_mod.public(settings_mod.save({"remote": r}))


# ----------------------------------------------------------------- RunPod
@app.get("/api/runpod/catalog")
def rp_catalog():
    """Live GPU prices. Needs no API key, so the picker works before setup."""
    try:
        return {"ok": True, "gpus": rp.catalog(), "datacenters": rp.PREFERRED_DCS}
    except rp.RunPodError as e:
        return {"ok": False, "error": str(e), "gpus": [], "datacenters": rp.PREFERRED_DCS}


@app.get("/api/runpod/status")
def rp_status():
    cfg = settings_mod.load()["runpod"]
    out = {"configured": bool(cfg.get("api_key")), "pod_id": cfg.get("pod_id", ""),
           "setup": dict(_setup),
           "gpu": cfg.get("gpu"), "datacenter": cfg.get("datacenter"),
           "tunnel": rp.tunnel_status(), "pod": None}
    if cfg.get("api_key") and cfg.get("pod_id"):
        try:
            pod = rp.get_pod(cfg["api_key"], cfg["pod_id"])
            host, port, mode = rp.ssh_target(cfg["api_key"], cfg["pod_id"])
            out["pod"] = {"status": pod.get("desiredStatus"),
                          "container_up": rp.ready(cfg["api_key"], cfg["pod_id"]),
                          "ip": host, "ssh_port": port, "ssh_mode": mode,
                          "cost_hr": pod.get("costPerHr") or pod.get("adjustedCostPerHr"),
                          "started": pod.get("lastStartedAt"), "name": pod.get("name")}
        except rp.RunPodError as e:
            out["error"] = str(e)
    return out


_setup = {"running": False, "step": "", "error": "", "done": False, "at": 0.0}


def _setup_worker(gpu: str, dc: str, models: str, volume_gb: int):
    """Rent -> wait for the container -> install models -> connect, all server side.
    The browser used to drive this; closing the tab stranded a paid pod half-configured."""
    def step(s):
        _setup.update(step=s, at=time.time())
        print("[setup]", s, flush=True)
    try:
        cfg = settings_mod.load()["runpod"]
        key = cfg["api_key"]
        pid = cfg.get("pod_id") or ""
        if pid:
            # a pod we already own may simply be stopped — start it rather than
            # waiting forever for a machine that nobody asked to boot
            try:
                pod = rp.get_pod(key, pid)
                if pod.get("desiredStatus") != "RUNNING":
                    step("starting your stopped machine")
                    try:
                        rp.start_pod(key, pid)
                    except rp.RunPodError as e:
                        step("could not restart it, renting a fresh one")
                        print("[setup] start failed:", e)
                        pid = ""
            except rp.RunPodError:
                pid = ""            # it no longer exists on this account
        if not pid:
            vol = cfg.get("volume_id")
            if vol:                 # make sure the volume is still ours
                try:
                    vols = {x.get("id") for x in rp.volumes(key)}
                    if vol not in vols:
                        vol = ""
                except rp.RunPodError:
                    pass
            if not vol:
                step("creating model volume")
                vol = rp.create_volume(key, "video-studio-models", volume_gb, dc).get("id", "")
            step("renting GPU")
            pod = rp.create_pod(key, gpu, dc, vol)
            pid = pod["id"]
            settings_mod.save({"runpod": {"pod_id": pid, "gpu": gpu, "datacenter": dc,
                                          "volume_id": vol or "", "models": models}})
        rp.set_pod(pid)

        step("waiting for the machine to boot")
        host = None
        for _ in range(200):                       # first boot can take ~30 min
            host, port, mode = rp.ssh_target(key, pid)
            if host:
                break
            time.sleep(6)
        if not host:
            raise RuntimeError("machine never became reachable — check the RunPod console")

        step("connecting")
        url = rp.open_tunnel(host, port, mode)
        settings_mod.save({"remote": {"url": url, "enabled": True,
                                      "label": f"RunPod {gpu} · {dc}"}})
        eng = comfy.Engine(url, local=False)
        for _ in range(60):
            if eng.alive(5):
                break
            time.sleep(5)

        step(f"installing models ({models}) — this is the long part")
        rp_provision({"models": models})
        for _ in range(720):                       # up to 2h for a big model pull
            time.sleep(10)
            try:
                with open(os.path.join(ROOT, "logs", "provision.log")) as f:
                    txt = f.read()
            except OSError:
                continue
            if "ENGINE UP" in txt or "!!" in txt:
                break
        step("restarting engine with the new models")
        for _ in range(90):
            if eng.alive(5):
                break
            time.sleep(5)
        rp.open_tunnel(host, port, mode)           # the pod restarts ComfyUI; refresh the tunnel
        _rm_cache["at"] = 0.0
        # don't claim success until the engine actually offers what we installed
        want = {"h3": "h3", "full": "ultra", "full+h3": "h3"}.get(models)
        _rm_cache["at"] = 0.0          # the 30s cache made this verdict race the install
        have = {}
        for _ in range(6):             # give the engine a moment to finish listing models
            have = _remote_models()
            if have.get(want) is True:
                break
            _rm_cache["at"] = 0.0
            time.sleep(5)
        if want and have.get(want) is not True:
            raise RuntimeError(f"models installed but the engine is not offering {want} "
                               f"- see Logs > rented GPU (got {have})")
        step("ready")
        _setup["done"] = True
    except Exception as e:  # noqa: BLE001
        _setup["error"] = str(e)[:300]
        step("failed")
    finally:
        _setup["running"] = False


@app.post("/api/runpod/setup")
def rp_setup(body: dict = None):
    """One call does everything and keeps running even if the browser goes away."""
    body = body or {}
    cfg = settings_mod.load()["runpod"]
    if not cfg.get("api_key"):
        return JSONResponse({"error": "set your RunPod API key first"}, status_code=400)
    if _setup["running"]:
        return {"ok": True, "already": True, "step": _setup["step"]}
    _setup.update(running=True, step="starting", error="", done=False, at=time.time())
    threading.Thread(target=_setup_worker, daemon=True, args=(
        body.get("gpu") or cfg["gpu"], body.get("datacenter") or cfg["datacenter"],
        body.get("models") or cfg.get("models") or "h3",
        int(body.get("volume_gb") or cfg.get("volume_gb") or 100))).start()
    return {"ok": True, "step": "starting"}


@app.post("/api/runpod/launch")
def rp_launch(body: dict = None):
    """Rent a GPU. Costs money from the moment the pod is created — only ever
    triggered by an explicit click in the UI."""
    body = body or {}
    cfg = settings_mod.load()["runpod"]
    key = cfg.get("api_key")
    if not key:
        return JSONResponse({"error": "set your RunPod API key first"}, status_code=400)
    gpu = body.get("gpu") or cfg["gpu"]
    dc = body.get("datacenter") or cfg["datacenter"]
    try:
        vol = cfg.get("volume_id")
        if body.get("use_volume", True) and not vol:
            size = int(body.get("volume_gb") or cfg.get("volume_gb") or 100)
            vol = rp.create_volume(key, "video-studio-models", size, dc).get("id", "")
        pod = rp.create_pod(key, gpu, dc, vol)
        settings_mod.save({"runpod": {"pod_id": pod["id"], "gpu": gpu, "datacenter": dc, "volume_id": vol or ""}})
        rp.set_pod(pod["id"])
        return {"ok": True, "pod_id": pod["id"], "cost_hr": pod.get("costPerHr"),
                "volume_id": vol, "note": "pod created; poll /api/runpod/status until it is RUNNING"}
    except rp.RunPodError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/runpod/connect")
def rp_connect():
    """Open the SSH tunnel to the running pod and point the remote engine at it."""
    cfg = settings_mod.load()["runpod"]
    if not (cfg.get("api_key") and cfg.get("pod_id")):
        return JSONResponse({"error": "no pod"}, status_code=400)
    try:
        host, port, mode = rp.ssh_target(cfg["api_key"], cfg["pod_id"])
        if not host:
            return JSONResponse({"error": "the container is still starting (pulling the image can take "
                                          "up to 30 minutes on a first deploy)"}, status_code=409)
        url = rp.open_tunnel(host, port, mode)
        settings_mod.save({"remote": {"url": url, "enabled": True,
                                      "label": f"RunPod {cfg.get('gpu','')} · {cfg.get('datacenter','')}"}})
        eng = comfy.Engine(url, local=False)
        ok = False
        for _ in range(30):
            if eng.alive(5):
                ok = True
                break
            time.sleep(2)
        return {"ok": ok, "url": url, "engine_reachable": ok,
                "hint": "" if ok else "tunnel is up but ComfyUI is not answering yet — it can take minutes on first boot"}
    except rp.RunPodError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/runpod/provision")
def rp_provision(body: dict = None):
    """Install ComfyUI deps and download models onto the pod, over SSH."""
    body = body or {}
    cfg = settings_mod.load()["runpod"]
    try:
        pod = rp.get_pod(cfg["api_key"], cfg["pod_id"])
    except rp.RunPodError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    host, port, ssh_mode = rp.ssh_target(cfg["api_key"], cfg["pod_id"])
    if not host:
        return JSONResponse({"error": "pod not reachable yet — container still starting"}, status_code=409)
    script = os.path.join(ROOT, "remote", "setup_vs.sh")
    models_mode = body.get("models") or cfg.get("models") or "full"
    logf = os.path.join(ROOT, "logs", "provision.log")
    with open(script) as f, open(logf, "a") as out:
        subprocess.Popen(rp.ssh_base(host, port, ssh_mode) + ["bash -s"],
                         stdin=f, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    return {"ok": True, "log": "logs/provision.log",
            "note": "downloading models on the pod; watch Logs → provision"}


@app.post("/api/runpod/disconnect")
def rp_disconnect(body: dict = None):
    """Close the tunnel and optionally stop or terminate the pod (stops billing)."""
    body = body or {}
    rp.close_tunnel()
    settings_mod.save({"remote": {"enabled": False}})
    cfg = settings_mod.load()["runpod"]
    action = body.get("action", "stop")
    if cfg.get("api_key") and cfg.get("pod_id") and action in ("stop", "terminate"):
        try:
            if action == "terminate":
                rp.terminate_pod(cfg["api_key"], cfg["pod_id"])
                settings_mod.save({"runpod": {"pod_id": ""}})
            else:
                rp.stop_pod(cfg["api_key"], cfg["pod_id"])
        except rp.RunPodError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "action": action}


@app.post("/api/remote/test")
def remote_test(body: dict = None):
    """Check a remote GPU: reachable, what card, which of our models it already has."""
    body = body or {}
    cfg = settings_mod.load()["remote"]
    url = (body.get("url") or cfg.get("url") or "").strip().rstrip("/")
    token = body.get("token")
    if token == "********" or token is None:
        token = cfg.get("token", "")
    if not url:
        return {"ok": False, "error": "no URL set"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    eng = comfy.Engine(url, token, local=False, name="remote")
    t0 = time.time()
    try:
        st = eng.stats()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"cannot reach engine: {str(e)[:300]}"}
    dev = (st.get("devices") or [{}])[0]
    unets = eng.unet_options()
    m14 = wf.MODELS["14b"]
    have_wan = any(n.startswith(m14["high"]) for n in unets) and any(n.startswith(m14["low"]) for n in unets)
    have_wan_t2v = any(n.startswith(m14["t2v_high"]) for n in unets) and any(n.startswith(m14["t2v_low"]) for n in unets)
    have_full = m14["high_full"] in unets and m14["low_full"] in unets
    have_full_t2v = m14["t2v_high_full"] in unets and m14["t2v_low_full"] in unets
    return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
            "comfyui": st.get("system", {}).get("comfyui_version"),
            "device": dev.get("name"), "vram_gb": round(dev.get("vram_total", 0) / 1e9, 1),
            "models": {"wan_i2v": have_wan, "wan_t2v": have_wan_t2v, "count": len(unets),
                       "ultra": have_full, "ultra_t2v": have_full_t2v},
            "unets": sorted(unets)[:40]}


_rm_cache = {"at": 0.0, "val": {}}


def _remote_models() -> dict:
    """Which tiers the rented box can actually serve. Cached — it is an HTTP round trip."""
    if time.time() - _rm_cache["at"] < 30:
        return _rm_cache["val"]
    eng = settings_mod.remote_engine()
    val = {}
    if eng:
        try:
            unets = eng.unet_options()
            val = {"h3": any(n in unets for n in wf.H3["unets"]),
                   "h3_refs": wf.h3_refs_ready(unets),
                   "ultra": wf.MODELS["14b"]["high_full"] in unets}
        except Exception:  # noqa: BLE001
            val = {}
    _rm_cache.update(at=time.time(), val=val)
    return val


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


LOG_FILES = {"engine": "engine.log", "server": "server.log", "downloads": "download2.log",
             "provision": "provision.log", "tunnel": "tunnel.log"}
_remote_log = {"at": 0.0, "text": ""}


def remote_log(lines: int = 400) -> str:
    """Tail the rented pod's ComfyUI log over the same ssh key. Cached briefly so
    polling the Logs tab doesn't open an ssh connection every second."""
    now = time.time()
    if now - _remote_log["at"] < 4:
        return _remote_log["text"]
    cfg = settings_mod.load()["runpod"]
    if not (cfg.get("api_key") and cfg.get("pod_id")):
        return "(no rented GPU configured)"
    try:
        host, port, ssh_mode = rp.ssh_target(cfg["api_key"], cfg["pod_id"])
        if not host:
            return "(pod not reachable)"
        out = subprocess.run(rp.ssh_base(host, port, ssh_mode) +
                             ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                              f"tail -c 200000 /var/log/vs-comfy.log"],
                             capture_output=True, text=True, timeout=40).stdout
    except Exception as e:  # noqa: BLE001
        return f"(could not read remote log: {str(e)[:200]})"
    _remote_log.update(at=now, text=out)
    return out
_ANSI = re.compile(r"\x1B\[[0-9;]*[mGKHF]")


@app.get("/api/logs/{name}")
def logs(name: str, lines: int = 200):
    """Tail a log file, collapsing tqdm carriage-returns so progress bars read cleanly."""
    if name == "remote":
        raw = remote_log(lines)
        out = [_ANSI.sub("", ln).rstrip() for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        out = [ln for ln in out if ln.strip()]
        return {"name": name, "text": "\n".join(out[-max(10, min(lines, 2000)):]), "size": len(raw)}
    fn = LOG_FILES.get(name)
    if not fn:
        raise HTTPException(404, "unknown log")
    path = os.path.join(ROOT, "logs", fn)
    if not os.path.exists(path):
        return {"name": name, "text": "(no log yet)", "size": 0}
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - 400_000))
        raw = f.read().decode("utf-8", "replace")
    out = []
    for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        ln = _ANSI.sub("", ln).rstrip()
        if ln.strip():
            out.append(ln)
    return {"name": name, "text": "\n".join(out[-max(10, min(lines, 2000)):]), "size": size}


@app.get("/api/jobs")
def get_jobs():
    return jobs.all_jobs()


@app.post("/api/jobs")
def post_job(body: dict):
    try:
        return jobs.submit(body.get("type", ""), body.get("params") or {})
    except (ValueError, KeyError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/jobs/{jid}/frames")
def job_frames(jid: str):
    """Frames finished so far in a running job — segments land here as they complete."""
    job = next((x for x in jobs.all_jobs() if x["id"] == jid), None)
    if not job or not job.get("tmpdir"):
        raise HTTPException(404)
    files = sorted(glob.glob(os.path.join(job["tmpdir"], "clip*", "*.png")))
    return {"count": len(files), "job": jid}


@app.get("/api/jobs/{jid}/frames/{n}")
def job_frame(jid: str, n: int, w: int = 260):
    job = next((x for x in jobs.all_jobs() if x["id"] == jid), None)
    if not job or not job.get("tmpdir"):
        raise HTTPException(404)
    files = sorted(glob.glob(os.path.join(job["tmpdir"], "clip*", "*.png")))
    if not files:
        raise HTTPException(404)
    import io
    from fastapi.responses import Response
    im = Image.open(files[max(0, min(len(files) - 1, n))]).convert("RGB")
    im.thumbnail((max(64, min(w, 1024)), 1024))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{jid}/preview")
def job_preview(jid: str):
    p = os.path.join(jobs.PREVIEW_DIR, os.path.basename(jid) + ".jpg")
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


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
