"""Persisted app settings — currently just the optional remote GPU."""
import json
import os
import threading

import comfy
import media

FILE = os.path.join(media.DATA, "settings.json")
_lock = threading.Lock()

DEFAULT = {
    "remote": {
        "enabled": False,      # master switch; when off, nothing can be sent off this machine
        "url": "",             # e.g. http://127.0.0.1:8189 (ssh tunnel) or https://<pod>-8188.proxy.runpod.net
        "token": "",           # optional bearer token, if the pod runs an auth proxy
        "label": "",           # free text, e.g. "RunPod H100 / Canada"
        "default_for_new": False,
    },
    "runpod": {
        "api_key": "",         # stays on this machine; never sent to the browser
        "pod_id": "",
        "gpu": "NVIDIA A100 80GB PCIe",
        "datacenter": "CA-MTL-3",
        "volume_id": "",
        "models": "full",      # 'full' = unquantised Wan fp16, 'h3' = MiniMax H3
    },
}


def load() -> dict:
    try:
        with open(FILE) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    out = {k: {**v, **(s.get(k) or {})} if isinstance(v, dict) else s.get(k, v) for k, v in DEFAULT.items()}
    return out


def save(patch: dict) -> dict:
    with _lock:
        s = load()
        for k, v in patch.items():
            if k in DEFAULT:
                s[k] = {**s[k], **v} if isinstance(DEFAULT[k], dict) else v
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, FILE)
        return s


def remote_engine(s: dict = None) -> comfy.Engine | None:
    s = s or load()
    r = s["remote"]
    if not (r.get("enabled") and r.get("url")):
        return None
    return comfy.Engine(r["url"], r.get("token", ""), local=False, name=r.get("label") or "remote")


def engine_for(compute: str) -> comfy.Engine:
    """compute is 'remote' or anything else (local). Falls back to local if remote is off,
    so a stale job parameter can never silently send work off the machine."""
    if compute == "remote":
        e = remote_engine()
        if e is None:
            raise ValueError("remote GPU is not configured or not enabled")
        return e
    return comfy.local_engine


def public(s: dict = None) -> dict:
    """Settings safe to hand to the browser — never echo secrets back."""
    s = s or load()
    r = dict(s["remote"])
    r["token"] = "" if not r.get("token") else "********"
    rp = dict(s["runpod"])
    rp["api_key"] = "********" if rp.get("api_key") else ""
    return {"remote": r, "runpod": rp}
