#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Zduńska Wola — imienne głosowania Rady Miasta Zduńska Wola (IX kadencja).

Źródło: BIP bip.zdunskawola.pl (CMS Logonet), kategoria
"Protokoły z sesji Rady Miasta" (/artykuly/555/protokoly-z-sesji).
Kategoria to JEDEN artykuł zbiorczy z listą załączników /attachments/download/{id}
("Protokół z {RZYMSKIE} sesji Rady Miasta.pdf"). Każdy protokół to PDF z warstwą
tekstową zawierający WSZYSTKE głosowania imienne sesji INLINE, w klasycznym
eSesja formacie tekstowym:
    Głosowano w sprawie:
    <temat>
    Wyniki głosowania
    ZA: 17, PRZECIW: 0, WSTRZYMUJĘ SIĘ: 0, BRAK GŁOSU: 0, NIEOBECNI: 4
    Wyniki imienne:
    ZA (17)
    Imię Nazwisko, ...
    PRZECIW (0) / WSTRZYMUJĘ SIĘ (0) / BRAK GŁOSU (0) / NIEOBECNI (n)
po ostatniej liście następuje narracja protokołu ("Rada Miasta podjęła uchwałę
nr ...") — odcinamy ją po wzorze końca. Data sesji z treści protokołu
("która odbyła się w dniu 9 lipca 2026 r."), numer sesji z tytułu załącznika
("Protokół z XXVIII sesji").

Użycie:
    python scrape_zdunska_wola.py --city-dir <cities/zdunska-wola> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.zdunskawola.pl"
CATEGORY = "/artykuly/555/protokoly-z-sesji"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

_MONTHS = {
    "stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,"lipca":7,
    "sierpnia":8,"wrzesnia":9,"września":9,"pazdziernika":10,"października":10,
    "listopada":11,"grudnia":12,
}

_ROMAN = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
          "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,
          "XIX":19,"XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,
          "XXVII":27,"XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33,
          "XXXIV":34,"XXXV":35}

REQ_DELAY = 0.8
_LAST = 0.0

def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()

def _get(url, cache_dir):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    from requests.exceptions import ConnectionError, Timeout
    for attempt in range(6):
        _rate()
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=90, verify=False)
            r.raise_for_status()
            data = r.content
            if cache_dir:
                (cache_dir / (key + ".dat")).write_bytes(data)
            return data
        except (ConnectionError, Timeout, OSError):
            if attempt == 5:
                raise
            time.sleep(3 + attempt * 4)
    raise RuntimeError(f"GET failed: {url}")

# ---------------- discovery ----------------
def discover_sessions(cache_dir):
    """One-page category: all protocol PDF attachments of the aggregate article."""
    from html import unescape
    t = _get(BIP + CATEGORY, cache_dir).decode("utf-8", "ignore")
    sessions = []
    seen = set()
    for m in re.finditer(r'<a[^>]*href="([^"]*attachments/download/(\d+))"[^>]*>(.*?)</a>', t, re.S):
        href = unescape(m.group(1))
        if not href.startswith("http"):
            href = BIP + href
        title = re.sub(r"<[^>]+>", " ", m.group(3))
        title = re.sub(r"\s+", " ", title).strip()
        if href in seen:
            continue
        seen.add(href)
        rm = re.search(r'Protokół z\s+([IVXLCDM]+)\s+sesji', title, re.I)
        num = _ROMAN.get(rm.group(1).upper()) if rm else None
        sessions.append({"url": href, "title": title, "num": num, "date": None})
    sessions.sort(key=lambda s: (s["num"] or 999))
    return sessions

# ---------------- imienne parsing ----------------
_LABEL_RE = re.compile(r'\b(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI):?\s*\((\d+)\)')
_COUNTS_RE = re.compile(
    r'ZA:\s*(\d+),?\s*PRZECIW:\s*(\d+),?\s*WSTRZYMUJĘ SIĘ:\s*(\d+),?\s*'
    r'BRAK GŁOSU:\s*(\d+),?\s*NIEOBECNI:\s*(\d+)')

_END_RE = re.compile(r'^(Rada Miasta|Radny |Radna |Przewodnicząc|Punkt |Głos |Pan |Pani |Ww\.|Wniosek|Uchwał|Następnie|Nastepnie)', )

_FOOTER_TOKENS = re.compile(
    r'(zakończono|godz|wygenerowano|za\s*pomocą|app\.esesja\.pl|strona\s*\d+\s*z\s*\d+|'
    r'głosowanie\s*z\s*dnia|w\s*dniu:|\d{1,2}:\d{2}:\d{2}|\|)', re.I)

