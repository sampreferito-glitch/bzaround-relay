"""Minigioco 6, La gara delle biciclette (centro storico, arrivo in Piazza Walther), lato server. Importato da giochi.py
(TIPI["gara"]).

Il server decide: chi corre e su quale posto della griglia, i concorrenti NPC (solo quando si corre da soli: con altri
giocatori veri non si aggiungono NPC), i tempi (presentazione, conto alla rovescia, via: uguali per tutti, sull'orologio
della partita), la falsa partenza, i checkpoint, i giri, la classifica in diretta, gli arrivi, i premi.

Il gioco manda la posizione della sua bici (e, da soli, quella degli NPC che guida lui) circa 12 volte al secondo. Il
server non si fida: una posizione troppo lontana dalla precedente per la velocita' massima di una bici e' scartata (un
salto non vale e non fa passare nessun checkpoint); un checkpoint vale solo se il tratto tra due posizioni accettate
attraversa la sua linea, nel verso giusto, e solo il prossimo in ordine. Chi salta un pezzo di percorso non passa il
checkpoint e non avanza in classifica. Il progresso in classifica e' la strada fatta lungo il tracciato (giri,
checkpoint, e dal checkpoint passato la distanza lungo la linea, mai oltre il prossimo), non la distanza in linea d'aria.

Fasi: "griglia" (la presentazione dei concorrenti in griglia) -> "conto" (3, 2, 1) -> "gara" (fino all'ultimo arrivo, o
fino a "dopo_primo" secondi dopo il primo, o al tempo massimo) -> "premi" (il podio) -> fine.

Messaggi dal gioco: {"a": "pos", "x", "z", "y", "r", "v", "st", "le", "pe", "fr", "k"}  la mia bici
                    {"a": "npc", "l": [[id, x, z, y, r, v, st, le, pe, fr, k], ...]}  da soli: gli NPC
                    {"a": "rimetti", "npc": id?}   rimesso in pista all'ultimo checkpoint (chiesto dal gioco)
Messaggi al gioco:  "via" (concorrenti, griglia, tempi), "fase", "corsa" (tutti, 10 volte al secondo), "cp" (il mio
                    checkpoint), "giro", "arrivo" (di chiunque), "falsa", "rimetti" (dove), "ritirato", poi "fine".
I campi st (sterzo), le (piega), pe (pedivelle), fr (frena), k (stato: 0 normale, 1 urto, 2 scatto, 3 fermo a terra)
servono solo agli altri giochi per disegnare la bici come la guida chi la guida.
"""
import json
import math
import os
import time

from giochi import Partita

HERE = os.path.dirname(os.path.abspath(__file__))
VMAX = 15.0               # m/s: nessuna bici va piu' veloce (la piu' veloce in gioco fa ~12.5 con lo scatto)
SALTO = 1.5               # metri in piu' tollerati tra due posizioni (rete, arrotondamenti)
MARGINE_CP = 4.0          # metri oltre la larghezza della pista in cui la linea conta ancora (da muro a muro: anche sotto le arcate)


