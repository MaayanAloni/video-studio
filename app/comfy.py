"""Minimal client for the local ComfyUI engine."""
import json
import os
import time
import uuid

import requests
import websocket

ENGINE = os.environ.get("ENGINE_URL", "http://127.0.0.1:8188")
ENGINE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine")


class Cancelled(Exception):
    pass


class EngineError(Exception):
    pass


def alive(timeout: int = 12) -> bool:
    """Health check. Generous timeout: a saturated engine answers slowly, and treating
    that as 'dead' used to make the watchdog spawn a duplicate process."""
    try:
        return requests.get(ENGINE + "/system_stats", timeout=timeout).ok
    except requests.RequestException:
        return False


def port_busy(port: int = 8188) -> bool:
    """Is something listening on the engine port at all?"""
    import socket
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def upload_image(path: str) -> str:
    name = f"vs_{uuid.uuid4().hex[:10]}{os.path.splitext(path)[1] or '.png'}"
    with open(path, "rb") as f:
        r = requests.post(ENGINE + "/upload/image", files={"image": (name, f)}, data={"overwrite": "true"}, timeout=60)
    r.raise_for_status()
    return r.json()["name"]


def interrupt():
    try:
        requests.post(ENGINE + "/interrupt", timeout=5)
    except requests.RequestException:
        pass


def free_memory():
    try:
        requests.post(ENGINE + "/free", json={"unload_models": True, "free_memory": True}, timeout=10)
    except requests.RequestException:
        pass


def _extract_image(buf: bytes):
    """ComfyUI preview frames arrive as binary ws messages: 4-byte event id, 4-byte
    image format, then the image. Scanning for the magic bytes instead of assuming
    fixed offsets keeps this working across the several header layouts it uses."""
    for magic in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"):
        i = buf.find(magic, 0, 1024)
        if i >= 0:
            return buf[i:]
    return None


def run(graph: dict, on_progress=None, cancelled=lambda: False, timeout=6 * 3600, on_preview=None) -> list[str]:
    """Queue a graph, block until done, return absolute paths of saved images (sorted)."""
    cid = uuid.uuid4().hex
    ws = websocket.WebSocket()
    ws.connect(ENGINE.replace("http", "ws") + f"/ws?clientId={cid}", timeout=10)
    ws.settimeout(2)
    r = requests.post(ENGINE + "/prompt", json={"prompt": graph, "client_id": cid}, timeout=30)
    if not r.ok:
        raise EngineError(f"engine rejected workflow: {r.text[:800]}")
    pid = r.json()["prompt_id"]
    t0 = time.time()
    samplers = sorted(k for k in graph if k.startswith("ks"))
    try:
        while True:
            if cancelled():
                interrupt()
                raise Cancelled()
            if time.time() - t0 > timeout:
                interrupt()
                raise EngineError("timed out")
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                # ws can miss the final event; fall back to history
                h = requests.get(f"{ENGINE}/history/{pid}", timeout=10).json()
                if pid in h and h[pid].get("status", {}).get("completed"):
                    break
                if pid in h and h[pid].get("status", {}).get("status_str") == "error":
                    raise EngineError(_err(h[pid]))
                continue
            if not isinstance(msg, str):
                if on_preview:                       # live latent preview during sampling
                    img = _extract_image(msg)
                    if img:
                        on_preview(img)
                continue
            m = json.loads(msg)
            d = m.get("data", {})
            if d.get("prompt_id") not in (None, pid):
                continue
            if m["type"] == "progress" and on_progress:
                node = d.get("node")
                frac = d["value"] / max(1, d["max"])
                if node in samplers and len(samplers) > 1:
                    frac = (samplers.index(node) + frac) / len(samplers)
                on_progress(frac if node in samplers else None, node)
            elif m["type"] == "executing" and on_progress and d.get("node"):
                on_progress(None, d["node"])
            elif m["type"] == "execution_error":
                raise EngineError(d.get("exception_message", "engine error")[:800])
            elif m["type"] == "execution_interrupted":
                raise Cancelled()
            elif m["type"] == "executing" and d.get("node") is None and d.get("prompt_id") == pid:
                break
    finally:
        ws.close()
    h = requests.get(f"{ENGINE}/history/{pid}", timeout=30).json().get(pid, {})
    if h.get("status", {}).get("status_str") == "error":
        raise EngineError(_err(h))
    files = []
    for out in h.get("outputs", {}).values():
        for im in out.get("images", []):
            files.append(os.path.join(ENGINE_DIR, "output", im.get("subfolder", ""), im["filename"]))
    if not files:
        raise EngineError("engine produced no frames")
    return sorted(files)


def _err(h):
    for m in h.get("status", {}).get("messages", []):
        if m[0] == "execution_error":
            return m[1].get("exception_message", "engine error")[:800]
    return "engine error"
