"""Fast API test of all ffmpeg-only operations + validation. Needs at least one video in the library."""
import requests, time, sys
B="http://127.0.0.1:7860"; fails=[]
def check(n,c,i=""): print(("PASS " if c else "FAIL ")+n,i); (None if c else fails.append(n))
def run(t,p):
    r=requests.post(B+"/api/jobs",json={"type":t,"params":p}); assert r.ok, r.text
    jid=r.json()["id"]
    for _ in range(600):
        j=[x for x in requests.get(B+"/api/jobs").json() if x["id"]==jid][0]
        if j["status"] not in("queued","running"): return j
        time.sleep(.5)
vids=requests.get(B+"/api/videos").json(); v=[x for x in vids if x["op"]=="generate"][-1]; n=v["frames"]; made=[]
def meta(j): made.append(j["video"]); return requests.get(B+f"/api/videos/{j['video']}").json()
j=run("trim",{"video":v["id"],"f0":5,"f1":14,"mode":"keep"}); m=meta(j); check("trim keep",m["frames"]==10,m["frames"])
j=run("trim",{"video":v["id"],"f0":5,"f1":14,"mode":"cut"}); m=meta(j); check("trim cut",m["frames"]==n-10,m["frames"])
j=run("reverse",{"video":v["id"]}); m=meta(j); check("reverse",m["frames"]==n and m["parent"]==v["id"])
j=run("interpolate",{"video":made[0]}); m=meta(j); check("interpolate 2x fps",m["fps"]==v["fps"]*2 and m["frames"]>=16,f'{m["frames"]}f @{m["fps"]}')
j=run("upscale",{"video":made[0]}); m=meta(j); check("upscale 2x",m["width"]==v["width"]*2,f'{m["width"]}x{m["height"]}')
other=[x for x in vids if x["id"]!=v["id"]][0]
j=run("join",{"video":made[0],"other":other["id"]}); m=meta(j); check("join (different sizes ok)",m["frames"]==10+other["frames"] and m["width"]==v["width"],f'{m["frames"]}')
r=requests.get(B+f"/videos/{made[0]}/video.mp4",headers={"Range":"bytes=0-99"}); check("mp4 range requests (seeking)",r.status_code==206)
check("download endpoint",requests.get(B+f"/api/videos/{made[0]}/download").headers.get("content-type")=="video/mp4")
check("filmstrip thumb",requests.get(B+f"/api/videos/{made[0]}/frame/3?w=200").headers["content-type"]=="image/jpeg")
for name,t,p in [("bad type","nope",{}),("missing video","extend",{"video":"zzz"}),("bad range","edit_range",{"video":v["id"],"f0":10,"f1":11,"prompt":"x"}),
                 ("range oob","edit_range",{"video":v["id"],"f0":0,"f1":9999,"prompt":"x"}),("no prompt","edit_range",{"video":v["id"],"f0":0,"f1":20,"prompt":" "}),
                 ("draft+end frame","generate",{"prompt":"x","quality":"draft","end_image":"nope.png"}),("path traversal img","generate",{"prompt":"x","start_image":"../../etc/passwd"})]:
    check("rejects "+name,requests.post(B+"/api/jobs",json={"type":t,"params":p}).status_code==400)
check("traversal on video id",requests.get(B+"/api/videos/..%2F..%2Fjobs.json").status_code in(404,400,500))
for x in made: requests.delete(B+f"/api/videos/{x}")
check("cleanup deletes",not any(x["id"] in made for x in requests.get(B+"/api/videos").json()))
sys.exit(1 if fails else 0)
