"""RunPod control: rent a GPU, reach it over an SSH tunnel, hand it back.

Design notes
- The API key lives in data/settings.json on this machine and is never echoed to the UI.
- We never expose ComfyUI to the internet. Only port 22 is opened; the engine stays on
  the pod's loopback and we reach it through `ssh -N -L`. RunPod's HTTP proxy is both
  unauthenticated AND capped at ~100s by Cloudflare, which would sever our progress
  websocket mid-render.
- A dedicated keypair is generated for this app, so a pod never sees a personal key.
"""
import json
import os
import shutil
import signal
import subprocess
import time

import requests

import media

REST = "https://rest.runpod.io/v1"
GRAPHQL = "https://api.runpod.io/graphql"
SSH_DIR = os.path.join(media.DATA, "ssh")
KEY = os.path.join(SSH_DIR, "id_ed25519")
LOCAL_PORT = 8189                      # tunnel entrance on this Mac

# Regions outside the US / EU / UK / South Korea that support network volumes.
# (RunPod has no UK or South Korea datacentre at all; EUR-* is Europe-but-not-EU.)
PREFERRED_DCS = ["CA-MTL-3", "CA-MTL-4", "AP-JP-1", "EUR-IS-1", "EUR-IS-3", "EUR-NO-1", "EUR-NO-2"]

BIG_GPUS = ["NVIDIA A100 80GB PCIe", "NVIDIA A100-SXM4-80GB", "NVIDIA H100 PCIe",
            "NVIDIA H100 80GB HBM3", "NVIDIA H100 NVL", "NVIDIA H200",
            "NVIDIA RTX PRO 6000 Blackwell Server Edition"]


class RunPodError(Exception):
    pass


# ------------------------------------------------------------------ ssh key
def ensure_key() -> str:
    """Create a dedicated keypair for this app if we don't have one. Returns the public key."""
    os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    if not os.path.exists(KEY):
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", KEY, "-C", "video-studio"],
                       check=True, capture_output=True)
        os.chmod(KEY, 0o600)
    with open(KEY + ".pub") as f:
        return f.read().strip()


# ------------------------------------------------------------------ api
def _req(method: str, path: str, key: str, **kw) -> dict:
    if not key:
        raise RunPodError("no RunPod API key set")
    r = requests.request(method, REST + path, headers={"Authorization": f"Bearer {key}"},
                         timeout=kw.pop("timeout", 90), **kw)
    if r.status_code == 401:
        raise RunPodError("RunPod rejected the API key")
    if not r.ok:
        raise RunPodError(f"RunPod {method} {path} -> {r.status_code}: {r.text[:400]}")
    return r.json() if r.content else {}


