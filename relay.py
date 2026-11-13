"""bzaround online: a tiny WebSocket relay so players walk about town together.

Each client sends small JSON messages:
  {"t": "hi", "name": "...", "look": {...}}          once, on joining (and again when the look changes)
  {"t": "p", "x": .., "y": .., "z": .., "r": .., "a": "walk", "w": "town"}   about ten times a second
  {"t": "e", "chat": "..."}                           a speech bubble
  {"t": "g", "a": ...}                                the minigames (server/giochi.py): handled here, never relayed
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
import math
import os
import re
import time
from urllib.parse import parse_qs, urlsplit

import websockets

from giochi import Giochi

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


MAX_COORD = 1e5        # metres: a position farther than this is not a position
HI_DT = 1.0            # seconds between two hellos of one game (a look that changes is rare)
E_BURST, E_RATE = 4, 1.0   # speech bubbles: 4 at once, then one a second
MAX_CHAT = 200
_CTRL = re.compile(r"[\x00-\x1f\x7f‎‏‪-‮⁦-⁩]")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _no_constant(name):
    raise ValueError("non-standard JSON number: " + name)


def parse(raw):
    """The client's JSON, without NaN/Infinity (which would then reach every other game in the maps and be refused there)."""
    return json.loads(raw, parse_constant=_no_constant)


