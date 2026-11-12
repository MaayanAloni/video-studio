# Running Video Studio on your own computer — start to finish

Two parts. **Part 1** gets the studio running on your Mac, generating video locally for free.
**Part 2** is optional: renting a big NVIDIA GPU by the hour so renders take minutes instead of
tens of minutes, and so the top-quality tiers (Ultra, MiniMax H3 with audio) become available.

Nothing in this repo contains anyone's videos, API keys or SSH keys. Everything you make
stays in your own `data/` folder, which git ignores.

---

## Part 1 — local install (Mac with Apple Silicon)

### What you need
- A Mac with an **M1 or newer** chip. 24 GB of memory is comfortable; 16 GB works for the Draft
  and Standard tiers. Intel Macs, Windows and Linux are not supported by the local engine
  (it runs on Apple's Metal GPU). On those machines you could still use the app purely with a
  rented GPU, but that path is untested.
- **~65 GB of free disk** (~115 GB if you also want the Master tier's higher-precision weights).
- **Homebrew** — the Mac package manager. If `brew --version` says "command not found", install
  it with the one-line command at https://brew.sh and reopen your terminal.
- A **GitHub account** that has been given access to this repository.

### Steps

1. **Get the code.**

       git clone https://github.com/MaayanAloni/video-studio.git
       cd video-studio

   If GitHub asks you to sign in, use a personal access token or `gh auth login` first.

2. **Run the installer.** It installs ffmpeg and Python 3.12 via Homebrew, creates a Python
   environment, downloads the ComfyUI engine at the exact version this app was tested with,
   installs PyTorch for Metal, and downloads about 55 GB of model weights from Hugging Face.

       ./setup.sh

   Optionally add `--hq` to also fetch the Q6_K weights the Master tier prefers (+48 GB).
   The download is the slow part: 30–90 minutes depending on your connection. **If it is
   interrupted, just run `./setup.sh` again.** It skips anything already downloaded.

3. **Start the studio.**

       ./start.sh

   It opens http://127.0.0.1:7860 in your browser. The engine takes about 20 seconds to warm up
   the first time; the status dot in the top bar turns green when it is ready.

4. **Make your first video.** In **Create**, type a prompt, pick **Draft** quality and **360p**,
   and press Generate. Draft at 360p is the fast way to check that everything works
   (a couple of minutes). Then move up:

   | Tier | What it is | 2 s clip at 480p on an M4 Pro |
   |---|---|---|
   | Draft | Wan 2.2 5B, 12 steps | ~5 min |
   | Standard | Wan 2.2 14B, 4 steps (distilled) | ~20 min |
   | Pro | 14B, 8 steps | ~40 min |
   | Master | 14B, 20 steps, real CFG | hours |

   Longer videos are rendered in 5-second passes and stitched losslessly. The **Editor** tab
   lets you extend a video, regenerate a marked range, trim, join, reverse, interpolate and
   upscale. Every operation creates a new version; the version dropdown is your undo.

5. **Stop it** when you are done: `./stop.sh`. Logs: `./logs.sh`.

### Where things live

| What | Where | In git? |
|---|---|---|
| App code | `app/`, `remote/`, scripts | yes |
| ComfyUI engine + models (~100 GB) | `engine/` | no, built by `setup.sh` |
| Your videos, uploads, settings | `data/` | no |
| Logs | `logs/` | no |

---

## Part 2 — renting a GPU (optional)

The app can rent an NVIDIA GPU from **RunPod** (https://www.runpod.io), install the models on
it, and send renders there. You pay RunPod for compute time only; the models are free open
weights. The app never rents anything on its own: every paid action is behind a button and a
confirmation dialog. `REMOTE.md` has the design background and licensing notes.

### 2.1 One-time: RunPod account and API key

1. Sign up at https://www.runpod.io and add credit (**$10–20** is plenty to start). Nothing is
   charged until you rent a machine.
2. Open https://www.console.runpod.io/user/settings → **API Keys** → **Create API Key**.
   Give it **All** permissions (the app needs to create, stop and delete pods and volumes).
3. **Copy the key immediately.** RunPod shows it only once.
4. In Video Studio open **Settings** → *Rented GPU* → paste the key → **Save key**.
   It is stored in `data/settings.json` on your Mac only and is never shown again.

### 2.2 Each session

1. **Settings → Rented GPU**: choose a **GPU** and a **region**, then press
   **Rent this GPU and set it up**, and confirm.
   - Default and recommended: **A100 80GB** in **Canada (CA-MTL-3)**. Live prices are shown;
     around **$1.60/hour**, billed by the second.
   - The region list deliberately excludes the US, EU, UK and South Korea because the MiniMax
     H3 licence excludes those territories. Canada and Japan are the good choices.
2. **Wait.** The status line under the button walks through: creating model volume → renting
   GPU → waiting for the machine to boot → connecting → installing models → ready.
   - **First time ever: 20–60 minutes**, most of it downloading ~66 GB of models onto a 100 GB
     network volume that RunPod keeps for you.
   - **Every later time: 2–5 minutes.** The volume survives stopping and even deleting the pod,
     so models are downloaded once.
   - It keeps running server-side; you can close the browser tab.
3. When it says **ready**, go to **Create**. Set **Render on → Rented GPU**. The remote-only
   tiers appear:
   - **H3 Turbo** — MiniMax H3, 6 steps, generates synchronised audio. A few minutes per clip.
   - **H3 Max** — MiniMax H3, 20 steps, best quality available.
   - **Ultra** — only if the pod was set up with the `full` Wan fp16 models (see below).
   You can also add up to 9 **reference images** with H3 to keep a subject, style or identity
   consistent.
4. **When you finish, press "Stop — end charges"** in the badge in the left sidebar (it shows
   the running cost). This is the single most important step. A forgotten A100 costs about
   **$38 a day**.

### 2.3 Costs

| Item | Price |
|---|---|
| A100 80GB compute | ~$1.60/hour, per second, only while running |
| Model volume (100 GB) | ~$7/month, whether or not a pod is running |
| One 5 s H3 Turbo clip | a few cents of compute |

To stop the monthly charge entirely, delete the network volume in the RunPod console
(Storage → Network Volumes). You will re-download the models next time.

### 2.4 Choosing which models the pod installs

The one-click button installs **MiniMax H3** (`h3`, ~66 GB). To get Wan 2.2 at full fp16
precision instead or in addition, edit `data/settings.json` before pressing Rent and set
`"models"` under `"runpod"` to one of:

| Value | Size | Gives you |
|---|---|---|
| `h3` (default) | ~66 GB | H3 Turbo, H3 Max, with audio and reference images |
| `full` | ~120 GB | Ultra (Wan 2.2 14B unquantised) |
| `fast` | ~40 GB | Standard/Pro/Master on the rented card, Q8 weights |
| `full+h3` | ~163 GB | everything |

Also raise `"volume_gb"` to fit (160 for `full`, 200 for `full+h3`). The volume size is fixed
at creation, so decide before the first rent.

**Licensing note.** Wan 2.2 is Apache-2.0: its output is yours with no strings. MiniMax H3's
weights are free to download, but **commercial use of its output requires a licence** sold
through Comfy, and its licence excludes the US, EU, UK and South Korea. For private,
non-commercial use it costs nothing.

### 2.5 Advanced: command-line bring-up

`.venv/bin/python remote/bringup.py` does the same as the button from a terminal, with
verbose logging, and **stops the pod automatically if anything fails** so nothing keeps
billing. Useful when debugging.

---

## Troubleshooting

| Symptom | What to do |
|---|---|
| `./setup.sh` dies mid-download | Run it again; it resumes. |
| "PyTorch cannot see the Metal GPU" | Update macOS to a recent version. |
| Status dot stays red after `./start.sh` | `./logs.sh engine` shows the engine log; the usual cause is a half-downloaded model file. Delete it and re-run `./setup.sh`. |
| Renders are very slow or the Mac swaps | Close other heavy apps; the 14B model wants ~16 GB. Use 360p to iterate, then re-run the same seed at 480p/720p. |
| Rented GPU: "no GPU available" | That GPU type is sold out in that region. Try another region (Japan) or GPU. The app already falls back across several 80 GB cards. |
| Rented GPU: stuck at "waiting for the machine to boot" | First boots can take up to 30 minutes while RunPod pulls the image. If it exceeds that, press Stop and try again. |
| Rented GPU: "models installed but the engine is not offering h3" | Open **Logs → provision** in the app. A model download failed; press Rent again, it resumes. |
| Tunnel drops mid-render | The app re-dials automatically every 10 s. If the pod itself restarted, press Rent again. |
| Worried about money | The RunPod console (https://www.console.runpod.io/pods) shows every running pod and live spend. **Stop** or **Terminate** there works too. |

## Tests

    .venv/bin/python tests/logic_test.py      # frame accounting, GPU mocked
    .venv/bin/python tests/api_ops_test.py    # tools + validation, needs the server running
    .venv/bin/python -m playwright install chromium && .venv/bin/python tests/gui_test.py