def catalog() -> list:
    """GPU types and live prices. This GraphQL query needs no key, so the picker
    can show real prices before the user has entered one."""
    q = "{ gpuTypes { id displayName memoryInGb securePrice communityPrice } }"
    try:
        r = requests.post(GRAPHQL, json={"query": q}, timeout=30)
        gpus = r.json()["data"]["gpuTypes"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        raise RunPodError(f"could not read RunPod catalog: {str(e)[:200]}")
    out = []
    for g in gpus:
        if g["id"] in BIG_GPUS and (g.get("securePrice") or g.get("communityPrice")):
            out.append({"id": g["id"], "name": g.get("displayName") or g["id"],
                        "vram_gb": g.get("memoryInGb"),
                        "usd_hr": g.get("securePrice") or g.get("communityPrice")})
    return sorted(out, key=lambda g: g["usd_hr"] or 999)


def volumes(key: str) -> list:
    v = _req("GET", "/networkvolumes", key)
    return v if isinstance(v, list) else v.get("data", [])


def create_volume(key: str, name: str, size_gb: int, dc: str) -> dict:
    return _req("POST", "/networkvolumes", key,
                json={"name": name, "size": int(size_gb), "dataCenterId": dc})


# Acceptable substitutes, best-value first. RunPod picks whichever has stock, which
# matters because a single pinned type is often sold out in one datacentre.
FALLBACK_GPUS = ["NVIDIA A100-SXM4-80GB", "NVIDIA A100 80GB PCIe",
                 "NVIDIA RTX PRO 6000 Blackwell Server Edition", "NVIDIA H100 PCIe",
                 "NVIDIA H100 80GB HBM3", "NVIDIA H100 NVL", "NVIDIA H200"]


def gpu_choices(preferred: str) -> list:
    return [preferred] + [g for g in FALLBACK_GPUS if g != preferred]


def create_pod(key: str, gpu: str, dc: str, volume_id: str = "", disk_gb: int = 60) -> dict:
    body = {
        "name": "video-studio",
        "imageName": "runpod/comfyui:latest",
        "gpuTypeIds": gpu_choices(gpu),
        "gpuTypePriority": "availability",
        "gpuCount": 1,
        "cloudType": "SECURE",            # network volumes and a public IP need Secure Cloud
        "computeType": "GPU",
        "containerDiskInGb": int(disk_gb),
        "ports": ["22/tcp"],              # ComfyUI is NOT exposed; we tunnel to it
        "supportPublicIp": True,
        "interruptible": False,
        "env": {"PUBLIC_KEY": ensure_key()},
    }
    if dc:
        body["dataCenterIds"] = [dc]
        body["dataCenterPriority"] = "custom"
    if volume_id:
        body["networkVolumeId"] = volume_id
        body["volumeMountPath"] = "/workspace"
    return _req("POST", "/pods", key, json=body)


def get_pod(key: str, pod_id: str) -> dict:
    return _req("GET", f"/pods/{pod_id}", key)


def stop_pod(key: str, pod_id: str) -> dict:
    return _req("POST", f"/pods/{pod_id}/stop", key)


def start_pod(key: str, pod_id: str) -> dict:
    return _req("POST", f"/pods/{pod_id}/start", key)


def terminate_pod(key: str, pod_id: str) -> dict:
    return _req("DELETE", f"/pods/{pod_id}", key)


def runtime(key: str, pod_id: str) -> dict:
    """Actual container state. REST's `desiredStatus: RUNNING` only means 'scheduled';
    `runtime` stays null until the image is pulled and the container is really up,
    and that is also when port mappings appear."""
    q = ("query($id:String!){ pod(input:{podId:$id}){ id desiredStatus machine{podHostId} "
         "runtime{ uptimeInSeconds ports{ ip isIpPublic privatePort publicPort type } } } }")
    try:
        r = requests.post(GRAPHQL, params={"api_key": key}, json={"query": q, "variables": {"id": pod_id}}, timeout=45)
        return (r.json().get("data") or {}).get("pod") or {}
    except (requests.RequestException, ValueError):
        return {}


def ssh_target(key: str, pod_id: str):
    """(host, port, mode). mode 'direct' = root@ip -p port; 'proxy' = <podHostId>@ssh.runpod.io."""
    p = runtime(key, pod_id)
    rt = p.get("runtime") or {}
    for prt in rt.get("ports") or []:
        if prt.get("privatePort") == 22 and prt.get("ip"):
            return prt["ip"], prt.get("publicPort"), "direct"
    host = (p.get("machine") or {}).get("podHostId")
    if host and rt:
        return host, None, "proxy"
    return None, None, None


def ready(key: str, pod_id: str) -> bool:
    return bool((runtime(key, pod_id).get("runtime") or {}).get("uptimeInSeconds") is not None)


# ------------------------------------------------------------------ tunnel
_tunnel = {"proc": None, "pod": None, "since": None}


def tunnel_up() -> bool:
    p = _tunnel["proc"]
    if p and p.poll() is None:
        return True
    # the ssh process outlives a server restart (own session), so also just ask the port
    import socket
    with socket.socket() as sk:
        sk.settimeout(1)
        return sk.connect_ex(("127.0.0.1", LOCAL_PORT)) == 0


def ssh_base(host: str, port, mode: str) -> list:
    """Common ssh args for either connection style."""
    cmd = ["ssh", "-i", KEY,
           "-o", "StrictHostKeyChecking=accept-new",
           "-o", "UserKnownHostsFile=" + os.path.join(SSH_DIR, "known_hosts"),
           "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]
    if mode == "direct":
        cmd += [f"root@{host}", "-p", str(port)]
    else:                       # RunPod's ssh proxy: user is the podHostId
        cmd += [f"{host}@ssh.runpod.io"]
    return cmd


REMOTE_ENGINE_PORT = 8199   # our own engine on the pod; the image's ComfyUI keeps 8188


def open_tunnel(host: str, port, mode: str = "direct", remote_port: int = REMOTE_ENGINE_PORT, local_port: int = LOCAL_PORT):
    """ssh -N -L local:127.0.0.1:remote. Nothing about ComfyUI touches the internet."""
    close_tunnel()
    if not shutil.which("ssh"):
        raise RunPodError("ssh not found on this Mac")
    base = ssh_base(host, port, mode)
    cmd = base[:1] + ["-N", "-o", "ExitOnForwardFailure=yes",
                      "-L", f"{local_port}:127.0.0.1:{remote_port}"] + base[1:]
    log = open(os.path.join(media.ROOT, "logs", "tunnel.log"), "a")
    _tunnel["proc"] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
    _tunnel["since"] = time.time()
    return f"http://127.0.0.1:{local_port}"


def close_tunnel():
    p = _tunnel["proc"]
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            p.terminate()
    _tunnel["proc"] = None
    _tunnel["since"] = None


def tunnel_status() -> dict:
    return {"up": tunnel_up(), "pod": _tunnel["pod"],
            "since": _tunnel["since"], "url": f"http://127.0.0.1:{LOCAL_PORT}"}


def ssh_run(cmd: str, timeout: int = 120) -> str:
    """Best-effort command on the current pod, using the stored key. Returns '' on failure."""
    try:
        import json as _j
        cfg = _j.load(open(os.path.join(media.DATA, "settings.json")))["runpod"]
        host, port, mode = ssh_target(cfg["api_key"], cfg["pod_id"])
        if not host:
            return ""
        p = subprocess.run(ssh_base(host, port, mode) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (p.stdout + p.stderr).strip()
    except Exception:  # noqa: BLE001
        return ""


def clean_outputs(prefix: str = "") -> str:
    """Remote renders are downloaded but ComfyUI keeps its copy, so the pod's volume
    fills up one clip at a time. Delete what we have already fetched."""
    target = f"/workspace/vs/ComfyUI/output/vs/{prefix}*" if prefix else "/workspace/vs/ComfyUI/output/vs"
    return ssh_run(f'rm -rf {target} 2>/dev/null; du -sh /workspace/vs/ComfyUI/output 2>/dev/null')


def set_pod(pod_id: str):
    _tunnel["pod"] = pod_id
