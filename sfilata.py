"""Minigioco 4, La sfilata di Mochi (Piazza Walther), lato server. Importato da giochi.py (TIPI["sfilata"]).

Il server decide tutto: il tema (a caso, mai lo stesso due volte di fila), i tempi di ogni fase, chi sfila (i giocatori
e, se sono meno di cinque, i concorrenti NPC che si vestono qui secondo il tema e il loro carattere), l'ordine della
passerella, il giudizio dei tre giudici, i voti dei giocatori, la classifica e i premi. Il gioco mostra e chiede; non
manda mai punteggi.

Fasi: "tema" (la scheda del tema per tutti) -> "vestire" (ognuno sceglie l'outfit nel suo guardaroba: il server riceve
il look e i capi, controlla che i capi esistano e lo rimanda a tutti; non da' nessun capo) -> "sfilata" (la giuria si
presenta, poi uno alla volta, a tempi fissi uguali per tutti; le reazioni dei giudici e del pubblico vengono da qui)
-> "voto" (con altri giocatori: ognuno da' 1..5 a tema, creativita' e abbinamento di ciascuno degli ALTRI concorrenti,
una volta sola, senza vedere i risultati) oppure "giudizio" (da soli: i giudici si consultano) -> "premi" (il podio,
svelato dal terzo al primo) -> fine.

Il punteggio: meta' la media dei tre giudici, meta' la media dei voti dei giocatori (da soli: solo i giudici). I giudici
guardano i capi indossati (sfilata_capi.json: genere, colori), mai il prezzo, la rarita' o da dove vengono.

Messaggi dal gioco: {"a": "look", "look": {...}, "aspetto": {...}}  {"a": "pronto"}
                    {"a": "voto", "per": uid, "tema": 1..5, "crea": 1..5, "abb": 1..5}
Messaggi al gioco:  "via", "fase" ({"fase", "resta", ...i dati della fase}), "pronti" ({"pronti", "di"}), "votato"
                    ({"per", "ok", "motivo"}), poi "fine".
"""
import colorsys
import hashlib
import json
import os
import time

from giochi import Partita

HERE = os.path.dirname(os.path.abspath(__file__))