# narracja protokołu wklejona w środek strumienia tekstu (pdfplumber łączy linie)
_NARR_RE = re.compile(
    r'(Protokół (z|został)|Porządek obrad|Punk[t]? \d|Plan pracy|Komisja \w+ '
    r'(została|uzyskała)|Radni Rady Miasta|Wniosek|Uwagi|Obrady|Sesja ot|'
    r'stanowisko|informacj|Omówi|Przedstawi)')

# markery wżartej narracji służące do ODCINANIA chunka listy imiennej
_NARR_CUT_RE = re.compile(
    r'(Rada Miasta |Rada Miasta$|Punkt \d|Radny |Radna |Przewodnicząc|Głos zabrał|'
    r'Głos zabrała|Protokół został|Porządek obrad|Następnie|poinformował|'
    r'Komisja (Uchwał|Rewizyjna|Skrutacyjna) (została|uzyskała|powołała)|'
    r'Ww\.|obrady|Sesja |Uchwała|uchwałę|Wniosek|stwierdzono|przyjęto|'
    r'Komisja \w+ pozytywnie|Kierownik|Naczelnik|Prezydent Miasta|Zastępca)')

def _clean_name(s):
    """Keep ONLY a strict person token 'First Last(-X)' — odrzuca page-numbery
    ('11 Tomasz Siemienkowicz'), narrację ('Adam Synowiec Protokół został…'),
    nagłówki punktów itd. Porządek obrad i narracja protokołu wżierają się w
    listy imienne przy fuzji linii przez pdfplumber — jedyna niezawodna granica
    to ścisły wzorzec nazwiska."""
    s = s.strip()
    if not s:
        return None
    s = re.sub(r"^\d+\s+", "", s)
    s = re.sub(r"\s+\d+$", "", s)
    if not s or not any(c.isalpha() for c in s):
        return None
    if _FOOTER_TOKENS.search(s) or _NARR_RE.search(s) or "." in s:
        return None
    if not re.match(r"^[A-ZŁŚŻŹĆĄĘŃ][a-złóśżźcąęń'-]+(?: [A-ZŁŚŻŹĆĄĘŃ][a-złóśżźcąęń'-]+){1,2}$", s):
        return None
    return re.sub(r"\s+", " ", s)

def parse_roster(text):
    """Header protokołu: 'Obecni na sesji radni - N' + 'N) Nazwisko Imię' +
    'Nieobecni na sesji radni:'. Zwraca listę 'Imię Nazwisko' (canonical roster)."""
    roster = []
    m = re.search(r'Obecni na sesji radni.*?\n(.*?)(?=\nNieobecni|\nW obradach)', text, re.S)
    blocks = [m.group(1)] if m else []
    mn = re.search(r'Nieobecni na sesji radni:?\s*\n(.*?)(?=\nW obradies?|\nW obradach|\n\n)', text, re.S)
    if mn:
        blocks.append(mn.group(1))
    for blk in blocks:
        for mm in re.finditer(r'\d+[).][ \t]*([A-ZŁŚŻŹĆĄĘŃ][\włóśżźcąęń\'’]*(?:[ \t]+(?:Nieobecn\w+|Punkt\w*)?[ \t]*[A-ZŁŚŻŹĆĄĘŃ][\włóśżźcąęń\'’]*){0,2})', blk):
            parts = mm.group(1).rstrip("; ").split()
            parts = [p for p in parts if not re.match(r'^(Nieobecn|Punkt|Obecni|Sesji|radni)', p)]
            if len(parts) < 2:
                continue
            # 'Nazwisko Imię' -> 'Imię Nazwisko' (reszta list imiennych jest w tym szyku)
            if len(parts) == 2:
                roster.append(f"{parts[1]} {parts[0]}")
            else:
                roster.append(" ".join(parts[1:]) + " " + parts[0])
    return roster

def merge_roster_variants(names):
    """'Renata Kosińska' vs 'Renata Kosińska-Graf' — ten sam radny: kanon = dłuższy."""
    names = sorted(set(names))
    canon = {}
    for n in names:
        replaced = None
        for other in names:
            if other != n and other.startswith(n + "-"):
                replaced = other
                break
        canon[n] = replaced or n
    return sorted(set(canon.values()))

def canon_name(tok, roster_set, roster_pairs):
    """Normalize a parsed name token to the roster canonical form (handles
    surname-first order and hyphenated surnames joined without comma)."""
    if tok in roster_set:
        return tok
    parts = tok.split()
    if len(parts) == 2:
        alt = f"{parts[1]} {parts[0]}"
        if alt in roster_set:
            return alt
    return tok

def _extract_names(remainder_lines, expected):
    # nieużywane — zastąpione przez roster-matching
    return []

