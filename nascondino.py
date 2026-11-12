"""Minigioco 8, Il nascondino in citta' (Piazza del Grano, i Portici e i vicoli intorno), lato server. Importato da
giochi.py (TIPI["nascondino"]).

Il server decide tutto e non si fida del gioco:
- la mappa dell'area (nascondino_mappa.json, presa dalla fisica vera della citta': py tools/nascondino_nascondigli.py)
  dice dove si cammina, i muri e le colonne (fermano la vista), gli ostacoli bassi (fermano la vista di chi e'
  accovacciato), i nascondigli verificati, le zone e le uscite;
- una sessione e' fatta di giri: a ogni giro un cercatore, a turno (il primo a caso, poi tutti una volta prima che
  qualcuno lo rifaccia; chi esce e' saltato, chi entra dopo si mette in coda);
- un giro: "presenta" (chi cerca) -> "nascondi" (il cercatore conta alla bacheca, non riceve le posizioni di nessuno)
  -> "cerca" -> "risultato" (i punti del giro); alla fine "classifica" e i premi;
- il cercatore riceve il movimento di un nascosto SOLO quando lo vede (linea di vista sulla mappa): il relay chiede a
  vede() prima di passargli una posizione, e quando uno esce dalla vista gli arriva "far" (il gioco lo toglie);
- la cattura: il cercatore la chiede, il server la concede solo da vicino (1.7 m), con la vista libera da muri e
  colonne, nell'area, durante la ricerca, una volta sola;
- l'area: chi ne esce e' avvisato, dopo 5 secondi riportato dentro (e perde 5 punti); chi finisce in un posto che non
  si raggiunge a piedi (dentro un muro) e' riportato dentro;
- gli indizi del cercatore (la zona, la direzione, i passi di chi corre, "fuocherello"): con i loro tempi, mai il punto;
  i nascosti sentono quando il cercatore e' vicino, senza sapere da dove;
- da soli (prova): concorrenti NPC che si nascondono e un NPC che cerca, con la stessa vista e le stesse regole.

Messaggi dal gioco: {"a": "prendi"} (il cercatore prova a prendere chi ha davanti)
Messaggi al gioco: "via", "giro", "fase", "preso", "mancato", "indizio", "allerta", "fuori", "rientra", "dentro", "npc",
"risultato", poi "fine".
"""
import heapq
import json
import math
import os
import random
import time

from giochi import Partita

HERE = os.path.dirname(os.path.abspath(__file__))
BACHECA = (14.0, -52.0)            # la bacheca: il cercatore conta qui, girato verso di lei (yaw: verso nord)
PARTENZA = (17.5, -55.5)           # dove partono i nascosti
SPETTATORI = (23.0, -33.5)         # chi e' stato trovato guarda da qui, fuori dall'area (la strada a sud)
CATTURA = 1.7
# I contenitori in cui ci si nasconde DENTRO (un bidone, un cespuglio, uno scatolone): chi c'e' dentro non si vede e non
# si prende da fuori; il cercatore deve andarci e guardare dentro ("apri"). Da quanto vicino si entra e si apre.
CONT_TIPI = ("bidone", "cespuglio", "scatola")
CONT_ENTRA = 1.9
CONT_APRI = 2.0
VISTA = 40.0
CORRE = 4.2                        # m/s: chi va piu' veloce di cosi' fa rumore
ACCUCCIATI = ("accucciato", "furtivo", "crouch")

NOMI_NPC = [
    {"id": "biscotto", "nome": "Biscotto", "bravura": 0.8}, {"id": "ciuffo", "nome": "Ciuffo", "bravura": 0.45},
    {"id": "gnocchetto", "nome": "Gnocchetto", "bravura": 0.3}, {"id": "lampone", "nome": "Lampone", "bravura": 0.7},
    {"id": "pistacchio", "nome": "Pistacchio", "bravura": 0.6}, {"id": "fragolina", "nome": "Fragolina", "bravura": 0.85},
    {"id": "zenzero", "nome": "Zenzero", "bravura": 0.55}, {"id": "tortellino", "nome": "Tortellino", "bravura": 0.35},
]


def _veloce():
    return bool(os.environ.get("GIOCHI_VELOCE"))


