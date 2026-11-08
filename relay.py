"""bzaround online: a tiny WebSocket relay so players walk about town together.

Each client sends small JSON messages:
  {"t": "hi", "name": "...", "look": {...}}          once, on joining (and again when the look changes)
  {"t": "p", "x": .., "y": .., "z": .., "r": .., "a": "walk", "w": "town"}   about ten times a second
  {"t": "e", "chat": "..."}                           a speech bubble
The relay stamps each with the sender's id. A player who leaves is announced to all with {"t": "bye", "id": ..}.
Rooms come from the URL path (wss://host/bolzano), so separate groups can play apart.

Two kinds of client:
- the games that say "v": 2 in their hello (or connect with wss://host/bolzano?v=2&k=24; "k" is the same) get the looks ("hi") of everyone, the movement ("p")
  and bubbles ("e") only of the k closest of who is near them (same place and, in town, within NEAR metres; k is what the
  game can draw, 24 by default), and every MAP_DT seconds the map: {"t": "m", "l": [[id, x, z, w], ...]} with everyone.
  Someone who comes near is sent at once with their last "p"; someone who goes away is announced with {"t": "far", "id": ..}.
  So a hundred players in one square cost each game only the few around it, and the dots on the map still show everyone.
- the older games (no ?v=2) get everything from everyone, as before.

Run:  pip install websockets   then   python relay.py        (listens on $PORT, default 8080)
"""
import asyncio
import hashlib
import itertools
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import websockets

ROOMS = {}             # room -> {websocket: peer dict}
_ids = itertools.count(1)
MAX_MSG = 4096
MAX_ROOM = 150
NEAR = 70.0            # metres: within this the movement is sent...
FAR = 85.0             # ...and beyond this it stops (the gap keeps someone at the edge from blinking in and out)
MAP_DT = 2.0           # seconds between two maps
MIN_P_DT = 0.05        # a game sending positions faster than 20 a second is slowed down to that
NEAR_DT = 0.5          # seconds between two reckonings of who sees whom
K_MAX = 40             # the most a game may ask to see moving
# who is connected, read with wss://host/_stats/<secret> (only its hash is here). What program connects, never an address.
STATS_HASH = "ffb6cbd5a5624cd345bd8a7a2c220ba16f09f9acb6a21f143e8bfafca0ef6aaa"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def stats():
    now = time.time()
    return {room: [{"id": m["id"], "name": m["name"], "ua": m["ua"], "origin": m["origin"], "s": round(now - m["since"]),
                    "n": m["n"], "last_p": round(now - m["last_p"]) if m["last_p"] else None, "v": m["v"], "sees": len(m["sees"])}
                   for m in peers.values()]
            for room, peers in ROOMS.items()}


def place(w):
    """Balconies are up on the town's facades: for who sees whom they count as the town."""
    return "town" if w and w.startswith("balcone:") else w


def dist2(a, b):
    """Squared distance between two players in the same place (rooms: 0, everyone in there is near), None elsewhere."""
    wa, wb = place(a["w"]), place(b["w"])
    if wa is None or wa != wb:
        return None
    if wa != "town":
        return 0.0
    dx = a["x"] - b["x"]
    dz = a["z"] - b["z"]
    return dx * dx + dz * dz


def reckon(peers):
    """Who each new game sees moving: the k closest within NEAR (one already seen stays until FAR). Who comes is sent
    their last movement at once, who goes is announced as far."""
    items = list(peers.items())
    for ws, me in items:
        if me["v"] < 2:
            continue
        cand = []
        for ows, o in items:
            if ows is ws:
                continue
            d = dist2(me, o)
            if d is None:
                continue
            seen = o["id"] in me["sees"]
            if d <= (FAR * FAR if seen else NEAR * NEAR):
                # (who is already seen counts a little closer: two at the same distance don't swap places every turn)
                cand.append((d * (0.8 if seen else 1.0), o["id"], o))
        cand.sort(key=lambda c: c[0])
        want = {c[1] for c in cand[:me["k"]]}
        for c in cand[:me["k"]]:
            if c[1] not in me["sees"] and c[2]["p"]:
                send(ws, c[2]["p"])
        for gone in me["sees"] - want:
            send(ws, json.dumps({"t": "far", "id": gone}))
        for oid in want - me["sees"]:
            WATCH.setdefault(oid, set()).add(ws)
        for oid in me["sees"] - want:
            WATCH.get(oid, set()).discard(ws)
        me["sees"] = want


WATCH = {}             # player id -> the new games that see them move


def send(ws, out):
    websockets.broadcast([ws], out)


async def near_loop():
    while True:
        await asyncio.sleep(NEAR_DT)
        for peers in list(ROOMS.values()):
            reckon(peers)


