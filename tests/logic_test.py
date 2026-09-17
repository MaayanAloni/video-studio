"""Frame-accounting tests for long generation / extend / multi-pass range edits, with the GPU mocked out."""
import os, sys, shutil, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from PIL import Image
import media
media.VIDEOS = tempfile.mkdtemp(prefix="vs_test_videos_")
import jobs, workflows as wf
fails = []; calls = []
def check(n, c, i=""): print(("PASS " if c else "FAIL ") + n, i); (None if c else fails.append(n))
def fake_render(ctx, clip, label):
    n = wf.snap_len(clip["frames"]); calls.append(dict(clip, n=n))
    d = os.path.join(ctx.tmp, f"c{ctx.done}"); os.makedirs(d); out = []
    for i in range(n):
        p = os.path.join(d, f"{i:06d}.png"); Image.new("RGB", (64, 32), (len(calls) * 40 % 255, i * 3 % 255, 0)).save(p); out.append(p)
    ctx.done += 1; return out
jobs.render_clip = fake_render
jobs.t2v14_ready = lambda: True
job = lambda t, p: {"id": "t", "type": t, "params": p}
# a live server may hold its own vs_* scratch dirs; only count ones WE create
PRE_EXISTING = {d for d in os.listdir(media.DATA) if d.startswith("vs_")}
px = lambda vid, i: Image.open(media.frames_of(vid)[i]).getpixel((0, 0))

calls.clear(); m = jobs.job_generate(job("generate", {"prompt": "a", "seconds": 23, "quality": "standard", "seed": 1}))
check("23s -> 5 passes", len(calls) == 5, len(calls))
check("23s frame count", m["frames"] == sum(c["n"] for c in calls) - 4 and abs(m["seconds"] - 23) < 0.6, f'{m["frames"]}f {m["seconds"]}s')
check("pass 1 is pure t2v, later passes start from previous last frame", calls[0]["start_image"] is None and all(c["start_image"] for c in calls[1:]))
check("segments tile the video", m["segments"][0]["start"] == 0 and m["segments"][-1]["end"] == m["frames"] - 1 and all(a["end"] + 1 == b["start"] for a, b in zip(m["segments"], m["segments"][1:])))
check("all passes <= 81 frames", all(c["n"] <= 81 for c in calls))

calls.clear(); m5 = jobs.job_generate(job("generate", {"prompt": "a", "seconds": 12, "quality": "draft", "seed": 1}))
check("draft 12s @24fps", m5["fps"] == 24 and abs(m5["seconds"] - 12) < 0.6 and all(c["n"] <= 121 for c in calls), f'{m5["frames"]}f')

calls.clear(); ms = jobs.job_generate(job("generate", {"prompt": "x", "quality": "standard", "seed": 1, "end_image": None,
      "segments": [{"prompt": "one", "seconds": 3}, {"prompt": "two", "seconds": 8, "camera": "orbit"}]}))
check("storyboard: 8s shot splits in 2, prompts + camera carried", [c["prompt"].split(",")[0] for c in calls] == ["one", "two", "two"] and "orbits" in calls[1]["prompt"])

calls.clear(); e = jobs.job_edit_range(job("edit_range", {"video": m["id"], "f0": 40, "f1": 250, "prompt": "edit", "seed": 2}))
f0, f1 = e["edits"][0]["start"], e["edits"][0]["end"]
check("long range -> multiple chained passes", len(calls) == 3 and all(c["n"] <= 81 for c in calls), [c["n"] for c in calls])
check("only the last pass gets the end frame", [bool(c["end_image"]) for c in calls] == [False, False, True])
check("first pass starts on original f0, last lands on original f1", calls[0]["start_image"] == media.frames_of(m["id"])[f0] and calls[-1]["end_image"] == media.frames_of(m["id"])[f1])
check("edit keeps total length", e["frames"] == m["frames"], f'{e["frames"]} vs {m["frames"]}')
same = [px(e["id"], i) == px(m["id"], i) for i in range(m["frames"])]
check("outside + boundary frames untouched, interior replaced", all(same[:f0 + 1]) and all(same[f1:]) and not any(same[f0 + 1:f1]), f"range {f0}-{f1}")
check("range length on 4k+1 grid", (f1 - f0) % 4 == 0)

calls.clear(); e2 = jobs.job_edit_range(job("edit_range", {"video": m["id"], "f0": m["frames"] - 7, "f1": m["frames"] - 1, "prompt": "tail"}))
check("edit at very end stays in bounds", e2["frames"] == m["frames"] and e2["edits"][-1]["end"] == m["frames"] - 1, e2["edits"][-1])

calls.clear(); x = jobs.job_extend(job("extend", {"video": m["id"], "seconds": 7, "prompt": ""}))
check("extend 7s -> 2 passes appended", len(calls) == 2 and x["frames"] == m["frames"] + sum(c["n"] - 1 for c in calls) and calls[0]["start_image"] == media.frames_of(m["id"])[-1])
check("extend keeps original frames", all(px(x["id"], i) == px(m["id"], i) for i in range(m["frames"])))
check("extend segment markers", x["segments"][-2]["start"] == m["frames"] and x["segments"][-1]["end"] == x["frames"] - 1)
leaked = {d for d in os.listdir(media.DATA) if d.startswith("vs_") and "test" not in d} - PRE_EXISTING
check("no temp dirs leaked", not leaked, str(leaked))
shutil.rmtree(media.VIDEOS)
sys.exit(1 if fails else 0)