def _pista():
    try:
        with open(os.path.join(HERE, "gara_bici.json"), encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class Pista:
    """Il tracciato: la linea centrale chiusa, le lunghezze lungo la linea, i checkpoint come segmenti da attraversare."""

    def __init__(self, d):
        self.d = d
        self.linea = [(float(p[0]), float(p[1])) for p in d.get("linea", [])]
        n = len(self.linea)
        self.arco = [0.0]
        for k in range(1, n):
            self.arco.append(self.arco[-1] + math.dist(self.linea[k - 1], self.linea[k]))
        self.L = self.arco[-1] + (math.dist(self.linea[-1], self.linea[0]) if n else 0.0)
        self.cp = []
        for c in d.get("checkpoint", []):
            px, pz = float(c["p"][0]), float(c["p"][1])
            dx, dz = float(c["d"][0]), float(c["d"][1])
            nx, nz = dz, -dx           # (sinistra, guardando avanti)
            sx, ds = float(c.get("sx", 3.0)) + MARGINE_CP, float(c.get("dx", 3.0)) + MARGINE_CP
            self.cp.append({"i": int(c["i"]), "p": (px, pz), "d": (dx, dz), "a": (px + nx * sx, pz + nz * sx),
                            "b": (px - nx * ds, pz - nz * ds), "s": self.arco[int(c["i"])]})
        self.griglia = [tuple(float(v) for v in g) for g in d.get("griglia", [])]

    def attraversa(self, k, p0, p1):
        """Il tratto p0 -> p1 attraversa la linea del checkpoint k, in avanti?"""
        c = self.cp[k]
        mx, mz = p1[0] - p0[0], p1[1] - p0[1]
        if mx * c["d"][0] + mz * c["d"][1] <= 0.0:
            return False
        return _incrocia(p0, p1, c["a"], c["b"])

    def proietta(self, p, i0, i1):
        """La lunghezza lungo la linea del punto della linea piu' vicino a p, cercando tra gli indici i0..i1 (in giro)."""
        n = len(self.linea)
        best, bs = 1e18, self.arco[i0]
        k = i0
        passi = (i1 - i0) % n or n
        for _ in range(passi + 1):
            a, b = self.linea[k % n], self.linea[(k + 1) % n]
            ax, az = b[0] - a[0], b[1] - a[1]
            L2 = ax * ax + az * az or 1e-9
            t = max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * az) / L2))
            qx, qz = a[0] + ax * t, a[1] + az * t
            dd = (p[0] - qx) ** 2 + (p[1] - qz) ** 2
            if dd < best:
                best = dd
                base = self.arco[k % n]
                bs = base + math.sqrt(L2) * t
            k += 1
        return bs


def _incrocia(p, q, a, b):
    def lato(o, x, y):
        return (x[0] - o[0]) * (y[1] - o[1]) - (x[1] - o[1]) * (y[0] - o[0])
    d1, d2 = lato(a, b, p), lato(a, b, q)
    d3, d4 = lato(p, q, a), lato(p, q, b)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


PISTA = Pista(_pista())