# ------------------------------------------------------------------------------------------ la mappa
class Mappa:
    def __init__(self, path):
        with open(path, encoding="utf8") as f:
            m = json.load(f)
        self.x0, self.z0, self.c = m["x0"], m["z0"], m["cella"]
        self.w, self.h = m["w"], m["h"]
        self.r = m["righe"]
        self.spots = m.get("nascondigli", [])
        self.zone = m.get("zone", [])
        self.uscite = m.get("uscite", [])
        # la griglia dei percorsi: mezzo metro, si passa se i quattro quadretti si camminano
        self.pw, self.ph = self.w // 2, self.h // 2
        self.passa = [[all(self.ch(2 * i + a, 2 * j + b) == "." for a in (0, 1) for b in (0, 1)) for i in range(self.pw)] for j in range(self.ph)]

    def ch(self, i, j):
        if i < 0 or j < 0 or i >= self.w or j >= self.h:
            return " "
        return self.r[j][i]

    def ij(self, x, z):
        return int(math.floor((x - self.x0) / self.c)), int(math.floor((z - self.z0) / self.c))

    def xz(self, i, j):
        return self.x0 + (i + 0.5) * self.c, self.z0 + (j + 0.5) * self.c

    def dentro(self, x, z, tolleranza=2):
        """Nell'area: su un quadretto raggiungibile, o a meno di mezzo metro da uno (chi sta contro un muro)."""
        i, j = self.ij(x, z)
        for dj in range(-tolleranza, tolleranza + 1):
            for di in range(-tolleranza, tolleranza + 1):
                if self.ch(i + di, j + dj) == ".":
                    return True
        return False

    def vicino_dentro(self, x, z):
        """Il quadretto raggiungibile piu' vicino (per riportare dentro chi esce)."""
        i0, j0 = self.ij(x, z)
        for r in range(0, 200):
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    if self.ch(i0 + di, j0 + dj) == ".":
                        return self.xz(i0 + di, j0 + dj)
        return PARTENZA

    def vede(self, a, b, basso_blocca=False):
        """Linea di vista da a a b ((x, z)): un muro o una colonna la fermano sempre, un ostacolo basso solo se chi e'
        guardato e' accovacciato. Si cammina sulla retta a passi di 12 cm."""
        (ax, az), (bx, bz) = a, b
        d = math.hypot(bx - ax, bz - az)
        n = max(1, int(d / (self.c * 0.5)))
        for k in range(1, n):
            t = k / n
            i, j = self.ij(ax + (bx - ax) * t, az + (bz - az) * t)
            c = self.ch(i, j)
            if c == "#" or (basso_blocca and c == "b"):
                return False
        return True

    def vede_corpo(self, occhio, corpo, accucciato):
        """Vede qualcuno: almeno un punto del suo corpo (il centro e quattro punti a 25 cm) in vista."""
        x, z = corpo
        for dx, dz in ((0, 0), (0.25, 0), (-0.25, 0), (0, 0.25), (0, -0.25)):
            if self.vede(occhio, (x + dx, z + dz), accucciato):
                return True
        return False

    def zona(self, x, z):
        if z < -83.0:
            k = 0
        elif x > 29.0 and z < -73.0:
            k = 1
        elif x > 37.0:
            k = 3
        elif x > 11.0 and z > -75.0:
            k = 2
        elif z > -55.0:
            k = 4
        else:
            k = 5
        return self.zone[k][1] if k < len(self.zone) else "qui intorno"

    # -- i percorsi degli NPC: A* sul mezzo metro, poi la corda tirata (si taglia dove si vede)
    def percorso(self, a, b):
        def pij(x, z):
            return max(0, min(self.pw - 1, int((x - self.x0) / (self.c * 2)))), max(0, min(self.ph - 1, int((z - self.z0) / (self.c * 2))))

        def pxz(i, j):
            return self.x0 + (i + 0.5) * self.c * 2, self.z0 + (j + 0.5) * self.c * 2
        s, g = pij(*a), pij(*b)
        s, g = self._libero(s), self._libero(g)
        if s is None or g is None:
            return [b]
        aperti = [(0.0, s)]
        da = {s: None}
        costo = {s: 0.0}
        passi = 0
        while aperti and passi < 25000:
            passi += 1
            _, cur = heapq.heappop(aperti)
            if cur == g:
                break
            ci, cj = cur
            for di, dj, w in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, 1.41), (1, -1, 1.41), (-1, 1, 1.41), (-1, -1, 1.41)):
                ni, nj = ci + di, cj + dj
                if not (0 <= ni < self.pw and 0 <= nj < self.ph) or not self.passa[nj][ni]:
                    continue
                if di and dj and not (self.passa[cj][ni] and self.passa[nj][ci]):
                    continue
                nc = costo[cur] + w
                if nc < costo.get((ni, nj), 1e9):
                    costo[(ni, nj)] = nc
                    da[(ni, nj)] = cur
                    heapq.heappush(aperti, (nc + math.hypot(g[0] - ni, g[1] - nj), (ni, nj)))
        if g not in da:
            return [b]
        celle = []
        cur = g
        while cur is not None:
            celle.append(pxz(*cur))
            cur = da[cur]
        celle.reverse()
        celle[-1] = b
        # (la corda tirata: da ogni punto al piu' lontano che si vede)
        out = [celle[0]]
        k = 0
        while k < len(celle) - 1:
            nxt = k + 1
            for m in range(len(celle) - 1, k, -1):
                if self.vede(celle[k], celle[m], True) and math.hypot(celle[m][0] - celle[k][0], celle[m][1] - celle[k][1]) < 14:
                    nxt = m
                    break
            out.append(celle[nxt])
            k = nxt
        return out[1:]

    def _libero(self, p):
        i, j = p
        for r in range(0, 8):
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    ni, nj = i + di, j + dj
                    if 0 <= ni < self.pw and 0 <= nj < self.ph and self.passa[nj][ni]:
                        return (ni, nj)
        return None


MAPPA = None


def mappa():
    global MAPPA
    if MAPPA is None:
        MAPPA = Mappa(os.path.join(HERE, "nascondino_mappa.json"))
    return MAPPA


