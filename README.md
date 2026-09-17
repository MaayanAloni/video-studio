# Video Studio

Local, offline video generation studio for Apple Silicon. Nothing leaves this machine; there is no
prompt filter, account, or per-video cost. Models: **Wan 2.2** (Apache-2.0 — outputs are yours).

    ./start.sh     # opens http://127.0.0.1:7860
    ./stop.sh

## What's inside
| Piece | Path | Notes |
|---|---|---|
| Engine | `engine/` | ComfyUI + ComfyUI-GGUF, runs on Metal (MPS), bound to 127.0.0.1 only |
| Models | `engine/models/` | Wan2.2 I2V-A14B (GGUF Q4_K_M, high+low noise experts), Wan2.2 TI2V-5B, umT5-XXL Q8, lightx2v 4-step LoRAs |
| Server | `app/server.py` | FastAPI; job queue, engine watchdog, media API |
| Jobs | `app/jobs.py` | generate / extend / edit_range / trim / join / reverse / interpolate / upscale |
| Workflows | `app/workflows.py` | the single "render one clip" graph everything is composed from |
| GUI | `app/static/` | vanilla JS, no build step |
| Data | `data/videos/<id>/` | `video.mp4`, lossless `frames/*.png`, `meta.json` |

## How the hard parts work
- **Long videos**: the model renders ≤5 s per pass. Longer requests are split into passes; each pass starts on
  the previous pass's last frame (lossless PNG, never a re-encoded frame), duplicates dropped on join.
  Storyboard mode gives each pass its own prompt/camera.
- **Range edit**: the marked range is snapped to the model's 4k+1 frame grid, then regenerated with the range's
  first frame as start image and last frame as end image (first-last-frame conditioning). The original boundary
  frames are kept, so everything outside the range is bit-identical. Ranges >5 s are chained automatically.
- **Text→video at 14B quality**: the 14B model is image→video, so the opening frame is first painted at 720p
  by the 5B model, then animated by the 14B model.
- Every operation creates a new version (parent link) — the version dropdown in the editor is your undo.

## Quality tiers (measured on M4 Pro 24 GB)
Draft = 5B/12 steps · Standard = 14B/4 steps (lightx2v) · Pro = 14B/8 steps · Master = 14B/20 steps, real CFG.
Generation is slow on a laptop GPU: ~20 min for a 2 s 480p Standard clip. Use 360p to iterate, then re-run
the same seed at 480p/720p. Close other heavy apps — the 14B model wants ~16 GB of unified memory.

## Tests
    python tests/gui_test.py       # headless browser smoke test
    python tests/api_ops_test.py   # tools + validation