def num(v):
    """A finite coordinate, or None (1e400 is infinity, "nan" a string float() would accept)."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) and abs(f) <= MAX_COORD else None


def to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return default


def clean_text(v, n):
    """A name or a bubble as the others will see it: no control or direction characters, at most n characters."""
    return _CTRL.sub("", str(v))[:n]


def clean_uid(v):
    """The id the minigames know a player by. "npc" is the server's own player and "id<N>" the fallback of a game
    without one: neither can be chosen."""
    u = str(v or "")[:64]
    if not u or u == "npc" or re.fullmatch(r"id\d+", u):
        return None
    return u


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
    # (the same as measuring dist2 between every two players, done cheaper: each one's place once, and in town only
    # the players of the 3x3 cells of FAR metres around, since nobody farther than FAR can be seen)
    far2, near2 = FAR * FAR, NEAR * NEAR
    cells = {}             # place -> [players]; town: (cell x, cell z) -> [players]
    rows = []
    for i, (ws, me) in enumerate(peers.items()):
        pl = place(me["w"])
        if pl is None:
            rows.append((ws, me, None, None))
            continue
        if pl == "town":
            key = (math.floor(me["x"] / FAR), math.floor(me["z"] / FAR))
            cells.setdefault(key, []).append((i, ws, me, me["x"], me["z"], me["id"]))
        else:
            key = None
            cells.setdefault(pl, []).append((i, ws, me, 0.0, 0.0, me["id"]))
        rows.append((ws, me, pl, key))
    for ws, me, pl, key in rows:
        if me["v"] < 2:
            continue
        cand = []
        sees = me["sees"]
        if pl is not None:
            if key is None:
                near = cells[pl]
                town = False
            else:
                cx, cz = key
                near = [r for gx in (cx - 1, cx, cx + 1) for gz in (cz - 1, cz, cz + 1) for r in cells.get((gx, gz), ())]
                town = True
            x, z = me["x"], me["z"]
            for i, ows, o, ox, oz, oid in near:
                if ows is ws:
                    continue
                if town:
                    dx = x - ox
                    dz = z - oz
                    d = dx * dx + dz * dz
                else:
                    d = 0.0
                seen = oid in sees
                if d <= (far2 if seen else near2):
                    # (who is already seen counts a little closer: two at the same distance don't swap places every turn)
                    cand.append((d * (0.8 if seen else 1.0), i, oid, o))
        cand.sort(key=lambda c: (c[0], c[1]))      # (ties in the order of the room, as before)
        cand = [(c[0], c[2], c[3]) for c in cand]
        want = {c[1] for c in cand[:me["k"]]}
        for c in cand[:me["k"]]:
            if c[1] not in me["sees"] and c[2]["p"] and GIOCHI.vede(me, c[2]):
                send(ws, c[2]["p"])
        for gone in me["sees"] - want:
            send(ws, json.dumps({"t": "far", "id": gone}))
        for oid in want - me["sees"]:
            WATCH.setdefault(oid, set()).add(ws)
        for oid in me["sees"] - want:
            WATCH.get(oid, set()).discard(ws)
        me["sees"] = want


WATCH = {}             # player id -> the new games that see them move
V1 = {}                # room -> the older games in it (they get everyone's movement): kept on arrive/upgrade/leave
SLOW = set()           # games whose outgoing buffer is long: they skip movement until it drains (near_loop)
SLOW_BYTES = 64 * 1024        # (about a second and a half of movement for a game behind a slow link)
DEAD_BYTES = 1024 * 1024      # (no game reads this far behind: the connection is dropped, it reconnects)


def backlog(ws):
    try:
        return ws.transport.get_write_buffer_size()
    except Exception:      # noqa: BLE001  (closing: no transport)
        return 0


def send(ws, out):
    websockets.broadcast([ws], out)


GIOCHI = Giochi(send, log)     # the minigames (server/giochi.py): they decide, the games only show and ask


async def giochi_loop():
    while True:
        await asyncio.sleep(0.25)
        try:
            GIOCHI.tick()
        except Exception as e:      # (a bug in one game must not stop the relay)
            log("giochi: errore", repr(e))


async def near_loop():
    while True:
        await asyncio.sleep(NEAR_DT)
        now = time.time()
        # (broadcast never waits for a slow reader: its buffer would grow without end. Movement is skipped for it, the
        # rest still goes; one that is hopelessly behind is cut and comes back)
        SLOW.clear()
        for peers in list(ROOMS.values()):
            for ws in list(peers):
                try:
                    b = backlog(ws)
                    if b > DEAD_BYTES:
                        log("troppo indietro", peers[ws]["id"], b, "byte: chiuso")
                        ws.transport.abort()      # (its handler ends as for a lost signal: the usual goodbye)
                    elif b > SLOW_BYTES:
                        SLOW.add(ws)
                except Exception as e:      # noqa: BLE001
                    log("vicini: errore", repr(e))
        for peers in list(ROOMS.values()):
            try:
                reckon(peers)
                for ws, me in list(peers.items()):
                    if me.get("hi_due") and now - me.get("last_hi", 0.0) >= HI_DT:
                        me["hi_due"] = False
                        me["last_hi"] = now
                        websockets.broadcast([w for w in peers if w is not ws], me["hi"])
            except Exception as e:      # (one odd peer must not stop who-sees-whom for every room, forever)
                log("vicini: errore", repr(e))


def map_of(peers):
    # (chi gioca a nascondino non compare sulla mappa di nessuno)
    try:
        via = GIOCHI.fuori_mappa()
    except Exception:      # noqa: BLE001
        via = set()
    return json.dumps({"t": "m", "l": [[m["id"], round(m["x"], 1), round(m["z"], 1), m["w"]] for m in peers.values() if m["w"] is not None and m["id"] not in via]})


async def map_loop():
    while True:
        await asyncio.sleep(MAP_DT)
        for peers in list(ROOMS.values()):
            try:
                v2 = [w for w, m in peers.items() if m["v"] >= 2]
                if v2:
                    websockets.broadcast(v2, map_of(peers))
            except Exception as e:
                log("mappa: errore", repr(e))


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
    me = {"id": next(_ids), "_ws": ws, "hi": None, "p": None, "name": None, "ua": str(headers.get("User-Agent", ""))[:160],
          "origin": str(headers.get("Origin", ""))[:80], "since": time.time(), "n": {"hi": 0, "p": 0, "e": 0}, "last_p": 0.0,
          "v": version, "k": k, "x": 0.0, "z": 0.0, "w": None, "sees": set()}
    log("arriva", me["id"], "room", room, "v", version, "ua", repr(me["ua"]), "origin", repr(me["origin"]))
    # what the newcomer should see at once: everyone's look; an older game also everyone's place, a new one the map
    try:
        for other in list(peers.values()):
            if other["hi"]:
                await ws.send(other["hi"])
            if version < 2 and other["p"]:
                await ws.send(other["p"])
    except websockets.ConnectionClosed:
        # (gone while arriving: a room made just for it would otherwise stay in ROOMS, empty, for ever)
        if not peers and ROOMS.get(room) is peers:
            ROOMS.pop(room, None)
        return
    # (the last one may have left the room while this one was arriving: put the room back)
    peers = ROOMS.setdefault(room, peers)
    peers[ws] = me
    if version < 2:
        V1.setdefault(room, set()).add(ws)
    try:
        await ws.send(json.dumps({"t": "you", "id": me["id"], "n": len(peers)}))
        if version >= 2:
            await ws.send(map_of(peers))
        async for raw in ws:
            if not isinstance(raw, str) or len(raw) > MAX_MSG:
                continue
            try:
                msg = parse(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("t") not in ("hi", "p", "e", "g"):
                continue
            t = msg["t"]
            if t == "g":
                # a minigame: to the server's own games, never passed on as it is
                if me["hi"]:
                    try:
                        GIOCHI.messaggio(room, ws, me, msg)
                    except Exception as e:
                        log("giochi: errore", repr(e))
                continue
            if t == "p":
                now = time.time()
                if now - me["last_p"] < MIN_P_DT:
                    continue
                x, z = num(msg.get("x", 0.0)), num(msg.get("z", 0.0))
                if x is None or z is None:
                    continue
                me["last_p"] = now
                me["x"], me["z"] = x, z
                msg["x"], msg["z"] = x, z      # (passed on as checked, never as sent)
                me["w"] = str(msg.get("w", "town"))[:80]
                msg["w"] = me["w"]
                y = num(msg.get("y", 0.0))
                if y is not None:
                    me["y"] = msg["y"] = y
                else:
                    msg.pop("y", None)
                if me["hi"]:
                    try:
                        GIOCHI.mosso(room, ws, me)      # (the clovers of the Talvera near this player)
                    except Exception as e:
                        log("giochi: errore", repr(e))
            if t == "e":
                # speech bubbles: a small bucket, refilled one a second
                now = time.time()
                me["e_tok"] = min(E_BURST, me.get("e_tok", E_BURST) + (now - me.get("e_t", now)) * E_RATE)
                me["e_t"] = now
                if me["e_tok"] < 1.0:
                    continue
                me["e_tok"] -= 1.0
                if "chat" in msg:
                    msg["chat"] = clean_text(msg["chat"], MAX_CHAT)
            msg["id"] = me["id"]
            me["n"][t] += 1
            if t == "hi":
                name = clean_text(msg.get("name", ""), 40)
                msg["name"] = name
                if name != me["name"]:
                    log("nome", me["id"], repr(name))
                me["name"] = name
                if not me.get("uid"):
                    # (the id the minigames know a player by, to come back to a match after a lost signal; it never changes once set)
                    me["uid"] = clean_uid(msg.get("uid"))
                v, k = to_int(msg.get("v", me["v"]), me["v"]), to_int(msg.get("k", me["k"]), me["k"])
                me["k"] = max(1, min(K_MAX, k))
                if v >= 2 and me["v"] < 2:
                    me["v"] = v
                    V1.get(room, set()).discard(ws)
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
                now = time.time()
                if now - me.get("last_hi", 0.0) < HI_DT:
                    # (a game says hello once and again when the look changes; a flood of them is held back: the last
                    # one goes out from near_loop, so the look the others end up with is always the latest)
                    me["hi_due"] = True
                    continue
                me["last_hi"] = now
                me["hi_due"] = False
                to = [w for w in peers if w is not ws]
            else:
                # movement and bubbles: the older games all of it, the new ones only from who they see
                # (the older ones from the room's own set, not by looking at every player for every movement)
                to = [w for w in V1.get(room, ()) if w is not ws]
                watch = WATCH.get(me["id"])
                if watch:
                    to += [w for w in watch if w in peers]
                if SLOW and t == "p":
                    # (a game whose link can't keep up skips movement for a moment: the next one is newer anyway)
                    to = [w for w in to if w not in SLOW]
                # (il nascondino: il cercatore non riceve chi non vede; chiesto solo se un nascondino e' in corso)
                try:
                    if GIOCHI.nascondino_in_corso():
                        to = [w for w in to if GIOCHI.vede(peers[w], me)]
                except Exception as e:      # noqa: BLE001
                    log("giochi: errore", repr(e))
            websockets.broadcast(to, out)
    except websockets.ConnectionClosed:
        pass                # (a closed tab or a lost phone signal: an ordinary way to leave)
    finally:
        log("esce", me["id"], repr(me["name"]), "dopo", round(time.time() - me["since"]), "s", me["n"])
        try:
            GIOCHI.disconnesso(room, me)
        except Exception as e:
            log("giochi: errore", repr(e))
        peers.pop(ws, None)
        SLOW.discard(ws)
        v1 = V1.get(room)
        if v1 is not None:
            v1.discard(ws)
            if not v1:
                V1.pop(room, None)
        WATCH.pop(me["id"], None)
        for oid in me["sees"]:
            WATCH.get(oid, set()).discard(ws)
        for o in peers.values():
            o["sees"].discard(me["id"])
        websockets.broadcast(list(peers), json.dumps({"t": "bye", "id": me["id"]}))
        if not peers and ROOMS.get(room) is peers:
            ROOMS.pop(room, None)
            try:
                GIOCHI.stanza_vuota(room)       # (the clovers taken and who saw what, kept per room)
            except Exception as e:
                log("giochi: errore", repr(e))


async def main():
    port = int(os.environ.get("PORT", "8080"))
    # compression=None: per-message deflate cost the processor once per recipient for every frame (the same movement
    # compressed 24 times over), for short JSON that barely shrinks. A client that offers it simply doesn't get it:
    # the extension is optional in the handshake and every websocket client works without it. The price is bandwidth
    # to the browser games (the only ones that ask for it): RELAY_DEFLATE=1 in the environment brings it back.
    deflate = "deflate" if os.environ.get("RELAY_DEFLATE") == "1" else None
    async with websockets.serve(handler, "0.0.0.0", port, max_size=MAX_MSG * 2, ping_interval=20, compression=deflate):
        print("bzaround relay on port", port, flush=True)
        asyncio.create_task(map_loop())
        asyncio.create_task(near_loop())
        asyncio.create_task(giochi_loop())
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
