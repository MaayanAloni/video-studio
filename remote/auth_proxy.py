"""Token-checking reverse proxy for ComfyUI (HTTP + WebSocket).

ComfyUI has no authentication and can read and write files on its host, so it must
never be exposed directly. Run this in front of it when you want to reach the pod
over a public URL instead of an SSH tunnel:

    python auth_proxy.py --token <secret> --listen 0.0.0.0:8188 --upstream http://127.0.0.1:8189

Accepts the token as `Authorization: Bearer <token>` or a `?token=` query parameter
(browsers cannot set headers on a WebSocket upgrade).
"""
import argparse
import asyncio

import aiohttp
from aiohttp import web

HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length"}


def authorised(request, token: str) -> bool:
    got = request.headers.get("Authorization", "")
    if got.startswith("Bearer ") and _eq(got[7:], token):
        return True
    return _eq(request.query.get("token", ""), token)


def _eq(a: str, b: str) -> bool:
    """Constant-time compare so the token can't be guessed a character at a time."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


async def handler(request: web.Request):
    cfg = request.app["cfg"]
    if not authorised(request, cfg.token):
        return web.json_response({"error": "unauthorized"}, status=401)
    target = cfg.upstream.rstrip("/") + request.rel_url.path_qs
    session: aiohttp.ClientSession = request.app["session"]

    # ---- websocket upgrade (progress + live preview frames) ----
    if request.headers.get("Upgrade", "").lower() == "websocket":
        client = web.WebSocketResponse(max_msg_size=0, heartbeat=30)
        await client.prepare(request)
        ws_target = target.replace("http://", "ws://").replace("https://", "wss://")
        async with session.ws_connect(ws_target, max_msg_size=0) as upstream:
            async def pump(src, dst):
                async for msg in src:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    else:
                        break
            await asyncio.gather(pump(upstream, client), pump(client, upstream),
                                 return_exceptions=True)
        return client

    # ---- plain HTTP ----
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    async with session.request(request.method, target, headers=headers,
                               data=request.content if request.body_exists else None,
                               allow_redirects=False) as up:
        resp = web.StreamResponse(status=up.status,
                                  headers={k: v for k, v in up.headers.items() if k.lower() not in HOP})
        await resp.prepare(request)
        async for chunk in up.content.iter_chunked(1 << 16):
            await resp.write(chunk)
        await resp.write_eof()
        return resp


async def _session(app):
    app["session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
    yield
    await app["session"].close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--listen", default="0.0.0.0:8188")
    ap.add_argument("--upstream", default="http://127.0.0.1:8189")
    cfg = ap.parse_args()
    if len(cfg.token) < 16:
        raise SystemExit("use a token of at least 16 characters")
    host, port = cfg.listen.rsplit(":", 1)
    app = web.Application(client_max_size=1 << 30)
    app["cfg"] = cfg
    app.cleanup_ctx.append(_session)
    app.router.add_route("*", "/{path:.*}", handler)
    print(f"auth proxy: {cfg.listen} -> {cfg.upstream}")
    web.run_app(app, host=host, port=int(port), print=None)


if __name__ == "__main__":
    main()
