"""Minigiochi di bzaround, lato server: il relay (relay.py) gli passa i messaggi {"t": "g", ...} e lui tiene tutto.

Il server decide e controlla: le sale d'attesa e l'abbinamento, il via, il timer, i turni, cosa c'e' nel prato, se una foto
o una raccolta e' valida (dalla posizione che il relay conosce, mai da quella che il gioco dichiara), i punteggi, la
classifica, i premi, chi esce e chi rientra. Il gioco mostra e chiede; non manda mai punteggi.

Messaggi dal gioco (tutti con "t": "g"):
  {"a": "entra", "gioco": "foto"|"quadrifogli"|"rigori", "prova": false}   in sala (prova: da soli, senza premi)
  {"a": "esci"}                                                           lascia la sala o la partita
  {"a": "foto", "ins": n, "c": [x, y, z], "d": [x, y, z]}                  uno scatto: dove sta la fotocamera, dove guarda
  {"a": "prendi", "q": n} / {"a": "raccogli", "id": n}                  raccoglie il quadrifoglio n del prato (in partita: punti)
  {"a": "prato"}                                                          i quadrifogli vicini, subito
  {"a": "tiro", "x": -1.3..1.3, "y": -0.1..1.4, "pot": 0..1, "eff": -1..1, "alz": 0..1}
                                                                          il rigore: mira (x: +-1 i pali, y: 0 terra,
                                                                          1 traversa), potenza, effetto, traiettoria
  {"a": "tuffo", "x": -1..1, "y": 0..1}                                   il portiere: dove si tuffa (anche il vecchio
                                                                          {"dir": -1|0|1, "alto": 0|1})
  "esito" dei rigori: esito, x, y (dove ha passato la linea), pot, eff, alz, palo, tuffo {dir, alto},
  traiettoria [[t, u, v, h]...] a 30 Hz in coordinate del campo, portiere_piano {v0, t0, dur, dest [v, h], tipo, presa,
  tocco} (portiere resta [uid, nome])
Messaggi al gioco: "sala" (chi c'e', tra quanto si parte), "via" (la partita: i dati per disegnarla), "stato" (timer e
classifica, ogni secondo), "esito" (di uno scatto, una raccolta, un rigore), "vicini" (i quadrifogli che si vedono da
li'), "preso", "turno" (rigori), e fuori dalle partite "q_vicini" ({"nuovi": [[id, dorato]], "via": [id]}),
"q_tolto" ({"id", "chi"}), "q_trovato" / "q_no" ({"id", "raro", "motivo"}), "fine" (classifica e premi di ciascuno), "errore".
Ogni messaggio porta "now": i secondi dall'inizio della partita, l'orologio comune.
"""
import json
import math
import os
import random
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GIOCHI = ("foto", "quadrifogli", "rigori", "sfilata", "caffe")
REJOIN = 45.0            # secondi per rientrare in una partita dopo una disconnessione
POS_STALE = 3.0          # una posizione piu' vecchia di cosi' non basta per convalidare un'azione