def _capi_cat():
    try:
        with open(os.path.join(HERE, "sfilata_capi.json"), encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CAPI = _capi_cat()
SLOT = ("maglia", "sopra", "sotto", "intero", "calze", "scarpe", "cappello", "occhiali", "cerchietto", "fermaglio", "zaino")
TESTA = ("cappello", "occhiali", "cerchietto", "fermaglio", "zaino")

# ------------------------------------------------------------------------------------------ i temi
# id, nome, frase, consigli (3 parole chiave per chi si veste), quanto piace ogni genere di capo (-3..3), i colori
# che vuole ("pastello", "vivaci", "caldi", "freddi", "scuri", "neutri"), i capi che lo dicono subito (id -> bonus)
TEMI = [
    {"id": "primavera", "nome": "Primavera in città", "frase": "Colori freschi, fiori, giacche leggere: Bolzano si risveglia.",
     "consigli": ["fiori e colori freschi", "giacche leggere o cardigan", "niente piumini"],
     "tag": {"fior": 3, "rom": 2, "est": 1, "cas": 1, "pic": 1, "inv": -2, "mont": -1},
     "colori": {"pastello": 0.6, "vivaci": 0.4}, "capi": {"cardigan": 0.06, "fermaglio_fiori": 0.06, "cerchietto_floreale": 0.06, "gonna_fiori": 0.06}},
    {"id": "montagna", "nome": "Passeggiata in montagna", "frase": "Scarponi, zaino e cappello: si sale al Renon!",
     "consigli": ["scarponcini e calzettoni", "zaino e cappello", "camicia a quadri o felpa"],
     "tag": {"mont": 3, "sp": 2, "inv": 1, "cas": 1, "el": -2, "est": -1, "buf": -1, "rom": -1},
     "colori": {"caldi": 0.5, "neutri": 0.5}, "capi": {"anfibi": 0.1, "zaino": 0.08, "esploratore": 0.06, "calzini_alti": 0.05}},
    {"id": "festa", "nome": "Festa in piazza", "frase": "Allegro e colorato: stasera si balla in Piazza Walther.",
     "consigli": ["colori vivaci", "un accessorio divertente", "vestiti che fanno festa"],
     "tag": {"fest": 3, "buf": 1, "rom": 1, "pat": 1, "mont": -2, "piog": -2, "sp": -1},
     "colori": {"vivaci": 0.8, "caldi": 0.2}, "capi": {"decorato": 0.05, "cerchietto_stelline": 0.05, "vestito_viola": 0.05}},
    {"id": "elegante", "nome": "Outfit elegante", "frase": "Il meglio del guardaroba: una serata al Teatro Comunale.",
     "consigli": ["blazer, camicia o abito", "scarpe eleganti", "pochi colori, ben scelti"],
     "tag": {"el": 3, "cl": 2, "rom": 1, "sp": -2, "mont": -3, "buf": -2, "cas": -1, "est": -1},
     "colori": {"scuri": 0.5, "neutri": 0.5}, "capi": {"blazer": 0.06, "scarpe_eleganti": 0.06, "aperte_eleganti": 0.05, "camicia": 0.04, "vestito_viola": 0.06}},
    {"id": "pioggia", "nome": "Giornata di pioggia", "frase": "Impermeabili e cappelli: sotto i Portici piove!",
     "consigli": ["stivali da pioggia", "cappotto o montgomery", "un cappello per l'acqua"],
     "tag": {"piog": 3, "inv": 1, "cl": 1, "est": -3, "fior": -1},
     "colori": {"vivaci": 0.4, "freddi": 0.6}, "capi": {"stivali_pioggia": 0.14, "pescatore": 0.07, "montgomery": 0.05, "cappotto": 0.04}},
    {"id": "picnic", "nome": "Picnic", "frase": "Comodo e solare, per un pranzo sul prato del Talvera.",
     "consigli": ["quadretti e fiori", "cappello di paglia", "scarpe comode o sandali"],
     "tag": {"pic": 3, "est": 2, "fior": 1, "rom": 1, "cas": 1, "inv": -2, "el": -1, "piog": -1},
     "colori": {"caldi": 0.5, "pastello": 0.5}, "capi": {"paglia": 0.08, "vestito_quadretti": 0.07, "vestito_ciliegie": 0.06, "salopette": 0.05}},
    {"id": "inverno", "nome": "Inverno cozy", "frase": "Strati morbidi, sciarpe e berretti: aria di mercatino di Natale.",
     "consigli": ["maglioni e cappotti", "berretto di lana", "stivali caldi"],
     "tag": {"inv": 3, "cl": 1, "rom": 1, "est": -3, "sp": -1},
     "colori": {"caldi": 0.6, "neutri": 0.4}, "capi": {"pompon": 0.06, "berretto": 0.05, "invernale": 0.06, "maglione": 0.05, "dolcevita": 0.04, "stivali": 0.05}},
    {"id": "pastello", "nome": "Colori pastello", "frase": "Rosa, lilla, menta, celeste: tutto delicato come un confetto.",
     "consigli": ["solo toni chiari e delicati", "rosa, menta, celeste, lilla", "niente colori scuri"],
     "tag": {"rom": 2, "fior": 1, "est": 1, "mont": -2, "sp": -1},
     "colori": {"pastello": 1.0}, "capi": {}},
]


def tema_di(tid):
    for t in TEMI:
        if t["id"] == tid:
            return t
    return TEMI[0]


# ------------------------------------------------------------------------------------------ la giuria
# Tre abitanti famosi per il loro gusto (nel gioco sono anche in citta': scripts/core/db.gd). Ognuno guarda le stesse
# cose con occhi suoi: quanto pesa ogni misura del look in ciascuno dei tre voti.
GIUDICI = [
    {"id": "velluta", "nome": "Madama Velluta", "chi": "stilista dell'atelier sotto i Portici",
     "tema": {"T": 0.75, "E": 0.25}, "crea": {"C": 0.55, "E": 0.45}, "abb": {"A": 0.6, "D": 0.4}},
    {"id": "zigo", "nome": "Zigo Zago", "chi": "designer, il re dei colori",
     "tema": {"T": 0.85, "C": 0.15}, "crea": {"C": 0.85, "B": 0.15}, "abb": {"A": 0.75, "B": 0.25}},
    {"id": "pervinca", "nome": "Pervinca", "chi": "consulente d'armonia",
     "tema": {"T": 0.7, "A": 0.3}, "crea": {"C": 0.6, "A": 0.4}, "abb": {"A": 0.75, "D": 0.25}},
]

# ------------------------------------------------------------------------------------------ i concorrenti NPC
# nome, chi e', stile (cosa gli piace mettersi), bravura (0..1: quanto segue il tema e cura i colori)
NPC = [
    {"id": "fiocco", "nome": "Fiocco", "chi": "la sarta di Gries", "stile": "elegante", "bravura": 0.85, "g": "f"},
    {"id": "bottone", "nome": "Bottone", "chi": "il barista di Via Museo", "stile": "classico", "bravura": 0.7, "g": "m"},
    {"id": "pralina", "nome": "Pralina", "chi": "studentessa d'arte", "stile": "creativo", "bravura": 0.75, "g": "f"},
    {"id": "trottola", "nome": "Trottola", "chi": "il postino distratto", "stile": "buffo", "bravura": 0.3, "g": "m"},
    {"id": "brezza", "nome": "Brezza", "chi": "la fioraia di Piazza Erbe", "stile": "romantico", "bravura": 0.65, "g": "f"},
    {"id": "pepe", "nome": "Pepe", "chi": "maestro di sci", "stile": "sportivo", "bravura": 0.55, "g": "m"},
    {"id": "nuvola", "nome": "Nuvola", "chi": "fotografa di matrimoni", "stile": "creativo", "bravura": 0.6, "g": "f"},
    {"id": "gomitolo", "nome": "Gomitolo", "chi": "violinista del Conservatorio", "stile": "elegante", "bravura": 0.6, "g": "m"},
    {"id": "ciliegina", "nome": "Ciliegina", "chi": "la pasticcera dei Portici", "stile": "buffo", "bravura": 0.5, "g": "f"},
    {"id": "ghianda", "nome": "Ghianda", "chi": "il ciclista della domenica", "stile": "sportivo", "bravura": 0.4, "g": "m"},
    {"id": "mirtillo", "nome": "Mirtillo", "chi": "la libraia di Via Goethe", "stile": "romantico", "bravura": 0.8, "g": "f"},
    {"id": "zucchero", "nome": "Zucchero", "chi": "il falegname del Renon", "stile": "classico", "bravura": 0.45, "g": "m"},
]
# quanto piace a ogni stile ogni genere di capo
STILI = {
    "elegante": {"el": 2.0, "cl": 1.2, "rom": 0.4, "sp": -1.5, "buf": -1.5},
    "classico": {"cl": 1.6, "el": 0.6, "cas": 0.6, "buf": -1.0},
    "creativo": {"buf": 1.4, "pat": 1.2, "fest": 1.0, "acc": 1.2, "cl": -0.5},
    "buffo": {"buf": 2.0, "pat": 1.0, "acc": 1.0, "el": -0.5},
    "romantico": {"rom": 1.8, "fior": 1.4, "el": 0.4, "sp": -1.0},
    "sportivo": {"sp": 2.0, "mont": 1.0, "cas": 0.8, "el": -1.0, "rom": -0.8},
}
CAPELLI = ["r_corti", "r_ricci", "r_laterale", "r_caschetto", "r_coda", "r_trecce", "r_chignon", "mochi"]
CAPELLI_M = ["r_corti", "r_ricci", "r_laterale", "mochi"]
PELLI = ["#fcc49a", "#f3ccae", "#f0c9a8", "#dfae8a", "#b98161", "#8a5a42", "#5f3d2e"]
COL_CAPELLI = ["#593829", "#3a2a24", "#2f2730", "#8b5a3c", "#a9764c", "#c8623a", "#e0a84e", "#f0cf7a", "#d7d2cc", "#eaa2b8", "#a68ac9", "#7f9fd6"]
OCCHI = ["bottone", "tondi", "fagiolo", "puntino", "stellina", "goccia"]
SOPRACCIGLIA = ["sottili", "folte", "tonde", "alte", "morbide"]
NASI = ["bottone", "patata", "puntino", "insu", "disegnato"]

POSE = ["fianchi", "giro", "saluto", "mostra", "inchino", "braccia"]


def _veloce():
    return bool(os.environ.get("GIOCHI_VELOCE"))


def _h(*parti):
    """Un numero 0..1 sempre uguale per le stesse cose (le piccole differenze di gusto dei giudici: non a caso)."""
    d = hashlib.sha1("|".join(str(p) for p in parti).encode()).digest()
    return int.from_bytes(d[:4], "big") / 4294967295.0


# ------------------------------------------------------------------------------------------ cosa vede un giudice
def _hsv(hexs):
    try:
        s = str(hexs).lstrip("#")
        r, g, b = int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0
        return colorsys.rgb_to_hsv(r, g, b)
    except (ValueError, IndexError):
        return (0.0, 0.0, 0.8)


def _colori_capo(capo):
    e = CAPI.get(capo["id"], {})
    col = capo.get("col")
    if isinstance(col, list) and col:
        return [str(c) for c in col]
    vs = e.get("colori", [])
    if not vs:
        return []
    return [str(c) for c in vs[max(0, min(len(vs) - 1, int(capo.get("var", 0))))]]


def pulisci_aspetto(a):
    """Solo i capi che esistono nel guardaroba, nel loro posto, con colori validi. Il resto (viso, capelli) passa com'e'
    ma piccolo."""
    if not isinstance(a, dict):
        return {}
    out = {}
    for k in ("capelli", "col_capelli", "pelle", "iride", "occhi", "sopracciglia", "naso", "bocca", "ciglia"):
        v = a.get(k)
        if isinstance(v, (str, bool)) and len(str(v)) < 40:
            out[k] = v
    capi = {}
    src = a.get("capi")
    if isinstance(src, dict):
        for s, c in src.items():
            if s not in SLOT or not isinstance(c, dict):
                continue
            cid = str(c.get("id", ""))
            e = CAPI.get(cid)
            if e is None or e.get("slot") != s:
                continue
            capo = {"id": cid}
            try:
                capo["var"] = max(0, min(int(c.get("var", 0)), max(0, len(e.get("colori", [])) - 1)))
            except (TypeError, ValueError):
                capo["var"] = 0
            col = c.get("col")
            if isinstance(col, list):
                ok = [str(x) for x in col[:3] if isinstance(x, str) and len(x) in (7, 9) and x.startswith("#")]
                if ok:
                    capo["col"] = ok
            capi[s] = capo
    out["capi"] = capi
    return out


def misura(aspetto, tema, altri=()):
    """Le misure di un look, 0..1: T tema, C creativita', A armonia dei colori, E eleganza, D completezza e cura,
    B coraggio dei colori; piu' le note per i commenti (il capo migliore e il peggiore per il tema, i difetti)."""
    capi = (aspetto or {}).get("capi", {}) or {}
    note = {"meglio": None, "peggio": None, "difetti": [], "pregi": []}
    pesi = {"maglia": 0.6 if "sopra" in capi else 1.0, "sopra": 1.0, "sotto": 1.0, "intero": 1.6, "scarpe": 0.8,
            "cappello": 0.8, "cerchietto": 0.6, "occhiali": 0.4, "fermaglio": 0.4, "zaino": 0.5, "calze": 0.4}
    # il tema: quanto piace ogni capo
    fit_sum = 0.0
    fit_w = 0.0
    best = (-99, None)
    worst = (99, None)
    eleg = 0.0
    estro = 0.0
    tags_tutti = []
    for s, c in capi.items():
        e = CAPI.get(c["id"], {})
        tags = e.get("tag", [])
        tags_tutti += tags
        w = pesi.get(s, 0.5)
        f = sum(tema["tag"].get(t, 0) for t in tags)
        f = max(-3.0, min(3.0, f + tema.get("capi", {}).get(c["id"], 0) * 20))
        fit_sum += f * w
        fit_w += w
        if f > best[0]:
            best = (f, e.get("nome", c["id"]))
        if f < worst[0]:
            worst = (f, e.get("nome", c["id"]))
        eleg += w * (1.0 * ("el" in tags) + 0.5 * ("cl" in tags) - 0.5 * ("sp" in tags) - 0.6 * ("buf" in tags))
        estro += 0.9 * ("buf" in tags) + 0.6 * ("pat" in tags) + 0.7 * ("acc" in tags) + 0.4 * ("fest" in tags)
    note["meglio"] = best[1] if best[0] >= 2 else None
    note["peggio"] = worst[1] if worst[0] <= -2 else None
    t_capi = 0.45 + (fit_sum / fit_w) / 5.0 if fit_w else 0.3
    bonus = sum(b for cid, b in tema.get("capi", {}).items() if any(c["id"] == cid for c in capi.values()))
    # i colori
    cols = []
    for s, c in capi.items():
        w = pesi.get(s, 0.5)
        for k, hx in enumerate(_colori_capo(c)[:2]):
            cols.append(_hsv(hx) + (w * (1.0 if k == 0 else 0.35),))
    tot = sum(x[3] for x in cols) or 1.0
    past = sum(x[3] for x in cols if x[2] >= 0.78 and 0.08 <= x[1] <= 0.5) / tot
    viv = sum(x[3] for x in cols if x[1] >= 0.55 and x[2] >= 0.55) / tot
    scu = sum(x[3] for x in cols if x[2] < 0.4) / tot
    neu = sum(x[3] for x in cols if x[1] < 0.22) / tot
    caldi = sum(x[3] for x in cols if x[1] >= 0.22 and (x[0] < 0.17 or x[0] > 0.92)) / tot
    freddi = sum(x[3] for x in cols if x[1] >= 0.22 and 0.42 < x[0] < 0.8) / tot
    misure_col = {"pastello": past, "vivaci": viv, "scuri": scu, "neutri": neu, "caldi": caldi, "freddi": freddi}
    col_fit = sum(p * min(1.0, misure_col.get(k, 0) * 1.6) for k, p in tema.get("colori", {}).items())
    if tema["id"] == "pastello":
        col_fit = max(0.0, past * 1.3 - scu * 1.2 - viv * 0.6)
    T = max(0.0, min(1.0, 0.68 * t_capi + 0.32 * col_fit + bonus))
    # armonia: quante famiglie di colori forti, e come stanno insieme
    forti = sorted([x for x in cols if x[1] > 0.3 and x[2] > 0.3 and x[3] >= 0.3], key=lambda x: -x[3])
    gruppi = []
    for x in forti:
        for g in gruppi:
            if min(abs(x[0] - g[0]), 1 - abs(x[0] - g[0])) < 0.09:
                g[1] += x[3]
                break
        else:
            gruppi.append([x[0], x[3], x[1]])
    ng = len(gruppi)
    if ng == 0:
        A = 0.6
    elif ng == 1:
        A = 0.86
    elif ng == 2:
        d = min(abs(gruppi[0][0] - gruppi[1][0]), 1 - abs(gruppi[0][0] - gruppi[1][0]))
        A = 0.9 if d < 0.17 else (0.8 if d > 0.42 else 0.72)
    elif ng == 3:
        A = 0.62
    else:
        A = 0.42
    scontri = 0
    for i in range(len(gruppi)):
        for j in range(i + 1, len(gruppi)):
            d = min(abs(gruppi[i][0] - gruppi[j][0]), 1 - abs(gruppi[i][0] - gruppi[j][0]))
            if 0.22 < d < 0.4 and gruppi[i][2] > 0.6 and gruppi[j][2] > 0.6:
                scontri += 1
    A -= 0.09 * scontri
    if scontri:
        note["difetti"].append("scontro")
    if ng == 0:
        note["difetti"].append("spento")
    if tags_tutti.count("pat") >= 3:
        A -= 0.12
        note["difetti"].append("fantasie")
    # completezza e cura
    D = 0.0
    if any(s in capi for s in ("maglia", "sopra", "intero")):
        D += 0.35
    if "sotto" in capi or "intero" in capi:
        D += 0.3
    if "scarpe" in capi:
        D += 0.2
    acc = sum(1 for s in TESTA if s in capi)
    if 1 <= acc <= 2:
        D += 0.15
    elif acc >= 4:
        D -= 0.12
        note["difetti"].append("troppi")
    if "cappello" in capi and "cerchietto" in capi:
        D -= 0.08
    sand = "scarpe" in capi and str(capi["scarpe"]["id"]).startswith(("sandali", "ciabatte", "aperte"))
    if sand and "calze" in capi:
        D -= 0.12
        note["difetti"].append("calze_sandali")
    scarpe_ok = False
    if "scarpe" in capi:
        sc = _colori_capo(capi["scarpe"])
        if sc:
            hs = _hsv(sc[0])
            scarpe_ok = hs[1] < 0.25 or any(min(abs(hs[0] - g[0]), 1 - abs(hs[0] - g[0])) < 0.09 for g in gruppi)
    D += 0.08 if scarpe_ok else -0.04
    D = max(0.0, min(1.0, D))
    A = max(0.0, min(1.0, A + (0.05 if scarpe_ok else 0.0)))
    E = max(0.0, min(1.0, 0.45 + eleg / max(fit_w, 1.0) * 0.55))
    B = max(0.0, min(1.0, viv * 1.2 + 0.15 * max(0, ng - 1)))
    # creativita': capi con carattere, gli accessori, i colori coraggiosi, e quanto e' diverso dagli altri in passerella
    ids = set(c["id"] for c in capi.values())
    orig = 1.0
    for o in altri:
        oi = set(c["id"] for c in ((o or {}).get("capi", {}) or {}).values())
        if ids or oi:
            orig = min(orig, 1.0 - len(ids & oi) / max(1, len(ids | oi)))
    C = 0.18 + 0.12 * min(estro, 3.0) + (0.0 if acc == 0 else (0.1 if acc <= 2 else 0.14)) + 0.28 * orig + 0.14 * B
    if T > 0.6 and any(sum(tema["tag"].get(t, 0) for t in CAPI.get(c["id"], {}).get("tag", [])) <= 0 for c in capi.values()):
        C += 0.06        # (dentro il tema con un capo che non ci si aspettava)
    if orig < 0.3:
        note["difetti"].append("copia")
    C = max(0.0, min(1.0, C))
    if T >= 0.75:
        note["pregi"].append("tema")
    if A >= 0.85:
        note["pregi"].append("colori")
    if C >= 0.7:
        note["pregi"].append("estro")
    if tema["id"] == "pastello" and past > 0.65:
        note["pregi"].append("pastello")
    return {"T": T, "C": C, "A": A, "E": E, "D": D, "B": B, "note": note}


def voto_giudice(g, m, chi, tema):
    """Il voto di un giudice (1..5 per tema, creativita', abbinamento) secondo i suoi gusti. Le piccole differenze tra
    un giudice e l'altro vengono dal suo occhio (sempre uguali per lo stesso look), non dal caso."""
    out = {}
    for k in ("tema", "crea", "abb"):
        pesi = dict(g[k])
        if k == "tema" and g["id"] == "velluta" and tema["id"] == "elegante":
            pesi = {"T": 0.5, "E": 0.5}
        x = sum(m[key] * w for key, w in pesi.items())
        if g["id"] == "zigo" and "calze_sandali" in m["note"]["difetti"] and k == "crea":
            x += 0.12        # (a lui i calzini coi sandali piacciono)
        occhio = (_h(g["id"], k, chi, tema["id"]) - 0.5) * 0.5
        x = max(0.0, min(1.0, (x - 0.28) / 0.66))        # (la scala dei giudici: un look qualunque sta nel mezzo)
        out[k] = max(1, min(5, int(round(1 + 4 * x + occhio))))
    return out


def commento(g, v, m, nome, tema):
    """Due righe del giudice, coerenti con il suo voto e con quello che ha guardato."""
    media = (v["tema"] + v["crea"] + v["abb"]) / 3.0
    n = m["note"]
    sc = lambda lst: lst[int(_h(g["id"], nome, tema["id"], len(lst)) * len(lst)) % len(lst)]
    gid = g["id"]
    if gid == "velluta":
        if n["peggio"] and v["tema"] <= 2:
            return sc(["%s con questo tema? Mia cara, no." % n["peggio"], "Il tema era «%s». %s non c'entra proprio." % (tema["nome"], n["peggio"])])
        if "calze_sandali" in n["difetti"]:
            return "Calzini con i sandali. Non lo dirò due volte."
        if media >= 4.3:
            return sc(["Impeccabile. Ogni capo al suo posto.", "Questo è stile: il tema c'è tutto, e con classe.",
                       "Brava, bravo: da copertina." if not n["meglio"] else "%s: scelta perfetta. Applausi." % n["meglio"]])
        if media >= 3.4:
            return sc(["Elegante, curato. Un dettaglio in più e sarebbe perfetto.", "Mi piace. Il tema è rispettato con garbo."])
        if media >= 2.6:
            return sc(["Corretto, ma senza slancio.", "Si poteva osare un po' di più, con più ordine."])
        return sc(["Manca la cura. E la moda è cura.", "Troppo disordine per i miei gusti."])
    if gid == "zigo":
        if "calze_sandali" in n["difetti"]:
            return "Calzini con i sandali! Che coraggio, lo adoro!"
        if "spento" in n["difetti"] and v["crea"] <= 3 and tema["id"] != "pastello":
            return sc(["Tutto beige? Dov'è il colore? Dov'è la vita?", "Un po' troppo prudente. Accendiamolo!"])
        if "copia" in n["difetti"]:
            return "L'ho già visto oggi in passerella... osa di più!"
        if media >= 4.3:
            return sc(["Spettacolare! Questa sì che è personalità!", "Wow! Piazza Walther non dimenticherà questo look!",
                       "Fantastico! %s è un colpo di genio." % n["meglio"] if n["meglio"] else "Fantastico! Un colpo di genio."])
        if media >= 3.4:
            return sc(["Mi piace, mi piace! C'è un'idea.", "Bello! Un pizzico di follia in più e sarei svenuto."])
        if media >= 2.6:
            return sc(["Carino, ma non mi fa saltare dalla sedia.", "Ci sei quasi. Dove sono le sorprese?"])
        return sc(["Mmh. Non ci ho capito molto, ma apprezzo l'impegno!", "Coraggio sì, idea... la cerchiamo insieme."])
    # pervinca
    if "pastello" in n["pregi"]:
        return "Toni delicati, perfettamente intonati. Un confetto!"
    if "scontro" in n["difetti"]:
        return sc(["Questi due colori litigano un po'.", "Qualche colore di troppo che si contraddice."])
    if "troppi" in n["difetti"]:
        return "Togliamo un accessorio: meno è meglio."
    if "fantasie" in n["difetti"]:
        return "Righe, quadri, pois... gli occhi non sanno dove posarsi."
    if media >= 4.3:
        return sc(["Armonia perfetta: colori, forme, accessori.", "Equilibrio da manuale. Brava, bravo."])
    if media >= 3.4:
        return sc(["Colori ben abbinati, presentazione piacevole.", "Molto armonioso. Le scarpe chiudono bene il look."])
    if media >= 2.6:
        return sc(["L'insieme regge, ma qualcosa stona.", "Un colore in meno e sarebbe stato più pulito."])
    return sc(["L'armonia qui si è persa per strada.", "Rivediamo gli abbinamenti, con calma."])


# la reazione dal vivo, mentre sfila: [animazione, frase breve]
def reazione(g, v, m):
    media = (v["tema"] + v["crea"] + v["abb"]) / 3.0
    buffo = "calze_sandali" in m["note"]["difetti"] or m["B"] > 0.8 and m["A"] < 0.5
    if buffo and g["id"] != "velluta":
        return ["ride", {"zigo": "Ahah, geniale!", "pervinca": "Oh! Che coraggio..."}.get(g["id"], "Oh!")]
    if media >= 4.3:
        return ["applaude", {"velluta": "Divino.", "zigo": "Bravissimo!", "pervinca": "Che armonia!"}[g["id"]]]
    if media >= 3.4:
        return ["annuisce", {"velluta": "Molto bene.", "zigo": "Mi piace!", "pervinca": "Bello."}[g["id"]]]
    if media >= 2.6:
        return ["appunti", {"velluta": "Mmh.", "zigo": "Interessante...", "pervinca": "Vediamo..."}[g["id"]]]
    return ["perplesso", {"velluta": "Oh no.", "zigo": "Coraggioso...", "pervinca": "Mmh, no."}[g["id"]]]


# ------------------------------------------------------------------------------------------ il guardaroba degli NPC
def _per_slot():
    out = {}
    for cid, e in CAPI.items():
        if e.get("slot") in SLOT:
            out.setdefault(e["slot"], []).append(cid)
    return out


PER_SLOT = _per_slot()


def veste_npc(npc, tema, rng):
    """Un concorrente NPC si veste per il tema, con il suo stile: piu' e' bravo, piu' segue il tema e cura i colori;
    i meno bravi pescano anche a caso. Usa gli stessi capi del guardaroba dei giocatori."""
    stile = STILI.get(npc["stile"], {})
    brav = npc["bravura"]

    def peso(cid, al_tema):
        tags = CAPI[cid].get("tag", [])
        f = sum(tema["tag"].get(t, 0) for t in tags) + tema.get("capi", {}).get(cid, 0) * 20
        s = sum(stile.get(t, 0) for t in tags)
        return 2.718 ** ((0.9 * f if al_tema else 0.0) + 0.6 * s)

    def scegli(slot):
        # (ogni capo: chi e' bravo pensa al tema, chi no prende quello che gli piace e basta)
        lst = [c for c in PER_SLOT.get(slot, []) if not c.startswith("piedi_")]
        if not lst:
            return None
        al_tema = rng.random() < 0.05 + 0.9 * brav
        ws = [peso(c, al_tema) for c in lst]
        return rng.choices(lst, weights=ws)[0]

    capi = {}
    vuole_intero = rng.random() < {"romantico": 0.55, "elegante": 0.35, "buffo": 0.3, "creativo": 0.3}.get(npc["stile"], 0.12) * (1.2 if npc["g"] == "f" else 0.45)
    if vuole_intero:
        capi["intero"] = {"id": scegli("intero")}
        if CAPI[capi["intero"]["id"]].get("sopra_maglia") and rng.random() < 0.6:
            capi["maglia"] = {"id": scegli("maglia")}
    else:
        capi["maglia"] = {"id": scegli("maglia")}
        capi["sotto"] = {"id": scegli("sotto")}
        if rng.random() < 0.35 + 0.4 * max(0, tema["tag"].get("inv", 0) + tema["tag"].get("piog", 0)) / 3.0:
            capi["sopra"] = {"id": scegli("sopra")}
    capi["scarpe"] = {"id": scegli("scarpe")}
    sand = capi["scarpe"]["id"].startswith(("sandali", "ciabatte", "aperte"))
    if rng.random() < (0.75 if not sand else (0.35 if npc["stile"] == "buffo" else 0.05)):
        capi["calze"] = {"id": scegli("calze")}
    n_acc = {"creativo": 2, "buffo": 2, "romantico": 1, "elegante": 1, "classico": 1, "sportivo": 1}.get(npc["stile"], 1)
    if rng.random() < 0.3:
        n_acc += 1 if npc["stile"] == "buffo" else -1
    testa = ["cappello", "cerchietto"] if npc["g"] == "f" or npc["stile"] in ("buffo", "creativo") else ["cappello"]
    for _ in range(max(0, n_acc)):
        s = rng.choice(testa + ["occhiali", "fermaglio" if npc["g"] == "f" else "occhiali", "zaino" if npc["stile"] == "sportivo" else "cappello"])
        if s in capi or (s == "cerchietto" and "cappello" in capi) or (s == "cappello" and "cerchietto" in capi):
            continue
        cid = scegli(s)
        if cid:
            capi[s] = {"id": cid}
    # i colori: i bravi provano le varianti e tengono quella che sta meglio con il resto
    for s in list(capi.keys()):
        c = capi[s]
        nv = len(CAPI[c["id"]].get("colori", [])) or 1
        if rng.random() < brav:
            best = None
            for v in range(nv):
                prova = dict(capi)
                prova[s] = {"id": c["id"], "var": v}
                mm = misura({"capi": prova}, tema)
                sc = mm["A"] + 0.5 * mm["T"] + rng.random() * 0.05
                if best is None or sc > best[0]:
                    best = (sc, v)
            c["var"] = best[1]
        else:
            c["var"] = rng.randrange(nv)
    cap = rng.choice(CAPELLI if npc["g"] == "f" else CAPELLI_M)
    a = {"capi": capi, "capelli": cap, "col_capelli": rng.choice(COL_CAPELLI[:9] if brav > 0.5 else COL_CAPELLI),
         "pelle": rng.choice(PELLI), "occhi": rng.choice(OCCHI), "sopracciglia": rng.choice(SOPRACCIGLIA), "naso": rng.choice(NASI),
         "ciglia": npc["g"] == "f"}
    return a


# ------------------------------------------------------------------------------------------ la partita
class Sfilata(Partita):
    gioco = "sfilata"
    ultimo_tema = {}         # stanza -> l'ultimo tema estratto (mai due volte di fila)

    def cfgv(self, k, d):
        v = self.cfg.get(k, d)
        if _veloce():
            v = {"tema": 3.0, "vestire": 14, "intro": 4.0, "per": 7.0, "voto": 14, "giudizio": 3.0, "premi": 10}.get(k, v)
        return v

    def avvia(self):
        temi = [t for t in TEMI if t["id"] != Sfilata.ultimo_tema.get(self.room)] or TEMI
        forza = os.environ.get("SFILATA_TEMA", "")
        self.tema = tema_di(forza) if forza else self.rng.choice(temi)
        Sfilata.ultimo_tema[self.room] = self.tema["id"]
        self.fase = "tema"
        self.fase_t = self.inizio + float(self.cfgv("tema", 7.0))
        self.ordine = []
        self.voti = {}            # votante -> {votato: {"tema", "crea", "abb"}}
        self.giuria = {}          # uid -> {gid: voto}
        self.misure = {}
        self.classifica = []
        self.sfilata_t = 0.0
        self.per = float(self.cfgv("per", 15.0))
        self.npc = {}             # uid -> {"nome", "chi", "look", "aspetto"}
        for gioc in self.giocatori.values():
            gioc.dati = {"look": self._look_hi(gioc), "aspetto": self._aspetto_hi(gioc), "pronto": False, "scelto": False}
        # i concorrenti NPC: fino a cinque in passerella (da soli o in pochi), mai oltre dieci
        quanti = max(0, min(int(self.cfg.get("concorrenti", 5)), int(self.cfg.get("max", 10))) - len(self.giocatori))
        for n in self.rng.sample(NPC, min(quanti, len(NPC))):
            uid = "npc:" + n["id"]
            a = veste_npc(n, self.tema, self.rng)
            self.npc[uid] = {"nome": n["nome"], "chi": n["chi"], "aspetto": a, "look": {"aspetto": a, "npc": True}}
        self.tutti(dict(self.dati_via(None), a="via"))
        self.g.log("via sfilata", self.id, self.tema["id"], len(self.giocatori), "giocatori", len(self.npc), "npc")

    def _look_hi(self, gioc):
        """Il look che il giocatore aveva entrando (dal suo "hi"): se non ne sceglie un altro, sfila con quello."""
        try:
            hi = json.loads(gioc.me.get("hi") or "{}")
            lk = hi.get("look")
            return lk if isinstance(lk, dict) else {}
        except ValueError:
            return {}

    def _aspetto_hi(self, gioc):
        lk = self._look_hi(gioc)
        return pulisci_aspetto(lk.get("aspetto")) if isinstance(lk.get("aspetto"), dict) else {"capi": {}}

    def resta(self):
        return max(0.0, round(self.fase_t - time.time(), 1))

    def nome(self, uid):
        if uid in self.npc:
            return self.npc[uid]["nome"]
        g = self.giocatori.get(uid)
        return g.nome if g else "?"

    def look_di(self, uid):
        if uid in self.npc:
            return self.npc[uid]["look"]
        g = self.giocatori.get(uid)
        return g.dati.get("look", {}) if g else {}

    def aspetto_di(self, uid):
        if uid in self.npc:
            return self.npc[uid]["aspetto"]
        g = self.giocatori.get(uid)
        return g.dati.get("aspetto", {}) if g else {}

    def _tema_dati(self):
        t = self.tema
        return [t["id"], t["nome"], t["frase"], t["consigli"]]

    def dati_via(self, gioc):
        d = {"tema": self._tema_dati(), "fase": self.fase, "resta": self.resta(), "prova": self.prova,
             "durate": {k: self.cfgv(k, dv) for k, dv in (("tema", 7.0), ("vestire", 100), ("intro", 10.0), ("per", 15.0), ("voto", 50), ("giudizio", 7.0), ("premi", 24))},
             "giocatori": [[x.uid, x.nome] for x in self.giocatori.values()],
             "npc": [[u, n["nome"], n["chi"]] for u, n in self.npc.items()],
             "giuria": [[g["id"], g["nome"], g["chi"]] for g in GIUDICI]}
        if self.fase in ("sfilata", "voto", "giudizio", "premi"):
            d["ordine"] = self.ordine
            d["per"] = self.per
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

    # -- la passerella: chi sfila, in che ordine, e come reagiscono giuria e pubblico
    def _prepara_passerella(self, t):
        uids = [x.uid for x in self.giocatori.values() if not x.dati.get("perso")] + list(self.npc.keys())
        self.rng.shuffle(uids)
        uids = uids[:int(self.cfg.get("max", 10))]
        for u in uids:
            altri = [self.aspetto_di(o) for o in uids if o != u]
            m = misura(self.aspetto_di(u), self.tema, altri)
            self.misure[u] = m
            self.giuria[u] = {g["id"]: voto_giudice(g, m, self._chiave(u), self.tema) for g in GIUDICI}
        self.ordine = []
        for u in uids:
            m = self.misure[u]
            gv = self.giuria[u]
            media = sum((v["tema"] + v["crea"] + v["abb"]) / 3.0 for v in gv.values()) / 3.0
            posa = self.rng.choice(POSE)
            reaz = {"giudici": [[g["id"]] + reazione(g, gv[g["id"]], m) for g in GIUDICI],
                    "pubblico": 3 if media >= 4.2 else (2 if media >= 3.4 else (1 if media >= 2.6 else 0)),
                    "buffo": "calze_sandali" in m["note"]["difetti"] or (m["B"] > 0.8 and m["A"] < 0.5)}
            self.ordine.append([u, self.nome(u), self.look_di(u), posa, u in self.npc, reaz])
        self.sfilata_t = t
        # (in tanti si cammina un po' piu' svelti: dieci in passerella restano due minuti)
        self.per = float(self.cfgv("per", 15.0)) * (1.0 if len(self.ordine) <= 6 else 0.8)

    def _chiave(self, u):
        """Il look come lo vede l'occhio di un giudice: lo stesso look ha lo stesso voto, chiunque lo indossi."""
        return json.dumps(self.aspetto_di(u).get("capi", {}), sort_keys=True)

    def gioca(self, t):
        if self.fase == "tema":
            if t >= self.fase_t:
                self._passa("vestire", float(self.cfgv("vestire", 100)))
        elif self.fase == "vestire":
            pres = self.presenti()
            tutti_pronti = pres and all(x.dati.get("pronto") for x in pres)
            if t >= self.fase_t or (tutti_pronti and t - self.inizio > 3.0):
                self._prepara_passerella(t)
                dur = float(self.cfgv("intro", 10.0)) + len(self.ordine) * self.per + 2.5
                self._passa("sfilata", dur)
                self.g.log("sfilata passerella", self.id, [r[1] for r in self.ordine])
        elif self.fase == "sfilata":
            if t >= self.fase_t:
                umani = [r for r in self.ordine if not r[4]]
                if len(umani) >= 2:
                    self._passa("voto", float(self.cfgv("voto", 50)))
                else:
                    self._passa("giudizio", float(self.cfgv("giudizio", 7.0)))
        elif self.fase in ("voto", "giudizio"):
            finiti = False
            if self.fase == "voto":
                pres = [x for x in self.presenti() if any(r[0] == x.uid for r in self.ordine)]
                finiti = pres and all(len(self.voti.get(x.uid, {})) >= len(self.ordine) - 1 for x in pres)
            if t >= self.fase_t or finiti:
                self._classifica()
                self._passa("premi", float(self.cfgv("premi", 24)))
        elif self.fase == "premi":
            if t >= self.fase_t:
                self.termina()

    def azione(self, gioc, msg, t):
        a = msg.get("a")
        if a == "look" and self.fase in ("tema", "vestire"):
            lk = msg.get("look")
            if isinstance(lk, dict) and len(json.dumps(lk)) < 6000:
                gioc.dati["look"] = lk
                gioc.dati["scelto"] = True
                asp = msg.get("aspetto")
                gioc.dati["aspetto"] = pulisci_aspetto(asp if isinstance(asp, dict) else lk.get("aspetto"))
        elif a == "pronto" and self.fase in ("tema", "vestire"):
            gioc.dati["pronto"] = True
            pr = sum(1 for x in self.presenti() if x.dati.get("pronto"))
            self.tutti({"a": "pronti", "pronti": pr, "di": len(self.presenti())})
        elif a == "voto" and self.fase == "voto":
            per = str(msg.get("per", ""))
            no = lambda why: self.manda(gioc, {"a": "votato", "per": per, "ok": False, "motivo": why})
            try:
                v = {k: int(msg.get(k, 0)) for k in ("tema", "crea", "abb")}
            except (TypeError, ValueError):
                return no("stelle")
            if per == gioc.uid:
                return no("tuo")              # niente voto a se stessi
            if not any(r[0] == per for r in self.ordine):
                return no("chi")
            if not any(r[0] == gioc.uid for r in self.ordine):
                return no("non_sfilato")      # vota solo chi ha sfilato
            if not all(1 <= x <= 5 for x in v.values()):
                return no("stelle")
            mine = self.voti.setdefault(gioc.uid, {})
            if per in mine:
                return no("gia")              # un voto solo per concorrente, e non si cambia
            mine[per] = v
            self.manda(gioc, {"a": "votato", "per": per, "ok": True, "voto": v})
            umani = [r[0] for r in self.ordine if not r[4] and self.giocatori.get(r[0]) and self.giocatori[r[0]].via is None]
            n = sum(1 for u in umani if len(self.voti.get(u, {})) >= len(self.ordine) - 1)
            self.tutti({"a": "pronti", "pronti": n, "di": len(umani), "fase": "voto"})

    def _classifica(self):
        """Meta' giudici, meta' giocatori (le medie dei voti validi: ognuno pesa uguale nella sua parte); da soli, i
        giudici. A parita' conta il voto della giuria, poi il tema; se resta uguale si condivide il posto."""
        righe = []
        for r in self.ordine:
            u = r[0]
            gv = self.giuria[u]
            giudici = sum((v["tema"] + v["crea"] + v["abb"]) / 3.0 for v in gv.values()) / 3.0
            rice = [v[u] for v in self.voti.values() if u in v]
            giocatori = sum((v["tema"] + v["crea"] + v["abb"]) / 3.0 for v in rice) / len(rice) if rice else None
            finale = giudici if giocatori is None else 0.5 * giudici + 0.5 * giocatori
            crit = {}
            for k in ("tema", "crea", "abb"):
                gk = sum(v[k] for v in gv.values()) / 3.0
                crit[k] = round(gk if not rice else 0.5 * gk + 0.5 * sum(v[k] for v in rice) / len(rice), 2)
            dett = [[g["id"], gv[g["id"]]["tema"], gv[g["id"]]["crea"], gv[g["id"]]["abb"], commento(g, gv[g["id"]], self.misure[u], r[1], self.tema)] for g in GIUDICI]
            righe.append({"uid": u, "nome": r[1], "finale": round(finale, 2), "giudici": round(giudici, 2),
                          "giocatori": None if giocatori is None else round(giocatori, 2), "voti": len(rice), "npc": r[4],
                          "crit": crit, "dett": dett, "_t": self.misure[u]["T"]})
        righe.sort(key=lambda x: (-x["finale"], -x["giudici"], -x["_t"], x["nome"]))
        pos = 0
        prima = None
        for i, x in enumerate(righe):
            chiave = (x["finale"], x["giudici"])
            if chiave != prima:
                pos = i + 1
                prima = chiave
            x["posto"] = pos
            x.pop("_t")
            g = self.giocatori.get(x["uid"])
            if g:
                g.punti = int(round(x["finale"] * 100))
                g.dati["riga"] = x
        self.classifica = righe

    def lista(self):
        return [[x["nome"], int(round(x["finale"] * 100)), x["uid"], 0 if x["npc"] or (self.giocatori.get(x["uid"]) and self.giocatori[x["uid"]].via is None) else 1]
                for x in self.classifica] if self.classifica else [[x.nome, 0, x.uid, 0 if x.via is None else 1] for x in self.giocatori.values()]

    def termina(self):
        """La fine: la classifica di tutti (anche gli NPC), il posto e i premi ai giocatori. I premi valgono anche da soli
        contro gli NPC: sono solo titoli e adesivi, e il voto e' quello della stessa giuria."""
        self.stato = "fine"
        self.chiusa = time.time()
        if not self.classifica:
            self._classifica()
        premi = __import__("giochi").CFG.get("premi", {}).get(self.gioco, {})
        for gioc in self.giocatori.values():
            r = gioc.dati.get("riga")
            pos = r["posto"] if r else 0
            mio = {}
            if r and len(self.ordine) >= 2:
                mio = dict(premi.get("tutti", {}))
                mio.update(premi.get(str(pos), {}))
            self.manda(gioc, {"a": "fine", "classifica": self.lista(), "posto": pos, "premi": mio, "extra": self.extra_fine(gioc)})
        self.g.log("fine", self.gioco, self.id, [(x["nome"], x["finale"]) for x in self.classifica])

    def extra_fine(self, gioc):
        r = gioc.dati.get("riga") or {}
        return {"tema": self._tema_dati(), "finale": r.get("finale", 0.0), "giudici": r.get("giudici", 0.0), "giocatori": r.get("giocatori"),
                "voti": r.get("voti", 0), "crit": r.get("crit", {}), "dett": r.get("dett", []),
                "podio": [[x["uid"], x["nome"], x["finale"], x["posto"]] for x in self.classifica[:3]]}
