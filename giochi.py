"""Minigiochi di bzaround, lato server: il relay (relay.py) gli passa i messaggi {"t": "g", ...} e lui tiene tutto.

Il server decide e controlla: le sale d'attesa e l'abbinamento, il via, il timer, i turni, cosa c'e' nel prato, se una foto
o una raccolta e' valida (dalla posizione che il relay conosce, mai da quella che il gioco dichiara), i punteggi, la
classifica, i premi, chi esce e chi rientra. Il gioco mostra e chiede; non manda mai punteggi.

Messaggi dal gioco (tutti con "t": "g"):
  {"a": "entra", "gioco": "foto"|"quadrifogli"|"rigori", "prova": false}   in sala (prova: da soli, senza premi)
  {"a": "esci"}                                                           lascia la sala o la partita
  {"a": "foto", "ins": n, "c": [x, y, z], "d": [x, y, z]}                  uno scatto: dove sta la fotocamera, dove guarda
  {"a": "prendi", "q": n}                                                 raccoglie un quadrifoglio
  {"a": "tiro", "x": -1..1, "y": 0..1, "pot": 0..1}                        il rigore: mira e potenza
  {"a": "tuffo", "dir": -1|0|1, "alto": 0|1}                              il portiere
Messaggi al gioco: "sala" (chi c'e', tra quanto si parte), "via" (la partita: i dati per disegnarla), "stato" (timer e
classifica, ogni secondo), "esito" (di uno scatto, una raccolta, un rigore), "vicini" (i quadrifogli che si vedono da
li'), "preso", "turno" (rigori), "fine" (classifica e premi di ciascuno), "errore".
Ogni messaggio porta "now": i secondi dall'inizio della partita, l'orologio comune.
"""
import json
import math
import os
import random
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GIOCHI = ("foto", "quadrifogli", "rigori")
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


# ------------------------------------------------------------------------------------------ 2. caccia ai quadrifogli
class Quadrifogli(Partita):
    gioco = "quadrifogli"

    def avvia(self):
        cfg = self.cfg
        self.fine_t = self.inizio + cfg.get("durata", 180)
        pts = punti_zona(cfg["zona"]) or [[-320, 0, 60, 0]]
        nascosti = [p for p in pts if p[3]]
        self.q = []
        want = cfg.get("quanti", 34)
        rari = cfg.get("rari", 4)
        tries = 0
        while len(self.q) < want and tries < 5000:
            tries += 1
            raro = len(self.q) < rari
            # (i rari stanno accanto a un albero o a un cespuglio: piu' difficili da vedere)
            src = nascosti if (raro or self.rng.random() < 0.3) and nascosti else pts
            p = self.rng.choice(src)
            x, z = p[0] + self.rng.uniform(-0.6, 0.6), p[2] + self.rng.uniform(-0.6, 0.6)
            if any(math.hypot(x - o["p"][0], z - o["p"][2]) < cfg.get("distanza", 3.0) for o in self.q):
                continue
            self.q.append({"n": len(self.q), "p": [round(x, 2), p[1], round(z, 2)], "raro": raro, "rot": round(self.rng.uniform(0, math.tau), 2),
                           "chi": None})
        for gioc in self.giocatori.values():
            gioc.dati = {"visti": set(), "trovati": 0, "rari": 0}
        self.tutti(dict(self.dati_via(None), a="via"))
        self.g.log("via quadrifogli", self.id, len(self.q))

    def dati_via(self, gioc):
        return {"durata": self.cfg.get("durata", 180), "zona": self.cfg["zona"], "totale": len(self.q), "prova": self.prova}

    def gioca(self, t):
        # a ognuno solo i quadrifogli vicini a lui: niente radar, niente quadrifogli visti da lontano o attraverso i muri
        vista = self.cfg.get("vista", 12.0)
        for gioc in self.presenti():
            if gioc.me.get("w") != "town":
                continue
            ora = {o["n"] for o in self.q if o["chi"] is None and math.hypot(gioc.me["x"] - o["p"][0], gioc.me["z"] - o["p"][2]) <= vista}
            if ora != gioc.dati["visti"]:
                nuovi = [o for o in self.q if o["n"] in ora - gioc.dati["visti"]]
                via = sorted(gioc.dati["visti"] - ora)
                gioc.dati["visti"] = ora
                self.manda(gioc, {"a": "vicini", "nuovi": [{"n": o["n"], "p": o["p"], "raro": o["raro"], "rot": o["rot"]} for o in nuovi], "via": via})
        if all(o["chi"] is not None for o in self.q):
            self.fine_t = min(self.fine_t, t + 1.0)

    def azione(self, gioc, msg, t):
        if msg.get("a") != "prendi":
            return
        if t - gioc.ultimo < 0.25:
            return
        gioc.ultimo = t
        try:
            n = int(msg.get("q", -1))
        except (TypeError, ValueError):
            return
        if not (0 <= n < len(self.q)):
            return
        o = self.q[n]
        if o["chi"] is not None:
            return self.manda(gioc, {"a": "esito", "ok": False, "q": n, "motivo": "preso"})
        if not self.vicino(gioc, o["p"], self.cfg.get("presa", 2.3)):
            return self.manda(gioc, {"a": "esito", "ok": False, "q": n, "motivo": "lontano"})
        o["chi"] = gioc.uid
        pt = self.cfg.get("punti", {})
        guadagno = pt.get("raro", 3) if o["raro"] else pt.get("normale", 1)
        gioc.punti += guadagno
        gioc.dati["trovati"] += 1
        gioc.dati["rari"] += 1 if o["raro"] else 0
        self.manda(gioc, {"a": "esito", "ok": True, "q": n, "raro": o["raro"], "guadagno": guadagno, "punti": gioc.punti,
                          "trovati": gioc.dati["trovati"]})
        for other in self.presenti():
            other.dati["visti"].discard(n)
        self.tutti({"a": "preso", "q": n, "chi": gioc.nome, "raro": o["raro"], "restano": sum(1 for x in self.q if x["chi"] is None)})

    def extra_fine(self, gioc):
        return {"trovati": gioc.dati.get("trovati", 0), "rari": gioc.dati.get("rari", 0)}