def _match_roster_in_chunk(chunk, roster_names, used, cut_narr=True):
    """Znajdź wystąpienia nazwisk z rosteru w tekście chunka (pozycyjnie).
    Odporność na: numery stron w środku nazwisk (usuwamy cyfry), wżartą narrację
    (odcinamy chunk na pierwszym markerze narracji — TYLKO dla ostatniej kategorii;
    w środkowych chunkach narracji nie ma, a markery typu 'uchwałę' są legalnymi
    słowami tematów), szyk Nazwisko-Imię."""
    if cut_narr:
        # odetnij wżartą narrację protokołu na najwcześniejszym markerze
        cut = len(chunk)
        for mm in _NARR_CUT_RE.finditer(chunk):
            cut = min(cut, mm.start())
        chunk = chunk[:cut]
    # numery stron wtapiają się w strumień: 'Lewandowski,\n18\nDawid' → zlep
    # 'LewandowskiDawid'. Cyfry zamieniamy na przecinki (separatory), nie spacje.
    clean = re.sub(r'\d+', ' , ', chunk)
    clean = re.sub(r'\s+', ' ', clean)
    # nazwiska dłuższe (złożone) najpierw — chronią przed cieniem krótszych
    order = sorted(roster_names, key=lambda n: -len(n))
    found = {}
    for nm in order:
        if nm in used:
            continue
        variants = [nm]
        parts = nm.split()
        if len(parts) == 2:
            variants.append(f"{parts[1]} {parts[0]}")
            if "-" in parts[1]:
                short = f"{parts[0]} {parts[1].split('-')[0]}"
                variants += [short, f"{parts[1].split('-')[0]} {parts[0]}"]
        for v in variants:
            for m in re.finditer(re.escape(v), clean):
                a, b = m.start(), m.end()
                if (a > 0 and clean[a-1].isalpha()) or (b < len(clean) and clean[b].isalpha()):
                    continue
                if any(nm in other and other in found and
                       found[other][0] <= a and b <= found[other][1]
                       for other in order):
                    continue  # match jest wewnątrz dłuższego nazwiska już znalezionego
                if nm not in found or a < found[nm][0]:
                    found[nm] = (a, b)
    return [nm for nm, _ in sorted(found.items(), key=lambda kv: kv[1][0])]

def _parse_block(blk, roster_names):
    cm = _COUNTS_RE.search(blk)
    if not cm:
        return None
    za, przeciw, wstrzym, brak, nieob = (int(x) for x in cm.groups())
    header_counts = {"za": za, "przeciw": przeciw, "wstrzymal_sie": wstrzym,
                     "brak": brak, "nieobecni": nieob}
    gs = blk.find("Głosowano w sprawie:")
    if gs == -1:
        # temat może zostać w poprzednim fragmencie po splitcie — nie odrzucamy głosu
        topic = "(glosowanie)"
    else:
        topic_raw = blk[gs + len("Głosowano w sprawie:"):cm.start()]
        topic = re.sub(r"\s+", " ", topic_raw).strip(" .,:;-")
        topic = topic or "(glosowanie)"
    wi = blk.find("Wyniki imienne")
    remainder = blk[wi:]
    labels = list(_LABEL_RE.finditer(remainder))
    cat_map = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
               "BRAK GŁOSU": "brak", "NIEOBECNI": "nieobecni"}
    named = defaultdict(list)
    for i, m in enumerate(labels):
        cat = cat_map.get(m.group(1))
        start = m.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(remainder)
        chunk = remainder[start:end]
        # narracja wżarta jest wyłącznie w chunk ostatniej kategorii — tam tniemy
        names = _match_roster_in_chunk(chunk, roster_names, set(),
                                       cut_narr=(i + 1 == len(labels)))
        named[cat] = names
    # de-duplikacja międzykategorialna: nazwisko w >1 kategorii → przypisz do tej,
    # której liczba listy zgadza się z nagłówkiem; gdy żadna — priorytet NIEOBECNI
    # (obserwowany kierunek błędu BIP: osoba nieobecna mylnie w liście ZA)
    prio = ["nieobecni", "za", "przeciw", "wstrzymal_sie", "brak"]
    seen_nm = {}
    for cat in prio:
        for nm in named.get(cat, []):
            seen_nm.setdefault(nm, []).append(cat)
    for nm, cats in seen_nm.items():
        if len(cats) < 2:
            continue
        keep = None
        for c in cats:
            if len(named[c]) == header_counts.get(c, -1):
                keep = c
                break
        if keep is None:
            keep = cats[0]  # prio order: NIEOBECNI wygrywa
        for c in cats:
            if c != keep:
                named[c] = [x for x in named[c] if x != nm]
    # agregaty: listy są źródłem prawdy (BIP ma literówki w nagłówkach)
    counts = {k: len(named.get(k, [])) for k in
              ("za", "przeciw", "wstrzymal_sie", "brak", "nieobecni")}
    return {"topic": topic, "counts": counts, "named": dict(named),
            "header_counts": header_counts}

