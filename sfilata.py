"""Minigioco 4, La sfilata di Mochi (Piazza Walther), lato server. Importato da giochi.py (TIPI["sfilata"]).

Il server decide tutto: il tema (a caso tra quelli della configurazione), i tempi di ogni fase, l'ordine della
passerella, chi vota chi, la classifica e i premi. Il gioco mostra e chiede; non manda mai punteggi.

Fasi: "vestire" (ognuno sceglie l'outfit SOLO con i capi che possiede: il gioco apre il suo armadio; il server
riceve il look scelto e lo rimanda a tutti, nessun capo viene dato da qui) -> "sfilata" (uno alla volta, nell'ordine
deciso qui, a tempi fissi uguali per tutti) -> "voto" (ognuno da' da 1 a 5 stelle a ciascuno degli ALTRI: niente
voto a se stessi, un voto solo per persona; conta la media delle stelle ricevute, cosi' chi ha piu' avversari non e'
avvantaggiato e i capi non contano, solo i voti) -> "premi" (il podio) -> fine (classifica e premi a ciascuno).

Messaggi dal gioco: {"a": "look", "look": {...}}  {"a": "pronto"}  {"a": "voto", "per": uid, "stelle": 1..5}
Messaggi al gioco:  "via" (tema, fase, tempi), "fase" ({"fase", "resta", ...i dati della fase}), "pronti"
({"pronti": n, "di": m}), "votato" ({"per", "ok", "motivo"}), poi "fine".
"""
import json
import os
import time

from giochi import CFG, Partita

TEMI = [
    ["primavera", "Primavera in città", "Colori freschi, fiori, giacche leggere: Bolzano si risveglia."],
    ["montagna", "Passeggiata in montagna", "Scarponi, zaino e cappello: si sale al Renon!"],
    ["festa", "Festa in piazza", "Allegro e colorato: stasera si balla in Piazza Walther."],
    ["elegante", "Outfit elegante", "Il meglio del guardaroba: una serata al Teatro Comunale."],
    ["pioggia", "Giornata di pioggia", "Impermeabili e cappelli: sotto i Portici piove!"],
    ["picnic", "Picnic", "Comodo e solare, per un pranzo sul prato del Talvera."],
    ["inverno", "Inverno cozy", "Strati morbidi, sciarpe e berretti: aria di mercatino di Natale."],
    ["pastello", "Colori pastello", "Rosa, lilla, menta, celeste: tutto delicato come un confetto."],
]
POSE = ["wave", "happy", "show", "dance", "bow", "surprised"]


def _veloce():
    return bool(os.environ.get("GIOCHI_VELOCE"))


