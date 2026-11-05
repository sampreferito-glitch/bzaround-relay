"""bzaround online: a tiny WebSocket relay so players walk about town together.

Each client sends small JSON messages:
  {"t": "hi", "name": "...", "look": {...}}          once, on joining (and again when the look changes)
  {"t": "p", "x": .., "y": .., "z": .., "r": .., "a": "walk", "w": "town"}   about ten times a second
The relay stamps each with the sender's id and passes it to everyone else in the same room; a newcomer first gets
every present player's last "hi" and "p"; a player who leaves is announced with {"t": "bye", "id": ..}.
Rooms come from the URL path (wss://host/bolzano), so separate groups can play apart.

Run:  pip install websockets   then   python relay.py        (listens on $PORT, default 8080)
"""
import asyncio
import hashlib
import itertools
import json
import os
import time

import websockets

ROOMS = {}             # room -> {websocket: {"id": int, "hi": str | None, "p": str | None}}
_ids = itertools.count(1)
MAX_MSG = 4096
MAX_ROOM = 40
# who is connected, read with wss://host/_stats/<secret> (only its hash is here). What program connects, never an address.
STATS_HASH = "ffb6cbd5a5624cd345bd8a7a2c220ba16f09f9acb6a21f143e8bfafca0ef6aaa"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def stats():
    now = time.time()
    return {room: [{"id": m["id"], "name": m["name"], "ua": m["ua"], "origin": m["origin"], "s": round(now - m["since"]),
                    "n": m["n"], "last_p": round(now - m["last_p"]) if m["last_p"] else None} for m in peers.values()]
            for room, peers in ROOMS.items()}


async def handler(ws):
    path = getattr(ws, "request", None)
    room = (path.path if path else "/").strip("/") or "bolzano"
    if room.startswith("_stats/"):
        if hashlib.sha256(room[7:].encode()).hexdigest() == STATS_HASH:
            await ws.send(json.dumps(stats()))
        await ws.close()
        return
    headers = path.headers if path else {}
    peers = ROOMS.setdefault(room, {})
    if len(peers) >= MAX_ROOM:
        await ws.close(1013, "room full")
        return
    me = {"id": next(_ids), "hi": None, "p": None, "name": None, "ua": str(headers.get("User-Agent", ""))[:160],
          "origin": str(headers.get("Origin", ""))[:80], "since": time.time(), "n": {"hi": 0, "p": 0, "e": 0}, "last_p": 0.0}
    log("arriva", me["id"], "room", room, "ua", repr(me["ua"]), "origin", repr(me["origin"]))
    # what the newcomer should see at once: everyone already here
    for other in list(peers.values()):
        for k in ("hi", "p"):
            if other[k]:
                await ws.send(other[k])
    peers[ws] = me
    await ws.send(json.dumps({"t": "you", "id": me["id"], "n": len(peers)}))
    try:
        async for raw in ws:
            if not isinstance(raw, str) or len(raw) > MAX_MSG:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("t") not in ("hi", "p", "e"):
                continue
            msg["id"] = me["id"]
            me["n"][msg["t"]] += 1
            if msg["t"] == "p":
                me["last_p"] = time.time()
            elif msg["t"] == "hi":
                me["name"] = str(msg.get("name", ""))[:40]
                log("nome", me["id"], repr(me["name"]))
            out = json.dumps(msg)
            if msg["t"] in ("hi", "p"):
                me[msg["t"]] = out
            websockets.broadcast([w for w in peers if w is not ws], out)
    except websockets.ConnectionClosed:
        pass                # (a closed tab or a lost phone signal: an ordinary way to leave)
    finally:
        log("esce", me["id"], repr(me["name"]), "dopo", round(time.time() - me["since"]), "s", me["n"])
        peers.pop(ws, None)
        websockets.broadcast(list(peers), json.dumps({"t": "bye", "id": me["id"]}))
        if not peers:
            ROOMS.pop(room, None)


async def main():
    port = int(os.environ.get("PORT", "8080"))
    async with websockets.serve(handler, "0.0.0.0", port, max_size=MAX_MSG * 2, ping_interval=20):
        print("bzaround relay on port", port, flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