def direzione(dx, dz):
    """La direzione a parole (nord e' -z nel gioco, come sulla mappa del telefono)."""
    a = math.degrees(math.atan2(dx, -dz)) % 360
    return ["nord", "nord-est", "est", "sud-est", "sud", "sud-ovest", "ovest", "nord-ovest"][int((a + 22.5) // 45) % 8]


# ------------------------------------------------------------------------------------------ gli NPC
class Npc:
    """Un concorrente della citta' (solo da soli): si nasconde o cerca, con le stesse regole di tutti."""

    def __init__(self, d, rng):
        self.uid = "npc:" + d["id"]
        self.nome = d["nome"]
        self.bravura = d["bravura"]
        self.x, self.z = PARTENZA
        self.yaw = 0.0
        self.anim = "idle"
        self.path = []
        self.vel = 0.0
        self.spot = None
        self.stato = "fermo"
        self.t_stato = 0.0
        self.allerta = {}            # (cercatore) uid -> quanto l'ha notato (0..1)
        self.bersaglio = None
        self.visti = set()           # (cercatore) i nascondigli gia' guardati
        self.ultimo_visto = None
        self.rng = rng
        self.cambi = 0
        self.aperti = set()          # (cercatore) i contenitori gia' aperti in questo giro

    def vai(self, dove, vel, anim):
        self.path = mappa().percorso((self.x, self.z), dove)
        self.vel = vel
        self.anim = anim

    def muovi(self, dt):
        if not self.path:
            return True
        rest = self.vel * dt
        while rest > 0 and self.path:
            tx, tz = self.path[0]
            d = math.hypot(tx - self.x, tz - self.z)
            if d < 1e-3:
                self.path.pop(0)
                continue
            self.yaw = math.atan2(tx - self.x, tz - self.z)
            if d <= rest:
                self.x, self.z = tx, tz
                rest -= d
                self.path.pop(0)
            else:
                self.x += (tx - self.x) / d * rest
                self.z += (tz - self.z) / d * rest
                rest = 0
        return not self.path


# ------------------------------------------------------------------------------------------ la partita
class Nascondino(Partita):
    gioco = "nascondino"

    def cfgv(self, k, d):
        v = self.cfg.get(k, d)
        if _veloce():
            v = {"presenta": 2.0, "nascondi": 8.0, "cerca": 45.0, "risultato": 4.0, "classifica": 4.0}.get(k, v)
        return v

    # -- la sessione
    def avvia(self):
        self.m = mappa()
        self.npc = {}
        self.punti = {}                # uid -> punti della sessione
        self.storia = []               # i risultati dei giri
        self.fatti = set()             # chi ha gia' cercato
        umani = [g.uid for g in self.giocatori.values()]
        # (in pochi: gli NPC riempiono i posti, da soli o in due; l'utente: "se manca gente riempi con npc")
        solo = self.prova or len(umani) == 1
        if solo or len(umani) < int(self.cfg.get("min", 3)):
            for d in self.rng.sample(NOMI_NPC, 4 if solo else max(2, 4 - len(umani))):
                n = Npc(d, self.rng)
                self.npc[n.uid] = n
        self.tutti_uid = umani + list(self.npc.keys())     # chi gioca (l'ordine dei turni e' a parte)
        self.ordine = list(self.tutti_uid)
        self.rng.shuffle(self.ordine)
        if self.npc and not solo:
            # in due con gli NPC: cerca ognuno dei giocatori una volta (gli NPC si nascondono)
            self.ordine = list(umani)
            self.rng.shuffle(self.ordine)
        if solo:
            # da soli: due giri, uno a cercare e uno a nascondersi (l'ordine a caso)
            me = umani[0]
            altro = next(u for u in self.ordine if u != me)
            self.ordine = [me, altro] if self.rng.random() < 0.5 else [altro, me]
        for u in self.tutti_uid:
            self.punti.setdefault(u, 0)
        for g in self.giocatori.values():
            g.dati = {"fuori_t": 0.0, "rientri": 0}
        self.giri = len(self.ordine) if not solo else 2
        self.cont = self._contenitori()
        self.n_giro = 0
        self.fase = "presenta"
        self.tutti(dict(self.dati_via(None), a="via"))
        self.g.log("via nascondino", self.id, len(self.giocatori), "giocatori", len(self.npc), "npc")
        self._nuovo_giro(time.time())

    def nome(self, uid):
        if uid in self.npc:
            return self.npc[uid].nome
        g = self.giocatori.get(uid)
        return g.nome if g else "?"

    def presente(self, uid):
        if uid in self.npc:
            return True
        g = self.giocatori.get(uid)
        # (chi perde la linea per un attimo resta in gioco dieci secondi: puo' rientrare)
        return g is not None and not g.dati.get("perso") and not g.dati.get("uscito") and (g.via is None or time.time() - g.via < 10.0)

    def dati_via(self, gioc):
        return {"prova": self.prova, "giri": self.giri, "ordine": [[u, self.nome(u), u in self.npc] for u in self.ordine],
                "partecipanti": [[u, self.nome(u), u in self.npc] for u in getattr(self, "tutti_uid", [])],
                "npc": [[n.uid, n.nome] for n in self.npc.values()], "punti": self.punti,
                "durate": {k: self.cfgv(k, d) for k, d in (("presenta", 5.0), ("nascondi", 30.0), ("cerca", 100.0), ("risultato", 10.0), ("classifica", 12.0))},
                "bacheca": BACHECA, "partenza": PARTENZA, "spettatori": SPETTATORI,
                "contenitori": [[c["x"], c["z"], c["yaw"], c["tipo"]] for c in getattr(self, "cont", [])],
                "giro": self._giro_dati(gioc) if getattr(self, "giro", None) else None, "fase": getattr(self, "fase", "")}

    def _giro_dati(self, gioc):
        gr = self.giro
        d = {"n": self.n_giro, "di": self.giri, "cercatore": [gr["cercatore"], self.nome(gr["cercatore"])], "fase": self.fase,
             "resta": max(0.0, round(self.fase_t - time.time(), 1)), "nascosti": len(gr["nascosti"]), "presi": len(gr["presi"])}
        if gioc is not None:
            d["ruolo"] = "cercatore" if gioc.uid == gr["cercatore"] else ("preso" if gioc.uid in gr["presi"] else ("nascosto" if gioc.uid in gr["nascosti"] else "spettatore"))
        return d

    # -- i contenitori (bidoni, cespugli, scatoloni)
    def _contenitori(self):
        """Una dozzina, sparsi: sui nascondigli verificati della mappa (dove si sta davvero, accanto a un muro), almeno
        9 m l'uno dall'altro, due o tre per zona, a turno bidone, cespuglio, scatolone."""
        spots = sorted(self.m.spots, key=lambda s: (s.get("zona", 0), -s["coperto"], s["x"], s["z"]))
        rng = random.Random(4242)
        rng.shuffle(spots)
        out = []
        per_zona = {}
        for s in spots:
            z = s.get("zona", 0)
            if per_zona.get(z, 0) >= 3:
                continue
            if any(math.hypot(s["x"] - c["x"], s["z"] - c["z"]) < 9.0 for c in out):
                continue
            if math.hypot(s["x"] - BACHECA[0], s["z"] - BACHECA[1]) < 8.0 or math.hypot(s["x"] - PARTENZA[0], s["z"] - PARTENZA[1]) < 5.0:
                continue
            out.append({"x": round(s["x"], 2), "z": round(s["z"], 2), "yaw": round(s.get("verso", 0.0), 3),
                        "tipo": CONT_TIPI[len(out) % 3], "chi": None, "aperto_t": 0.0})
            per_zona[z] = per_zona.get(z, 0) + 1
            if len(out) >= 14:
                break
        return out

    def dentro_di(self, uid):
        for i, c in enumerate(getattr(self, "cont", [])):
            if c["chi"] == uid:
                return i
        return -1

    def _ai_non_cercatori(self, msg):
        gr = getattr(self, "giro", None)
        for g in self.giocatori.values():
            if gr is None or g.uid != gr["cercatore"]:
                self.manda(g, msg)

    def _entra(self, uid, i):
        gr = getattr(self, "giro", None)
        if gr is None or self.fase not in ("nascondi", "cerca") or uid not in gr["nascosti"] or uid in gr["presi"]:
            return False
        if not (0 <= i < len(self.cont)) or self.cont[i]["chi"] is not None or self.dentro_di(uid) >= 0:
            return False
        p = self.pos(uid)
        c = self.cont[i]
        if p is None or math.hypot(p[0] - c["x"], p[1] - c["z"]) > CONT_ENTRA:
            return False
        c["chi"] = uid
        if uid in self.npc:
            n = self.npc[uid]
            n.x, n.z, n.anim, n.path = c["x"], c["z"], "dentro", []
        g = self.giocatori.get(uid)
        if g:
            self.manda(g, {"a": "in_cont", "c": i, "x": c["x"], "z": c["z"], "tipo": c["tipo"]})
        self._ai_non_cercatori({"a": "cont", "c": i, "chi": uid})
        return True

    def _esci(self, uid):
        i = self.dentro_di(uid)
        if i < 0:
            return
        c = self.cont[i]
        c["chi"] = None
        # fuori, davanti al contenitore (dalla parte della strada)
        ex = (round(c["x"] + math.sin(c["yaw"]) * 0.9, 2), round(c["z"] + math.cos(c["yaw"]) * 0.9, 2))
        if uid in self.npc:
            n = self.npc[uid]
            n.x, n.z = ex
        g = self.giocatori.get(uid)
        if g:
            self.manda(g, {"a": "fuori_cont", "c": i, "x": ex[0], "z": ex[1]})
        self._ai_non_cercatori({"a": "cont", "c": i, "chi": ""})

    def _apri(self, cerc, i, t):
        self.g.log("nascondino apri", self.id, self.nome(cerc), i)
        gr = self.giro
        if self.fase != "cerca" or cerc != gr["cercatore"] or not (0 <= i < len(self.cont)):
            return
        c = self.cont[i]
        if t - c["aperto_t"] < 1.0:
            return
        p = self.pos(cerc)
        if p is None or math.hypot(p[0] - c["x"], p[1] - c["z"]) > CONT_APRI:
            return
        c["aperto_t"] = t
        chi = c["chi"]
        self.tutti({"a": "aperto", "c": i, "vuoto": chi is None})
        if chi is None:
            g = self.giocatori.get(cerc)
            if g:
                self.manda(g, {"a": "vuoto", "c": i})
            return
        self._esci(chi)
        self._cattura(cerc, chi, t)

    def _prossimo_cercatore(self):
        # chi non ha ancora cercato, nell'ordine; se l'hanno fatto tutti, si ricomincia il giro dei turni
        for u in self.ordine:
            if u not in self.fatti and self.presente(u):
                return u
        return None

    def _nuovo_giro(self, t):
        cerc = self._prossimo_cercatore() if self.n_giro < self.giri else None
        if cerc is None:
            return self._fine_sessione(t)
        self.n_giro += 1
        self.fatti.add(cerc)
        nascosti = [u for u in self.tutti_uid if u != cerc and self.presente(u)]
        for c in getattr(self, "cont", []):
            c["chi"] = None          # (un giro nuovo: i contenitori di nuovo vuoti)
        self.giro = {"cercatore": cerc, "nascosti": set(nascosti), "presi": [], "inizio_cerca": 0.0, "durata": 0.0,
                     "visibili": set(), "indizi": [], "ind_t": 0.0, "passi_t": 0.0, "vicino_liv": 0, "vicino_t": 0.0,
                     "allerte": {}, "zone_dette": set(), "pos_prima": {}, "punti": {}, "presi_t": {}}
        self._passa("presenta", float(self.cfgv("presenta", 5.0)), t)
        # i posti: il cercatore alla bacheca, gli altri alla partenza (sparsi), i trovati di prima tornano in gioco
        for i, u in enumerate([cerc] + nascosti):
            if u == cerc:
                pos = (BACHECA[0], BACHECA[1] + 0.9, math.pi)      # (a sud della bacheca, girato verso di lei)
            else:
                a = i * 2.4
                pos = (PARTENZA[0] + math.cos(a) * 1.4, PARTENZA[1] + math.sin(a) * 1.4, math.pi)
            if u in self.npc:
                n = self.npc[u]
                n.x, n.z, n.yaw = pos
                n.path, n.stato, n.anim, n.allerta, n.bersaglio, n.visti, n.spot, n.cambi = [], "fermo", "idle", {}, None, set(), None, 0
                n.aperti = set()
            else:
                g = self.giocatori.get(u)
                if g:
                    self.manda(g, {"a": "posto", "x": round(pos[0], 2), "z": round(pos[1], 2), "yaw": round(pos[2], 3)})
                    g.dati["fuori_t"] = 0.0
        self.g.log("nascondino giro", self.id, self.n_giro, "cerca", self.nome(cerc))

    def _passa(self, fase, dur, t=None):
        t = t or time.time()
        self.fase = fase
        self.fase_t = t + dur
        if fase == "cerca":
            self.giro["inizio_cerca"] = t
            self.giro["durata"] = dur
        for gioc in self.giocatori.values():
            self.manda(gioc, {"a": "fase", "fase": fase, "resta": round(dur, 1), "giro": self._giro_dati(gioc), "punti": self.punti})
        # (il cercatore, finita la conta, deve smettere di non vedere: chi e' in vista gli arriva alla prossima ronda)
        self.giro["visibili"] = set()
        if fase == "nascondi":
            # comincia la conta: al cercatore spariscono tutti (fermi dove li aveva visti l'ultima volta, li avrebbe ancora)
            gc = self.giocatori.get(self.giro["cercatore"])
            if gc and gc.via is None:
                for u in self.giro["nascosti"]:
                    o = self.giocatori.get(u)
                    if o and o.me.get("id") is not None:
                        self.g.send(gc.ws, json.dumps({"t": "far", "id": o.me["id"]}))

    # -- chi e' dove
    def pos(self, uid):
        i = self.dentro_di(uid) if hasattr(self, "cont") else -1
        if i >= 0:
            return (self.cont[i]["x"], self.cont[i]["z"])
        if uid in self.npc:
            n = self.npc[uid]
            return (n.x, n.z)
        g = self.giocatori.get(uid)
        if g and g.me.get("x") is not None and g.me.get("w", "town") == "town":
            return (g.me["x"], g.me["z"])
        return None

    def accucciato(self, uid):
        if uid in self.npc:
            return self.npc[uid].anim in ACCUCCIATI
        g = self.giocatori.get(uid)
        if not g or not g.me.get("p"):
            return False
        try:
            return json.loads(g.me["p"]).get("a") in ACCUCCIATI
        except ValueError:
            return False

    def vede(self, cerc, uid):
        """Il cercatore vede uid? (per passargli o no il suo movimento, e per gli NPC)."""
        a, b = self.pos(cerc), self.pos(uid)
        if a is None or b is None:
            return False
        if self.dentro_di(uid) >= 0:
            return False          # (dentro un bidone, un cespuglio, uno scatolone: solo aprendolo)
        d = math.hypot(a[0] - b[0], a[1] - b[1])
        if d < 2.0:
            return True
        if d > VISTA:
            return False
        return self.m.vede_corpo(a, b, self.accucciato(uid))

    def nascosto_a(self, dst_uid, src_uid):
        """Il relay chiede: dst puo' ricevere il movimento di src? No se dst cerca e src e' un nascosto che non vede
        (durante la conta nessuno)."""
        gr = getattr(self, "giro", None)
        if self.stato != "via" or gr is None or dst_uid != gr["cercatore"] or src_uid not in gr["nascosti"] or src_uid in gr["presi"]:
            return False
        if self.fase in ("presenta", "nascondi"):
            return True
        if self.fase == "cerca":
            return src_uid not in gr["visibili"]
        return False

    def fuori_mappa(self):
        """I partecipanti non compaiono sulla mappa del telefono di nessuno, finche' si gioca."""
        return {g.me.get("id") for g in self.giocatori.values()}

    # -- il tempo
    def gioca(self, t):
        gr = self.giro if hasattr(self, "giro") else None
        if self.fase == "classifica":
            if t >= self.fase_t:
                self.termina()
            return
        if gr is None:
            return
        # chi cerca e' uscito: il giro finisce senza punti
        if not self.presente(gr["cercatore"]) and self.fase in ("presenta", "nascondi", "cerca"):
            self.tutti({"a": "avviso", "testo": "%s e' uscito: il giro finisce qui." % self.nome(gr["cercatore"])})
            return self._fine_giro(t, annullato=True)
        for u in list(gr["nascosti"]):
            if not self.presente(u):
                gr["nascosti"].discard(u)
        for c in self.cont:
            if c["chi"] is not None and not self.presente(c["chi"]):
                c["chi"] = None
        if not self.prova and not self.npc and sum(1 for g in self.giocatori.values() if self.presente(g.uid)) < int(self.cfg.get("min", 3)):
            self.tutti({"a": "avviso", "testo": "Siete rimasti in pochi: la partita si chiude qui."})
            return self._fine_sessione(t)
        dt = 0.25
        self._area(t)
        self._npc(t, dt)
        if self.fase == "presenta":
            if t >= self.fase_t:
                self._passa("nascondi", float(self.cfgv("nascondi", 30.0)), t)
        elif self.fase == "nascondi":
            if t >= self.fase_t:
                n = len(gr["nascosti"])
                self._passa("cerca", min(160.0, float(self.cfgv("cerca", 100.0)) + 6.0 * max(0, n - 2)), t)
        elif self.fase == "cerca":
            self._visibili()
            self._indizi(t)
            if not (gr["nascosti"] - set(gr["presi"])):
                return self._fine_giro(t)
            if t >= self.fase_t:
                return self._fine_giro(t)
        elif self.fase == "risultato":
            if t >= self.fase_t:
                self._nuovo_giro(t)
        self._manda_npc()

    def _visibili(self):
        """Chi il cercatore vede adesso; a chi esce dalla vista, al cercatore arriva "far"."""
        gr = self.giro
        cerc = gr["cercatore"]
        ora = {u for u in gr["nascosti"] if u not in gr["presi"] and self.vede(cerc, u)}
        g = self.giocatori.get(cerc)
        if g and g.via is None:
            for u in gr["visibili"] - ora:
                o = self.giocatori.get(u)
                if o and o.me.get("id") is not None:
                    self.g.send(g.ws, json.dumps({"t": "far", "id": o.me["id"]}))
            for u in ora - gr["visibili"]:
                o = self.giocatori.get(u)
                if o and o.me.get("p"):
                    self.g.send(g.ws, o.me["p"])        # (subito, non al prossimo passo)
        gr["visibili"] = ora

    # -- l'area
    def _area(self, t):
        gr = self.giro
        for g in self.giocatori.values():
            if g.via is not None or g.uid in gr["presi"] or self.fase not in ("nascondi", "cerca"):
                continue
            p = self.pos(g.uid)
            if p is None:
                continue
            # (durante la conta il cercatore resta alla bacheca)
            if g.uid == gr["cercatore"] and self.fase == "nascondi":
                if math.hypot(p[0] - BACHECA[0], p[1] - BACHECA[1] - 0.9) > 3.0:
                    self.manda(g, {"a": "posto", "x": BACHECA[0], "z": BACHECA[1] + 0.9, "yaw": round(math.pi, 3)})
                continue
            if self.m.dentro(p[0], p[1]):
                if g.dati.get("fuori_t"):
                    g.dati["fuori_t"] = 0.0
                    self.manda(g, {"a": "dentro"})
                continue
            if not g.dati.get("fuori_t"):
                g.dati["fuori_t"] = t
                self.manda(g, {"a": "fuori", "secondi": 5})
            elif t - g.dati["fuori_t"] > 5.0:
                x, z = self.m.vicino_dentro(p[0], p[1])
                self.manda(g, {"a": "rientra", "x": round(x, 2), "z": round(z, 2)})
                g.dati["fuori_t"] = 0.0
                g.dati["rientri"] = g.dati.get("rientri", 0) + 1
                self.punti[g.uid] = self.punti.get(g.uid, 0) - 5
                gr["punti"][g.uid] = gr["punti"].get(g.uid, 0) - 5

    # -- la cattura
    def azione(self, gioc, msg, t):
        a = msg.get("a")
        if a == "prendi":
            self._prendi(gioc.uid, t)
            return
        try:
            i = int(msg.get("c", -1))
        except (TypeError, ValueError, OverflowError):
            i = -1
        if a == "entra_cont":
            if not self._entra(gioc.uid, i):
                self.manda(gioc, {"a": "no_cont", "c": i})
        elif a == "esci_cont":
            self._esci(gioc.uid)
        elif a == "apri":
            self._apri(gioc.uid, i, t)

    def _prendi(self, cerc, t):
        gr = self.giro
        if self.fase != "cerca" or cerc != gr["cercatore"]:
            return
        if t - gr.get("prendi_t", 0.0) < 0.6:
            return
        gr["prendi_t"] = t
        a = self.pos(cerc)
        if a is None:
            return
        best = None
        for u in gr["nascosti"]:
            if u in gr["presi"] or self.dentro_di(u) >= 0:
                continue
            b = self.pos(u)
            if b is None:
                continue
            g = self.giocatori.get(u)
            if g and time.time() - g.me.get("last_p", 0) > 3.0 and g.me.get("x") is None:
                continue
            d = math.hypot(a[0] - b[0], a[1] - b[1])
            # (vicino e con la vista libera da muri e colonne: sopra una panchina ci si arriva, attraverso no)
            if d <= CATTURA and self.m.vede(a, b, False) and (best is None or d < best[0]):
                best = (d, u)
        if best is None:
            g = self.giocatori.get(cerc)
            if g:
                self.manda(g, {"a": "mancato"})
            return
        self._cattura(cerc, best[1], t)

    def _cattura(self, cerc, u, t):
        gr = self.giro
        if u in gr["presi"] or u not in gr["nascosti"]:
            return          # (mai due volte la stessa persona: l'NPC che cerca la riprendeva nello stesso istante)
        gr["presi"].append(u)
        dopo = round(t - gr["inizio_cerca"], 1)
        gr["presi_t"][u] = dopo
        restano = len(gr["nascosti"]) - len(gr["presi"])
        self.tutti({"a": "preso", "chi": [u, self.nome(u)], "da": [cerc, self.nome(cerc)], "dopo": dopo, "restano": restano,
                    "presi": len(gr["presi"]), "dove": [round(c, 2) for c in (self.pos(u) or PARTENZA)]})
        if u in self.npc:
            n = self.npc[u]
            n.stato, n.path, n.anim = "preso", [], "sorpreso"
        self.g.log("nascondino preso", self.id, self.nome(u), "da", self.nome(cerc), dopo)

    # -- gli indizi
    def _indizi(self, t):
        gr = self.giro
        cerc = gr["cercatore"]
        a = self.pos(cerc)
        if a is None:
            return
        liberi = [u for u in gr["nascosti"] if u not in gr["presi"] and self.pos(u) is not None]
        if not liberi:
            return
        k = (t - gr["inizio_cerca"]) / max(1.0, gr["durata"])
        dist = sorted((math.hypot(self.pos(u)[0] - a[0], self.pos(u)[1] - a[1]), u) for u in liberi)
        # la zona di qualcuno: a un terzo, a meta', ai tre quarti, poi piu' spesso
        momenti = [0.3, 0.52, 0.72, 0.86]
        for i, mk in enumerate(momenti):
            if k >= mk and i not in gr["zone_dette"]:
                gr["zone_dette"].add(i)
                u = self.rng.choice(liberi)
                self._indizio({"tipo": "zona", "testo": "Qualcuno si nasconde %s." % self.m.zona(*self.pos(u))})
        # nell'ultimo quarto: da che parte si sente qualcuno (il piu' vicino), ogni 12 s
        if k > 0.75 and t - gr["ind_t"] > 12.0:
            gr["ind_t"] = t
            b = self.pos(dist[0][1])
            self._indizio({"tipo": "direzione", "testo": "Hai sentito un fruscio verso %s." % direzione(b[0] - a[0], b[1] - a[1])})
        # "fuocherello": quanto e' vicino il piu' vicino (dopo il primo quinto), solo quando cambia, con calma
        liv = 3 if dist[0][0] < 3.5 else (2 if dist[0][0] < 6.5 else (1 if dist[0][0] < 10.0 else 0))
        if k > 0.2 and liv != gr["vicino_liv"] and (liv > gr["vicino_liv"] or t - gr["vicino_t"] > 4.0):
            if liv > gr["vicino_liv"] and t - gr["vicino_t"] < 3.0:
                pass
            else:
                gr["vicino_liv"] = liv
                gr["vicino_t"] = t
                self._indizio({"tipo": "vicino", "livello": liv})
        # i passi: chi corre vicino al cercatore si sente (furtivi e accucciati no)
        for u in liberi:
            b = self.pos(u)
            prima = gr["pos_prima"].get(u)
            gr["pos_prima"][u] = b
            if prima is None:
                continue
            v = math.hypot(b[0] - prima[0], b[1] - prima[1]) / 0.25
            if v > CORRE and math.hypot(b[0] - a[0], b[1] - a[1]) < 14.0 and t - gr["passi_t"] > 5.0 and u not in gr["visibili"]:
                gr["passi_t"] = t
                self._indizio({"tipo": "passi", "testo": "Passi di corsa verso %s!" % direzione(b[0] - a[0], b[1] - a[1])})
        # ai nascosti: il cercatore e' vicino (senza dire da dove)
        for u in liberi:
            b = self.pos(u)
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            lv = 2 if d < 3.5 else (1 if d < 7.0 else 0)
            if lv != gr["allerte"].get(u, 0):
                gr["allerte"][u] = lv
                g = self.giocatori.get(u)
                if g:
                    self.manda(g, {"a": "allerta", "livello": lv})

    def _indizio(self, d):
        gr = self.giro
        g = self.giocatori.get(gr["cercatore"])
        if g:
            self.manda(g, dict(d, a="indizio"))
        n = self.npc.get(gr["cercatore"])
        if n and d.get("tipo") == "zona":
            n.indizio = d.get("testo", "")

    # -- gli NPC
    def _npc(self, t, dt):
        gr = self.giro
        for n in self.npc.values():
            if n.uid == gr["cercatore"]:
                self._npc_cerca(n, t, dt)
            elif n.uid in gr["presi"]:
                self._npc_preso(n, t, dt)
            elif n.uid in gr["nascosti"]:
                self._npc_nasconde(n, t, dt)

    def _npc_preso(self, n, t, dt):
        if n.stato == "preso":
            n.stato = "va"
            n.t_stato = t + 1.6
        elif n.stato == "va" and t >= n.t_stato:
            n.vai(SPETTATORI, 2.4, "walk")
            n.stato = "spettatore"
        elif n.stato == "spettatore" and n.muovi(dt):
            n.anim = "idle"

    def _npc_nasconde(self, n, t, dt):
        if self.fase == "presenta":
            n.anim = "idle"
            return
        if self.fase == "nascondi" and n.stato == "fermo":
            # sceglie un nascondiglio: i bravi tra i migliori, gli altri anche tra i mediocri (e si vede...)
            presi = {s_["x"] for s_ in [o.spot for o in self.npc.values() if o.spot]}
            spots = [s for s in self.m.spots if s["x"] not in presi]
            spots.sort(key=lambda s: -s["coperto"])
            k = max(3, int(len(spots) * (1.0 - n.bravura) * 0.9))
            n.spot = self.rng.choice(spots[:k]) if spots else None
            # a volte dentro un bidone, un cespuglio o uno scatolone (uno libero, che nessun altro ha scelto)
            liberi = [i for i, c in enumerate(self.cont) if c["chi"] is None and c["x"] not in presi]
            if liberi and self.rng.random() < 0.45:
                i = self.rng.choice(liberi)
                c = self.cont[i]
                n.spot = {"x": c["x"], "z": c["z"], "verso": c["yaw"], "tipo": "cont", "coperto": 1.0, "cont": i}
            if n.spot:
                n.vai((n.spot["x"], n.spot["z"]), 4.4, "run")
            n.stato = "va"
        if n.stato == "va":
            if n.muovi(dt):
                n.stato = "nascosto"
                if n.spot and n.spot.get("cont") is not None and self._entra(n.uid, int(n.spot["cont"])):
                    return
                if n.spot:
                    n.yaw = n.spot["verso"] + math.pi
                n.anim = "accucciato" if (n.spot and n.spot["tipo"] == "basso") or n.bravura > 0.5 else "appoggiato"
                n.t_stato = t + self.rng.uniform(6, 14)
        elif n.stato == "nascosto" and self.dentro_di(n.uid) >= 0:
            n.anim = "dentro"          # (zitto zitto dentro, finche' non lo aprono)
        elif n.stato == "nascosto" and self.fase == "cerca":
            a = self.pos(self.giro["cercatore"])
            if a is None:
                return
            d = math.hypot(a[0] - n.x, a[1] - n.z)
            visto = self.vede(self.giro["cercatore"], n.uid)
            # il cercatore si avvicina senza vederlo: i bravi cambiano posto (una volta o due), gli altri tremano
            if d < 5.5 and not visto and n.bravura > 0.55 and n.cambi < 2 and self.rng.random() < 0.1:
                lontani = [s for s in self.m.spots if math.hypot(s["x"] - a[0], s["z"] - a[1]) > 14 and math.hypot(s["x"] - n.x, s["z"] - n.z) < 22]
                if lontani:
                    n.spot = self.rng.choice(lontani)
                    n.vai((n.spot["x"], n.spot["z"]), 2.0, "furtivo")
                    n.stato = "va"
                    n.cambi += 1
                    return
            if d < 4.0:
                n.anim = "accucciato"
            elif t > n.t_stato:
                # ogni tanto sbircia (i meno bravi un po' troppo)
                n.anim = "sbircia" if n.anim != "sbircia" else ("accucciato" if n.spot and n.spot["tipo"] == "basso" else "appoggiato")
                n.t_stato = t + (self.rng.uniform(2, 4) if n.anim == "sbircia" else self.rng.uniform(5, 12) * (0.5 + n.bravura))

    def _npc_cerca(self, n, t, dt):
        gr = self.giro
        if self.fase in ("presenta", "nascondi"):
            n.anim = "conta"
            n.yaw = math.pi
            return
        if self.fase != "cerca":
            n.anim = "happy" if not (gr["nascosti"] - set(gr["presi"])) else "sad"
            return
        liberi = [u for u in gr["nascosti"] if u not in gr["presi"]]
        # notare: chi e' nel suo sguardo (120 gradi), a portata, si nota piano piano (subito se corre o e' vicino)
        for u in liberi:
            b = self.pos(u)
            if b is None:
                continue
            d = math.hypot(b[0] - n.x, b[1] - n.z)
            ang = abs((math.atan2(b[0] - n.x, b[1] - n.z) - n.yaw + math.pi) % (2 * math.pi) - math.pi)
            acc = self.accucciato(u)
            prima = n.allerta.get("_p_" + u)
            n.allerta["_p_" + u] = b
            corre = prima is not None and math.hypot(b[0] - prima[0], b[1] - prima[1]) / dt > CORRE
            portata = 9.0 if acc else (24.0 if corre else 18.0)
            vede = d < portata and (ang < math.radians(65) or d < 2.5) and self.m.vede_corpo((n.x, n.z), b, acc)
            if vede:
                n.allerta[u] = min(1.0, n.allerta.get(u, 0.0) + dt * (2.5 if d < 4 else (1.3 if d < 8 else 0.7)))
            else:
                n.allerta[u] = max(0.0, n.allerta.get(u, 0.0) - dt * 0.35)
            if n.allerta[u] >= 1.0 and n.bersaglio != u and u in liberi:
                n.bersaglio = u
                n.stato = "insegue"
                n.anim = "run"
        if n.bersaglio and n.bersaglio in liberi:
            b = self.pos(n.bersaglio)
            if b is not None:
                d = math.hypot(b[0] - n.x, b[1] - n.z)
                if d <= 1.3 and self.m.vede((n.x, n.z), b):
                    n.anim = "prende"
                    self._cattura(n.uid, n.bersaglio, t)
                    n.bersaglio = None
                    n.stato = "guarda"
                    n.t_stato = t + 1.5
                    n.path = []
                    return
                if n.allerta.get(n.bersaglio, 0.0) <= 0.2:
                    # perso di vista: va dove l'ha visto l'ultima volta e si guarda intorno
                    n.ultimo_visto = b
                    n.bersaglio = None
                    n.vai(b, 3.2, "walk")
                    n.stato = "va"
                elif not n.path or t - n.t_stato > 0.75:
                    n.vai(b, 4.6, "run")
                    n.t_stato = t
                n.muovi(dt)
                return
        n.bersaglio = None
        # un bidone, un cespuglio, uno scatolone a portata: ci guarda dentro (i bravi quasi sempre)
        for i, c in enumerate(self.cont):
            if i in n.aperti or math.hypot(c["x"] - n.x, c["z"] - n.z) > 1.8:
                continue
            n.aperti.add(i)
            if self.rng.random() < 0.5 + 0.45 * n.bravura:
                n.anim = "prende"
                n.yaw = math.atan2(c["x"] - n.x, c["z"] - n.z)
                self._apri(n.uid, i, t)
                n.stato = "guarda"
                n.t_stato = t + 1.2
                n.path = []
                return
        if n.stato == "guarda":
            n.anim = "guarda"
            n.yaw += dt * 1.8 * (1 if int(t) % 4 < 2 else -1)
            if t >= n.t_stato:
                n.stato = "pattuglia"
            return
        if n.stato == "fermo":
            # prima di tutto un giro della piazza, guardandosi intorno
            n.vai((27.0, -66.0), 3.4, "walk")
            n.stato = "va"
            n.zona_ultima = "in Piazza del Grano"
        elif n.stato == "pattuglia" or not n.path:
            # il prossimo nascondiglio da controllare: i migliori prima, non troppo lontani, una zona diversa dall'ultima
            # (ci si torna dopo), quella dell'indizio prima di tutte
            ind = getattr(n, "indizio", "")
            best = None
            dacontrollare = list(self.m.spots) + [{"x": c["x"], "z": c["z"], "coperto": 1.2} for i, c in enumerate(self.cont) if i not in n.aperti]
            for s in dacontrollare:
                key = (round(s["x"], 1), round(s["z"], 1))
                if key in n.visti:
                    continue
                d = math.hypot(s["x"] - n.x, s["z"] - n.z)
                zn = self.m.zona(s["x"], s["z"])
                sc = d * 0.6 - s["coperto"] * 5.0 - (10.0 if ind and zn in ind else 0.0) + (4.0 if zn == getattr(n, "zona_ultima", "") and d > 6 else 0.0)                     + self.rng.random() * 6.0
                if best is None or sc < best[0]:
                    best = (sc, s, key)
            if best is None:
                n.visti = set()
                return
            n.visti.add(best[2])
            n.zona_ultima = self.m.zona(best[1]["x"], best[1]["z"])
            n.vai((best[1]["x"], best[1]["z"]), 3.2, "walk")
            n.stato = "va"
        if n.stato == "va" and n.muovi(dt):
            n.stato = "guarda"
            n.t_stato = t + self.rng.uniform(1.0, 2.2)

    def _manda_npc(self):
        """Gli NPC a chi li deve vedere: al cercatore umano solo quelli che vede, agli altri tutti."""
        if not self.npc:
            return
        gr = self.giro
        for g in self.giocatori.values():
            if g.via is not None:
                continue
            lista = []
            for n in self.npc.values():
                if g.uid == gr["cercatore"] and n.uid in gr["nascosti"] and n.uid not in gr["presi"] and self.fase in ("nascondi", "cerca") \
                        and (self.fase == "nascondi" or not self.vede(g.uid, n.uid)):
                    continue
                lista.append([n.uid, round(n.x, 2), round(n.z, 2), round(n.yaw, 2), n.anim])
            self.manda(g, {"a": "npc", "l": lista})

    # -- la fine di un giro e della sessione
    def _fine_giro(self, t, annullato=False):
        gr = self.giro
        if self.fase in ("risultato", "classifica"):
            return
        cerc = gr["cercatore"]
        # (tempo scaduto: chi era dentro un contenitore esce, salvo)
        for c in self.cont:
            if c["chi"] is not None:
                self._esci(c["chi"])
        dur = max(1.0, gr["durata"])
        passato = (t - gr["inizio_cerca"]) if gr["inizio_cerca"] else 0.0
        salvi = [u for u in gr["nascosti"] if u not in gr["presi"]]
        tutti = not salvi and gr["presi"]
        pt = gr["punti"]
        if not annullato:
            # il cercatore: 10 a testa, 20 se li trova tutti, e il tempo che avanza
            pt[cerc] = pt.get(cerc, 0) + 10 * len(gr["presi"]) + (20 + int(max(0.0, dur - passato) / 3.0) if tutti else 0)
            # i nascosti: un punto ogni 4 secondi resistiti (chi e' stato preso: fino a quando l'hanno preso), 15 a
            # chi non e' trovato, 8 all'ultimo trovato
            durata_vera = min(dur, passato)
            for u in gr["nascosti"]:
                resistito = gr["presi_t"].get(u, durata_vera)
                pt[u] = pt.get(u, 0) + int(resistito / 4.0) + (15 if u in salvi else 0)
            if tutti:
                pt[gr["presi"][-1]] = pt.get(gr["presi"][-1], 0) + 8
        for u, v in pt.items():
            self.punti[u] = self.punti.get(u, 0) + v
        vince = "annullato" if annullato else ("cercatore" if tutti else "nascosti")
        ris = {"a": "risultato", "n": self.n_giro, "di": self.giri, "vince": vince, "cercatore": [cerc, self.nome(cerc)],
               "presi": [[u, self.nome(u)] for u in gr["presi"]], "salvi": [[u, self.nome(u)] for u in salvi],
               "punti_giro": pt, "punti": self.punti, "durata": round(passato, 1), "ultimo": self.n_giro >= self.giri}
        self.storia.append(ris)
        self.fase = "risultato"
        self.fase_t = t + float(self.cfgv("risultato", 10.0))
        self.tutti(ris)
        self.g.log("nascondino fine giro", self.id, vince, [self.nome(u) for u in gr["presi"]], [self.nome(u) for u in salvi])

    def _fine_sessione(self, t):
        self.fase = "classifica"
        self.fase_t = t + float(self.cfgv("classifica", 12.0))
        for g in self.giocatori.values():
            g.punti = self.punti.get(g.uid, 0)
        cl = sorted(self.punti.items(), key=lambda kv: -kv[1])
        self.tutti({"a": "fase", "fase": "classifica", "resta": round(self.fase_t - t, 1), "classifica": [[u, self.nome(u), p, u in self.npc] for u, p in cl],
                    "punti": self.punti})

    def lista(self):
        cl = sorted(self.punti.items(), key=lambda kv: -kv[1]) if getattr(self, "punti", None) else [(g.uid, 0) for g in self.giocatori.values()]
        out = []
        for u, p in cl:
            g = self.giocatori.get(u)
            out.append([self.nome(u), p, u, 0 if (u in getattr(self, "npc", {}) or (g and g.via is None)) else 1])
        return out

    def termina(self):
        """La classifica di tutti (anche gli NPC) e i premi ai giocatori, per posto; da soli i premi valgono (sono
        solo titoli e adesivi) se si e' giocato tutto."""
        self.stato = "fine"
        self.chiusa = time.time()
        premi = __import__("giochi").CFG.get("premi", {}).get(self.gioco, {})
        cl = self.lista()
        posti = {}
        pos, last = 0, None
        for i, r in enumerate(cl):
            if last is None or r[1] != last:
                pos, last = i + 1, r[1]
            posti[r[2]] = pos
        for gioc in self.giocatori.values():
            p = posti.get(gioc.uid, 0)
            mio = {}
            if len(self.storia) >= 1 and self.punti.get(gioc.uid, 0) > 0:
                mio = dict(premi.get("tutti", {}))
                mio.update(premi.get(str(p), {}))
            self.manda(gioc, {"a": "fine", "classifica": cl, "posto": p, "premi": mio,
                              "extra": {"giri": len(self.storia), "punti": self.punti.get(gioc.uid, 0)}})
        self.g.log("fine", self.gioco, self.id, cl)

    # -- chi entra a sessione iniziata
    def entra_in_corsa(self, me, ws):
        if self.prova or self.stato != "via" or self.fase == "classifica" or len(self.giocatori) >= int(self.cfg.get("max", 10)):
            return False
        gioc = self.aggiungi_silenzioso(me, ws)
        gioc.dati = {"fuori_t": 0.0, "rientri": 0}
        self.ordine.append(gioc.uid)
        self.tutti_uid.append(gioc.uid)
        self.punti.setdefault(gioc.uid, 0)
        self.giri += 1
        # (nel giro in corso guarda, dagli spettatori: entra in gioco dal prossimo)
        self.manda(gioc, dict(self.dati_via(gioc), a="via", rientro=False, ospite=True))
        self.tutti({"a": "avviso", "testo": "%s si unisce: giochera' dal prossimo giro." % gioc.nome})
        return True

    def aggiungi_silenzioso(self, me, ws):
        from giochi import Giocatore
        gioc = Giocatore(me, ws)
        self.giocatori[gioc.uid] = gioc
        return gioc

    def rientra(self, gioc, me, ws):
        gioc.me, gioc.ws, gioc.via = me, ws, None
        if self.stato == "via":
            self.manda(gioc, dict(self.dati_via(gioc), a="via", rientro=True))
