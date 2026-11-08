"""Il cameriere pasticcione: il Caffe' dei Portici (Piazza del Municipio, dove sbucano i Portici), lato server.

Un gioco di squadra: da 1 a 6 giocatori mandano avanti il locale di Mochi per qualche minuto. Il server tiene tutto:
i clienti (arrivano, si siedono, ordinano, aspettano, mangiano, se ne vanno), gli ordini, le bevande in preparazione
alla macchina, cio' che e' pronto sul passe, cosa c'e' sul vassoio di ognuno (lo vedono tutti), le consegne, i
pasticci, le trovate di Mochi e il punteggio della squadra. Le distanze le controlla sulla posizione che conosce il
relay (mai su quella dichiarata). La disposizione del locale e' in caffe.json (copia di data/caffe.json).

Dal gioco ({"t": "g", ...}):
  {"a": "ordine", "tav": k}            prende l'ordinazione al tavolo k
  {"a": "prepara", "st": "macchina"|"succhi"|"dolci"}   prepara la prossima cosa che serve a quella stazione
                                       (le bevande vanno sul passe dopo un po', i dolci subito sul vassoio)
  {"a": "prendi"}                      dal passe al proprio vassoio (la prima cosa pronta)
  {"a": "servi", "tav": k}             porta al tavolo k cio' che gli serve, se e' sul vassoio
  {"a": "cade", "n": 1}                dal proprio vassoio e' caduto qualcosa (il gioco simula l'equilibrio)
  {"a": "svuota"}                      lascia il vassoio sul bancone (le cose tornano sul passe)
Al gioco: "via" (la disposizione, il menu, la durata), "cs" (lo stato del locale, a ogni cambiamento: clienti,
passe, preparazioni, vassoi, punti), "ev" (un fatto da animare: consegna, sbaglio, caduta, Mochi, cliente
arrabbiato, pronto), poi "stato" e "fine" come gli altri minigiochi (la fine porta stelle e riepilogo in "extra").
"""
import json
import math
import os
import time

from giochi import CFG, HERE, Partita

MENU = {
    "caffe": {"nome": "Caffè", "st": "macchina", "t": 2.2, "prop": "o_cup_coffee"},
    "cappuccino": {"nome": "Cappuccino", "st": "macchina", "t": 3.0, "prop": "cappuccino"},
    "cioccolata": {"nome": "Cioccolata calda", "st": "macchina", "t": 3.4, "prop": "o_cup_cocoa"},
    "succo": {"nome": "Succo di mela", "st": "succhi", "t": 2.6, "prop": "o_glass_juice"},
    "strudel": {"nome": "Strudel", "st": "dolci", "t": 0.0, "prop": "o_plate_strudel"},
    "krapfen": {"nome": "Krapfen", "st": "dolci", "t": 0.0, "prop": "krapfen"},
    "torta": {"nome": "Torta Sacher", "st": "dolci", "t": 0.0, "prop": "o_plate_sacher"},
    "kaiserschmarrn": {"nome": "Kaiserschmarrn", "st": "dolci", "t": 0.0, "prop": "o_plate_kaiser"},
}
SPECIE = ["gatto", "cane", "volpe", "orso", "marmotta"]
NOMI = ["Ugo", "Lotte", "Sepp", "Mia", "Anton", "Greta", "Tobia", "Resi", "Kurt", "Nora", "Hans", "Lina", "Toni", "Vroni", "Pia", "Franz"]
VASSOIO = 3          # quante cose sul vassoio
VICINO = 2.6         # metri: a un tavolo, a una stazione


def _layout():
    try:
        with open(os.path.join(HERE, "caffe.json"), encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"tavoli": [[0, 0]], "stazioni": {"macchina": [0, 0], "succhi": [0, 0], "dolci": [0, 0], "passe": [0, 0]}}


