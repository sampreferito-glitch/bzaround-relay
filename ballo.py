"""Minigioco 7, Balla al Waltherhaus (la sala da ballo del Waltherhaus), lato server. Importato da giochi.py
(TIPI["ballo"]).

Il server decide tutto: la difficolta' (la sceglie chi apre la sala), il brano (tempo e coreografia, uguali per tutti),
quando si parte, e giudica ogni mossa: il gioco manda solo quale mossa e in quale istante del brano (sul suo orologio
allineato al server), il server la confronta con la coreografia e decide perfetto / bene / ok / sbagliata, i punti, le
combo, la sequenza perfetta, la classifica e i premi. Se mancano ballerini (da soli o in pochi) ballano anche gli NPC.

La coreografia: dopo una battuta di attacco, coppie di battute: nella prima Mochi, sul palco, mostra la sequenza (i
ballerini guardano), nella seconda tutti la ripetono, mossa per mossa, sugli stessi tempi.

Messaggi dal gioco: {"a": "mossa", "m": "destra"|..., "tb": secondi del brano}
Messaggi al gioco:  "via" (difficolta', bpm, coreografia, inizio, ballerini), "colpo" (una mossa giudicata, a tutti),
                    "sequenza" (una sequenza finita: chi l'ha fatta perfetta), poi "fine".
"""
import os
import time

from giochi import Partita

MOSSE = ["destra", "sinistra", "salto", "giro", "mani", "braccia", "avanti", "speciale"]
DIFF = {
    # bpm, battute del brano (escluso l'attacco), mosse possibili, quante mosse per sequenza (min, max), mezzi tempi
    "facile": {"bpm": 96, "coppie": 26, "mosse": ["destra", "sinistra", "mani", "braccia", "salto"], "n": (2, 3), "mezzi": False},
    "normale": {"bpm": 112, "coppie": 30, "mosse": ["destra", "sinistra", "mani", "braccia", "salto", "giro", "avanti"], "n": (3, 4), "mezzi": False},
    "difficile": {"bpm": 126, "coppie": 34, "mosse": MOSSE, "n": (4, 6), "mezzi": True},
}
FINESTRE = [(0.07, "perfetto", 100), (0.14, "bene", 70), (0.24, "ok", 40)]
NPC = [{"id": "trottola", "nome": "Trottola", "bravura": 0.55}, {"id": "pralina", "nome": "Pralina", "bravura": 0.8},
       {"id": "gomitolo", "nome": "Gomitolo", "bravura": 0.4}, {"id": "brezza", "nome": "Brezza", "bravura": 0.7},
       {"id": "pepe", "nome": "Pepe", "bravura": 0.6}, {"id": "nuvola", "nome": "Nuvola", "bravura": 0.75}]


def _veloce():
    return bool(os.environ.get("GIOCHI_VELOCE"))


