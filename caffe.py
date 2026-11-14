"""Il cameriere pasticcione: la sala del CAPITOL Caffe' (Via Dr. Streiter), lato server.

Un gioco di squadra con due mansioni che si aiutano, come in una piccola caffetteria vera:
  il BAR, dietro il bancone: le bevande si avviano alle stazioni (la tazza sotto la macchina, le mele nella pressa...),
      le stazioni lavorano da sole, poi si completano (il latte montato, il te' versato) e si posano sul banco di
      servizio, ognuna con il cartellino del suo tavolo;
  la SALA, tra i tavoli: si prendono gli ordini, si prendono i dolci dalla vetrina e le bevande dal banco sul vassoio
      (fino a 4 cose, anche per piu' tavoli), si serve, e quando i clienti se ne vanno si sparecchia.
Le stoviglie sono contate: ogni consumazione usa una tazza o un piattino pulito; sparecchiando tornano sporchi e la
lavastoviglie li rende puliti. I ruoli si assegnano all'inizio (in due: uno al bar e uno in sala) ma non sono rigidi:
chiunque puo' fare tutto, conta solo dove si e'. Da soli, Mochi lavora al bar (piu' lento); in due o piu' da' piccoli
aiuti (sparecchia un tavolo dimenticato, porta al banco una bevanda lasciata li') e combina le sue gag.
Il server tiene tutto e controlla le distanze sulla posizione che conosce il relay (mai su quella dichiarata). La
disposizione della sala e' in caffe.json (copia di data/caffe.json).

Dal gioco ({"t": "g", ...}):
  {"a": "ordine", "tav": k}          prende l'ordinazione al tavolo k
  {"a": "avvia", "st": S}            alla stazione S: la prossima bevanda che serve (macchina, cioccolatiera, bollitore,
                                     spremimele); usa una tazza pulita, la stazione lavora da sola
  {"a": "completa", "st": S}         prende dalla stazione la bevanda pronta (latte montato, te' versato...): in mano,
                                     o sul vassoio se se ne ha uno
  {"a": "posa", "i": slot}           sul banco di servizio: la tazza in mano (o le cose pulite del vassoio)
  {"a": "prendi", "i": slot}         dal banco al proprio vassoio (senza slot: la cosa piu' utile)
  {"a": "dolce"}                     dalla vetrina: il prossimo dolce che serve, sul vassoio (usa un piattino pulito)
  {"a": "servi", "tav": k}           porta al tavolo k quello che gli serve, tra le cose che si hanno
  {"a": "sparecchia", "tav": k}      le stoviglie sporche del tavolo k sul vassoio
  {"a": "carica"}                    le stoviglie sporche nella lavastoviglie (parte da sola)
  {"a": "cade", "n": 1}              dal vassoio e' caduto qualcosa (il gioco simula l'equilibrio): si rompe
Al gioco: "via" (durata, tavoli, menu, ruolo, stoviglie), "cs" (lo stato del locale a ogni cambiamento: clienti, cose
con dove stanno, mani, banco, stazioni, tavoli sporchi, stoviglie, lavastoviglie, punti, ruoli, Mochi), "ev" (un fatto
da animare), poi "stato" e "fine" come gli altri minigiochi (la fine porta stelle e riepilogo in "extra").
"""
import json
import math
import os
import time

from giochi import CFG, HERE, POS_STALE, Partita

# le consumazioni: dove si fanno e quanto ci vuole (avvia/completa: le mani occupate; lavora: la stazione da sola)
MENU = {
    "caffe": {"nome": "Caffè", "st": "macchina", "avvia": 0.8, "lavora": 2.0, "completa": 0.4},
    "cappuccino": {"nome": "Cappuccino", "st": "macchina", "avvia": 0.8, "lavora": 2.0, "completa": 1.6},
    "cioccolata": {"nome": "Cioccolata calda", "st": "cioccolatiera", "avvia": 0.8, "lavora": 2.6, "completa": 0.6},
    "te": {"nome": "Tè", "st": "bollitore", "avvia": 0.8, "lavora": 3.0, "completa": 0.7},
    "succo": {"nome": "Succo di mela", "st": "spremimele", "avvia": 0.8, "lavora": 2.4, "completa": 0.6},
    "strudel": {"nome": "Strudel", "st": "vetrina", "prendi": 0.6},
    "krapfen": {"nome": "Krapfen", "st": "vetrina", "prendi": 0.6},
    "torta": {"nome": "Torta Sacher", "st": "vetrina", "prendi": 0.6},
}
BEVANDE = {"cappuccino": 0.3, "caffe": 0.25, "cioccolata": 0.15, "te": 0.15, "succo": 0.15}   # quanto spesso si ordinano
DOLCI = ["strudel", "krapfen", "torta"]
STAZIONI = {"macchina": 2, "cioccolatiera": 1, "bollitore": 1, "spremimele": 1}   # quante bevande insieme
VASSOIO = 4            # quante cose sul vassoio
SLOT_BANCO = 6         # i posti sul banco di servizio
T = {"posa": 0.3, "prendi": 0.3, "servi": 0.5, "sparecchia": 0.8, "carica": 0.7, "ordine": 0.5}
LAVAGGIO = 7.0         # secondi di un giro di lavastoviglie
MANGIA = 10.0          # quanto restano al tavolo dopo essere stati serviti
VICINO = 2.4           # metri: a un tavolo, a una stazione, al banco
MOCHI_LENTO = 1.6      # Mochi fa tutto un po' piu' piano di un giocatore
MOCHI_PASSO = 1.6      # metri al secondo
SPECIE = ["gatto", "cane", "volpe", "orso", "marmotta"]
NOMI = ["Ugo", "Lotte", "Sepp", "Mia", "Anton", "Greta", "Tobia", "Resi", "Kurt", "Nora", "Hans", "Lina", "Toni", "Vroni", "Pia", "Franz"]
RUOLI = ["bar", "sala", "sala", "bar", "sala", "bar"]