# I concorrenti NPC: nomi inventati e giocosi, ognuno col suo modo di correre. vmax: velocita' massima (m/s, il
# giocatore ne ha 9.6, 11.6 con lo scatto); acc: quanto spinge sui pedali; curve: quanto osa in curva (aderenza);
# freno: con quanto anticipo frena (piu' alto, piu' prudente); scatto: quanto usa lo scatto; linea: quanto ondeggia.
NPC = [
    {"id": "zefiro", "nome": "Zefiro", "chi": "il postino a pedali di Gries", "stile": "velocista",
     "vmax": 10.1, "acc": 1.0, "curve": 0.86, "freno": 1.0, "scatto": 0.85, "linea": 0.35,
     "look": {"skin": 2, "hair": "black", "style": "short", "shirt": "#3f7fb0", "pattern": 1, "accent": "#f2c14e", "pants": "#2f3a4a", "shoes": "#f2c14e", "hat": "cap", "hat_col": "#f2c14e"}},
    {"id": "curvetta", "nome": "Curvetta", "chi": "scende dal Renon senza freni", "stile": "curve",
     "vmax": 9.5, "acc": 0.98, "curve": 1.14, "freno": 0.9, "scatto": 0.5, "linea": 0.2,
     "look": {"skin": 0, "hair": "red", "style": "ponytail", "shirt": "#5fa37a", "pattern": 0, "pants": "#34477a", "shoes": "#f8f4ec", "hat": ""}},
    {"id": "lumachina", "nome": "Lumachina", "chi": "la prudenza fatta ciclista", "stile": "prudente",
     "vmax": 9.3, "acc": 0.9, "curve": 0.95, "freno": 1.35, "scatto": 0.25, "linea": 0.15,
     "look": {"skin": 1, "hair": "blonde", "style": "bob", "shirt": "#f4b6c8", "pattern": 0, "pants": "#7a5236", "shoes": "#d9534f", "glasses": True, "hat": ""}},
    {"id": "scheggia", "nome": "Scheggia", "chi": "parte sempre per primo", "stile": "aggressivo",
     "vmax": 9.6, "acc": 1.22, "curve": 0.96, "freno": 0.85, "scatto": 1.0, "linea": 0.45,
     "look": {"skin": 3, "hair": "brown", "style": "spiky", "shirt": "#e8845c", "pattern": 1, "accent": "#2a2730", "pants": "#2a2730", "shoes": "#2a2730", "hat": ""}},
    {"id": "ghianda", "nome": "Ghianda", "chi": "il ciclista della domenica", "stile": "intermedio",
     "vmax": 9.6, "acc": 1.0, "curve": 1.0, "freno": 1.05, "scatto": 0.6, "linea": 0.3,
     "look": {"skin": 4, "hair": "brown", "style": "curly", "shirt": "#a37ad9", "pattern": 0, "pants": "#4a5878", "shoes": "#f8f4ec", "hat": "beanie", "hat_col": "#f2c14e"}},
    {"id": "pedalina", "nome": "Pedalina", "chi": "consegna strudel in tutto il centro", "stile": "intermedio",
     "vmax": 9.5, "acc": 1.05, "curve": 1.02, "freno": 1.0, "scatto": 0.55, "linea": 0.3,
     "look": {"skin": 5, "hair": "black", "style": "braids", "shirt": "#f2c14e", "pattern": 0, "pants": "#5fa37a", "shoes": "#3f7fb0", "bag": "backpack", "bag_col": "#d9534f", "hat": ""}},
]
DIFFICOLTA = {"facile": 0.92, "normale": 1.0, "difficile": 1.06}


class Corridore:
    def __init__(self, uid, nome, npc=False, look=None, chi="", car=None):
        self.uid, self.nome, self.npc, self.look, self.chi, self.car = uid, nome, npc, look or {}, chi, car or {}
        self.slot = 0
        self.pos = None           # (x, z) accettata
        self.vis = [0.0] * 9      # y, r, v, st, le, pe, fr, k: per disegnarla
        self.t_pos = 0.0
        self.fatti = 0            # checkpoint passati dall'inizio (il primo e' il traguardo, appena partiti)
        self.prog = -1e9
        self.arrivo = None        # tempo di gara (secondi, penalita' comprese)
        self.posto = 0
        self.penalita = 0.0
        self.falsa = False
        self.ritirato = False
        self.scarti = 0
        self.rimesso = None       # (x, z) dove il server lo ha rimesso: la prossima posizione puo' essere li'
        self.giri_t = []          # i tempi dei giri
        self.cp_t = 0.0

    def riga(self):
        return [self.uid, round(self.pos[0], 2) if self.pos else 0.0, round(self.pos[1], 2) if self.pos else 0.0] + \
               [round(float(v), 2) for v in self.vis[:8]] + [self.fatti, self.posto]


