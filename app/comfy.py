"""Client for a ComfyUI engine — either the local one or a rented GPU.

Both are driven through the same Engine object. The only real difference is how
rendered frames come back: the local engine writes them to a directory we can read,
a remote one hands them over via HTTP. Either way run() returns local file paths.
"""
import json
import os
import shutil
import time
import uuid

import requests
import websocket

ENGINE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine")
LOCAL_URL = os.environ.get("ENGINE_URL", "http://127.0.0.1:8188")


class Cancelled(Exception):
    pass


class EngineError(Exception):
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


class Engine:
    def __init__(self, url: str = LOCAL_URL, token: str = "", local: bool = True, name: str = "local"):
        self.url = (url or LOCAL_URL).rstrip("/")
        self.token = (token or "").strip()
        self.local = local
        self.name = name

    # ---------------------------------------------------------------- basics
    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _ws_url(self, cid: str) -> str:
        base = self.url.replace("https://", "wss://").replace("http://", "ws://")
        u = f"{base}/ws?clientId={cid}"
        if self.token:
            u += f"&token={self.token}"  # for proxies that cannot read headers on upgrade
        return u

    def alive(self, timeout: int = 12) -> bool:
        """Health check. Generous timeout: a saturated engine answers slowly, and treating
        that as 'dead' used to make the watchdog spawn a duplicate process."""
        try:
            return requests.get(self.url + "/system_stats", headers=self.headers, timeout=timeout).ok
        except requests.RequestException:
            return False

    def stats(self) -> dict:
        r = requests.get(self.url + "/system_stats", headers=self.headers, timeout=20)
        r.raise_for_status()
        return r.json()

    def models(self, folder: str) -> list:
        """What weights does this engine actually have? Lets the UI show which
        quality tiers a remote box can serve."""
        try:
            r = requests.get(f"{self.url}/models/{folder}", headers=self.headers, timeout=20)
            return r.json() if r.ok else []
        except (requests.RequestException, ValueError):
            return []

    def object_info(self, node: str) -> dict:
        try:
            r = requests.get(f"{self.url}/object_info/{node}", headers=self.headers, timeout=20)
            return r.json() if r.ok else {}
        except (requests.RequestException, ValueError):
            return {}

    def has_node(self, node: str) -> bool:
        return bool(self.object_info(node))

    def loader_options(self, node: str, field: str) -> set:
        """What files can this loader node actually load? /models/<folder> only reports
        safetensors, so .gguf weights are invisible there — ask the node instead."""
        try:
            return set(self.object_info(node)[node]["input"]["required"][field][0])
        except (KeyError, IndexError, TypeError):
            return set()

    def unet_options(self) -> set:
        return self.loader_options("UNETLoader", "unet_name") | self.loader_options("UnetLoaderGGUF", "unet_name")

    def clip_options(self) -> set:
        return self.loader_options("CLIPLoader", "clip_name") | self.loader_options("CLIPLoaderGGUF", "clip_name")

    def upload_image(self, path: str) -> str:
        name = f"vs_{uuid.uuid4().hex[:10]}{os.path.splitext(path)[1] or '.png'}"
        with open(path, "rb") as f:
            r = requests.post(self.url + "/upload/image", headers=self.headers,
                              files={"image": (name, f)}, data={"overwrite": "true"}, timeout=300)
        r.raise_for_status()
        return r.json()["name"]

    def interrupt(self):
        try:
            requests.post(self.url + "/interrupt", headers=self.headers, timeout=5)
        except requests.RequestException:
            pass

    def free_memory(self):
        try:
            requests.post(self.url + "/free", headers=self.headers,
                          json={"unload_models": True, "free_memory": True}, timeout=10)
        except requests.RequestException:
            pass

    # ---------------------------------------------------------------- output
    def _collect(self, history: dict, dest_dir: str) -> list[str]:
        """Bring rendered frames into dest_dir, from disk (local) or over HTTP (remote)."""
        os.makedirs(dest_dir, exist_ok=True)
        items = []
        for out in history.get("outputs", {}).values():
            items += out.get("images", [])
        items.sort(key=lambda im: im["filename"])
        paths = []
        for i, im in enumerate(items):
            dst = os.path.join(dest_dir, f"{i:06d}.png")
            if self.local:
                src = os.path.join(ENGINE_DIR, "output", im.get("subfolder", ""), im["filename"])
                shutil.move(src, dst)
            else:
                r = requests.get(self.url + "/view", headers=self.headers, timeout=300, stream=True,
                                 params={"filename": im["filename"], "subfolder": im.get("subfolder", ""),
                                         "type": im.get("type", "output")})
                r.raise_for_status()
                with open(dst, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            paths.append(dst)
        return paths

    # ---------------------------------------------------------------- run
    def run(self, graph: dict, dest_dir: str, on_progress=None, cancelled=lambda: False,
            timeout=12 * 3600, on_preview=None) -> list[str]:
        """Queue a graph, block until it finishes, return local paths to the frames."""
        cid = uuid.uuid4().hex
        ws = websocket.WebSocket()
        ws.connect(self._ws_url(cid), timeout=30,
                   header=[f"{k}: {v}" for k, v in self.headers.items()])
        ws.settimeout(2)
        r = requests.post(self.url + "/prompt", headers=self.headers,
                          json={"prompt": graph, "client_id": cid}, timeout=60)
        if not r.ok:
            ws.close()
            raise EngineError(f"engine rejected workflow: {r.text[:800]}")
        pid = r.json()["prompt_id"]
        t0 = time.time()
        samplers = sorted(k for k in graph if k.startswith("ks"))
        try:
            while True:
                if cancelled():
                    self.interrupt()
                    raise Cancelled()
                if time.time() - t0 > timeout:
                    self.interrupt()
                    raise EngineError("timed out")
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    h = self._history(pid)
                    if h.get("status", {}).get("completed"):
                        break
                    if h.get("status", {}).get("status_str") == "error":
                        raise EngineError(_err(h))
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
        h = self._history(pid)
        if h.get("status", {}).get("status_str") == "error":
            raise EngineError(_err(h))
        files = self._collect(h, dest_dir)
        if not files:
            raise EngineError("engine produced no frames")
        return files

    def _history(self, pid: str) -> dict:
        try:
            return requests.get(f"{self.url}/history/{pid}", headers=self.headers,
                                timeout=60).json().get(pid, {})
        except (requests.RequestException, ValueError):
            return {}


def _err(h):
    for m in h.get("status", {}).get("messages", []):
        if m[0] == "execution_error":
            return m[1].get("exception_message", "engine error")[:800]
    return "engine error"


# ------------------------------------------------------------------ local engine
local_engine = Engine(LOCAL_URL, local=True, name="local")


def alive(timeout: int = 12) -> bool:
    return local_engine.alive(timeout)


def port_busy(port: int = 8188) -> bool:
    """Is something listening on the local engine port at all?"""
    import socket
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def interrupt():
    local_engine.interrupt()
