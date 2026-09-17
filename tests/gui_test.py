"""Headless GUI smoke test. Run while the server is up: python tests/gui_test.py"""
import os, sys, json
from playwright.sync_api import sync_playwright
OUT = os.environ.get("SHOTS", "logs/shots"); os.makedirs(OUT, exist_ok=True)
errors, fails = [], []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, info); (None if cond else fails.append(name))
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://127.0.0.1:7860"); pg.wait_for_timeout(1500)
    check("title", pg.title() == "Video Studio")
    check("status loaded", "engine" in pg.inner_text("#sys"), pg.inner_text("#sys").replace("\n", " | "))
    check("aspect options", pg.locator("#aspect option").count() >= 5)
    check("camera options", pg.locator("#camera option").count() >= 10)
    pg.screenshot(path=f"{OUT}/1_create.png")
    # validation: empty prompt must be rejected, not queued
    n0 = len(pg.evaluate("fetch('/api/jobs').then(r=>r.json())"))
    pg.click("#generate"); pg.wait_for_timeout(600)
    check("empty prompt rejected", "prompt" in pg.inner_text("#createErr").lower(), pg.inner_text("#createErr"))
    check("no job created", len(pg.evaluate("fetch('/api/jobs').then(r=>r.json())")) == n0)
    # modes
    pg.click("[data-mode=i2v]"); check("i2v shows frame drops", pg.is_visible("#dropStart") and pg.is_visible("#dropEnd"))
    pg.evaluate("dropTarget='start_image'")
    pg.set_input_files("#file", "data/videos/%s/thumb.jpg" % sorted(os.listdir("data/videos"))[0]); pg.wait_for_timeout(1500)
    check("upload shows preview", "has" in (pg.get_attribute("#dropStart", "class") or ""))
    pg.click("[data-mode=story]"); check("storyboard has 2 shots", pg.locator("#shots .shot").count() == 2)
    pg.click("#addShot"); pg.fill("#shots .shot:nth-child(1) textarea", "a"); pg.fill("#shots .shot:nth-child(3) .sec", "12")
    pg.wait_for_timeout(200); check("duration hint counts passes", "passes" in pg.inner_text("#durHint"), pg.inner_text("#durHint"))
    pg.screenshot(path=f"{OUT}/2_story.png")
    # library -> editor
    pg.click("[data-view=library]"); pg.wait_for_timeout(1000)
    tiles = pg.locator("#grid .tile").count(); check("library lists videos", tiles >= 1, str(tiles))
    pg.screenshot(path=f"{OUT}/3_library.png")
    pg.locator("#grid .tile").first.click(); pg.wait_for_timeout(2500)
    check("editor opened", pg.is_visible("#editorMain") and pg.locator("#strip img").count() > 3)
    check("video element loaded", pg.evaluate("document.querySelector('#vid').readyState") >= 1, "(headless shell may lack H.264)")
    box = pg.locator("#timeline").bounding_box()
    y = box["y"] + 25
    pg.mouse.move(box["x"] + box["width"] * .25, y); pg.mouse.down(); pg.mouse.move(box["x"] + box["width"] * .6, y, steps=8); pg.mouse.up()
    pg.wait_for_timeout(300)
    sel = pg.evaluate("S.sel"); fr = pg.evaluate("S.cur.frames")
    check("drag creates selection", bool(sel) and abs(sel[0] - round(.25 * (fr - 1))) <= 1 and abs(sel[1] - round(.6 * (fr - 1))) <= 1, f"{sel} of {fr}")
    check("regen enabled with selection", pg.is_enabled("#btnRegen"))
    # drag right handle
    hb = pg.locator("#sel .h.r").bounding_box()
    pg.mouse.move(hb["x"] + 4, y); pg.mouse.down(); pg.mouse.move(box["x"] + box["width"] * .8, y, steps=6); pg.mouse.up()
    sel2 = pg.evaluate("S.sel"); check("handle drag resizes", sel2[0] == sel[0] and sel2[1] > sel[1], str(sel2))
    pg.click("#btnRegen"); pg.wait_for_timeout(300)
    check("regen needs prompt", "Describe" in pg.inner_text("#edErr"))
    pg.keyboard.press("Escape"); check("esc clears selection", pg.evaluate("S.sel") is None and not pg.is_enabled("#btnRegen"))
    pg.screenshot(path=f"{OUT}/4_editor.png")
    pg.click("[data-view=queue]"); pg.wait_for_timeout(500); check("queue lists jobs", pg.locator("#jobList .job").count() >= 1)
    pg.screenshot(path=f"{OUT}/5_queue.png")
    # narrow layout must not scroll sideways
    pg.set_viewport_size({"width": 900, "height": 800}); pg.click("[data-view=create]")
    check("no horizontal overflow @900", pg.evaluate("document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth + 1"))
    b.close()
errors = [e for e in errors if "400" not in e]  # the empty-prompt rejection above is expected
check("no console errors", not errors, json.dumps(errors[:5]))
sys.exit(1 if fails else 0)