def map_of(peers):
    return json.dumps({"t": "m", "l": [[m["id"], round(m["x"], 1), round(m["z"], 1), m["w"]] for m in peers.values() if m["w"] is not None]})


async def map_loop():
    while True:
        await asyncio.sleep(MAP_DT)
        for peers in list(ROOMS.values()):
            v2 = [w for w, m in peers.items() if m["v"] >= 2]
            if v2:
                websockets.broadcast(v2, map_of(peers))


async def handler(ws):
    req = getattr(ws, "request", None)
    parts = urlsplit(req.path if req else "/")
    room = parts.path.strip("/") or "bolzano"
    try:
        version = int(parse_qs(parts.query).get("v", ["1"])[0])
    except ValueError:
        version = 1
    try:
        k = max(1, min(K_MAX, int(parse_qs(parts.query).get("k", ["24"])[0])))
    except ValueError:
        k = 24
    if room.startswith("_stats/"):
        if hashlib.sha256(room[7:].encode()).hexdigest() == STATS_HASH:
            await ws.send(json.dumps(stats()))
        await ws.close()
        return
    headers = req.headers if req else {}
    peers = ROOMS.setdefault(room, {})
    if len(peers) >= MAX_ROOM:
        await ws.close(1013, "room full")
        return
    me = {"id": next(_ids), "hi": None, "p": None, "name": None, "ua": str(headers.get("User-Agent", ""))[:160],
          "origin": str(headers.get("Origin", ""))[:80], "since": time.time(), "n": {"hi": 0, "p": 0, "e": 0}, "last_p": 0.0,
          "v": version, "k": k, "x": 0.0, "z": 0.0, "w": None, "sees": set()}
    log("arriva", me["id"], "room", room, "v", version, "ua", repr(me["ua"]), "origin", repr(me["origin"]))
    # what the newcomer should see at once: everyone's look; an older game also everyone's place, a new one the map
    for other in list(peers.values()):
        if other["hi"]:
            await ws.send(other["hi"])
        if version < 2 and other["p"]:
            await ws.send(other["p"])
    peers[ws] = me
    await ws.send(json.dumps({"t": "you", "id": me["id"], "n": len(peers)}))
    if version >= 2:
        await ws.send(map_of(peers))
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
            t = msg["t"]
            if t == "p":
                now = time.time()
                if now - me["last_p"] < MIN_P_DT:
                    continue
                me["last_p"] = now
                try:
                    me["x"] = float(msg.get("x", 0.0))
                    me["z"] = float(msg.get("z", 0.0))
                except (TypeError, ValueError):
                    continue
                me["w"] = str(msg.get("w", "town"))[:80]
            msg["id"] = me["id"]
            me["n"][t] += 1
            if t == "hi":
                me["name"] = str(msg.get("name", ""))[:40]
                log("nome", me["id"], repr(me["name"]))
                try:
                    v, k = int(msg.get("v", me["v"])), int(msg.get("k", me["k"]))
                except (TypeError, ValueError):
                    v, k = me["v"], me["k"]
                me["k"] = max(1, min(K_MAX, k))
                if v >= 2 and me["v"] < 2:
                    me["v"] = v
                    send(ws, map_of(peers))      # (from now on: the map, and the movement of the closest only)
                    # until now it was sent everyone's movement: count them as seen, the next reckoning sends "far" to the extra
                    for ows, o in peers.items():
                        if ows is not ws and o["p"]:
                            me["sees"].add(o["id"])
                            WATCH.setdefault(o["id"], set()).add(ws)
            out = json.dumps(msg)
            if t in ("hi", "p"):
                me[t] = out
            if t == "hi":
                to = [w for w in peers if w is not ws]
            else:
                # movement and bubbles: the older games all of it, the new ones only from who they see
                to = [w for w, o in peers.items() if w is not ws and o["v"] < 2] + [w for w in WATCH.get(me["id"], ()) if w in peers]
            websockets.broadcast(to, out)
    except websockets.ConnectionClosed:
        pass                # (a closed tab or a lost phone signal: an ordinary way to leave)
    finally:
        log("esce", me["id"], repr(me["name"]), "dopo", round(time.time() - me["since"]), "s", me["n"])
        peers.pop(ws, None)
        WATCH.pop(me["id"], None)
        for oid in me["sees"]:
            WATCH.get(oid, set()).discard(ws)
        for o in peers.values():
            o["sees"].discard(me["id"])
        websockets.broadcast(list(peers), json.dumps({"t": "bye", "id": me["id"]}))
        if not peers:
            ROOMS.pop(room, None)


async def main():
    port = int(os.environ.get("PORT", "8080"))
    async with websockets.serve(handler, "0.0.0.0", port, max_size=MAX_MSG * 2, ping_interval=20):
        print("bzaround relay on port", port, flush=True)
        asyncio.create_task(map_loop())
        asyncio.create_task(near_loop())
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