def _load(name, default):
    try:
        with open(os.path.join(HERE, name), encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


CFG = _load("giochi_config.json", {})
if os.environ.get("GIOCHI_VELOCE"):
    # (collaudi: si parte subito e si gioca poco)
    for _g in GIOCHI:
        CFG.setdefault(_g, {})["attesa"] = 2
        if "durata" in CFG[_g]:
            CFG[_g]["durata"] = int(os.environ.get("GIOCHI_VELOCE"))
    CFG.setdefault("rigori", {})["esito"] = 0.3
TALVERA = _load("giochi_talvera.json", {"punti": [], "campo": {}})


def stagione(now=None):
    """La stagione e' quella del server, uguale per tutti (Bolzano: un'ora avanti di UTC basta per il mese)."""
    forced = os.environ.get("GIOCHI_STAGIONE", "")
    if forced in ("primavera", "estate", "autunno", "inverno"):
        return forced
    m = time.gmtime((now or time.time()) + 3600).tm_mon
    return {12: "inverno", 1: "inverno", 2: "inverno", 3: "primavera", 4: "primavera", 5: "primavera",
            6: "estate", 7: "estate", 8: "estate"}.get(m, "autunno")


def punti_zona(z):
    return [p for p in TALVERA.get("punti", []) if z["x0"] <= p[0] <= z["x1"] and z["z0"] <= p[2] <= z["z1"]]


# ------------------------------------------------------------------------------------------ insetti (stessi conti del gioco)
# Come vola (o cammina) ogni tipo: raggio del giro, velocita' (rad/s), altezza, quanto sale e scende, se saltella.
MOTO = {
    "farfalla": (2.0, 4.0, 0.35, 0.6, 0.8, 1.6, 0.25, 0),
    "falena": (1.5, 3.0, 0.3, 0.45, 0.7, 1.4, 0.2, 0),
    "libellula": (3.0, 5.0, 0.6, 0.9, 1.0, 1.8, 0.15, 0),
    "ape": (1.5, 2.5, 0.7, 0.9, 0.5, 1.0, 0.2, 0),
    "coccinella": (0.5, 1.0, 0.12, 0.2, 0.03, 0.03, 0.0, 0),
    "cimice": (0.5, 1.0, 0.1, 0.18, 0.03, 0.03, 0.0, 0),
    "uccello": (1.0, 2.2, 0.22, 0.32, 0.02, 0.02, 0.12, 1),
    "scoiattolo": (2.0, 3.0, 0.3, 0.4, 0.02, 0.02, 0.15, 1),
    "ermellino": (2.0, 3.0, 0.35, 0.45, 0.02, 0.02, 0.1, 1),
    "tracce": (0.0, 0.0, 0.0, 0.0, 0.01, 0.01, 0.0, 0),
}


def pos_insetto(ins, t):
    """Dove sta l'insetto al tempo t della partita. Il gioco (scripts/giochi/foto_natura.gd) fa lo stesso conto."""
    cx, cy, cz = ins["c"]
    r, w, ph, b = ins["r"], ins["w"], ins["ph"], ins["b"]
    a = w * (t - ins["t0"]) + ph
    x = cx + r * math.cos(a) + 0.3 * r * math.sin(2.3 * a + ph)
    z = cz + r * math.sin(a) * 0.75 + 0.3 * r * math.cos(1.7 * a)
    if ins["salta"]:
        y = cy + b * abs(math.sin(5.0 * a))
    else:
        y = cy + b * math.sin(3.0 * a + ph)
    return (x, y, z)


# ------------------------------------------------------------------------------------------ giocatori e partite
class Giocatore:
    def __init__(self, me, ws):
        self.uid = me.get("uid") or ("id%d" % me["id"])
        self.nome = (me.get("name") or "Viaggiatore")[:20]
        self.me = me
        self.ws = ws
        self.punti = 0
        self.via = None          # quando si e' disconnesso
        self.ultimo = 0.0        # l'ultima azione (per non inondare)
        self.dati = {}


class Partita:
    gioco = ""

    def __init__(self, giochi, room, prova=False):
        self.g = giochi
        self.room = room
        self.prova = prova
        self.cfg = CFG.get(self.gioco, {})
        self.id = next(giochi.ids)
        self.stato = "attesa"
        self.giocatori = {}      # uid -> Giocatore
        self.parte = None        # quando parte (server)
        self.inizio = 0.0
        self.fine_t = 0.0
        self.chiusa = 0.0
        self.rng = random.Random()
        self._stato_t = 0.0

    # -- tempo e invii
    def now(self):
        return round(time.time() - self.inizio, 3) if self.stato != "attesa" else 0.0

    def manda(self, gioc, msg):
        if gioc.via is None:
            msg = dict(msg, t="g", id=self.id, gioco=self.gioco, now=self.now())
            self.g.send(gioc.ws, json.dumps(msg))

    def tutti(self, msg):
        for gioc in self.giocatori.values():
            self.manda(gioc, msg)

    def presenti(self):
        return [x for x in self.giocatori.values() if x.via is None]

    def lista(self):
        return [[x.nome, x.punti, x.uid, 0 if x.via is None else 1] for x in sorted(self.giocatori.values(), key=lambda x: -x.punti)]

    # -- sala
    def aggiungi(self, me, ws):
        gioc = Giocatore(me, ws)
        self.giocatori[gioc.uid] = gioc
        self.sala()
        return gioc

    def sala(self):
        resta = None if self.parte is None else max(0, round(self.parte - time.time()))
        self.tutti({"a": "sala", "nome": self.cfg.get("nome", self.gioco), "giocatori": [[x.nome, x.uid] for x in self.giocatori.values()],
                    "min": 1 if self.prova else self.cfg.get("min", 2), "max": self.cfg.get("max", 10), "parte_tra": resta, "prova": self.prova})

    def tick(self, t):
        if self.stato == "attesa":
            n = len(self.presenti())
            need = 1 if self.prova else self.cfg.get("min", 2)
            if n < need:
                if self.parte is not None:
                    self.parte = None
                    self.sala()
            elif self.parte is None:
                self.parte = t + (3 if self.prova else self.cfg.get("attesa", 20))
                self.sala()
            elif n >= self.cfg.get("max", 10) and self.parte - t > 5:
                self.parte = t + 5
                self.sala()
            if self.parte is not None and t >= self.parte:
                self.stato = "via"
                self.inizio = t
                self.avvia()
            return
        if self.stato == "via":
            for gioc in list(self.giocatori.values()):
                if gioc.via is not None and t - gioc.via > REJOIN:
                    gioc.dati["perso"] = True
            if not self.presenti():
                self.stato = "fine"
                self.chiusa = t
                return
            self.gioca(t)
            if self.stato == "via" and t - self._stato_t >= 1.0:
                self._stato_t = t
                self.tutti({"a": "stato", "resta": max(0, round(self.fine_t - t, 1)) if self.fine_t else None, "classifica": self.lista()})
            if self.fine_t and t >= self.fine_t and self.stato == "via":
                self.termina()

    def rientra(self, gioc, me, ws):
        gioc.me, gioc.ws, gioc.via = me, ws, None
        if self.stato == "via":
            self.manda(gioc, dict(self.dati_via(gioc), a="via", rientro=True))

    # -- da fare in ogni gioco
    def avvia(self):
        self.tutti({"a": "via"})

    def dati_via(self, gioc):
        return {}

    def gioca(self, t):
        pass

    def azione(self, gioc, msg, t):
        pass

    # -- la fine: classifica e premi (decisi qui)
    def termina(self):
        self.stato = "fine"
        self.chiusa = time.time()
        cl = sorted(self.giocatori.values(), key=lambda x: -x.punti)
        premi = CFG.get("premi", {}).get(self.gioco, {})
        pos = 0
        last = None
        for i, gioc in enumerate(cl):
            if last is None or gioc.punti != last:
                pos = i + 1
                last = gioc.punti
            mio = {}
            if not self.prova and len(self.giocatori) >= self.cfg.get("min", 2) and gioc.punti > 0:
                mio = dict(premi.get("tutti", {}))
                mio.update(premi.get(str(pos), {}))
            self.manda(gioc, {"a": "fine", "classifica": self.lista(), "posto": pos, "premi": mio, "extra": self.extra_fine(gioc)})
        self.g.log("fine", self.gioco, self.id, [(x.nome, x.punti) for x in cl])

    def extra_fine(self, gioc):
        return {}

    def vicino(self, gioc, p, maxd):
        """Il giocatore (secondo il relay) sta entro maxd metri da p, nel prato della citta', con una posizione recente?"""
        me = gioc.me
        if me.get("w") != "town" or time.time() - me.get("last_p", 0) > POS_STALE:
            return False
        return math.hypot(me["x"] - p[0], me["z"] - p[2]) <= maxd


# ------------------------------------------------------------------------------------------ 1. fotografi della natura
class FotoNatura(Partita):
    gioco = "foto"

    def avvia(self):
        cfg = self.cfg
        self.stagione = stagione()
        specie = cfg.get("stagioni", {}).get(self.stagione, [])
        pts = punti_zona(cfg["zona"]) or [[-320, 0, -40, 0]]
        dur = cfg.get("durata", 180)
        self.fine_t = self.inizio + dur
        vmin, vmax = cfg.get("vita", [25, 60])
        self.insetti = []
        pesi = [s.get("prob", 1) for s in specie]
        # quanti ce ne sono sempre: ognuno arriva, gira un po' e se ne va, e un altro prende il suo posto
        for k in range(cfg.get("insetti_insieme", 20)):
            t0 = -self.rng.uniform(0, vmax) if k % 2 == 0 else 0.0
            while t0 < dur:
                vita = self.rng.uniform(vmin, vmax)
                sp = self.rng.choices(specie, weights=pesi)[0]
                p = self.rng.choice(pts)
                rmin, rmax, wmin, wmax, hmin, hmax, b, salta = MOTO.get(sp["tipo"], MOTO["farfalla"])
                ins = {"n": len(self.insetti), "sp": sp["sp"], "nome": sp["nome"], "col": sp["col"], "hex": sp["hex"], "tipo": sp["tipo"],
                       "raro": bool(sp.get("raro", False)), "c": [p[0], round(p[1] + self.rng.uniform(hmin, hmax), 2), p[2]],
                       "r": round(self.rng.uniform(rmin, rmax), 2), "w": round(self.rng.uniform(wmin, wmax) * self.rng.choice([-1, 1]), 3),
                       "ph": round(self.rng.uniform(0, math.tau), 3), "b": b, "salta": salta,
                       "t0": round(max(t0, 0.0), 2), "t1": round(min(t0 + vita, dur + 1), 2)}
                if ins["t1"] > ins["t0"] + 3:
                    self.insetti.append(ins)
                t0 += vita + self.rng.uniform(1, 6)
        for gioc in self.giocatori.values():
            gioc.dati = {"colori": {}, "doppi": 0, "album": []}
        self.tutti(dict(self.dati_via(None), a="via"))
        self.g.log("via foto", self.id, self.stagione, len(self.insetti), "insetti", len(self.giocatori), "giocatori")

    def dati_via(self, gioc):
        return {"stagione": self.stagione, "insetti": self.insetti, "durata": self.cfg.get("durata", 180), "zona": self.cfg["zona"],
                "prova": self.prova, "punti": self.cfg.get("punti", {})}

    def azione(self, gioc, msg, t):
        if msg.get("a") != "foto":
            return
        cfg = self.cfg
        if t - gioc.ultimo < cfg.get("ogni", 0.5):
            return self.manda(gioc, {"a": "esito", "ok": False, "motivo": "piano"})
        gioc.ultimo = t
        try:
            n = int(msg.get("ins", -1))
            cam = [float(v) for v in msg.get("c", [])][:3]
            d = [float(v) for v in msg.get("d", [])][:3]
        except (TypeError, ValueError):
            return
        if not (0 <= n < len(self.insetti)) or len(cam) != 3 or len(d) != 3:
            return
        ins = self.insetti[n]
        tt = t - self.inizio
        no = lambda why: self.manda(gioc, {"a": "esito", "ok": False, "motivo": why, "ins": n})
        if not (ins["t0"] - 0.5 <= tt <= ins["t1"] + 0.5):
            return no("andato")
        # la fotocamera e' dove sta il giocatore (quello che sa il relay), non dove dice il gioco
        if not self.vicino(gioc, cam, 3.0):
            return no("lontano")
        dl = math.sqrt(sum(v * v for v in d)) or 1.0
        d = [v / dl for v in d]
        best = None
        for lag in (0.0, 0.15, 0.3, 0.5):
            p = pos_insetto(ins, tt - lag)
            v = [p[i] - cam[i] for i in range(3)]
            dist = math.sqrt(sum(x * x for x in v)) or 0.001
            ang = math.degrees(math.acos(max(-1.0, min(1.0, sum(v[i] * d[i] for i in range(3)) / dist))))
            if best is None or ang < best[1]:
                best = (dist, ang)
        dist, ang = best
        if dist > cfg.get("raggio", 12.0):
            return no("troppo_lontano")
        if dist < 0.3 or ang > cfg.get("angolo", 14.0) + math.degrees(math.atan2(0.4, dist)):
            return no("fuori")
        pt = cfg.get("punti", {})
        nuovo = ins["col"] not in gioc.dati["colori"]
        guadagno = 0
        if nuovo:
            guadagno = pt.get("colore_nuovo", 10) + (pt.get("raro", 15) if ins["raro"] else 0)
            gioc.dati["colori"][ins["col"]] = ins["sp"]
        elif gioc.dati["doppi"] < pt.get("doppi_max", 5):
            guadagno = pt.get("doppio", 1)
            gioc.dati["doppi"] += guadagno
        gioc.punti += guadagno
        if ins["sp"] not in gioc.dati["album"]:
            gioc.dati["album"].append(ins["sp"])
        self.manda(gioc, {"a": "esito", "ok": True, "ins": n, "sp": ins["sp"], "nome": ins["nome"], "col": ins["col"], "hex": ins["hex"],
                          "raro": ins["raro"], "nuovo": nuovo, "guadagno": guadagno, "punti": gioc.punti, "colori": len(gioc.dati["colori"]),
                          "stagione": self.stagione})

    def extra_fine(self, gioc):
        return {"colori": sorted(gioc.dati.get("colori", {}).keys()), "album": gioc.dati.get("album", []), "stagione": self.stagione}


# ------------------------------------------------------------------------------------------ il prato di trifogli del Talvera
M32 = 0xFFFFFFFF


def h32(a, b, c):
    """Un numero a caso ma sempre lo stesso per (a, b, c): lo stesso conto in scripts/giochi/prato_trifogli.gd."""
    x = (a * 0x9E3779B1 + b * 0x85EBCA77 + c * 0xC2B2AE3D) & M32
    x ^= x >> 16
    x = (x * 0x7FEB352D) & M32
    x ^= x >> 15
    x = (x * 0x846CA68B) & M32
    x ^= x >> 16
    return x


def _r2(v):
    return math.floor(v * 100.0 + 0.5) / 100.0


def piante_trifogli(punti, campo, par):
    """Le piantine del prato: [(id, x, z)], dai punti d'erba del Talvera (giochi_talvera.json) e data/trifogli.json.
    Il gioco fa lo stesso conto (PratoTrifogli.genera): stessi id, stessi posti."""
    seme = int(par.get("seme", 2026))
    ogni = max(1, int(par.get("ogni", 3)))
    pmin, pmax = [int(v) for v in par.get("piante", [3, 8])]
    raggio = float(par.get("raggio", 1.1))
    yaw = float(campo.get("yaw", 0.0))
    fx = (math.cos(yaw), math.sin(yaw))
    fz = (-math.sin(yaw), math.cos(yaw))
    cx, cz = float(campo.get("x", 1e9)), float(campo.get("z", 1e9))
    out = []
    for i, p in enumerate(punti):
        if h32(i, 1, seme) % ogni != 0:
            continue
        n = pmin + h32(i, 2, seme) % (pmax - pmin + 1)
        for j in range(n):
            pid = i * 16 + j
            a = h32(pid, 3, seme) / 4294967296.0 * math.tau
            r = math.sqrt(h32(pid, 4, seme) / 4294967296.0) * raggio
            x = _r2(float(p[0]) + math.cos(a) * r)
            z = _r2(float(p[2]) + math.sin(a) * r)
            dx, dz = x - cx, z - cz
            # (non sul campetto)
            if abs(dx * fx[0] + dz * fx[1]) < 10.2 and abs(dx * fz[0] + dz * fz[1]) < 8.6:
                continue
            out.append((pid, x, z))
    return out


class Prato:
    """I trifogli del Talvera stanno sempre nel mondo (il gioco li disegna tutti, da soli); quali sono quadrifogli lo sa
    solo il server (seme segreto, una parte su venti) e lo dice a ognuno solo per quelli che ha a pochi metri: niente
    radar. Uno raccolto e' di chi l'ha preso per primo (gli altri lo vedono sparire) e ricresce dopo un po'.
    Durante una caccia ai quadrifogli e' lo stesso prato: la partita lo fa ricrescere tutto nella sua zona al via."""

    def __init__(self, giochi):
        self.g = giochi
        cfg = CFG.get("prato", {})
        self.prob = float(cfg.get("quadrifogli", 0.05))
        self.dorati = float(cfg.get("dorati", 0.08))
        self.segreto = int(cfg.get("seme_segreto", 914273))
        self.vista = float(cfg.get("vista", 12.0))
        self.presa = float(cfg.get("presa", 2.0))
        self.ricresce = float(cfg.get("ricresce_ore", 2.0)) * 3600.0
        par = _load("trifogli.json", {})
        self.piante = piante_trifogli(TALVERA.get("punti", []), TALVERA.get("campo", {}), par)
        self.pos = {pid: (x, z) for pid, x, z in self.piante}
        self.quadri = {}          # id -> dorato
        self.griglia = {}         # cella 6 m -> [id] dei quadrifogli
        for pid, x, z in self.piante:
            if h32(pid, 7, self.segreto) / 4294967296.0 < self.prob:
                self.quadri[pid] = h32(pid, 8, self.segreto) / 4294967296.0 < self.dorati
                self.griglia.setdefault((int(math.floor(x / 6.0)), int(math.floor(z / 6.0))), []).append(pid)
        xs = [p[1] for p in self.piante] or [0.0]
        zs = [p[2] for p in self.piante] or [0.0]
        self.box = (min(xs) - 15.0, min(zs) - 15.0, max(xs) + 15.0, max(zs) + 15.0)
        self.presi = {}           # room -> {id: quando ricresce}
        self.visti = {}           # room -> {ws: {"set": set(), "t": 0.0}}

    def _presi(self, room):
        pr = self.presi.setdefault(room, {})
        now = time.time()
        for pid in [k for k, v in pr.items() if v <= now]:
            del pr[pid]
        return pr

    def vicini(self, room, x, z):
        pr = self._presi(room)
        cx, cz = int(math.floor(x / 6.0)), int(math.floor(z / 6.0))
        k = int(math.ceil(self.vista / 6.0))
        out = set()
        for i in range(cx - k, cx + k + 1):
            for j in range(cz - k, cz + k + 1):
                for pid in self.griglia.get((i, j), ()):
                    px, pz = self.pos[pid]
                    if pid not in pr and math.hypot(px - x, pz - z) <= self.vista:
                        out.add(pid)
        return out

    def mosso(self, room, ws, me, forza=False):
        """Il giocatore si e' mosso (il relay, a ogni posizione): i quadrifogli che ora ha vicino, e quelli che non ha piu'."""
        vv = self.visti.setdefault(room, {})
        st = vv.get(ws)
        now = time.time()
        if st is None:
            st = vv[ws] = {"set": set(), "t": 0.0}
        if not forza and now - st["t"] < 0.4:
            return
        st["t"] = now
        dentro = me.get("w") == "town" and self.box[0] <= me.get("x", 0) <= self.box[2] and self.box[1] <= me.get("z", 0) <= self.box[3]
        ora = self.vicini(room, me["x"], me["z"]) if dentro else set()
        if ora == st["set"]:
            return
        nuovi = sorted(ora - st["set"])
        via = sorted(st["set"] - ora)
        st["set"] = ora
        self.g.send(ws, json.dumps({"t": "g", "a": "q_vicini", "nuovi": [[pid, self.quadri[pid]] for pid in nuovi], "via": via}))

    def via(self, room, ws):
        self.visti.get(room, {}).pop(ws, None)

    def prendi(self, room, me, pid):
        """Il quadrifoglio pid preso da chi sta in me: (ok, motivo, dorato). Il server controlla da dove (la posizione del
        relay, non quella dichiarata), che sia davvero un quadrifoglio e che nessuno l'abbia preso prima."""
        if pid not in self.quadri:
            return False, "trifoglio", False
        pr = self._presi(room)
        if pid in pr:
            return False, "preso", False
        if me.get("w") != "town" or time.time() - me.get("last_p", 0) > POS_STALE:
            return False, "lontano", False
        px, pz = self.pos[pid]
        if math.hypot(me["x"] - px, me["z"] - pz) > self.presa:
            return False, "lontano", False
        pr[pid] = time.time() + self.ricresce
        chi = me.get("name") or "Viaggiatore"
        for ws, st in self.visti.get(room, {}).items():
            if pid in st["set"]:
                st["set"].discard(pid)
                self.g.send(ws, json.dumps({"t": "g", "a": "q_tolto", "id": pid, "chi": chi}))
        return True, "", self.quadri[pid]

    def ricresci(self, room, zona):
        """Tutti i quadrifogli raccolti nella zona tornano (il via di una caccia)."""
        pr = self._presi(room)
        for pid in list(pr):
            x, z = self.pos[pid]
            if zona["x0"] <= x <= zona["x1"] and zona["z0"] <= z <= zona["z1"]:
                del pr[pid]
        for st in self.visti.get(room, {}).values():
            st["t"] = 0.0

    def in_zona(self, zona):
        return [pid for pid in self.quadri if zona["x0"] <= self.pos[pid][0] <= zona["x1"] and zona["z0"] <= self.pos[pid][1] <= zona["z1"]]


# ------------------------------------------------------------------------------------------ 2. caccia ai quadrifogli
class Quadrifogli(Partita):
    """La caccia si fa nel prato vero (Prato): al via ricresce tutto nella zona; ogni piantina si puo' guardare (E), i
    quadrifogli valgono 1, i dorati 3. Chi trova piu' punti in tre minuti vince."""
    gioco = "quadrifogli"

    def avvia(self):
        cfg = self.cfg
        self.fine_t = self.inizio + cfg.get("durata", 180)
        prato = self.g.prato
        prato.ricresci(self.room, cfg["zona"])
        self.zona_q = prato.in_zona(cfg["zona"])
        for gioc in self.giocatori.values():
            gioc.dati = {"trovati": 0, "rari": 0}
        self.tutti(dict(self.dati_via(None), a="via"))
        self.g.log("via quadrifogli", self.id, len(self.zona_q), "nel prato")

    def dati_via(self, gioc):
        return {"durata": self.cfg.get("durata", 180), "zona": self.cfg["zona"], "totale": len(getattr(self, "zona_q", [])), "prova": self.prova}

    def gioca(self, t):
        pr = self.g.prato._presi(self.room)
        if self.zona_q and all(q in pr for q in self.zona_q):
            self.fine_t = min(self.fine_t, t + 1.0)

    def azione(self, gioc, msg, t):
        if msg.get("a") not in ("prendi", "raccogli"):
            return
        if t - gioc.ultimo < 0.25:
            return
        gioc.ultimo = t
        try:
            n = int(msg.get("q", msg.get("id", -1)))
        except (TypeError, ValueError):
            return
        ok, motivo, raro = self.g.prato.prendi(self.room, gioc.me, n)
        if not ok:
            return self.manda(gioc, {"a": "esito", "ok": False, "q": n, "motivo": motivo})
        pt = self.cfg.get("punti", {})
        guadagno = pt.get("raro", 3) if raro else pt.get("normale", 1)
        gioc.punti += guadagno
        gioc.dati["trovati"] += 1
        gioc.dati["rari"] += 1 if raro else 0
        self.manda(gioc, {"a": "esito", "ok": True, "q": n, "raro": raro, "guadagno": guadagno, "punti": gioc.punti,
                          "trovati": gioc.dati["trovati"]})
        pr = self.g.prato._presi(self.room)
        self.tutti({"a": "preso", "q": n, "chi": gioc.nome, "raro": raro, "restano": sum(1 for q in self.zona_q if q not in pr)})

    def extra_fine(self, gioc):
        return {"trovati": gioc.dati.get("trovati", 0), "rari": gioc.dati.get("rari", 0)}


# ------------------------------------------------------------------------------------------ 3. rigori al Talvera
NPC = "npc"

# -- la fisica del pallone (coordinate del campo: u verso la porta dei rigori, v di traverso, h in alto; metri, secondi)
# Deterministica: con lo stesso tiro e lo stesso portiere viene sempre uguale. Il gioco mostra la traiettoria che manda
# il server (campionata a 30 Hz): tutti vedono lo stesso pallone.
R_L = 9.0               # linea di porta
R_HALF = 1.5            # mezza porta (pali in v = +-1.5)
R_ALTA = 1.8            # traversa
R_POST = 0.06           # raggio di pali e traversa
R_BALL = 0.11           # raggio del pallone
R_NET = 10.0            # fondo della rete
R_SPOT = 3.0            # il dischetto
R_DT = 1.0 / 120.0
R_G = 9.0
R_KD = 0.012            # attrito dell'aria
R_KM = 0.45             # effetto (Magnus): accelerazione laterale = R_KM * eff * velocita' in avanti
R_TMAX = 2.6
R_KEEP_U = R_L - 0.2    # dove stanno le mani del portiere (appena davanti alla linea)


def _kp_hands(kp, t):
    """Mani (v, h) e piedi/anca (v, h) del portiere al tempo t."""
    v0 = kp["v0"]
    if t <= kp["t0"]:
        return (v0, 0.75), (v0, 0.35)
    p = min(1.0, (t - kp["t0"]) / max(kp["dur"], 0.01))
    # (a dive starts slow, the push of the legs, and flies at the end)
    e = p * p * (2.0 - p) if kp["tipo"] == "tuffo" else p * p * (3.0 - 2.0 * p)
    dv, dh = kp["dest"]
    hv = v0 + (dv - v0) * e
    hh = 0.75 + (dh - 0.75) * e + (math.sin(math.pi * e) * 0.15 if kp["tipo"] == "tuffo" else 0.0)
    # il corpo segue le mani a meta' strada (nel tuffo si allunga verso la palla)
    bv = v0 + (dv - v0) * e * 0.55
    bh = 0.35 + (max(0.2, dh * 0.5) - 0.35) * e
    return (hv, hh), (bv, bh)


def _dist_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    k = 0.0 if l2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    qx, qy = ax + dx * k, ay + dy * k
    return math.hypot(px - qx, py - qy)


def simula_tiro(vel, eff, kp=None, rng=None, solidi=True, fino_linea=False):
    """Il volo del pallone dal dischetto. vel = (vu, vv, vh) iniziali, eff = effetto (-1..1, + curva verso +v).
    kp: il portiere ({v0, t0, dur, dest, tipo}) o None. Torna {campioni, linea: (t, v, h) o None, palo, tocco, presa,
    dentro (e' finita in porta), respinta}."""
    u, v, h = R_SPOT, 0.0, R_BALL
    vu, vv, vh = vel
    w = eff
    t = 0.0
    out = {"campioni": [[0.0, u, v, h]], "linea": None, "palo": False, "tocco": None, "presa": False, "dentro": False, "respinta": False}
    held = False
    passata = False         # il portiere l'ha toccata ma non tenuta (mano debole): non la ritocca
    step = 0
    while t < R_TMAX:
        step += 1
        t = step * R_DT
        if held:
            (hv, hh), _ = _kp_hands(kp, t)
            # la palla in presa scende con il portiere, che dopo il tuffo finisce a terra
            fall = max(0.0, t - (kp["t0"] + kp["dur"]))
            u, v, h = R_KEEP_U - 0.1, hv, max(0.3, hh - fall * 1.8)
            if step % 4 == 0:
                out["campioni"].append([round(t, 3), round(u, 3), round(v, 3), round(h, 3)])
            continue
        # forze
        sp = math.sqrt(vu * vu + vv * vv + vh * vh)
        au = -R_KD * sp * vu - R_KM * w * vv * 0.3
        av = -R_KD * sp * vv + R_KM * w * vu
        ah = -R_KD * sp * vh - R_G
        vu += au * R_DT
        vv += av * R_DT
        vh += ah * R_DT
        w *= math.exp(-0.4 * R_DT)
        pu = u
        u += vu * R_DT
        v += vv * R_DT
        h += vh * R_DT
        # terra
        if h < R_BALL:
            h = R_BALL
            if vh < 0:
                vh = -vh * 0.5
                if vh < 0.5:
                    vh = 0.0
                vu *= 0.85
                vv *= 0.85
                w *= 0.6
        if h <= R_BALL + 1e-6 and vh == 0.0:
            hs = math.hypot(vu, vv)
            if hs > 0:
                k = max(0.0, hs - 1.6 * R_DT) / hs
                vu *= k
                vv *= k
        # la linea di porta
        if out["linea"] is None and pu < R_L <= u:
            f = (R_L - pu) / max(u - pu, 1e-9)
            lv, lh = v - vv * R_DT * (1 - f), h - vh * R_DT * (1 - f)
            out["linea"] = (round(t - R_DT * (1 - f), 3), lv, lh)
            if abs(lv) < R_HALF and lh < R_ALTA:
                out["dentro"] = True
        if fino_linea and out["linea"] is not None:
            break
        if solidi:
            # pali (verticali) e traversa (orizzontale): cilindri, la palla rimbalza
            for pv in (-R_HALF, R_HALF):
                if h < R_ALTA + R_BALL:
                    dx, dy = u - R_L, v - pv
                    d = math.hypot(dx, dy)
                    if d < R_POST + R_BALL and d > 1e-6:
                        nx, ny = dx / d, dy / d
                        u, v = R_L + nx * (R_POST + R_BALL), pv + ny * (R_POST + R_BALL)
                        dot = vu * nx + vv * ny
                        if dot < 0:
                            vu -= 1.6 * dot * nx
                            vv -= 1.6 * dot * ny
                            out["palo"] = True
            if abs(v) < R_HALF + R_BALL:
                dx, dy = u - R_L, h - R_ALTA
                d = math.hypot(dx, dy)
                if d < R_POST + R_BALL and d > 1e-6:
                    nx, ny = dx / d, dy / d
                    u, h = R_L + nx * (R_POST + R_BALL), R_ALTA + ny * (R_POST + R_BALL)
                    dot = vu * nx + vh * ny
                    if dot < 0:
                        vu -= 1.6 * dot * nx
                        vh -= 1.6 * dot * ny
                        out["palo"] = True
            # dentro la porta: la rete (fondo, lati, tetto) ferma la palla
            if out["dentro"] and u > R_L:
                if u > R_NET - R_BALL:
                    u = R_NET - R_BALL
                    vu = -abs(vu) * 0.12
                    vv *= 0.35
                    vh *= 0.35
                    w = 0.0
                if abs(v) > R_HALF - R_BALL:
                    v = math.copysign(R_HALF - R_BALL, v)
                    vv = -vv * 0.2
                    vu *= 0.5
                top = R_ALTA - 0.3 * (u - R_L) - R_BALL
                if h > top:
                    h = top
                    vh = -abs(vh) * 0.2
                if u < R_L + R_BALL:
                    u = R_L + R_BALL
                    vu = abs(vu) * 0.2
            # il portiere
            if kp is not None and not out["dentro"] and not passata and abs(u - R_KEEP_U) < 0.3:
                (hv, hh), (bv, bh) = _kp_hands(kp, t)
                d = min(math.hypot(v - hv, h - hh), _dist_seg(v, h, bv, bh, hv, hh) + 0.05)
                # the body itself (standing, or the first moment of a dive): a block from the feet to the shoulders
                if kp["tipo"] != "tuffo" or t < kp["t0"] + 0.12:
                    d = min(d, _dist_seg(v, h, bv, 0.05, bv, 1.0) - 0.08)
                if d < R_BALL + 0.11:
                    sp = math.sqrt(vu * vu + vv * vv + vh * vh)
                    out["tocco"] = round(t, 3)
                    # mano debole: le botte piu' forti a volte passano lo stesso
                    weak = max(0.0, min(0.55, (sp - 12.0) / 9.0))
                    if rng is not None and rng.random() < weak:
                        passata = True
                        vu *= 0.8
                        vv += (v - hv) * 2.0
                    elif sp < 10.0 and abs(v - kp["v0"]) < 0.9:
                        held = True
                        out["presa"] = True
                    else:
                        # respinta: via dalla porta, di lato o sopra la traversa
                        out["respinta"] = True
                        side = 1.0 if (v - hv if abs(v - hv) > 0.02 else (v if abs(v) > 0.02 else 1.0)) > 0 else -1.0
                        vu = -abs(vu) * 0.3 - 1.0
                        vv = side * (abs(vv) * 0.5 + 3.0 + (rng.random() * 1.5 if rng else 0.7))
                        vh = abs(vh) * 0.4 + (2.5 if h > 1.0 else 1.2)
                        w = 0.0
        if step % 4 == 0:
            out["campioni"].append([round(t, 3), round(u, 3), round(v, 3), round(h, 3)])
        if h <= R_BALL + 1e-6 and vh == 0.0 and math.hypot(vu, vv) < 0.05:
            break
        if abs(v) > 12 or u > 14 or u < -2:
            break
    last = out["campioni"][-1]
    if last[0] < round(t, 3):
        out["campioni"].append([round(t, 3), round(u, 3), round(v, 3), round(h, 3)])
    return out


def mira_tiro(tx, ty, pot, eff, alz):
    """La velocita' iniziale che porta la palla (senza nessuno di mezzo) al punto (tx, ty) della porta (tx: +-1 sui pali,
    ty: 0 a terra, 1 sulla traversa), con il tempo di volo della potenza; l'effetto cambia la strada, non la meta."""
    vt = tx * R_HALF
    ht = R_BALL + ty * (R_ALTA - R_BALL)
    T = (0.85 + (0.38 - 0.85) * pot) * (1.0 + 0.45 * alz)
    dist = R_L - R_SPOT
    vu, vv, vh = dist / T, vt / T, (ht - R_BALL + 0.5 * R_G * T * T) / T
    for _ in range(4):
        r = simula_tiro((vu, vv, vh), eff, solidi=False, fino_linea=True)
        if r["linea"] is None:
            vu *= 1.25
            continue
        tc, vc, hc = r["linea"]
        tc = max(tc, 0.05)
        vv += (vt - vc) / tc
        vh += (ht - hc) / tc
        vu *= tc / T
    return (vu, vv, vh)


def _reach(v0, dv, dh):
    """Dove arrivano davvero le mani: di lato al massimo 1.75 m di tuffo + le braccia, in alto 1.85 m al centro, meno ai lati."""
    side = max(-2.2, min(2.2, dv - v0))
    hmax = 1.85 - 0.45 * abs(side) / 2.2
    return (v0 + side, max(0.15, min(hmax, dh)))


def _tipo(side, dh):
    return "tuffo" if side > 0.5 else ("salto" if dh > 1.2 else "piedi")


def piano_umano(tuffo, v0):
    """Il portiere giocatore: si tuffa dove ha scelto (x: +-1 i pali, y: 0 a terra, 1 la traversa), appena parte il tiro."""
    dv, dh = _reach(v0, tuffo["x"] * R_HALF, 0.15 + tuffo["y"] * 1.65)
    side = abs(dv - v0)
    return {"v0": v0, "t0": 0.05, "dur": round(0.26 + 0.32 * side / 2.2, 3), "dest": [round(dv, 3), round(dh, 3)], "tipo": _tipo(side, dh)}


def piano_npc(vel, eff, v0, storia, rng):
    """Toni: a volte anticipa (indovina una parte, piu' spesso se chi tira e' andato due volte dallo stesso lato); di
    solito guarda partire la palla e dopo il tempo di reazione si tuffa dove la vede andare, ma senza l'effetto (i tiri
    a effetto lo ingannano) e con un po' d'errore."""
    lati = [1 if hh["x"] > 0.33 else (-1 if hh["x"] < -0.33 else 0) for hh in storia[-2:]]
    p_ant = 0.22 + (0.25 if len(lati) == 2 and lati[0] == lati[1] and lati[0] != 0 else 0.0)
    if rng.random() < p_ant:
        lato = lati[-1] if (lati and lati[-1] != 0 and rng.random() < 0.6) else rng.choice([-1, 1])
        gv, gh = lato * rng.uniform(0.9, 1.35), rng.uniform(0.25, 1.2)
        t0 = rng.uniform(-0.06, 0.04)
    else:
        tr = rng.uniform(0.22, 0.34)
        # dove sta e dove va la palla dopo tr secondi (quella vera, letta come se andasse dritta)
        r = simula_tiro(vel, eff, solidi=False)
        k = max(0, min(int(tr * 30), len(r["campioni"]) - 2))
        a, b = r["campioni"][k], r["campioni"][k + 1]
        dt = max(b[0] - a[0], 1e-3)
        su, sv, sh = (b[1] - a[1]) / dt, (b[2] - a[2]) / dt, (b[3] - a[3]) / dt
        if su <= 0.1:
            gv, gh = a[2], a[3]
        else:
            tc = (R_KEEP_U - a[1]) / su
            gv = a[2] + sv * tc
            gh = max(R_BALL, a[3] + sh * tc - 0.5 * R_G * tc * tc)
        gv += rng.gauss(0, 0.24)
        gh += rng.gauss(0, 0.16)
        t0 = tr
    dv, dh = _reach(v0, gv, gh)
    side = abs(dv - v0)
    return {"v0": v0, "t0": round(t0, 3), "dur": round(0.28 + 0.36 * side / 2.2, 3), "dest": [round(dv, 3), round(dh, 3)], "tipo": _tipo(side, dh)}


def calcia(tiro, tuffo, storia, rng):
    """Un rigore intero: l'errore di chi tira, la palla, il portiere (tuffo: quello scelto dal giocatore, o None per
    Toni), l'esito. Torna i campi del messaggio "esito" (senza tiratore, portiere e gol)."""
    pot = float(tiro.get("pot", 0.5))
    eff = float(tiro.get("eff", 0.0))
    alz = float(tiro.get("alz", 0.3))
    # la potenza giusta e' a meta' strada: troppo piano si para facile, troppo forte si sbaglia la mira (e si spara
    # alto); tanto effetto e i pallonetti sono piu' difficili da mettere giusti
    err = 0.05 + max(0.0, pot - 0.8) * 1.7 + max(0.0, 0.3 - pot) * 0.35 + abs(eff) * 0.08 + alz * 0.04
    tx = float(tiro.get("x", 0.0)) + rng.gauss(0, err)
    ty = float(tiro.get("y", 0.3)) + rng.gauss(0, err * 0.8) + max(0.0, pot - 0.82) * 1.8
    vel = mira_tiro(tx, ty, pot, eff, alz)
    v0 = round(rng.uniform(-0.25, 0.25), 3)
    kp = piano_umano(tuffo, v0) if tuffo is not None else piano_npc(vel, eff, v0, storia, rng)
    r = simula_tiro(vel, eff, kp, rng)
    linea = r["linea"]
    if r["dentro"]:
        esito = "gol"
    elif r["presa"] or r["respinta"]:
        esito = "parata"
    elif r["palo"]:
        esito = "palo"
    else:
        esito = "fuori"
    lv, lh = (linea[1], linea[2]) if linea else (tx * R_HALF, R_BALL + ty * (R_ALTA - R_BALL))
    kp["presa"] = r["presa"]
    kp["tocco"] = r["tocco"]
    dv, dh = kp["dest"]
    return {"esito": esito, "x": round(lv / R_HALF, 3), "y": round((lh - R_BALL) / (R_ALTA - R_BALL), 3), "pot": round(pot, 2),
            "eff": round(eff, 2), "alz": round(alz, 2), "palo": r["palo"], "traiettoria": r["campioni"], "portiere_piano": kp,
            "tuffo": {"dir": (1 if dv > 0.5 else (-1 if dv < -0.5 else 0)), "alto": 1 if dh > 1.0 else 0}}


class Rigori(Partita):
    """Due alla volta. In due: una sfida (5 rigori a testa, a turno, poi supplementari). Da 3 a 8: un piccolo torneo a
    eliminazione, una sfida dopo l'altra (gli altri guardano dalla tribuna). Da soli (prova): contro il portiere e il
    tiratore del computer. Chi tira sceglie mira e potenza, chi para il tuffo: il server mette l'imprecisione e decide."""
    gioco = "rigori"

    def avvia(self):
        self.campo = TALVERA.get("campo", {})
        uids = [x.uid for x in self.presenti()]
        self.rng.shuffle(uids)
        if len(uids) == 1:
            uids.append(NPC)
        self.turno_torneo = 1
        self.in_gara = uids          # chi e' ancora in gara (in ordine)
        self.eliminati = []          # [uid, turno] chi e' uscito e quando
        self.coppie = []
        self.sfida = None
        for gioc in self.giocatori.values():
            gioc.dati = {"gol": 0, "parate": 0}
        self.tutti(dict(self.dati_via(None), a="via"))
        self.g.log("via rigori", self.id, [self.nome(u) for u in uids])
        self.prossima_coppia()

    def rientra(self, gioc, me, ws):
        super().rientra(gioc, me, ws)
        if self.sfida is not None and self.stato == "via":
            self.manda_turno()

    def nome(self, uid):
        return "Portiere Toni" if uid == NPC else (self.giocatori[uid].nome if uid in self.giocatori else "?")

    def prossima_coppia(self):
        if not self.coppie:
            if len(self.in_gara) <= 1:
                return self.termina()
            # un turno del torneo: a coppie, chi resta spaiato passa (bye)
            ug = self.in_gara[:]
            self.coppie = [(ug[i], ug[i + 1]) for i in range(0, len(ug) - 1, 2)]
            self.passa = ug[-1] if len(ug) % 2 else None
            self.vincitori = [self.passa] if self.passa else []
        a, b = self.coppie.pop(0)
        self.sfida = {"a": a, "b": b, "gol": {a: 0, b: 0}, "tiri": {a: 0, b: 0}, "k": 0, "fase": "mira", "tiro": None, "tuffo": None, "t": 0.0,
                      "storia": []}
        self.nuovo_tiro()

    def nuovo_tiro(self):
        s = self.sfida
        s["tiratore"], s["portiere"] = (s["a"], s["b"]) if s["k"] % 2 == 0 else (s["b"], s["a"])
        s["fase"], s["tiro"], s["tuffo"] = "mira", None, None
        s["t"] = time.time() + self.cfg.get("scelta", 12)
        # il computer sceglie subito (ma non lo dice); il suo tuffo lo decide al momento del tiro (calcia)
        if s["tiratore"] == NPC:
            s["tiro"] = {"x": self.rng.uniform(-0.95, 0.95), "y": self.rng.uniform(0.05, 0.85), "pot": self.rng.uniform(0.45, 0.85),
                         "eff": self.rng.uniform(-0.6, 0.6) if self.rng.random() < 0.5 else 0.0, "alz": self.rng.uniform(0.0, 0.45)}
        if s["portiere"] == NPC:
            s["tuffo"] = {"npc": True}
        self.manda_turno()

    def manda_turno(self):
        s = self.sfida
        self.tutti({"a": "turno", "tiratore": [s["tiratore"], self.nome(s["tiratore"])], "portiere": [s["portiere"], self.nome(s["portiere"])],
                    "sfida": [[s["a"], self.nome(s["a"])], [s["b"], self.nome(s["b"])]], "gol": [s["gol"][s["a"]], s["gol"][s["b"]]],
                    "tiri": [s["tiri"][s["a"]], s["tiri"][s["b"]]], "storia": s["storia"][-12:], "scelta": self.cfg.get("scelta", 12),
                    "turno_torneo": self.turno_torneo, "in_gara": [[u, self.nome(u)] for u in self.in_gara]})

    def dati_via(self, gioc):
        return {"campo": self.campo, "tiri": self.cfg.get("tiri", 5), "prova": self.prova, "in_gara": [[u, self.nome(u)] for u in self.in_gara]}

    def gioca(self, t):
        s = self.sfida
        if s is None:
            return
        if s["fase"] == "mira":
            # chi non c'e' piu' (disconnesso) sceglie a caso, come chi non sceglie in tempo
            for who, key in ((s["tiratore"], "tiro"), (s["portiere"], "tuffo")):
                g = self.giocatori.get(who)
                if s[key] is None and (t >= s["t"] or (g is not None and g.via is not None)):
                    s[key] = ({"x": self.rng.uniform(-0.6, 0.6), "y": self.rng.uniform(0.1, 0.6), "pot": 0.5, "eff": 0.0, "alz": 0.3} if key == "tiro"
                              else {"x": self.rng.choice([-0.75, 0.0, 0.75]), "y": 0.2})
            if s["tiro"] is not None and s["tuffo"] is not None:
                self.risolvi(t)
        elif s["fase"] == "esito" and t >= s["t"]:
            self.dopo_tiro()

    def azione(self, gioc, msg, t):
        s = self.sfida
        if s is None or s["fase"] != "mira":
            return
        try:
            if msg.get("a") == "tiro" and gioc.uid == s["tiratore"] and s["tiro"] is None:
                s["tiro"] = {"x": max(-1.3, min(1.3, float(msg.get("x", 0)))), "y": max(-0.1, min(1.4, float(msg.get("y", 0.3)))),
                             "pot": max(0.0, min(1.0, float(msg.get("pot", 0.5)))), "eff": max(-1.0, min(1.0, float(msg.get("eff", 0.0)))),
                             "alz": max(0.0, min(1.0, float(msg.get("alz", 0.3))))}
                self.tutti({"a": "pronto", "chi": "tiratore"})
            elif msg.get("a") == "tuffo" and gioc.uid == s["portiere"] and s["tuffo"] is None:
                if "x" in msg or "y" in msg:
                    tx, ty = float(msg.get("x", 0.0)), float(msg.get("y", 0.3))
                else:
                    # (il vecchio tuffo: una parte, alto o basso)
                    d = max(-1, min(1, int(msg.get("dir", 0))))
                    tx, ty = d * 0.75, (0.7 if int(msg.get("alto", 0)) else 0.2)
                s["tuffo"] = {"x": max(-1.0, min(1.0, tx)), "y": max(0.0, min(1.0, ty))}
                self.tutti({"a": "pronto", "chi": "portiere"})
        except (TypeError, ValueError):
            return

    def risolvi(self, t):
        s = self.sfida
        tiro, tuffo = s["tiro"], s["tuffo"]
        tir = s["tiratore"]
        prima = [hh for hh in s["storia"] if hh["tiratore"] == tir]
        r = calcia(tiro, None if tuffo.get("npc") else tuffo, prima, self.rng)
        esito = r["esito"]
        x = r["x"]
        s["tiri"][tir] += 1
        if esito == "gol":
            s["gol"][tir] += 1
            if tir in self.giocatori:
                self.giocatori[tir].punti += 1
                self.giocatori[tir].dati["gol"] += 1
        elif esito == "parata" and s["portiere"] in self.giocatori:
            self.giocatori[s["portiere"]].dati["parate"] += 1
        s["storia"].append({"tiratore": tir, "esito": esito, "x": round(x, 2)})
        s["fase"] = "esito"
        s["t"] = t + self.cfg.get("esito", 5.0)
        self.tutti(dict(r, a="esito", tiratore=[tir, self.nome(tir)], portiere=[s["portiere"], self.nome(s["portiere"])],
                        gol=[s["gol"][s["a"]], s["gol"][s["b"]]]))

    def dopo_tiro(self):
        s = self.sfida
        s["k"] += 1
        a, b = s["a"], s["b"]
        n = self.cfg.get("tiri", 5)
        ta, tb, ga, gb = s["tiri"][a], s["tiri"][b], s["gol"][a], s["gol"][b]
        vince = None
        if ta <= n and tb <= n:
            # vinta prima della fine se l'altro non puo' piu' recuperare
            if ga > gb + (n - tb):
                vince = a
            elif gb > ga + (n - ta):
                vince = b
            elif ta == n and tb == n and ga != gb:
                vince = a if ga > gb else b
        elif ta == tb and ga != gb:
            vince = a if ga > gb else b          # supplementari: a coppie di tiri
        if vince is None and ta == tb and ta >= n + self.cfg.get("supplementari", 6):
            vince = self.rng.choice([a, b])      # (dopo tanti supplementari: la monetina)
        if vince is None:
            return self.nuovo_tiro()
        perde = b if vince == a else a
        self.eliminati.append([perde, self.turno_torneo])
        self.vincitori.append(vince)
        self.tutti({"a": "vince", "chi": [vince, self.nome(vince)], "gol": [ga, gb], "sfida": [[a, self.nome(a)], [b, self.nome(b)]]})
        s["fase"] = "pausa"
        if not self.coppie:
            self.in_gara = self.vincitori
            self.turno_torneo += 1
        self.sfida = None
        self.g.dopo(4.0, self._riprendi)

    def _riprendi(self):
        if self.stato == "via":
            self.prossima_coppia()

    def termina(self):
        # la classifica del torneo: chi vince in cima, poi chi e' uscito piu' tardi; a parita' i gol
        rank = {}
        if len(self.in_gara) == 1:
            rank[self.in_gara[0]] = 1000
        for uid, turno in self.eliminati:
            rank[uid] = turno
        for gioc in self.giocatori.values():
            gioc.punti = rank.get(gioc.uid, 0) * 100 + gioc.dati.get("gol", 0)
        super().termina()

    def lista(self):
        return [[x.nome, x.dati.get("gol", 0) if self.stato != "fine" else x.punti, x.uid, 0 if x.via is None else 1]
                for x in sorted(self.giocatori.values(), key=lambda x: -x.punti)]

    def extra_fine(self, gioc):
        return {"gol": gioc.dati.get("gol", 0), "parate": gioc.dati.get("parate", 0), "vince": [self.in_gara[0], self.nome(self.in_gara[0])] if len(self.in_gara) == 1 else None}


TIPI = {"foto": FotoNatura, "quadrifogli": Quadrifogli, "rigori": Rigori}

# I minigiochi del centro storico, ognuno nel suo modulo (server/sfilata.py, server/caffe.py): ognuno importa da qui
# Partita e le funzioni comuni (qui sono gia' definite) e si aggiunge a TIPI. Se un modulo manca, il relay va lo stesso.
for _mod, _nome in (("sfilata", "Sfilata"), ("caffe", "Caffe")):
    try:
        TIPI[_mod] = getattr(__import__(_mod), _nome)
    except Exception as _e:      # noqa: BLE001
        print("giochi: %s non disponibile (%r)" % (_mod, _e))


# ------------------------------------------------------------------------------------------ il gestore
class Giochi:
    def __init__(self, send, log):
        import itertools
        self.send = send
        self.log = log
        self.ids = itertools.count(1)
        self.partite = {}        # id -> Partita
        self.sale = {}           # (room, gioco) -> la partita in attesa
        self._dopo = []          # [(quando, funzione)]
        self.prato = Prato(self)  # i trifogli del Talvera, sempre li'

    def dopo(self, s, fn):
        self._dopo.append((time.time() + s, fn))

    def di(self, room, uid):
        for p in self.partite.values():
            if p.room == room and uid in p.giocatori and p.stato != "fine":
                return p
        return None

    def mosso(self, room, ws, me):
        """Una posizione nuova di un giocatore (dal relay): i quadrifogli che ha vicino."""
        self.prato.mosso(room, ws, me)

    def messaggio(self, room, ws, me, msg):
        a = msg.get("a")
        uid = me.get("uid") or ("id%d" % me["id"])
        p = self.di(room, uid)
        if a == "raccogli":
            # un quadrifoglio del prato: nella caccia vale punti, fuori e' un ritrovamento
            if p is not None and p.stato == "via" and p.gioco == "quadrifogli":
                return p.azione(p.giocatori[uid], msg, time.time())
            try:
                pid = int(msg.get("id", -1))
            except (TypeError, ValueError):
                return
            ok, motivo, raro = self.prato.prendi(room, me, pid)
            out = {"t": "g", "a": "q_trovato" if ok else "q_no", "id": pid, "raro": raro}
            if not ok:
                out["motivo"] = motivo
            return self.send(ws, json.dumps(out))
        if a == "prato":
            # (il gioco chiede subito i vicini: appena entrato, o tornato in citta')
            return self.prato.mosso(room, ws, me, True)
        if a == "entra":
            gioco = msg.get("gioco")
            if gioco not in TIPI:
                return self.send(ws, json.dumps({"t": "g", "a": "errore", "motivo": "gioco"}))
            if p is not None:
                g = p.giocatori[uid]
                if g.via is not None or g.ws is not ws:
                    p.rientra(g, me, ws)          # (rientro dopo una disconnessione: stessa partita, stessi punti)
                    p.sala() if p.stato == "attesa" else None
                    return
                if p.gioco == gioco:
                    return p.sala() if p.stato == "attesa" else None
                self.lascia_partita(p, uid)
            prova = bool(msg.get("prova", False))
            if prova:
                np = TIPI[gioco](self, room, prova=True)
                self.partite[np.id] = np
                np.aggiungi(me, ws)
                return
            key = (room, gioco)
            sala = self.sale.get(key)
            if sala is None or sala.stato != "attesa" or len(sala.giocatori) >= sala.cfg.get("max", 10):
                sala = TIPI[gioco](self, room)
                self.sale[key] = sala
                self.partite[sala.id] = sala
            sala.aggiungi(me, ws)
        elif a == "esci":
            if p is not None:
                self.lascia_partita(p, uid)
                self.send(ws, json.dumps({"t": "g", "a": "uscito"}))
        elif p is not None and p.stato == "via":
            p.azione(p.giocatori[uid], msg, time.time())

    def lascia_partita(self, p, uid):
        g = p.giocatori.get(uid)
        if g is None:
            return
        if p.stato == "attesa":
            del p.giocatori[uid]
            p.sala()
        else:
            g.via = time.time()
            g.dati["uscito"] = True

    def disconnesso(self, room, me):
        """Una connessione chiusa: in sala si esce, in partita si resta per un po' (si puo' rientrare)."""
        uid = me.get("uid") or ("id%d" % me["id"])
        p = self.di(room, uid)
        ws = me.get("_ws")
        if ws is not None:
            self.prato.via(room, ws)
        if p is None:
            return
        if p.stato == "attesa":
            p.giocatori.pop(uid, None)
            p.sala()
        else:
            p.giocatori[uid].via = time.time()

    def tick(self):
        t = time.time()
        due = [x for x in self._dopo if x[0] <= t]
        self._dopo = [x for x in self._dopo if x[0] > t]
        for _, fn in due:
            fn()
        for pid, p in list(self.partite.items()):
            if p.stato == "fine":
                if t - p.chiusa > 30:
                    del self.partite[pid]
                continue
            if p.stato == "attesa" and not p.giocatori:
                del self.partite[pid]
                continue
            p.tick(t)
            if p.stato != "attesa":
                for k, v in list(self.sale.items()):
                    if v is p:
                        del self.sale[k]