class Sfilata(Partita):
    gioco = "sfilata"

    def cfgv(self, k, d):
        v = self.cfg.get(k, d)
        if _veloce():
            v = {"vestire": 14, "per": 7.0, "voto": 14, "premi": 8, "intro": 2.0}.get(k, v)
        return v

    def avvia(self):
        temi = self.cfg.get("temi") or TEMI
        self.tema = self.rng.choice(temi)
        self.fase = "vestire"
        self.fase_t = self.inizio + float(self.cfgv("vestire", 100))
        self.ordine = []
        self.voti = {}            # votante -> {votato: stelle}
        self.classifica = []
        for gioc in self.giocatori.values():
            gioc.dati = {"look": self._look_hi(gioc), "pronto": False, "scelto": False}
        self.tutti(dict(self.dati_via(None), a="via"))
        self.g.log("via sfilata", self.id, self.tema[0], len(self.giocatori), "giocatori")

    def _look_hi(self, gioc):
        """Il look che il giocatore aveva entrando (dal suo "hi"): se non ne sceglie un altro, sfila con quello."""
        try:
            hi = json.loads(gioc.me.get("hi") or "{}")
            lk = hi.get("look")
            return lk if isinstance(lk, dict) else {}
        except ValueError:
            return {}

    def resta(self):
        return max(0.0, round(self.fase_t - time.time(), 1))

    def _ordine_dati(self):
        return [[u, self.giocatori[u].nome, self.giocatori[u].dati.get("look", {}), p] for u, p in self.ordine]

    def dati_via(self, gioc):
        d = {"tema": self.tema, "fase": self.fase, "resta": self.resta(), "vestire": self.cfgv("vestire", 100), "prova": self.prova,
             "giocatori": [[x.uid, x.nome] for x in self.giocatori.values()]}
        if self.fase in ("sfilata", "voto", "premi"):
            d["ordine"] = self._ordine_dati()
            d["per"] = self.cfgv("per", 9.0)
            d["intro"] = self.cfgv("intro", 3.0)
            d["iniziata"] = round(time.time() - self.sfilata_t, 2) if self.fase == "sfilata" else None
        if self.fase == "voto" and gioc is not None:
            d["miei"] = self.voti.get(gioc.uid, {})
        if self.fase == "premi":
            d["classifica"] = self.classifica
        return d

    def _passa(self, fase, dur):
        self.fase = fase
        self.fase_t = time.time() + dur
        for gioc in self.giocatori.values():
            self.manda(gioc, dict(self.dati_via(gioc), a="fase"))

    def gioca(self, t):
        if self.fase == "vestire":
            pres = self.presenti()
            tutti_pronti = pres and all(x.dati.get("pronto") for x in pres)
            if t >= self.fase_t or (tutti_pronti and t - self.inizio > 3.0):
                uids = [x.uid for x in self.giocatori.values() if not x.dati.get("perso")]
                self.rng.shuffle(uids)
                self.ordine = [[u, self.rng.choice(POSE)] for u in uids]
                self.sfilata_t = t
                dur = float(self.cfgv("intro", 3.0)) + len(self.ordine) * float(self.cfgv("per", 9.0)) + 1.5
                self._passa("sfilata", dur)
                self.g.log("sfilata passerella", self.id, [self.giocatori[u].nome for u, _ in self.ordine])
        elif self.fase == "sfilata":
            if t >= self.fase_t:
                if self.prova or len(self.ordine) < 2:
                    self._classifica()
                    self._passa("premi", float(self.cfgv("premi", 9)))
                else:
                    self._passa("voto", float(self.cfgv("voto", 30)))
        elif self.fase == "voto":
            pres = [x for x in self.presenti() if any(u == x.uid for u, _ in self.ordine)]
            finiti = pres and all(len(self.voti.get(x.uid, {})) >= len(self.ordine) - 1 for x in pres)
            if t >= self.fase_t or finiti:
                self._classifica()
                self._passa("premi", float(self.cfgv("premi", 9)))
        elif self.fase == "premi":
            if t >= self.fase_t:
                self.termina()

    def azione(self, gioc, msg, t):
        a = msg.get("a")
        if a == "look" and self.fase == "vestire":
            lk = msg.get("look")
            if isinstance(lk, dict) and len(json.dumps(lk)) < 3500:
                gioc.dati["look"] = lk
                gioc.dati["scelto"] = True
        elif a == "pronto" and self.fase == "vestire":
            gioc.dati["pronto"] = True
            pr = sum(1 for x in self.presenti() if x.dati.get("pronto"))
            self.tutti({"a": "pronti", "pronti": pr, "di": len(self.presenti())})
        elif a == "voto" and self.fase == "voto":
            per = str(msg.get("per", ""))
            try:
                stelle = int(msg.get("stelle", 0))
            except (TypeError, ValueError):
                return
            no = lambda why: self.manda(gioc, {"a": "votato", "per": per, "ok": False, "motivo": why})
            if per == gioc.uid:
                return no("tuo")              # niente voto a se stessi
            if not any(u == per for u, _ in self.ordine):
                return no("chi")
            if not any(u == gioc.uid for u, _ in self.ordine):
                return no("non_sfilato")      # vota solo chi ha sfilato
            if not 1 <= stelle <= 5:
                return no("stelle")
            mine = self.voti.setdefault(gioc.uid, {})
            if per in mine:
                return no("gia")              # un voto solo per persona
            mine[per] = stelle
            self.manda(gioc, {"a": "votato", "per": per, "ok": True, "stelle": stelle})
            n = sum(1 for v in self.voti.values() if len(v) >= len(self.ordine) - 1)
            self.tutti({"a": "pronti", "pronti": n, "di": len(self.ordine), "fase": "voto"})

    def _classifica(self):
        """La media delle stelle ricevute (0 se nessuno ha votato); a parita' conta chi ha avuto piu' 5 stelle."""
        out = []
        for u, _ in self.ordine:
            rice = [v[u] for v in self.voti.values() if u in v]
            media = sum(rice) / len(rice) if rice else 0.0
            cinque = sum(1 for s in rice if s == 5)
            g = self.giocatori[u]
            g.punti = int(round(media * 20)) * 10 + cinque      # (la media in decimi, poi i 5 stelle come spareggio)
            g.dati["media"] = round(media, 2)
            g.dati["voti"] = len(rice)
            out.append([u, g.nome, round(media, 2), len(rice), cinque, g.dati.get("look", {})])
        out.sort(key=lambda r: (-r[2], -r[4], r[1]))
        self.classifica = out

    def lista(self):
        return [[x.nome, x.punti, x.uid, 0 if x.via is None else 1] for x in sorted(self.giocatori.values(), key=lambda x: -x.punti)]

    def extra_fine(self, gioc):
        return {"tema": self.tema, "media": gioc.dati.get("media", 0.0), "voti": gioc.dati.get("voti", 0),
                "podio": [[r[0], r[1], r[2]] for r in self.classifica[:3]]}