def parse_protocol_text(text, roster_names):
    """Parse one protocol's text into vote records using the roster vocabulary."""
    if "Wyniki imienne" not in text:
        return [], None, 0
    dm = re.search(r'w dniu\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})\s+r\.', text)
    date = None
    if dm:
        mon = _MONTHS.get(dm.group(2))
        if mon:
            date = f"{int(dm.group(3))}-{mon:02d}-{int(dm.group(1)):02d}"
    n_total = text.count("Wyniki imienne")
    # split per-vote: głosowania proceduralne bywają BEZ markera 'Głosowano w
    # sprawie' (narracja + 'Wyniki głosowania') — split po obu markerach; temat
    # dla bloków bez markera rekonstruujemy z tekstu poprzedzającego.
    blocks = re.split(r'(?=Głosowano\s+w\s+sprawie|Wyniki\s+g[oł]osowania)', text)
    recs = []
    prev_tail = ""
    for blk in blocks:
        if "Wyniki imienne" not in blk:
            prev_tail = (prev_tail + " " + re.sub(r"\s+", " ", blk)).strip()[-900:] if blk.strip() else prev_tail
            continue
        r = _parse_block(blk, roster_names)
        if r:
            if r["topic"] == "(glosowanie)" and prev_tail:
                gm = prev_tail.rfind("Głosowano w sprawie:")
                if gm != -1:
                    r["topic"] = prev_tail[gm + len("Głosowano w sprawie:"):].strip(" .,:;-")[:300] or r["topic"]
            recs.append(r)
            prev_tail = re.sub(r"\s+", " ", blk).strip()[-900:]
    return recs, date, n_total

def protocol_text(data):
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""

def validate_vote(rec):
    for cat, expected in rec["counts"].items():
        got = len(rec["named"].get(cat, []))
        if got != expected:
            return False, f"{cat}: got {got} expect {expected}"
    return True, ""

# ---------------- output (wzór: scrape_naklo) ----------------
def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
            'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)

def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []; vid = 0; sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
            "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "nieobecni":
                    councilors_data[nm]["votes_nieobecny"] += 1
                elif cat == "brak":
                    councilors_data[nm]["votes_brak"] += 1
                elif cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []; names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}, total_votes, total_sessions

def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak": 0,
                              "nieobecni": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]:
                    cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] and r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "brak")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                "votes_nieobecny": vd["nieobecni"], "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    work_dir = Path(args.work_dir) if args.work_dir else city_dir / "work"
    pdf_dir = work_dir / "pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache_dir) if args.cache_dir else None

    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    sessions = discover_sessions(cache)
    print(f"[zdw] {len(sessions)} protokołów sesji w kategorii 555")

    # PASS 1: pobierz wszystko + zbuduj globalny roster z nagłówków protokołów
    texts = {}
    for se in sessions:
        try:
            pdf_name = re.sub(r"[^A-Za-z0-9]+", "_", se["url"]).strip("_") + ".pdf"
            data = _get(se["url"], cache)
            (pdf_dir / pdf_name).write_bytes(data)
            texts[se["url"]] = protocol_text(data)
        except Exception as e:
            print(f"  [ERR dl {se['title'][:40]}] {type(e).__name__}: {e}")
    roster = merge_roster_variants(
        [nm for t in texts.values() for nm in parse_roster(t)])
    print(f"[zdw] globalny roster: {len(roster)} radnych")

    # PASS 2: parsuj głosowania przy słowniku rosteru
    records = []
    for se in sessions:
        text = texts.get(se["url"])
        if not text:
            continue
        try:
            recs, date, n_total = parse_protocol_text(text, roster)
            if not recs:
                print(f"  [skip] {se['title'][:60]} (brak Wyniki imienne)")
                continue
            ok_n = 0
            for r in recs:
                ok, msg = validate_vote(r)
                if ok:
                    r["date"] = date; r["num"] = se["num"]
                    records.append(r); ok_n += 1
                else:
                    print(f"    [VAL-FAIL {se['title'][:40]}] {msg} | {r['topic'][:60]}")
            print(f"  [ok] {se['title'][:52]} date={date} votes={ok_n}/{n_total}")
        except Exception as e:
            print(f"  [ERR {se['title'][:40]}] {type(e).__name__}: {e}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"; docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[zdw] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])}")

if __name__ == "__main__":
    main()