class Ballo(Partita):
    gioco = "ballo"

    def __init__(self, giochi, room, prova=False):
        super().__init__(giochi, room, prova)
        self.diff = ""

    def scelta(self, msg):
        """Chi apre la sala sceglie la difficolta' (chi arriva dopo balla quella)."""
        d = str(msg.get("diff", ""))
        if not self.diff and d in DIFF:
            self.diff = d

    def avvia(self):
        if self.diff not in DIFF:
            self.diff = "normale"
        cfg = DIFF[self.diff]
        self.bpm = cfg["bpm"]
        self.batt = 60.0 / self.bpm
        coppie = cfg["coppie"] if not _veloce() else 4
        # la coreografia: [battuta della ripetizione, [[tempo, mossa], ...]] (i tempi in secondi dal via del brano)
        self.sequenze = []
        b0 = 4 * self.batt                                   # (una battuta di attacco)
        for k in range(coppie):
            mostra = b0 + k * 8 * self.batt
            ripeti = mostra + 4 * self.batt
            # (piu' avanti nel brano le sequenze si allungano un poco)
            lo, hi = cfg["n"]
            n = min(hi, lo + (k * (hi - lo + 1)) // max(1, coppie))
            n = max(lo, min(hi, n + (1 if self.rng.random() < 0.3 else 0)))
            posti = [i * 0.5 for i in range(8)] if cfg["mezzi"] else [float(i) for i in range(4)]
            scelti = sorted(self.rng.sample(posti, min(n, len(posti))))
            mosse = []
            for tb in scelti:
                m = self.rng.choice(cfg["mosse"])
                # (la mossa speciale: al massimo una a sequenza, mai in apertura)
                if m == "speciale" and (tb == 0 or any(x[1] == "speciale" for x in mosse)):
                    m = "giro"
                mosse.append([round(tb * self.batt, 3), m])
            self.sequenze.append({"mostra": round(mostra, 3), "ripeti": round(ripeti, 3), "mosse": mosse})
        self.durata = b0 + coppie * 8 * self.batt + 2 * self.batt
        self.attesa = 5.0 if not _veloce() else 3.0          # il conto alla rovescia
        self.t0 = self.inizio + self.attesa                    # il via del brano (ora del server)
        self.fine_t = self.t0 + self.durata + 1.0
        # i ballerini: i giocatori e, se mancano, gli NPC (fino a quattro in pista; da soli tre NPC)
        self.npc = {}
        quanti = max(0, int(self.cfg.get("ballerini", 4)) - len(self.giocatori))
        for d in self.rng.sample(NPC, min(quanti, len(NPC))):
            self.npc["npc:" + d["id"]] = {"nome": d["nome"], "bravura": d["bravura"], "punti": 0, "combo": 0, "max_combo": 0, "giudizi": {}}
        for g in self.giocatori.values():
            g.dati = {"combo": 0, "max_combo": 0, "fatti": set(), "giudizi": {"perfetto": 0, "bene": 0, "ok": 0, "mancato": 0}, "seq_ok": {}}
        for n in self.npc.values():
            n["giudizi"] = {"perfetto": 0, "bene": 0, "ok": 0, "mancato": 0}
        self.prossimo = 0          # (la prossima mossa da controllare per i mancati e per gli NPC)
        self.mosse = [(s["ripeti"] + tm, m, i, j) for i, s in enumerate(self.sequenze) for j, (tm, m) in enumerate(s["mosse"])]
        self.mosse.sort()
        self.tutti(dict(self.dati_via(None), a="via"))
        self.g.log("via ballo", self.id, self.diff, len(self.giocatori), "giocatori", len(self.npc), "npc", len(self.mosse), "mosse")

    def dati_via(self, gioc):
        return {"diff": self.diff, "bpm": self.bpm, "sequenze": self.sequenze, "durata": round(self.durata, 2), "prova": self.prova,
                "inizio_brano": round(self.t0 - self.inizio, 3),
                "ballerini": [[g.uid, g.nome, False] for g in self.giocatori.values()] + [[u, n["nome"], True] for u, n in self.npc.items()]}

    def brano(self, t=None):
        return (t or time.time()) - self.t0

    # -- una mossa da un giocatore
    def azione(self, gioc, msg, t):
        if msg.get("a") != "mossa":
            return
        m = str(msg.get("m", ""))
        if m not in MOSSE:
            return
        ora = self.brano(t)
        try:
            tc = float(msg.get("tb", ora))
        except (TypeError, ValueError):
            tc = ora
        # (l'istante che dice il gioco vale se e' vicino a quello del server: l'orologio allineato e la rete, non di piu')
        if abs(tc - ora) > 0.6:
            tc = ora
        if t - gioc.ultimo < 0.06:
            return
        gioc.ultimo = t
        # la mossa della coreografia piu' vicina, non ancora fatta
        best = None
        for (tm, mm, i, j) in self.mosse:
            if (i, j) in gioc.dati["fatti"]:
                continue
            d = abs(tc - tm)
            if d <= FINESTRE[-1][0] and (best is None or d < best[0]):
                best = (d, mm, i, j)
            if tm > tc + 1.0:
                break
        if best is None:
            # (una mossa fuori tempo, quando non c'e' niente da fare: rompe la combo)
            if gioc.dati["combo"] > 0:
                self._giudica(gioc.uid, gioc.nome, None, m, "fuori", 0, gioc.dati)
            return
        d, mm, i, j = best
        gioc.dati["fatti"].add((i, j))
        if mm != m:
            return self._giudica(gioc.uid, gioc.nome, (i, j), m, "sbagliata", 0, gioc.dati)
        for lim, nome, pt in FINESTRE:
            if d <= lim:
                return self._giudica(gioc.uid, gioc.nome, (i, j), m, nome, pt, gioc.dati, speciale=(m == "speciale"))

    def _giudica(self, uid, nome, ij, m, giudizio, base, dati, speciale=False):
        """Punti, combo e la mossa a tutti (per farla ballare a quel ballerino)."""
        g = self.giocatori.get(uid)
        if base > 0:
            dati["combo"] += 1
            dati["max_combo"] = max(dati["max_combo"], dati["combo"])
            molt = 1.0 + min(dati["combo"], 30) / 15.0
            pt = int(round(base * molt * (2 if speciale else 1)))
            dati["giudizi"][giudizio] = dati["giudizi"].get(giudizio, 0) + 1
        else:
            dati["combo"] = 0
            pt = 0
            if giudizio in ("sbagliata", "mancato"):
                dati["giudizi"]["mancato"] = dati["giudizi"].get("mancato", 0) + 1
        if g:
            g.punti += pt
        else:
            self.npc[uid]["punti"] += pt
        if ij is not None:
            dati.setdefault("seq", {}).setdefault(ij[0], []).append(base >= 70)
        self.tutti({"a": "colpo", "chi": uid, "m": m, "giudizio": giudizio, "punti": pt, "combo": dati["combo"],
                    "tot": g.punti if g else self.npc[uid]["punti"], "seq": ij[0] if ij else -1})

    # -- il tempo: i mancati, gli NPC, le sequenze finite
    def gioca(self, t):
        ora = self.brano(t)
        while self.prossimo < len(self.mosse) and self.mosse[self.prossimo][0] < ora - FINESTRE[-1][0] - 0.05:
            tm, mm, i, j = self.mosse[self.prossimo]
            self.prossimo += 1
            for g in self.giocatori.values():
                if (i, j) not in g.dati["fatti"]:
                    g.dati["fatti"].add((i, j))
                    if g.via is None:
                        self._giudica(g.uid, g.nome, (i, j), mm, "mancato", 0, g.dati)
            # la sequenza e' finita? chi l'ha fatta tutta bene ha il bonus
            seq = self.sequenze[i]
            if j == len(seq["mosse"]) - 1:
                self._fine_sequenza(i)
        # gli NPC: ballano la loro mossa all'istante giusto, bene o male secondo la loro bravura
        for (tm, mm, i, j) in self.mosse:
            if tm > ora:
                break
            for u, n in self.npc.items():
                k = (i, j)
                if k in n.setdefault("fatti", set()):
                    continue
                n["fatti"].add(k)
                r = self.rng.random()
                b = n["bravura"]
                if r < b * 0.55:
                    gz, pt = "perfetto", 100
                elif r < b * 0.9:
                    gz, pt = "bene", 70
                elif r < 0.25 + b * 0.75:
                    gz, pt = "ok", 40
                else:
                    gz, pt = "mancato", 0
                self._giudica(u, n["nome"], k, mm if gz != "mancato" else mm, gz, pt, n, speciale=(mm == "speciale" and pt > 0))

    def _fine_sequenza(self, i):
        chi = []
        for g in self.giocatori.values():
            s = g.dati.get("seq", {}).get(i, [])
            if s and len(s) == len(self.sequenze[i]["mosse"]) and all(s):
                g.punti += 150
                chi.append(g.uid)
        for u, n in self.npc.items():
            s = n.get("seq", {}).get(i, [])
            if s and len(s) == len(self.sequenze[i]["mosse"]) and all(s):
                n["punti"] += 150
                chi.append(u)
        if chi:
            self.tutti({"a": "sequenza", "i": i, "perfetti": chi})

    def lista(self):
        righe = [[g.nome, g.punti, g.uid, 0 if g.via is None else 1] for g in self.giocatori.values()]
        righe += [[n["nome"], n["punti"], u, 0] for u, n in getattr(self, "npc", {}).items()]
        return sorted(righe, key=lambda r: -r[1])

    def termina(self):
        """La classifica di tutti (anche gli NPC), i premi ai giocatori secondo il posto."""
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
            if gioc.punti > 0:
                mio = dict(premi.get("tutti", {}))
                mio.update(premi.get(str(p), {}))
            self.manda(gioc, {"a": "fine", "classifica": cl, "posto": p, "premi": mio,
                              "extra": {"diff": self.diff, "max_combo": gioc.dati.get("max_combo", 0), "giudizi": gioc.dati.get("giudizi", {})}})
        self.g.log("fine", self.gioco, self.id, cl)
