"""One-shot, verified pod bring-up. Run from the Mac:  .venv/bin/python remote/bringup.py

  1. start the stored pod, or rent a fresh one on the same volume if it can't start
  2. wait until the container is genuinely up (GraphQL runtime, not desiredStatus)
  3. stream remote/setup_vs.sh to it: own venv + pinned ComfyUI on the volume, port 8199
  4. verify through the ssh tunnel that H3 (+ references, + AddGuide) is really served
  5. on any failure: STOP the pod so nothing keeps billing, and say why

Exit code 0 only when the app can render on it right now.
"""
import json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import requests  # noqa: E402
import runpod    # noqa: E402
import comfy     # noqa: E402

S = os.path.join(ROOT, "data", "settings.json")
cfg = json.load(open(S))
k = cfg["runpod"]; key = k["api_key"]
H = {"Authorization": f"Bearer {key}"}
t0 = time.time()


def log(m):
    print(f"[{time.strftime('%H:%M:%S')} +{int(time.time()-t0):>4}s] {m}", flush=True)


def save():
    json.dump(cfg, open(S, "w"), indent=1)


def stop_pod(pid, why):
    try:
        requests.post(f"https://rest.runpod.io/v1/pods/{pid}/stop", headers=H, timeout=60)
        log(f"STOPPED pod {pid} — billing off. Reason: {why}")
    except Exception as e:  # noqa: BLE001
        log(f"could not stop pod {pid}: {e} — STOP IT IN THE RUNPOD CONSOLE")
    runpod.close_tunnel()


def fail(pid, why):
    stop_pod(pid, why)
    sys.exit(1)


# ---- 1. a running pod ----------------------------------------------------------
pid = k.get("pod_id") or ""
if pid:
    pod = requests.get(f"https://rest.runpod.io/v1/pods/{pid}", headers=H, timeout=60).json()
    if pod.get("desiredStatus") != "RUNNING":
        log(f"starting stored pod {pid}")
        r = requests.post(f"https://rest.runpod.io/v1/pods/{pid}/start", headers=H, timeout=90)
        if not r.ok:
            log(f"cannot start it ({r.text[:80]}) — releasing it and renting a fresh one")
            requests.delete(f"https://rest.runpod.io/v1/pods/{pid}", headers=H, timeout=60)
            pid = ""
if not pid:
    try:
        new = runpod.create_pod(key, k["gpu"], k["datacenter"], k["volume_id"])
    except runpod.RunPodError as e:
        log(f"no GPU available right now: {str(e)[:120]}")
        sys.exit(2)
    pid = new["id"]; k["pod_id"] = pid; save()
    log(f"rented {pid} at ${new.get('costPerHr')}/hr")

# ---- 2. container really up -----------------------------------------------------
log("waiting for the container")
host = None
for _ in range(120):
    host, port, mode = runpod.ssh_target(key, pid)
    if host:
        break
    time.sleep(10)
if not host:
    fail(pid, "container never came up")
log(f"container up at {host}:{port}")


def ssh(cmd, t=300):
    r = subprocess.run(runpod.ssh_base(host, port, mode) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=25", cmd],
                       capture_output=True, text=True, timeout=t)
    return (r.stdout + r.stderr).strip()


# ---- 3. isolated engine on the volume -------------------------------------------
log("installing/starting our own engine (first time: torch download, a few minutes)")
logf = os.path.join(ROOT, "logs", "setup_vs.log")
with open(os.path.join(ROOT, "remote", "setup_vs.sh")) as f, open(logf, "w") as out:
    p = subprocess.run(runpod.ssh_base(host, port, mode) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=25", f"bash -s {k.get('models') or 'h3'}"],
                       stdin=f, stdout=out, stderr=subprocess.STDOUT, timeout=2400)
txt = open(logf).read()
for line in txt.splitlines():
    if line.startswith(("==>", "   ", "!!", " ENGINE", "=====")):
        log("  " + line.strip())
if "ENGINE UP" not in txt:
    fail(pid, "engine did not come up on the pod (see logs/setup_vs.log)")

# ---- 4. verify through the tunnel exactly what the app will use -----------------
runpod.open_tunnel(host, port, mode)
eng = comfy.Engine(f"http://127.0.0.1:{runpod.LOCAL_PORT}", local=False)
for _ in range(20):
    if eng.alive(8):
        break
    time.sleep(3)
if not eng.alive(8):
    fail(pid, "tunnel up but engine not reachable through it")
unets = eng.unet_options()
h3 = [n for n in unets if "minimax_h3_fl2va" in n]
ref = [n for n in unets if "minimax_h3_ref2va" in n]
nodes = {n: eng.has_node(n) for n in ("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo", "MiniMaxH3AddGuide")}
dev = eng.stats()["devices"][0]
log(f"GPU {dev['name']} {dev['vram_total']/1e9:.0f} GB")
log(f"H3 model: {h3}  references model: {ref}")
log(f"nodes: {nodes}")
if not (h3 and nodes["MiniMaxH3ImageToVideo"]):
    fail(pid, "engine is up but does not serve H3")

# ---- 5. hand it to the app ------------------------------------------------------
cfg["remote"].update(enabled=True, url=f"http://127.0.0.1:{runpod.LOCAL_PORT}",
                     label=f"RunPod {dev['name'].split(':')[-1].strip()} · {k['datacenter']}")
save()
log("READY — H3" + (" + references" if ref else "") + (" + start/end-with-references" if nodes["MiniMaxH3AddGuide"] else ""))
log("the pod is RUNNING and billing; press Stop in the app sidebar when you're done")