# ------------------------------------------------------------------------------------------ 3. rigori al Talvera
NPC = "npc"


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
        # il computer sceglie subito (ma non lo dice)
        if s["tiratore"] == NPC:
            s["tiro"] = {"x": self.rng.uniform(-0.95, 0.95), "y": self.rng.uniform(0.05, 0.85), "pot": self.rng.uniform(0.45, 0.85)}
        if s["portiere"] == NPC:
            s["tuffo"] = self.tuffo_npc(s)
        self.manda_turno()

    def tuffo_npc(self, s):
        # il portiere del computer: un po' indovina, un po' segue dove ha tirato l'ultima volta quel tiratore
        prima = [h for h in s["storia"] if h["tiratore"] == s["tiratore"]]
        if prima and self.rng.random() < 0.35:
            last = prima[-1]["x"]
            d = -1 if last < -0.33 else (1 if last > 0.33 else 0)
        else:
            d = self.rng.choice([-1, 0, 1, -1, 1])
        return {"dir": d, "alto": 1 if self.rng.random() < 0.35 else 0}

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
                    s[key] = ({"x": self.rng.uniform(-0.6, 0.6), "y": self.rng.uniform(0.1, 0.6), "pot": 0.5} if key == "tiro"
                              else {"dir": self.rng.choice([-1, 0, 1]), "alto": 0})
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
                s["tiro"] = {"x": max(-1.0, min(1.0, float(msg.get("x", 0)))), "y": max(0.0, min(1.0, float(msg.get("y", 0.3)))),
                             "pot": max(0.0, min(1.0, float(msg.get("pot", 0.5))))}
                self.tutti({"a": "pronto", "chi": "tiratore"})
            elif msg.get("a") == "tuffo" and gioc.uid == s["portiere"] and s["tuffo"] is None:
                d = int(msg.get("dir", 0))
                s["tuffo"] = {"dir": max(-1, min(1, d)), "alto": 1 if int(msg.get("alto", 0)) else 0}
                self.tutti({"a": "pronto", "chi": "portiere"})
        except (TypeError, ValueError):
            return

    def risolvi(self, t):
        s = self.sfida
        tiro, tuffo = s["tiro"], s["tuffo"]
        pot = tiro["pot"]
        # la potenza giusta e' a meta' strada: troppo piano si para facile, troppo forte si sbaglia la mira
        err = 0.06 + max(0.0, pot - 0.8) * 1.1 + max(0.0, 0.35 - pot) * 0.3
        x = tiro["x"] + self.rng.gauss(0, err)
        y = tiro["y"] + self.rng.gauss(0, err * 0.8) + max(0.0, pot - 0.85) * 0.9
        esito = "gol"
        if abs(x) > 1.08 or y > 1.08 or y < -0.05:
            esito = "fuori"
        elif abs(x) > 0.97 or y > 0.97:
            esito = "palo" if self.rng.random() < 0.5 else ("gol" if abs(x) <= 1.0 and y <= 1.0 else "fuori")
        if esito == "gol":
            zona = -1 if x < -0.33 else (1 if x > 0.33 else 0)
            alto = 1 if y > 0.55 else 0
            p_par = 0.0
            if tuffo["dir"] == zona:
                p_par = 0.78 if zona == 0 else 0.7
                p_par -= 0.3 * max(0.0, abs(x) - 0.75) / 0.25           # nell'angolino e' durissima
                p_par -= 0.25 * max(0.0, pot - 0.6)                     # e una botta forte scappa
                p_par += 0.1 if tuffo["alto"] == alto else -0.25
                p_par += 0.15 * max(0.0, 0.4 - pot)                     # un tiro moscio si blocca
            elif abs(tuffo["dir"] - zona) == 1 and abs(x) < 0.45:
                p_par = 0.15                                            # di piede, per caso
            if self.rng.random() < max(0.0, min(0.92, p_par)):
                esito = "parata"
        tir = s["tiratore"]
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
        self.tutti({"a": "esito", "esito": esito, "x": round(x, 3), "y": round(y, 3), "pot": round(pot, 2), "tuffo": tuffo,
                    "tiratore": [tir, self.nome(tir)], "portiere": [s["portiere"], self.nome(s["portiere"])],
                    "gol": [s["gol"][s["a"]], s["gol"][s["b"]]]})

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

    def dopo(self, s, fn):
        self._dopo.append((time.time() + s, fn))

    def di(self, room, uid):
        for p in self.partite.values():
            if p.room == room and uid in p.giocatori and p.stato != "fine":
                return p
        return None

    def messaggio(self, room, ws, me, msg):
        a = msg.get("a")
        uid = me.get("uid") or ("id%d" % me["id"])
        p = self.di(room, uid)
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