class Caffe(Partita):
    gioco = "caffe"

    def __init__(self, giochi, room, prova=False):
        super().__init__(giochi, room, prova)
        self.lay = _layout()
        if os.environ.get("GIOCHI_VELOCE") and not prova:
            # (collaudi: si gioca anche da soli, ma si aspetta abbastanza perche' entri anche il secondo)
            self.cfg = dict(self.cfg, attesa=10)

    def avvia(self):
        cfg = self.cfg
        self.lay = _layout()
        self.dur = cfg.get("durata", 240)
        self.fine_t = self.inizio + self.dur
        self.clienti = []          # [{id, tav, stato, ordine, serviti, arriva, fino, nome, specie, seme, chi}]
        self.passe = []            # [{item, da}] pronto sul bancone
        self.prep = []             # [{st, item, fino, chi}]
        self.vassoi = {}           # uid -> [{item, da}]
        self.team = 0
        self.prox = 0.0
        self.n_id = 0
        self.mochi_t = 0.0
        self.stat = {"serviti": 0, "ordini": 0, "persi": 0, "cadute": 0, "sbagli": 0, "mochi": 0, "squadra": 0, "clienti": 0,
                     "consegne": {}, "piu_veloce": None, "mochi_aiuti": 0}
        for gioc in self.giocatori.values():
            gioc.dati = {"consegne": 0, "preparati": 0, "ordini": 0, "cadute": 0}
            self.vassoi[gioc.uid] = []
        self.tutti(dict(self.dati_via(None), a="via"))
        # i primi clienti arrivano subito
        for _ in range(min(2, self.n_tavoli())):
            self.nuovo_cliente(self.inizio)
        self.prox = self.inizio + 6.0
        self.mochi_t = self.inizio + self.rng.uniform(10, 16)
        self.manda_stato()
        self.g.log("via caffe", self.id, len(self.giocatori), "giocatori")

    def dati_via(self, gioc):
        return {"durata": self.dur, "tavoli": self.n_tavoli(), "menu": {k: [v["nome"], v["st"], v["prop"]] for k, v in MENU.items()},
                "vassoio": VASSOIO, "prova": self.prova}

    # -- quanto e' difficile: secondo chi c'e'
    def n_giocatori(self):
        return max(1, len(self.presenti()))

    def n_tavoli(self):
        return min(len(self.lay["tavoli"]), 2 + 2 * self.n_giocatori()) if self.stato == "via" else min(len(self.lay["tavoli"]), 4)

    def ogni(self):
        """Secondi tra un cliente e l'altro: piu' giocatori, piu' clienti (ma mai una valanga)."""
        n = self.n_giocatori()
        base = self.cfg.get("ogni", 14.0)
        return max(4.5, base / (0.55 + 0.45 * n))

    def pazienza(self):
        return self.cfg.get("pazienza", 70.0)

    # -- i clienti
    def tavoli_liberi(self):
        occ = {c["tav"] for c in self.clienti if c["stato"] != "via"}
        return [k for k in range(self.n_tavoli()) if k not in occ]

    def nuovo_cliente(self, t):
        liberi = self.tavoli_liberi()
        if not liberi:
            return
        k = self.rng.choice(liberi)
        self.n_id += 1
        n_cose = 1 if self.rng.random() < 0.55 else 2
        bev = [i for i in MENU if MENU[i]["st"] != "dolci"]
        dol = [i for i in MENU if MENU[i]["st"] == "dolci"]
        ordine = [self.rng.choice(bev)]
        if n_cose == 2:
            ordine.append(self.rng.choice(dol if self.rng.random() < 0.7 else bev))
        self.clienti.append({"id": self.n_id, "tav": k, "stato": "arriva", "ordine": ordine, "serviti": [], "arriva": t + 4.0,
                             "fino": 0.0, "nome": self.rng.choice(NOMI), "specie": self.rng.choice(SPECIE), "seme": self.rng.randrange(1 << 20),
                             "chi": [], "preso": 0.0})
        self.stat["clienti"] += 1

    def cliente_al(self, k):
        for c in self.clienti:
            if c["tav"] == k and c["stato"] in ("aspetta", "ordinato", "arriva"):
                return c
        return None

    def gioca(self, t):
        cambiato = False
        # nuovi clienti (l'ultimo mezzo minuto, piu' radi: si chiude)
        if t >= self.prox and t < self.fine_t - 20:
            self.nuovo_cliente(t)
            self.prox = t + self.ogni() * self.rng.uniform(0.8, 1.25) * (1.5 if t > self.fine_t - 45 else 1.0)
            cambiato = True
        for c in self.clienti:
            if c["stato"] == "arriva" and t >= c["arriva"]:
                # seduto: alza la mano per ordinare
                c["stato"] = "aspetta"
                c["fino"] = t + self.pazienza() * 0.8
                cambiato = True
            elif c["stato"] in ("aspetta", "ordinato") and t >= c["fino"]:
                c["stato"] = "via"
                c["esce"] = t
                self.stat["persi"] += 1
                self.team = max(0, self.team - 3)
                self.tutti({"a": "ev", "tipo": "arrabbiato", "tav": c["tav"], "id": c["id"]})
                cambiato = True
            elif c["stato"] == "mangia" and t >= c["fino"]:
                c["stato"] = "via"
                c["esce"] = t
                cambiato = True
        # via quelli usciti da un po' (il gioco li ha gia' fatti andare)
        self.clienti = [c for c in self.clienti if not (c["stato"] == "via" and t - c.get("esce", t) > 6.0)]
        # le bevande pronte vanno sul passe
        for p in list(self.prep):
            if t >= p["fino"]:
                self.prep.remove(p)
                self.passe.append({"item": p["item"], "da": p["chi"], "t": t})
                self.tutti({"a": "ev", "tipo": "pronto", "item": p["item"], "st": p["st"]})
                cambiato = True
        # Mochi: ogni tanto ne combina una (o da' davvero una mano)
        if t >= self.mochi_t:
            self.mochi_t = t + self.rng.uniform(14, 22)
            self.trovata(t)
            cambiato = True
        if cambiato:
            self.manda_stato()

    def trovata(self, t):
        """Mochi porta qualcosa dal passe a chi lo aspetta (a volte ci riesce), oppure inciampa, assaggia, balla."""
        self.stat["mochi"] += 1
        da_portare = None
        for i, it in enumerate(self.passe):
            # (solo cio' che nessuno ritira da un po': Mochi aiuta, non ruba la tazza a chi sta arrivando)
            if t - it.get("t", 0.0) < 8.0:
                continue
            for c in self.clienti:
                if c["stato"] == "ordinato" and self.manca(c, it["item"]):
                    da_portare = (i, c)
                    break
            if da_portare:
                break
        r = self.rng.random()
        if da_portare and r < 0.45:
            i, c = da_portare
            it = self.passe.pop(i)
            self.stat["mochi_aiuti"] += 1
            self.tutti({"a": "ev", "tipo": "mochi", "fa": "porta", "tav": c["tav"], "item": it["item"]})
            self.consegna(c, [it], None, t)
        elif da_portare and r < 0.7:
            i, c = da_portare
            it = self.passe.pop(i)
            self.stat["cadute"] += 1
            self.tutti({"a": "ev", "tipo": "mochi", "fa": "inciampa", "tav": c["tav"], "item": it["item"]})
        else:
            fa = self.rng.choice(["balla", "assaggia", "sbadiglia", "giocoliere"])
            self.tutti({"a": "ev", "tipo": "mochi", "fa": fa})

    def manca(self, c, item):
        voluti = list(c["ordine"])
        for s in c["serviti"]:
            if s in voluti:
                voluti.remove(s)
        return item in voluti

    def serve_ancora(self):
        """Le cose ordinate che non sono ancora ne' servite, ne' in preparazione, ne' sul passe, ne' su un vassoio."""
        voluti = []
        for c in self.clienti:
            if c["stato"] == "ordinato":
                v = list(c["ordine"])
                for s in c["serviti"]:
                    if s in v:
                        v.remove(s)
                voluti += v
        for x in [p["item"] for p in self.prep] + [p["item"] for p in self.passe] + [i["item"] for v in self.vassoi.values() for i in v]:
            if x in voluti:
                voluti.remove(x)
        return voluti

    def consegna(self, c, items, gioc, t):
        for it in items:
            c["serviti"].append(it["item"])
            if it.get("da") and it["da"] not in c["chi"]:
                c["chi"].append(it["da"])
        if gioc and gioc.uid not in c["chi"]:
            c["chi"].append(gioc.uid)
        if all(not self.manca(c, x) for x in c["ordine"]) and len(c["serviti"]) >= len(c["ordine"]):
            # tutto servito: contento, mangia, poi se ne va
            frac = max(0.0, (c["fino"] - t) / self.pazienza())
            pt = 10 * len(c["ordine"]) + int(round(10 * frac))
            squadra = len(c["chi"]) >= 2
            if squadra:
                pt += 5
                self.stat["squadra"] += 1
            self.team += pt
            self.stat["serviti"] += 1
            attesa = round(t - c["preso"], 1) if c["preso"] else None
            if attesa is not None and (self.stat["piu_veloce"] is None or attesa < self.stat["piu_veloce"][1]):
                self.stat["piu_veloce"] = [c["nome"], attesa]
            c["stato"] = "mangia"
            c["fino"] = t + 9.0
            self.tutti({"a": "ev", "tipo": "contento", "tav": c["tav"], "punti": pt, "squadra": squadra, "frac": round(frac, 2)})
            for g in self.giocatori.values():
                g.punti = self.team

    # -- i giocatori
    def vicino_a(self, gioc, xz):
        return self.vicino(gioc, [xz[0], 0, xz[1]], VICINO)

    def azione(self, gioc, msg, t):
        a = msg.get("a")
        if t - gioc.ultimo < 0.15:
            return
        gioc.ultimo = t
        no = lambda why, **kw: self.manda(gioc, dict({"a": "ev", "tipo": "no", "motivo": why}, **kw))
        try:
            k = int(msg.get("tav", -1))
        except (TypeError, ValueError):
            k = -1
        v = self.vassoi.setdefault(gioc.uid, [])
        st = self.lay["stazioni"]
        if a == "ordine":
            c = self.cliente_al(k)
            if c is None or c["stato"] != "aspetta":
                return no("niente")
            if not self.vicino_a(gioc, self.lay["tavoli"][k]):
                return no("lontano")
            c["stato"] = "ordinato"
            c["preso"] = t
            c["fino"] = t + self.pazienza()
            gioc.dati["ordini"] += 1
            self.stat["ordini"] += 1
            self.tutti({"a": "ev", "tipo": "ordine", "tav": k, "ordine": c["ordine"], "chi": gioc.nome})
        elif a == "prepara":
            s = str(msg.get("st", ""))
            if s not in ("macchina", "succhi", "dolci"):
                return
            if not self.vicino_a(gioc, st[s]):
                return no("lontano")
            serve = [i for i in self.serve_ancora() if MENU[i]["st"] == s]
            if not serve:
                return no("niente_da_fare", st=s)
            item = serve[0]
            if s == "dolci":
                if len(v) >= VASSOIO:
                    return no("pieno")
                v.append({"item": item, "da": gioc.uid})
                gioc.dati["preparati"] += 1
                self.tutti({"a": "ev", "tipo": "preso", "item": item, "chi": gioc.uid})
            else:
                if sum(1 for p in self.prep if p["st"] == s) >= 2:
                    return no("occupata", st=s)
                self.prep.append({"st": s, "item": item, "fino": t + MENU[item]["t"], "chi": gioc.uid, "da": t})
                gioc.dati["preparati"] += 1
                self.tutti({"a": "ev", "tipo": "prepara", "item": item, "st": s, "chi": gioc.uid, "dur": MENU[item]["t"]})
        elif a == "prendi":
            if not self.vicino_a(gioc, st["passe"]):
                return no("lontano")
            if not self.passe:
                return no("vuoto")
            if len(v) >= VASSOIO:
                return no("pieno")
            v.append(self.passe.pop(0))
            self.tutti({"a": "ev", "tipo": "preso", "item": v[-1]["item"], "chi": gioc.uid})
        elif a == "servi":
            c = self.cliente_al(k)
            if c is None or c["stato"] != "ordinato":
                return no("niente")
            if not self.vicino_a(gioc, self.lay["tavoli"][k]):
                return no("lontano")
            dati = [it for it in v if self.manca(c, it["item"])]
            # (uno per tipo: due cappuccini per chi ne ha chiesto uno no)
            scelti = []
            for it in dati:
                tmp = dict(c, serviti=c["serviti"] + [x["item"] for x in scelti])
                if self.manca(tmp, it["item"]):
                    scelti.append(it)
            if not scelti:
                self.stat["sbagli"] += 1
                return self.tutti({"a": "ev", "tipo": "sbagliato", "tav": k, "chi": gioc.uid, "vassoio": [x["item"] for x in v]})
            for it in scelti:
                v.remove(it)
            gioc.dati["consegne"] += len(scelti)
            self.stat["consegne"][gioc.nome] = self.stat["consegne"].get(gioc.nome, 0) + len(scelti)
            self.tutti({"a": "ev", "tipo": "servito", "tav": k, "chi": gioc.uid, "items": [x["item"] for x in scelti]})
            self.consegna(c, scelti, gioc, t)
        elif a == "cade":
            if not v:
                return
            n = max(1, min(len(v), int(msg.get("n", 1)) if str(msg.get("n", 1)).isdigit() else 1))
            caduti = [v.pop() for _ in range(n)]
            gioc.dati["cadute"] += n
            self.stat["cadute"] += n
            self.tutti({"a": "ev", "tipo": "cade", "chi": gioc.uid, "items": [x["item"] for x in caduti]})
        elif a == "svuota":
            if not self.vicino_a(gioc, st["passe"]) or not v:
                return
            self.passe += [dict(i, t=t) for i in v]
            self.vassoi[gioc.uid] = []
        else:
            return
        self.manda_stato()

    def manda_stato(self):
        t = time.time()
        self.tutti({"a": "cs", "clienti": [{"id": c["id"], "tav": c["tav"], "stato": c["stato"], "ordine": c["ordine"] if c["stato"] != "aspetta" and c["stato"] != "arriva" else [],
                                             "serviti": c["serviti"], "resta": round(max(0.0, c["fino"] - t), 1), "nome": c["nome"],
                                             "specie": c["specie"], "seme": c["seme"]} for c in self.clienti],
                    "passe": [p["item"] for p in self.passe], "prep": [{"st": p["st"], "item": p["item"], "resta": round(max(0.0, p["fino"] - t), 2)} for p in self.prep],
                    "vassoi": {u: [i["item"] for i in v] for u, v in self.vassoi.items()}, "punti": self.team, "serviti": self.stat["serviti"],
                    "tavoli": self.n_tavoli()})

    def rientra(self, gioc, me, ws):
        super().rientra(gioc, me, ws)
        if self.stato == "via":
            self.manda_stato()

    # -- la fine: tutta la squadra insieme
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
        for gioc in self.giocatori.values():
            gioc.punti = self.team
            self.manda(gioc, {"a": "fine", "classifica": self.lista(), "posto": 1 if stelle >= 2 else 0, "premi": mio, "extra": self.extra_fine(gioc)})
        self.g.log("fine", self.gioco, self.id, "stelle", stelle, self.stat)

    def lista(self):
        return [[x.nome, x.dati.get("consegne", 0), x.uid, 0 if x.via is None else 1]
                for x in sorted(self.giocatori.values(), key=lambda x: -x.dati.get("consegne", 0))]

    def extra_fine(self, gioc):
        cons = self.stat["consegne"]
        mvp = max(cons, key=cons.get) if cons else None
        return {"stelle": self.stelle(), "punti": self.team, "serviti": self.stat["serviti"], "clienti": self.stat["clienti"],
                "persi": self.stat["persi"], "cadute": self.stat["cadute"], "sbagli": self.stat["sbagli"], "squadra": self.stat["squadra"],
                "mochi": self.stat["mochi"], "mochi_aiuti": self.stat["mochi_aiuti"], "piu_veloce": self.stat["piu_veloce"], "mvp": mvp,
                "mie": gioc.dati}
