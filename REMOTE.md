# Renting a GPU

Optional. Everything works without this — the rented GPU only removes the Apple
bottleneck and unlocks the **Ultra** tier (unquantised weights), which cannot run
on 24 GB of unified memory.

You pay RunPod for **compute only**. The models are open weights we download onto
the pod. Nobody bills you for the model itself.

---

## Why an SSH tunnel and not the public URL

RunPod gives every pod an HTTPS address (`https://<pod>-8188.proxy.runpod.net`).
We deliberately do **not** use it:

- **It has no authentication.** RunPod's own docs say the pod ID is the only thing
  protecting it. ComfyUI has no password of any kind and `POST /prompt` executes
  arbitrary Python — anyone who finds the URL owns the box. A cryptomining campaign
  hit 1,000+ exposed ComfyUI instances in April 2026.
- **It would break us anyway.** The proxy runs through Cloudflare, which cuts
  connections at ~100 seconds. Our progress and live-preview websocket stays open for
  the whole render.

So the app opens **port 22 only**, leaves ComfyUI on the pod's loopback where the
internet cannot see it, and tunnels to it. The app generates its own keypair in
`data/ssh/` — your personal SSH key is never sent to a rented machine.

---

## One-time setup

### 1. Account and credit
Sign up at [runpod.io](https://www.runpod.io) and add credit. You need at least one
hour's worth to deploy. Nothing is charged until you rent a pod.

### 2. API key
Go to **https://www.console.runpod.io/user/settings** → **API Keys** →
**Create API Key**. Permission **All** (it needs to create and destroy pods).

**Copy it immediately — RunPod never shows it again.**

### 3. Give it to the app
Video Studio → **Settings** → *Rent a GPU from RunPod* → paste into **RunPod API key**.

It is stored in `data/settings.json` on this Mac, is never sent to the browser, and
is never displayed again — the field shows `********` afterwards.

### 4. Choose GPU and region

Defaults are **A100 80GB PCIe** in **CA-MTL-3 (Canada)**. Prices are pulled live.

| GPU | VRAM | ~$/hr |
|---|---|---|
| A100 80GB PCIe | 80 | 1.59 |
| RTX PRO 6000 Blackwell | 96 | 2.09 |
| H100 PCIe | 80 | 2.89 |
| H200 | 141 | 4.59 |

A100 80GB is the sweet spot: everything we run fits, and it's the cheapest 80 GB card.

**On region.** The app only offers datacentres outside the US, EU, UK and South Korea,
because some model licences exclude those territories. RunPod has no UK or South Korea
datacentre at all. `CA-MTL-3` is first because it is the only non-EU region with both
network-volume support and 80 GB stock. `AP-JP-1` (Japan) is the runner-up.
Note `EUR-IS-*` and `EUR-NO-*` are Iceland and Norway — outside the EU, but inside the
EEA, so still GDPR territory.

---

## Each session

| Step | What happens | Billing |
|---|---|---|
| **① Rent GPU** | Creates the pod and a 300 GB model volume | **starts now** |
| wait | First boot can take several minutes, occasionally up to 30 | running |
| **② Install models** | Downloads weights onto the pod over SSH | running |
| **③ Connect** | Opens the SSH tunnel; Ultra appears in Create | running |
| render | Pick **Ultra**, set *Render on* → Rented GPU | running |
| **Stop** | Compute billing stops; the model volume is kept | **stops** |

Watch the download in **Logs → provision**.

**Step ② only ever happens once.** The models live on the network volume, which
survives stopping *and* terminating the pod. Next session you rent, connect, and go.

---

## What it costs

- **Compute:** billed per second while the pod runs. A100 80GB ≈ $1.59/hr.
- **Model volume:** 300 GB ≈ **$21/month**, billed whether or not the pod is running.
  This is the price of not re-downloading 120 GB every session.
- **A 5-second Ultra clip:** I estimate 15–25 minutes on an A100, so roughly
  **$0.40–0.65**. That is an extrapolation, not a measurement — the first real render
  will tell us.

**Stop** the pod when you finish. It is the difference between $21/month and $1,150/month.

> The app will never rent a pod on its own. Every paid action is behind a button and
> a confirmation.

---

## Models on the pod

`remote/setup_pod.sh` takes a mode:

- **`full`** (default, ~120 GB) — Wan 2.2 at **fp16, unquantised**: 28.58 GB per expert
  plus an 11.37 GB fp16 text encoder. This is what **Ultra** uses. Apache 2.0, so the
  output is yours with no strings.
- **`fast`** (~40 GB) — Q8 quantised. Good, and enough for Standard/Pro/Master.

### MiniMax H3 — read this before choosing it

H3 is the strongest open-weights video model available (4th overall on the blind
preference leaderboard, ahead of Veo 3.1 and Sora 2) and it generates synchronised
audio in the same pass. But:

- **Commercial use of its output requires a licence sold through Comfy.** Its weights
  are downloadable; using the results commercially is not free. That is precisely the
  "paying a company for the model" you wanted to avoid. For private, non-commercial
  use it costs nothing.
- **Its licence excludes the US, EU, UK and South Korea** — pick the region accordingly.
- Full BF16 is ~118 GB of weights and does not fit one 80 GB card. The practical build
  is the pruned int8 DiT (20.97 GB) plus the NVFP4 text encoder (15.69 GB) — about
  42.5 GB, which is what the official templates ship.

**Wan 2.2 at fp16 is the recommendation** unless you specifically want H3's audio: it
is Apache 2.0, unrestricted, unfiltered, has the largest LoRA ecosystem, and satisfies
your "compute only" rule exactly.

---

## If something goes wrong

- **Tunnel won't open** — the pod has no public IP yet. Wait and press Connect again.
- **Connected but the engine doesn't answer** — ComfyUI is still starting. First boot
  is slow; watch **Logs → tunnel** and **Logs → provision**.
- **Ultra is greyed out** — it needs *Render on → Rented GPU* and the fp16 weights
  present. Press **Test connection** in Settings to see what the pod actually has.
- **Worried about cost** — the RunPod console shows live spend. Terminate deletes the
  pod for good; the model volume survives and must be deleted separately in the console.