class Gara(Partita):
    gioco = "gara"

    def cfgv(self, k, d):
        return self.cfg.get(k, d)

    def opzioni(self, msg):
        """Dal messaggio "entra": la difficolta' degli NPC (conta solo se poi si corre da soli)."""
        d = str(msg.get("diff", ""))
        if d in DIFFICOLTA:
            self.diff = d

    def avvia(self):
        self.pista = PISTA
        self.N = len(self.pista.cp)
        self.giri = int(self.cfgv("giri", self.pista.d.get("giri", 3)))
        self.t_conto = float(self.cfgv("presenta", 8.0))
        self.t_via = self.t_conto + 3.0
        self.fase = "griglia"
        self.fase_t = self.inizio + self.t_conto
        self.corridori = {}
        self.ordine_arrivo = []
        self.primo_t = None
        self._corsa_t = 0.0
        self.chiusura = None
        umani = [x for x in self.giocatori.values()]
        self.rng.shuffle(umani)
        for g in umani:
            c = Corridore(g.uid, g.nome)
            self.corridori[g.uid] = c
        # da soli: gli NPC (con altri giocatori veri no: si corre tra persone)
        self.ospite = umani[0].uid if len(umani) == 1 else None
        if len(umani) == 1:
            quanti = max(2, min(int(self.cfgv("concorrenti_soli", 5)), len(NPC) + 1)) - 1
            mult = DIFFICOLTA.get(getattr(self, "diff", self.cfgv("difficolta", "normale")), 1.0)
            for n in self.rng.sample(NPC, quanti):
                car = {k: n[k] for k in ("vmax", "acc", "curve", "freno", "scatto", "linea", "stile")}
                car["vmax"] = round(car["vmax"] * mult, 3)
                car["acc"] = round(car["acc"] * (0.5 + 0.5 * mult), 3)
                car["curve"] = round(car["curve"] * (0.6 + 0.4 * mult), 3)
                car["seme"] = self.rng.randint(1, 10 ** 6)
                self.corridori["npc:" + n["id"]] = Corridore("npc:" + n["id"], n["nome"], True, n["look"], n["chi"], car)
        # la griglia: gli NPC davanti o dietro a caso, i giocatori anche (nessun vantaggio fisso)
        uids = list(self.corridori.keys())
        self.rng.shuffle(uids)
        for i, u in enumerate(uids[:len(self.pista.griglia)]):
            c = self.corridori[u]
            c.slot = i
            gx, gz, _ = self.pista.griglia[i]
            c.pos = (gx, gz)
            c.t_pos = self.inizio
            c.prog = self._progresso(c)
        for u in uids[len(self.pista.griglia):]:
            self.corridori[u].ritirato = True      # (piu' di 8: non ci stanno in griglia; non succede, max 8)
        self.tutti(dict(self.dati_via(None), a="via"))
        self.g.log("via gara", self.id, len(umani), "giocatori", len(self.corridori) - len(umani), "npc")

    def dati_via(self, gioc):
        return {"fase": self.fase, "giri": self.giri, "t_conto": self.t_conto, "t_via": self.t_via, "prova": self.prova,
                "ospite": self.ospite, "pista": self.pista.d.get("nome", ""),
                "concorrenti": [[c.uid, c.nome, c.chi, c.look, c.npc, c.slot, c.car] for c in self.corridori.values()],
                "stato": [c.riga() for c in self.corridori.values()],
                "arrivi": [[u, self.corridori[u].nome, round(self.corridori[u].arrivo, 2), self.corridori[u].posto] for u in self.ordine_arrivo],
                "durate": {"premi": float(self.cfgv("premi", 18.0)), "dopo_primo": float(self.cfgv("dopo_primo", 45.0)),
                           "massimo": float(self.cfgv("massimo", 420.0))}}

    # -- la strada fatta
    def _progresso(self, c):
        """Metri fatti lungo il tracciato dal traguardo (prima del via: negativi, dietro la linea)."""
        p = self.pista
        if c.pos is None or not p.cp:
            return -1e9
        if c.fatti == 0:
            s = p.proietta(c.pos, p.cp[-1]["i"], p.cp[0]["i"])
            return s - p.L if s > p.L * 0.5 else s - p.L
        k = c.fatti - 1
        giro, n = divmod(k, self.N)
        a = p.cp[n]
        b = p.cp[(n + 1) % self.N]
        s = p.proietta(c.pos, a["i"], b["i"])
        lung = (b["s"] - a["s"]) % p.L or p.L
        avanti = max(0.0, min(lung, (s - a["s"]) % p.L))
        return giro * p.L + a["s"] + avanti

    def _accetta(self, c, x, z, t):
        """Una posizione nuova: e' plausibile? Se si', i checkpoint che ha attraversato."""
        if c.ritirato or c.arrivo is not None and c.pos is not None and self.fase == "premi":
            return False
        dt = max(0.0, t - c.t_pos)
        d = math.dist(c.pos, (x, z)) if c.pos else 0.0
        if c.rimesso is not None:
            if math.dist(c.rimesso, (x, z)) < 3.0:
                c.pos, c.t_pos, c.rimesso = (x, z), t, None
                c.prog = self._progresso(c)
                return True
            if d > VMAX * dt * 1.25 + SALTO:
                return False
        if d > VMAX * dt * 1.25 + SALTO:
            c.scarti += 1
            return False
        p0 = c.pos
        c.pos, c.t_pos = (x, z), t
        tg = t - self.inizio
        if tg < self.t_via:
            # prima del via: chi si stacca dalla griglia ha la falsa partenza (una volta sola)
            gx, gz, _ = self.pista.griglia[c.slot]
            if not c.falsa and math.dist((gx, gz), (x, z)) > float(self.cfgv("falsa_metri", 1.6)):
                c.falsa = True
                c.penalita += float(self.cfgv("falsa", 3.0))
                self.tutti({"a": "falsa", "uid": c.uid, "nome": c.nome, "secondi": float(self.cfgv("falsa", 3.0))})
            c.prog = self._progresso(c)
            return True
        if c.arrivo is None and p0 is not None:
            k = c.fatti % self.N
            if self.pista.attraversa(k, p0, (x, z)):
                c.fatti += 1
                c.cp_t = tg
                self._checkpoint(c, k, tg)
        c.prog = self._progresso(c)
        return True

    def _checkpoint(self, c, k, tg):
        tot = self.giri * self.N + 1
        giro = (c.fatti - 1) // self.N + 1        # il giro in cui si e' adesso (1..)
        if k == 0 and c.fatti > 1:
            # un giro finito
            fatto = (c.fatti - 1) // self.N
            prima = c.giri_t[-1][1] if c.giri_t else self.t_via
            c.giri_t.append([fatto, round(tg, 2), round(tg - prima, 2)])
            if c.fatti >= tot:
                c.arrivo = round(tg - self.t_via + c.penalita, 2)
                self.ordine_arrivo.append(c.uid)
                self._riposti()
                if self.primo_t is None:
                    self.primo_t = self.inizio + tg      # (l'ora della partita, non quella del muro)
                self.tutti({"a": "arrivo", "uid": c.uid, "nome": c.nome, "tempo": c.arrivo, "posto": c.posto,
                            "penalita": c.penalita, "giri": c.giri_t})
                return
            self._a_lui(c, {"a": "giro", "giro": fatto, "di": self.giri, "tempo": round(tg - prima, 2)})
        self._a_lui(c, {"a": "cp", "k": k, "fatti": c.fatti, "giro": min(giro, self.giri), "prossimo": c.fatti % self.N,
                        "di": self.N, "t": round(tg - self.t_via, 2)})

    def _a_lui(self, c, msg):
        """A chi guida quella bici: il giocatore, o (gli NPC) chi li guida da soli."""
        g = self.giocatori.get(c.uid if not c.npc else (self.ospite or ""))
        if g is not None:
            self.manda(g, dict(msg, uid=c.uid))

    def _riposti(self):
        arrivati = [self.corridori[u] for u in self.ordine_arrivo]
        arrivati.sort(key=lambda c: (c.arrivo, c.uid))
        corsa = [c for c in self.corridori.values() if c.arrivo is None and not c.ritirato]
        corsa.sort(key=lambda c: -c.prog)
        fermi = [c for c in self.corridori.values() if c.arrivo is None and c.ritirato]
        fermi.sort(key=lambda c: -c.prog)
        for i, c in enumerate(arrivati + corsa + fermi):
            c.posto = i + 1

    # -- il gioco
    def azione(self, gioc, msg, t):
        a = msg.get("a")
        if self.fase == "premi":
            return
        if a == "pos":
            c = self.corridori.get(gioc.uid)
            if c is None:
                return
            if t - c.t_pos < 0.04:
                return
            v = _numeri(msg, ("x", "z", "y", "r", "v", "st", "le", "pe", "fr", "k"))
            if v is None:
                return
            if self._accetta(c, v[0], v[1], t):
                c.vis = [v[2], v[3], min(abs(v[4]), VMAX), v[5], v[6], v[7], v[8], v[9]]
        elif a == "npc":
            if gioc.uid != self.ospite or not isinstance(msg.get("l"), list):
                return
            for e in msg["l"][:8]:
                if not isinstance(e, list) or len(e) < 11:
                    continue
                c = self.corridori.get("npc:" + str(e[0]))
                if c is None or not c.npc or t - c.t_pos < 0.04:
                    continue
                v = _numeri({str(i): e[i + 1] for i in range(10)}, tuple(str(i) for i in range(10)))
                if v is None:
                    continue
                if self._accetta(c, v[0], v[1], t):
                    c.vis = [v[2], v[3], min(abs(v[4]), VMAX), v[5], v[6], v[7], v[8], v[9]]
        elif a == "rimetti":
            uid = gioc.uid if not msg.get("npc") else "npc:" + str(msg.get("npc"))
            c = self.corridori.get(uid)
            if c is None or (c.npc and gioc.uid != self.ospite) or c.arrivo is not None:
                return
            if t - self.inizio < self.t_via:
                gx, gz, gyaw = self.pista.griglia[c.slot]
                dove = (gx, gz, gyaw)
            elif c.fatti == 0:
                gx, gz, gyaw = self.pista.griglia[c.slot]
                dove = (gx, gz, gyaw)
            else:
                cp = self.pista.cp[(c.fatti - 1) % self.N]
                # (un po' oltre la linea, al centro: dal checkpoint passato si riparte; il prossimo resta da fare)
                dove = (cp["p"][0] + cp["d"][0] * 1.5, cp["p"][1] + cp["d"][1] * 1.5, math.atan2(cp["d"][0], cp["d"][1]))
            c.rimesso = (dove[0], dove[1])
            self._a_lui(c, {"a": "rimetti", "x": round(dove[0], 2), "z": round(dove[1], 2), "r": round(dove[2], 3)})

    def gioca(self, t):
        tg = t - self.inizio
        if self.fase == "griglia" and tg >= self.t_conto:
            self.fase = "conto"
            self.tutti({"a": "fase", "fase": "conto", "t_via": self.t_via})
        elif self.fase == "conto" and tg >= self.t_via:
            self.fase = "gara"
            self.tutti({"a": "fase", "fase": "gara", "t_via": self.t_via})
        # chi ha perso la connessione da troppo (o e' uscito) si ritira: non blocca l'arrivo degli altri
        for g in self.giocatori.values():
            c = self.corridori.get(g.uid)
            if c and not c.ritirato and c.arrivo is None and (g.dati.get("perso") or g.dati.get("uscito")):
                c.ritirato = True
                self.tutti({"a": "ritirato", "uid": c.uid, "nome": c.nome})
        if self.ospite and self.giocatori.get(self.ospite) and self.giocatori[self.ospite].via is not None:
            pass    # (gli NPC aspettano chi li guida: se non torna, la partita finisce con lui)
        if self.fase in ("gara", "conto", "griglia"):
            self._riposti()
            if t - self._corsa_t >= 0.1:
                self._corsa_t = t
                self.tutti({"a": "corsa", "c": [c.riga() for c in self.corridori.values() if not c.ritirato or c.pos]})
        if self.fase == "gara":
            vivi = [c for c in self.corridori.values() if not c.ritirato]
            tutti_arrivati = all(c.arrivo is not None for c in vivi) and vivi
            umani_arrivati = all(c.arrivo is not None or c.ritirato for c in self.corridori.values() if not c.npc)
            fine = tutti_arrivati or tg >= self.t_via + float(self.cfgv("massimo", 420.0))
            if self.primo_t is not None and t - self.primo_t >= float(self.cfgv("dopo_primo", 45.0)):
                fine = True
            # (da soli: arrivato il giocatore, gli NPC ancora in strada hanno ancora un po' di tempo)
            if umani_arrivati and not tutti_arrivati and self.primo_t is not None:
                ultimo_umano = max((c.arrivo or 0.0) for c in self.corridori.values() if not c.npc)
                if tg - self.t_via - ultimo_umano > float(self.cfgv("dopo_umani", 25.0)):
                    fine = True
            if fine:
                if self.chiusura is None:
                    self.chiusura = t + (2.5 if tutti_arrivati else 0.0)
                if t >= self.chiusura:
                    self._riposti()
                    self.fase = "premi"
                    self.fase_t = t + float(self.cfgv("premi", 18.0))
                    for gioc in self.giocatori.values():
                        self.manda(gioc, {"a": "fase", "fase": "premi", "classifica": self._classifica()})
        elif self.fase == "premi" and t >= self.fase_t:
            self.termina()

    def _classifica(self):
        cl = sorted(self.corridori.values(), key=lambda c: c.posto)
        return [{"uid": c.uid, "nome": c.nome, "posto": c.posto, "tempo": c.arrivo, "npc": c.npc, "penalita": c.penalita,
                 "giri": c.giri_t, "ritirato": c.ritirato and c.arrivo is None, "giro": min(self.giri, (max(c.fatti, 1) - 1) // self.N + 1)}
                for c in cl]

    def lista(self):
        if not hasattr(self, "corridori"):
            return super().lista()
        cl = sorted(self.corridori.values(), key=lambda c: c.posto or 99)
        return [[c.nome, int(round(c.arrivo * 100)) if c.arrivo is not None else -1, c.uid,
                 0 if c.npc or (self.giocatori.get(c.uid) and self.giocatori[c.uid].via is None) else 1] for c in cl]

    def rientra(self, gioc, me, ws):
        super().rientra(gioc, me, ws)

    def termina(self):
        """La fine: la classifica di tutti (anche gli NPC), il posto e i premi ai giocatori arrivati. Da soli i premi
        valgono lo stesso: sono solo titoli e adesivi, e gli NPC corrono davvero (nessun aiuto, nessun freno)."""
        self.stato = "fine"
        self.chiusa = time.time()
        if hasattr(self, "corridori"):
            self._riposti()
        premi = __import__("giochi").CFG.get("premi", {}).get(self.gioco, {})
        cl = self._classifica() if hasattr(self, "corridori") else []
        for gioc in self.giocatori.values():
            c = self.corridori.get(gioc.uid) if hasattr(self, "corridori") else None
            pos = c.posto if c else 0
            mio = {}
            if c and c.arrivo is not None and len(self.corridori) >= 2:
                mio = dict(premi.get("tutti", {}))
                mio.update(premi.get(str(pos), {}))
            gioc.punti = int(round(c.arrivo * 100)) if c and c.arrivo is not None else 0
            self.manda(gioc, {"a": "fine", "classifica": self.lista(), "posto": pos if c and c.arrivo is not None else 0,
                              "premi": mio, "extra": {"gara": cl, "giri": self.giri, "mio": gioc.uid}})
        self.g.log("fine", self.gioco, self.id, [(r["nome"], r["tempo"]) for r in cl])


def _numeri(msg, chiavi):
    out = []
    for k in chiavi:
        try:
            v = float(msg.get(k, 0.0))
        except (TypeError, ValueError):
            return None
        if math.isnan(v) or math.isinf(v) or abs(v) > 1e5:
            return None
        out.append(v)
    return out