def _layout():
    try:
        with open(os.path.join(HERE, "caffe.json"), encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        st = {s: [0, 0] for s in list(STAZIONI) + ["vetrina", "lavastoviglie"]}
        return {"tavoli": [[0, 0]], "stazioni": st, "banco": [[0, 0]] * SLOT_BANCO, "mochi_bar": [0, 0], "mochi_sala": [0, 0]}


def _veloce():
    return bool(os.environ.get("GIOCHI_VELOCE"))


class Caffe(Partita):
    gioco = "caffe"

    def __init__(self, giochi, room, prova=False):
        super().__init__(giochi, room, prova)
        self.lay = _layout()
        if _veloce() and not prova:
            # (collaudi: si gioca anche da soli, ma si aspetta abbastanza perche' entri anche il secondo)
            self.cfg = dict(self.cfg, attesa=10)

    # ------------------------------------------------------------------ l'inizio
    def avvia(self):
        cfg = self.cfg
        self.lay = _layout()
        self.dur = cfg.get("durata", 240)
        if _veloce():
            self.dur = max(self.dur, 90)      # (il giro completo, fino alla lavastoviglie, ci deve stare)
        self.fine_t = self.inizio + self.dur
        self.mangia = 5.0 if _veloce() else MANGIA
        self.clienti = []          # [{id, tav, stato, ordine, serviti, persone, fino, tot, preso, chi, arriva}]
        self.cose = {}             # u -> {item, tav, sporco, dove, fase, fino, chi, t}
        self.n_u = 0
        self.banco = [None] * SLOT_BANCO
        self.stazioni = {s: [None] * n for s, n in STAZIONI.items()}
        self.sporchi = {}          # tavolo -> [u] (le stoviglie lasciate)
        self.sporco_da = {}        # tavolo -> da quando
        self.mani = {}             # uid -> {"tipo": ""|"mano"|"vassoio", "u": [..]}
        self.libero = {}           # uid -> fino a quando ha le mani occupate
        self.team = 0
        self.prox = 0.0
        self.n_id = 0
        presenti = self.presenti()
        n = max(1, len(presenti))
        self.ruoli = {}
        for i, gioc in enumerate(presenti):
            self.ruoli[gioc.uid] = "tutto" if n == 1 else RUOLI[i % len(RUOLI)]
        self.stoviglie = min(14, 8 + 2 * (n - 1))
        self.lava = {"dentro": 0, "coda": 0, "fino": 0.0}
        self.stat = {"serviti": 0, "ordini": 0, "persi": 0, "cadute": 0, "sbagli": 0, "mochi": 0, "squadra": 0, "clienti": 0,
                     "consegne": {}, "preparati": {}, "piu_veloce": None, "mochi_aiuti": 0, "sparecchiati": 0, "lavati": 0,
                     "tazze_nuove": 0}
        for gioc in self.giocatori.values():
            gioc.dati = {"consegne": 0, "preparati": 0, "ordini": 0, "cadute": 0, "sparecchiati": 0,
                         "ruolo": self.ruoli.get(gioc.uid, "sala")}
            self.mani[gioc.uid] = {"tipo": "", "u": []}
        self.mani["mochi"] = {"tipo": "", "u": []}
        self.mochi = {"fa": "riposa", "fino": self.inizio + 3.0, "pos": list(self.casa_mochi()), "job": None, "ordine_t": 0.0}
        for gioc in self.giocatori.values():
            self.manda(gioc, dict(self.dati_via(gioc), a="via"))
        # un primo cliente subito, gli altri piano piano
        self.nuovo_cliente(self.inizio, primo=True)
        self.prox = self.inizio + (6.0 if _veloce() else 9.0)
        self.manda_stato()
        self.g.log("via caffe", self.id, n, "giocatori", self.ruoli)

    def dati_via(self, gioc):
        return {"durata": self.dur, "tavoli": self.n_tavoli(), "menu": {k: [v["nome"], v["st"]] for k, v in MENU.items()},
                "vassoio": VASSOIO, "banco": SLOT_BANCO, "prova": self.prova, "stoviglie": self.stoviglie,
                "ruolo": self.ruoli.get(gioc.uid, "sala") if gioc else "sala", "solo": self.n_giocatori() == 1}

    # ------------------------------------------------------------------ quanto e' difficile: secondo chi c'e'
    def n_giocatori(self):
        return max(1, len(self.presenti()))

    def n_tavoli(self):
        return min(len(self.lay["tavoli"]), 2 + 2 * self.n_giocatori()) if self.stato == "via" else min(len(self.lay["tavoli"]), 4)

    def ogni(self):
        """Secondi tra un cliente e l'altro: piu' giocatori, piu' clienti (ma mai una valanga)."""
        n = self.n_giocatori()
        base = self.cfg.get("ogni", 18.0)
        return max(5.5, base / (0.6 + 0.4 * n))

    def pazienza(self, n_cose=1):
        return self.cfg.get("pazienza", 90.0) + 12.0 * max(0, n_cose - 1)

    def pazienza_ordine(self):
        return self.cfg.get("pazienza_ordine", 50.0)

    def casa_mochi(self):
        return self.lay.get("mochi_bar" if self.n_giocatori() == 1 else "mochi_sala", [0, 0])

    # ------------------------------------------------------------------ i clienti
    def tavoli_liberi(self):
        occ = {c["tav"] for c in self.clienti if c["stato"] != "via"}
        return [k for k in range(self.n_tavoli()) if k not in occ and not self.sporchi.get(k)]

    def nuovo_cliente(self, t, primo=False):
        liberi = self.tavoli_liberi()
        if not liberi:
            return
        k = self.rng.choice(liberi)
        self.n_id += 1
        frac = 0.0 if self.dur <= 0 else max(0.0, min(1.0, (t - self.inizio) / self.dur))
        presto = t - self.inizio < 30.0
        n_pers = 2 if (not presto and self.rng.random() < 0.15 + 0.35 * frac) else 1
        persone, ordine = [], []
        for _ in range(n_pers):
            persone.append({"nome": self.rng.choice(NOMI), "specie": self.rng.choice(SPECIE), "seme": self.rng.randrange(1 << 20)})
            ordine.append(self.rng.choices(list(BEVANDE), weights=list(BEVANDE.values()))[0])
            if self.rng.random() < (0.3 if primo else 0.45):
                ordine.append(self.rng.choice(DOLCI))
        self.clienti.append({"id": self.n_id, "tav": k, "stato": "arriva", "ordine": ordine, "serviti": [], "persone": persone,
                             "arriva": t + (2.5 if _veloce() else 4.0), "fino": 0.0, "tot": 0.0, "chi": [], "preso": 0.0})
        self.stat["clienti"] += 1

    def cliente_al(self, k):
        for c in self.clienti:
            if c["tav"] == k and c["stato"] in ("aspetta", "ordinato", "arriva", "mangia"):
                return c
        return None

    def manca(self, c):
        """Le cose ordinate non ancora servite (con le ripetizioni: due cappuccini sono due)."""
        v = list(c["ordine"])
        for s in c["serviti"]:
            if s in v:
                v.remove(s)
        return v

    def bisogni(self):
        """Quello che serve e che nessuno sta gia' facendo o portando: [(cliente, cosa)], dagli ordini piu' vecchi."""
        out = []
        for c in sorted([c for c in self.clienti if c["stato"] == "ordinato"], key=lambda c: c["preso"]):
            v = self.manca(c)
            for x in self.cose.values():
                if x["tav"] == c["tav"] and not x["sporco"] and x["dove"][0] != "tavolo" and x["item"] in v:
                    v.remove(x["item"])
            out += [(c, it) for it in v]
        return out

    def prossimo(self, st):
        for c, it in self.bisogni():
            if MENU[it]["st"] == st:
                return it, c["tav"]
        return None, -1

    def riassegna(self):
        """Ogni cosa pulita in giro (stazione, banco, mani) ha il cartellino di un tavolo che la aspetta davvero: quelle
        in piu' (il cliente se n'e' andato, o e' stato servito con la tazza di un altro tavolo) passano al primo che ne
        ha bisogno, o restano senza tavolo (-1) finche' qualcuno la ordina."""
        resta = {c["tav"]: self.manca(c) for c in self.clienti if c["stato"] == "ordinato"}
        orfane = []
        for u in sorted(self.cose):
            x = self.cose[u]
            if x["sporco"] or x["dove"][0] == "tavolo":
                continue
            v = resta.get(x["tav"])
            if v is not None and x["item"] in v:
                v.remove(x["item"])
            else:
                x["tav"] = -1
                orfane.append(u)
        for u in orfane:
            x = self.cose[u]
            for c in sorted([c for c in self.clienti if c["stato"] == "ordinato"], key=lambda c: c["preso"]):
                v = resta.get(c["tav"], [])
                if x["item"] in v:
                    v.remove(x["item"])
                    x["tav"] = c["tav"]
                    break

    # ------------------------------------------------------------------ il tempo che passa
    def gioca(self, t):
        cambiato = False
        rel = t - self.inizio
        # nuovi clienti: pochi all'inizio, poi di piu'; l'ultimo mezzo minuto si chiude
        if t >= self.prox and t < self.fine_t - 25:
            self.nuovo_cliente(t)
            frac = rel / max(1.0, self.dur)
            ritmo = 1.35 if rel < 40 else (1.0 if frac < 0.7 else 0.85)
            self.prox = t + self.ogni() * ritmo * self.rng.uniform(0.85, 1.2)
            cambiato = True
        for c in self.clienti:
            if c["stato"] == "arriva" and t >= c["arriva"]:
                c["stato"] = "aspetta"
                c["tot"] = self.pazienza_ordine()
                c["fino"] = t + c["tot"]
                cambiato = True
            elif c["stato"] in ("aspetta", "ordinato") and t >= c["fino"]:
                self.via_cliente(c, t, arrabbiato=True)
                cambiato = True
            elif c["stato"] == "mangia" and t >= c["fino"]:
                self.via_cliente(c, t)
                cambiato = True
        self.clienti = [c for c in self.clienti if not (c["stato"] == "via" and t - c.get("esce", t) > 6.0)]
        # le stazioni finiscono il loro lavoro
        for u, x in self.cose.items():
            if x["dove"][0] == "st" and x.get("fase") == "lavora" and t >= x["fino"]:
                x["fase"] = "pronto"
                x["pronto_t"] = t
                self.tutti({"a": "ev", "tipo": "pronto", "u": u, "st": x["dove"][1], "item": x["item"], "tav": x["tav"]})
                cambiato = True
        # la lavastoviglie
        if self.lava["dentro"] > 0 and t >= self.lava["fino"]:
            n = self.lava["dentro"]
            self.stoviglie += n
            self.stat["lavati"] += n
            self.lava["dentro"], self.lava["coda"] = self.lava["coda"], 0
            if self.lava["dentro"] > 0:
                self.lava["fino"] = t + LAVAGGIO
            self.tutti({"a": "ev", "tipo": "lavato", "n": n})
            cambiato = True
        # Mochi
        if t >= self.mochi["fino"]:
            if self.mochi_passo(t):
                cambiato = True
        if cambiato:
            self.manda_stato()

    def via_cliente(self, c, t, arrabbiato=False):
        k = c["tav"]
        c["stato"] = "via"
        c["esce"] = t
        # quello che hanno avuto resta sul tavolo, da sparecchiare
        lasciati = [u for u, x in self.cose.items() if x["dove"] == ["tavolo", k]]
        for u in lasciati:
            self.cose[u]["sporco"] = True
            self.cose[u]["dove"] = ["sporco", k]
        if lasciati:
            self.sporchi[k] = self.sporchi.get(k, []) + lasciati
            self.sporco_da.setdefault(k, t)
        if arrabbiato:
            self.stat["persi"] += 1
            self.team = max(0, self.team - 3)
            # le cose che erano per lui cercano un altro tavolo
            self.tutti({"a": "ev", "tipo": "arrabbiato", "tav": k, "id": c["id"]})
        else:
            self.tutti({"a": "ev", "tipo": "esce", "tav": k, "id": c["id"]})
        self.riassegna()

    # ------------------------------------------------------------------ le mani
    def prendi_in_mano(self, uid, u, vassoio=False):
        """Mette la cosa u nelle mani di uid: in mano se sono vuote (vassoio=False), altrimenti sul vassoio. False se non c'e' posto."""
        m = self.mani.setdefault(uid, {"tipo": "", "u": []})
        x = self.cose[u]
        if m["tipo"] == "":
            m["tipo"] = "vassoio" if (vassoio or x["sporco"]) else "mano"
            m["u"] = [u]
        elif m["tipo"] == "mano":
            if x["sporco"] or self.cose[m["u"][0]]["sporco"]:
                return False
            m["tipo"] = "vassoio"      # (la tazza che si aveva in mano va sul vassoio con la nuova)
            m["u"].append(u)
        else:
            if len(m["u"]) >= VASSOIO or any(self.cose[v]["sporco"] != x["sporco"] for v in m["u"]):
                return False
            m["u"].append(u)
        x["dove"] = [m["tipo"], uid]
        for v in m["u"]:
            self.cose[v]["dove"] = [m["tipo"], uid]
        return True

    def perche_no(self, uid, sporco=False):
        """Perche' non si puo' prendere altro in mano: per il messaggio del gioco."""
        m = self.mani.get(uid, {"tipo": "", "u": []})
        if any(self.cose[v]["sporco"] for v in m["u"]) != sporco:
            return "sporchi" if not sporco else "mani"
        return "pieno"

    def posto_in_mano(self, uid, sporco=False, vassoio=False):
        m = self.mani.get(uid, {"tipo": "", "u": []})
        if m["tipo"] == "":
            return True
        sp = any(self.cose[v]["sporco"] for v in m["u"])
        if sp != sporco:
            return False
        if m["tipo"] == "mano":
            return not sporco
        return len(m["u"]) < VASSOIO

    def togli_di_mano(self, uid, u):
        m = self.mani.get(uid)
        if not m or u not in m["u"]:
            return
        m["u"].remove(u)
        if not m["u"]:
            m["tipo"] = ""

    def nuova_cosa(self, item, tav, dove, chi, t, **kw):
        self.n_u += 1
        u = self.n_u
        self.cose[u] = dict({"item": item, "tav": tav, "sporco": False, "dove": dove, "chi": chi, "t": t}, **kw)
        return u

    # ------------------------------------------------------------------ dove si e'
    def vicino_a(self, gioc, xz, maxd=VICINO):
        """Vicino a un punto della sala, e nella sala del Capitol (le stanze stanno tutte nello stesso posto del mondo:
        conta anche in quale stanza dice il relay che sei)."""
        if gioc is None:
            return True      # (Mochi)
        stanza = self.lay.get("stanza")
        if not stanza:
            return self.vicino(gioc, [xz[0], 0, xz[1]], maxd)
        me = gioc.me
        if me.get("w") != "room:" + stanza or time.time() - me.get("last_p", 0) > POS_STALE:
            return False
        return math.hypot(me["x"] - xz[0], me["z"] - xz[1]) <= maxd

    def pos_di(self, gioc):
        if gioc is None:
            return self.mochi["pos"]
        return [gioc.me.get("x", 0.0), gioc.me.get("z", 0.0)]

    def slot_vicino(self, gioc, libero):
        """Il posto del banco piu' vicino a chi agisce, libero (o occupato)."""
        p = self.pos_di(gioc)
        best, bd = -1, 1e9
        for i, xz in enumerate(self.lay["banco"]):
            if (self.banco[i] is None) != libero:
                continue
            d = math.hypot(p[0] - xz[0], p[1] - xz[1])
            if d < bd:
                best, bd = i, d
        return best

    # ------------------------------------------------------------------ le azioni
    def azione(self, gioc, msg, t):
        a = msg.get("a")
        if t - gioc.ultimo < 0.12:
            return
        gioc.ultimo = t
        if a != "cade" and self.libero.get(gioc.uid, 0.0) > t + 0.05:
            return      # (ha le mani occupate: il gioco lo sa gia' e non lo chiede)
        r = self.fai(gioc.uid, gioc, msg, t)
        if r is True:
            self.manda_stato()
        elif isinstance(r, str):
            self.manda(gioc, {"a": "ev", "tipo": "no", "motivo": r, "st": msg.get("st", "")})

    def occupa(self, uid, t, dur):
        lento = MOCHI_LENTO if uid == "mochi" else 1.0
        self.libero[uid] = t + dur * lento
        return dur * lento

    def fai(self, uid, gioc, msg, t):
        """Fa l'azione per uid (un giocatore, o "mochi" con gioc None). True se e' cambiato qualcosa, una stringa se no."""
        a = msg.get("a")
        try:
            k = int(msg.get("tav", -1))
        except (TypeError, ValueError):
            k = -1
        st = self.lay["stazioni"]
        m = self.mani.setdefault(uid, {"tipo": "", "u": []})
        if a == "ordine":
            c = self.cliente_al(k)
            if c is None or c["stato"] != "aspetta":
                return "niente"
            if not self.vicino_a(gioc, self.lay["tavoli"][k]):
                return "lontano"
            c["stato"] = "ordinato"
            c["preso"] = t
            c["tot"] = self.pazienza(len(c["ordine"]))
            c["fino"] = t + c["tot"]
            if gioc:
                gioc.dati["ordini"] += 1
            self.stat["ordini"] += 1
            self.riassegna()
            self.tutti({"a": "ev", "tipo": "ordine", "tav": k, "ordine": c["ordine"], "chi": uid,
                        "dur": self.occupa(uid, t, T["ordine"])})
            return True
        if a in ("avvia", "completa"):
            s = str(msg.get("st", ""))
            if s not in STAZIONI:
                return "niente"
            if not self.vicino_a(gioc, st[s]):
                return "lontano"
            if a == "avvia":
                slot = next((i for i, v in enumerate(self.stazioni[s]) if v is None), -1)
                if slot < 0:
                    return "occupata"
                item, tav = self.prossimo(s)
                if item is None:
                    return "niente_da_fare"
                if self.stoviglie <= 0:
                    return "stoviglie"
                self.stoviglie -= 1
                av = MENU[item]["avvia"]
                dur = self.occupa(uid, t, av)
                u = self.nuova_cosa(item, tav, ["st", s, slot], uid, t, fase="lavora", fino=t + dur + MENU[item]["lavora"])
                self.stazioni[s][slot] = u
                if gioc:
                    gioc.dati["preparati"] += 1
                    self.stat["preparati"][gioc.nome] = self.stat["preparati"].get(gioc.nome, 0) + 1
                self.tutti({"a": "ev", "tipo": "avvia", "st": s, "slot": slot, "u": u, "item": item, "tav": tav, "chi": uid,
                            "dur": dur, "lavora": MENU[item]["lavora"]})
                return True
            # completa: la piu' vecchia pronta (prima quelle avviate da chi la completa)
            pronti = [(v, i) for i, v in enumerate(self.stazioni[s]) if v is not None and self.cose[v].get("fase") == "pronto"]
            if not pronti:
                lavora = any(v is not None for v in self.stazioni[s])
                return "non_pronto" if lavora else "niente"
            pronti.sort(key=lambda p: (self.cose[p[0]]["chi"] != uid, self.cose[p[0]].get("pronto_t", 0)))
            u, slot = pronti[0]
            if not self.posto_in_mano(uid):
                return self.perche_no(uid)
            if not self.prendi_in_mano(uid, u):
                return "mani"
            self.stazioni[s][slot] = None
            x = self.cose[u]
            x.pop("fase", None)
            x.pop("fino", None)
            self.tutti({"a": "ev", "tipo": "completa", "st": s, "slot": slot, "u": u, "item": x["item"], "tav": x["tav"],
                        "chi": uid, "dur": self.occupa(uid, t, MENU[x["item"]]["completa"])})
            return True
        if a == "posa":
            if m["tipo"] == "" or any(self.cose[v]["sporco"] for v in m["u"]):
                return "niente" if m["tipo"] == "" else "sporchi"
            try:
                want = int(msg.get("i", -1))
            except (TypeError, ValueError):
                want = -1
            messi = []
            for v in list(m["u"]):
                i = want if (0 <= want < SLOT_BANCO and self.banco[want] is None) else self.slot_vicino(gioc, True)
                if i < 0:
                    break
                if not self.vicino_a(gioc, self.lay["banco"][i]):
                    if not messi:
                        return "lontano"
                    break
                want = -1
                self.banco[i] = v
                self.cose[v]["dove"] = ["banco", i]
                self.cose[v]["t"] = t
                self.togli_di_mano(uid, v)
                messi.append([v, i])
            if not messi:
                return "banco_pieno"
            self.tutti({"a": "ev", "tipo": "posa", "chi": uid, "cose": messi, "dur": self.occupa(uid, t, T["posa"])})
            return True
        if a == "prendi":
            try:
                i = int(msg.get("i", -1))
            except (TypeError, ValueError):
                i = -1
            if not (0 <= i < SLOT_BANCO and self.banco[i] is not None):
                i = self.scegli_dal_banco(uid, gioc)
            if i < 0:
                return "vuoto"
            if not self.vicino_a(gioc, self.lay["banco"][i]):
                return "lontano"
            if not self.posto_in_mano(uid, vassoio=True):
                return self.perche_no(uid)
            u = self.banco[i]
            if not self.prendi_in_mano(uid, u, vassoio=True):
                return "pieno"
            self.banco[i] = None
            self.tutti({"a": "ev", "tipo": "prendi", "chi": uid, "u": u, "i": i, "dur": self.occupa(uid, t, T["prendi"])})
            return True
        if a == "dolce":
            if not self.vicino_a(gioc, st["vetrina"]):
                return "lontano"
            item, tav = self.prossimo("vetrina")
            if item is None:
                return "niente_da_fare"
            if not self.posto_in_mano(uid, vassoio=True):
                return self.perche_no(uid)
            if self.stoviglie <= 0:
                return "stoviglie"
            self.stoviglie -= 1
            u = self.nuova_cosa(item, tav, ["vassoio", uid], uid, t)
            self.prendi_in_mano(uid, u, vassoio=True)
            self.tutti({"a": "ev", "tipo": "dolce", "chi": uid, "u": u, "item": item, "tav": tav,
                        "dur": self.occupa(uid, t, MENU[item]["prendi"])})
            return True
        if a == "servi":
            c = self.cliente_al(k)
            if c is None or c["stato"] != "ordinato":
                return "niente"
            if not self.vicino_a(gioc, self.lay["tavoli"][k]):
                return "lontano"
            manca = self.manca(c)
            mie = [v for v in m["u"] if not self.cose[v]["sporco"]]
            # prima le cose con il cartellino di questo tavolo, poi le altre che gli servono
            mie.sort(key=lambda v: self.cose[v]["tav"] != k)
            scelti = []
            for v in mie:
                if self.cose[v]["item"] in manca:
                    manca.remove(self.cose[v]["item"])
                    scelti.append(v)
            if not scelti:
                self.stat["sbagli"] += 1
                self.tutti({"a": "ev", "tipo": "sbagliato", "tav": k, "chi": uid})
                return True
            for v in scelti:
                x = self.cose[v]
                x["tav"] = k
                x["dove"] = ["tavolo", k]
                self.togli_di_mano(uid, v)
                c["serviti"].append(x["item"])
                for who in (x.get("chi"), uid):
                    if who and who != "mochi" and who not in c["chi"]:
                        c["chi"].append(who)
            if gioc:
                gioc.dati["consegne"] += len(scelti)
                self.stat["consegne"][gioc.nome] = self.stat["consegne"].get(gioc.nome, 0) + len(scelti)
            self.tutti({"a": "ev", "tipo": "servito", "tav": k, "chi": uid, "u": scelti, "dur": self.occupa(uid, t, T["servi"])})
            if not self.manca(c):
                self.contento(c, t)
            self.riassegna()
            return True
        if a == "sparecchia":
            lasciati = self.sporchi.get(k, [])
            if not lasciati:
                return "niente"
            if not self.vicino_a(gioc, self.lay["tavoli"][k]):
                return "lontano"
            if not self.posto_in_mano(uid, sporco=True):
                return self.perche_no(uid, sporco=True)
            presi = []
            for v in list(lasciati):
                if not self.prendi_in_mano(uid, v, vassoio=True):
                    break
                lasciati.remove(v)
                presi.append(v)
            if not presi:
                return "pieno"
            finito = not lasciati
            if finito:
                self.sporchi.pop(k, None)
                self.sporco_da.pop(k, None)
                self.team += 2
                self.stat["sparecchiati"] += 1
                if gioc:
                    gioc.dati["sparecchiati"] += 1
            self.tutti({"a": "ev", "tipo": "sparecchia", "tav": k, "chi": uid, "u": presi, "finito": finito,
                        "dur": self.occupa(uid, t, T["sparecchia"])})
            self.aggiorna_punti()
            return True
        if a == "carica":
            if not self.vicino_a(gioc, st["lavastoviglie"]):
                return "lontano"
            sporchi = [v for v in m["u"] if self.cose[v]["sporco"]]
            if not sporchi:
                return "niente"
            for v in sporchi:
                self.togli_di_mano(uid, v)
                del self.cose[v]
            n = len(sporchi)
            if self.lava["dentro"] == 0:
                self.lava["dentro"] = n
                self.lava["fino"] = t + LAVAGGIO
            else:
                self.lava["coda"] += n
            self.team += n
            self.tutti({"a": "ev", "tipo": "carica", "chi": uid, "n": n, "u": sporchi, "dur": self.occupa(uid, t, T["carica"])})
            self.aggiorna_punti()
            return True
        if a == "cade":
            if not m["u"]:
                return "niente"
            try:
                n = max(1, min(len(m["u"]), int(msg.get("n", 1))))
            except (TypeError, ValueError):
                n = 1
            caduti = m["u"][-n:]
            items = []
            for v in caduti:
                items.append(self.cose[v]["item"])
                self.togli_di_mano(uid, v)
                del self.cose[v]
            if gioc:
                gioc.dati["cadute"] += n
            self.stat["cadute"] += n
            self.tutti({"a": "ev", "tipo": "cade", "chi": uid, "u": caduti, "items": items})
            self.riassegna()
            self.tazze_nuove(t)
            return True
        return "niente"

    def scegli_dal_banco(self, uid, gioc):
        """La cosa piu' utile da prendere: per i tavoli che ho gia' sul vassoio, poi la piu' vecchia; vicino a me."""
        miei = {self.cose[v]["tav"] for v in self.mani.get(uid, {"u": []})["u"]}
        p = self.pos_di(gioc)
        best, bk = -1, None
        for i, v in enumerate(self.banco):
            if v is None:
                continue
            x = self.cose[v]
            d = math.hypot(p[0] - self.lay["banco"][i][0], p[1] - self.lay["banco"][i][1])
            key = (d > VICINO, x["tav"] not in miei, x["t"], d)
            if bk is None or key < bk:
                best, bk = i, key
        return best

    def contento(self, c, t):
        """Tutto servito: contento, mangia, poi se ne va (e lascia le stoviglie)."""
        frac = max(0.0, (c["fino"] - t) / max(1.0, c["tot"]))
        pt = 8 * len(c["ordine"]) + int(round(12 * frac))
        squadra = len(c["chi"]) >= 2
        if squadra:
            pt += 5
            self.stat["squadra"] += 1
        self.team += pt
        self.stat["serviti"] += 1
        attesa = round(t - c["preso"], 1) if c["preso"] else None
        if attesa is not None and (self.stat["piu_veloce"] is None or attesa < self.stat["piu_veloce"][1]):
            self.stat["piu_veloce"] = [c["persone"][0]["nome"], attesa]
        c["stato"] = "mangia"
        c["fino"] = t + self.mangia
        c["tot"] = self.mangia
        self.tutti({"a": "ev", "tipo": "contento", "tav": c["tav"], "punti": pt, "squadra": squadra, "frac": round(frac, 2)})
        self.aggiorna_punti()

    def aggiorna_punti(self):
        for g in self.giocatori.values():
            g.punti = self.team

    def tazze_nuove(self, t):
        """Mai bloccati: se non c'e' piu' una stoviglia in giro, Mochi ne porta di nuove dal magazzino."""
        in_giro = len(self.cose) + self.lava["dentro"] + self.lava["coda"]
        if self.stoviglie <= 0 and in_giro == 0:
            self.stoviglie += 4
            self.stat["tazze_nuove"] += 4
            self.tutti({"a": "ev", "tipo": "mochi", "fa": "tazze_nuove", "n": 4})
            return True
        return False

    # ------------------------------------------------------------------ Mochi
    def mochi_va(self, t, dove, xz, poi):
        """Mochi cammina fino a xz (il gioco lo anima lungo i tavoli), poi fa `poi`."""
        p = self.mochi["pos"]
        d = math.hypot(xz[0] - p[0], xz[1] - p[1])
        dur = 0.3 + d / MOCHI_PASSO
        self.mochi["pos"] = list(xz)
        self.mochi["fa"] = poi
        self.mochi["fino"] = t + dur
        self.tutti({"a": "ev", "tipo": "mochi", "fa": "va", "dove": dove, "xz": [round(xz[0], 2), round(xz[1], 2)], "dur": round(dur, 2)})

    def mochi_lavoro(self, s):
        """Dove sta Mochi per usare la stazione s (davanti, dalla parte del bar)."""
        xz = self.lay["stazioni"][s]
        if s in STAZIONI or s == "lavastoviglie":
            return [xz[0], xz[1] + 0.85]
        return xz

    def mochi_passo(self, t):
        """Un passo del lavoro di Mochi. True se e' cambiato qualcosa."""
        mo = self.mochi
        fa = mo["fa"]
        solo = self.n_giocatori() == 1
        if self.tazze_nuove(t):
            mo["fino"] = t + 2.0
            return True
        # le azioni arrivate a destinazione
        if fa == "va_avvia":
            s = mo["job"]
            r = self.fai("mochi", None, {"a": "avvia", "st": s}, t)
            if r is True:
                mo["fa"] = "aspetta"
                u = self.stazioni[s][next(i for i, v in enumerate(self.stazioni[s]) if v is not None and self.cose[v]["chi"] == "mochi" and self.cose[v].get("fase"))]
                mo["fino"] = max(self.libero.get("mochi", t), self.cose[u]["fino"]) + 0.2
                return True
            mo["fa"], mo["fino"] = "riposa", t + 1.0
            return False
        if fa in ("aspetta", "va_completa"):
            s = mo["job"]
            r = self.fai("mochi", None, {"a": "completa", "st": s}, t)
            if r is True:
                mo["fa"] = "porta_banco"
                mo["fino"] = self.libero.get("mochi", t)
                return True
            if r == "non_pronto":
                mo["fino"] = t + 0.5
                return False
            mo["fa"], mo["fino"] = "riposa", t + 1.0
            return False
        if fa == "porta_banco":
            i = self.slot_vicino(None, True)
            if i < 0:
                mo["fino"] = t + 1.0      # (il banco e' pieno: aspetta)
                return False
            self.mochi_va(t, "banco", [self.lay["banco"][i][0], self.lay["banco"][i][1] - 0.7], "va_posa")
            return True
        if fa == "va_posa":
            r = self.fai("mochi", None, {"a": "posa"}, t)
            mo["fa"] = "riposa"
            mo["fino"] = self.libero.get("mochi", t) + (1.5 if solo else 1.0)
            if r is True:
                if not solo:
                    self.stat["mochi_aiuti"] += 1
                return True
            mo["fino"] = t + 1.0
            return False
        if fa == "va_sparecchia":
            k = mo["job"]
            r = self.fai("mochi", None, {"a": "sparecchia", "tav": k}, t)
            if r is True:
                mo["fa"] = "porta_lava"
                mo["fino"] = self.libero.get("mochi", t)
                return True
            mo["fa"], mo["fino"] = "riposa", t + 1.0
            return False
        if fa == "porta_lava":
            # (con il vassoio sporco: a volte traballa, di rado si rompe una tazza)
            r = self.rng.random()
            if r < 0.08 and self.mani["mochi"]["u"]:
                v = self.mani["mochi"]["u"][-1]
                self.togli_di_mano("mochi", v)
                del self.cose[v]
                self.stat["cadute"] += 1
                self.tutti({"a": "ev", "tipo": "mochi", "fa": "rompe", "u": [v]})
            elif r < 0.4:
                self.tutti({"a": "ev", "tipo": "mochi", "fa": "traballa"})
            self.mochi_va(t, "lavastoviglie", self.mochi_lavoro("lavastoviglie"), "va_carica")
            return True
        if fa == "va_carica":
            self.fai("mochi", None, {"a": "carica"}, t)
            self.stat["mochi_aiuti"] += 1
            mo["fa"] = "torna"
            mo["fino"] = self.libero.get("mochi", t) + 0.3
            return True
        if fa == "torna":
            self.mochi_va(t, "casa", self.casa_mochi(), "riposa")
            return True
        # libero: che si fa?
        if self.mani["mochi"]["u"]:
            # (qualcosa in mano: al banco se e' pulito, in lavastoviglie se e' sporco)
            sp = any(self.cose[v]["sporco"] for v in self.mani["mochi"]["u"])
            mo["fa"] = "porta_lava" if sp else "porta_banco"
            mo["fino"] = t
            return False
        # 1. una bevanda pronta lasciata alla stazione (da soli: subito; in squadra: dimenticata da un po')
        attesa_max = 0.5 if solo else 12.0
        for s, slots in self.stazioni.items():
            for v in slots:
                if v is not None and self.cose[v].get("fase") == "pronto" and t - self.cose[v].get("pronto_t", t) >= attesa_max:
                    mo["job"] = s
                    self.mochi_va(t, s, self.mochi_lavoro(s), "va_completa")
                    return True
        # 2. da soli: Mochi e' il barista
        if solo and self.stoviglie > 0:
            for c, it in self.bisogni():
                s = MENU[it]["st"]
                if s in STAZIONI and any(v is None for v in self.stazioni[s]):
                    mo["job"] = s
                    self.mochi_va(t, s, self.mochi_lavoro(s), "va_avvia")
                    return True
        # 3. in squadra: un tavolo sporco da un po'
        if not solo:
            vecchi = [k for k, da in self.sporco_da.items() if t - da >= 10.0 and self.sporchi.get(k)]
            if vecchi:
                k = min(vecchi, key=lambda k: self.sporco_da[k])
                mo["job"] = k
                tx = self.lay["tavoli"][k]
                self.mochi_va(t, "tav", [tx[0], tx[1] - 0.8], "va_sparecchia")
                self.tutti({"a": "ev", "tipo": "mochi", "fa": "sparecchia", "tav": k})
                return True
        # 4. il bar in ordine: un piccolo gesto di soddisfazione
        if self.bar_in_ordine() and t - mo["ordine_t"] > 40.0 and self.stat["serviti"] > 0:
            mo["ordine_t"] = t
            mo["fino"] = t + 3.0
            self.tutti({"a": "ev", "tipo": "mochi", "fa": "ordine"})
            return False
        # 5. le gag (non troppo spesso)
        if not solo and self.rng.random() < 0.35:
            self.stat["mochi"] += 1
            fa2 = self.rng.choice(["balla", "assaggia", "sbadiglia", "giocoliere", "asciuga"])
            self.tutti({"a": "ev", "tipo": "mochi", "fa": fa2})
            mo["fino"] = t + self.rng.uniform(9, 15)
            return False
        mo["fino"] = t + (1.0 if solo else self.rng.uniform(3, 6))
        if solo and self.rng.random() < 0.15:
            self.tutti({"a": "ev", "tipo": "mochi", "fa": "asciuga"})
        return False

    def bar_in_ordine(self):
        return (not self.sporchi and all(v is None for v in self.banco) and self.lava["dentro"] == 0
                and all(v is None for sl in self.stazioni.values() for v in sl))

    # ------------------------------------------------------------------ lo stato a tutti
    def manda_stato(self):
        t = time.time()
        cose = {}
        for u, x in self.cose.items():
            e = {"item": x["item"], "tav": x["tav"], "dove": x["dove"]}
            if x["sporco"]:
                e["sporco"] = True
            if x["dove"][0] == "st":
                e["fase"] = x.get("fase", "lavora")
                e["resta"] = round(max(0.0, x.get("fino", t) - t), 2)
            cose[str(u)] = e
        clienti = []
        for c in self.clienti:
            clienti.append({"id": c["id"], "tav": c["tav"], "stato": c["stato"],
                            "ordine": c["ordine"] if c["stato"] not in ("aspetta", "arriva") else [],
                            "serviti": c["serviti"], "resta": round(max(0.0, c["fino"] - t), 1), "tot": c["tot"],
                            "persone": c["persone"]})
        self.tutti({"a": "cs", "clienti": clienti, "cose": cose,
                    "mani": {u: {"tipo": m["tipo"], "u": list(m["u"])} for u, m in self.mani.items()},
                    "banco": list(self.banco), "stazioni": {s: list(v) for s, v in self.stazioni.items()},
                    "sporchi": {str(k): list(v) for k, v in self.sporchi.items() if v},
                    "stoviglie": self.stoviglie, "stoviglie_tot": self.stoviglie + len(self.cose) + self.lava["dentro"] + self.lava["coda"],
                    "lava": {"dentro": self.lava["dentro"], "coda": self.lava["coda"], "resta": round(max(0.0, self.lava["fino"] - t), 1) if self.lava["dentro"] else 0.0},
                    "punti": self.team, "serviti": self.stat["serviti"], "tavoli": self.n_tavoli(),
                    "ruoli": self.ruoli, "nomi": {g.uid: g.nome for g in self.giocatori.values()},
                    "mochi": {"fa": self.mochi["fa"], "pos": self.mochi["pos"]}})

    def rientra(self, gioc, me, ws):
        super().rientra(gioc, me, ws)
        if self.stato == "via":
            self.mani.setdefault(gioc.uid, {"tipo": "", "u": []})
            self.manda_stato()

    # ------------------------------------------------------------------ la fine: tutta la squadra insieme
    def stelle(self):
        tot = max(1, self.stat["clienti"])
        r = self.stat["serviti"] / tot
        return 3 if r >= 0.8 else (2 if r >= 0.55 else (1 if r >= 0.25 or self.stat["serviti"] >= 1 else 0))

    def termina(self):
        self.stato = "fine"
        self.chiusa = time.time()
        stelle = self.stelle()
        premi_cfg = CFG.get("premi", {}).get(self.gioco, {})
        mio = dict(premi_cfg.get(str(stelle), {})) if (not self.prova or stelle >= 1) and self.stat["serviti"] > 0 else {}
        if self.prova:
            mio = {}
        esito = "stelle%d" % stelle if mio and stelle >= 1 else None      # (come i premi: uguale per tutta la squadra)
        for gioc in self.giocatori.values():
            gioc.punti = self.team
            self.manda(gioc, dict({"a": "fine", "classifica": self.lista(), "posto": 1 if stelle >= 2 else 0, "premi": mio, "extra": self.extra_fine(gioc)},
                                  **self.ricevuta(gioc, esito)))
        self.g.log("fine", self.gioco, self.id, "stelle", stelle, self.stat)

    def lista(self):
        return [[x.nome, x.dati.get("consegne", 0) + x.dati.get("preparati", 0), x.uid, 0 if x.via is None else 1]
                for x in sorted(self.giocatori.values(), key=lambda x: -(x.dati.get("consegne", 0) + x.dati.get("preparati", 0)))]

    def extra_fine(self, gioc):
        cons = self.stat["consegne"]
        prep = self.stat["preparati"]
        return {"stelle": self.stelle(), "punti": self.team, "serviti": self.stat["serviti"], "clienti": self.stat["clienti"],
                "persi": self.stat["persi"], "cadute": self.stat["cadute"], "sbagli": self.stat["sbagli"], "squadra": self.stat["squadra"],
                "mochi": self.stat["mochi"], "mochi_aiuti": self.stat["mochi_aiuti"], "piu_veloce": self.stat["piu_veloce"],
                "mvp": max(cons, key=cons.get) if cons else None, "barista": max(prep, key=prep.get) if prep else None,
                "sparecchiati": self.stat["sparecchiati"], "lavati": self.stat["lavati"], "mie": gioc.dati}
