#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Kłodzko — imienne głosowania Rady Miejskiej w Kłodzku (IX kadencja).

Źródło: BIP Urzędu Miasta (eBOI/aste, um.bip.klodzko.pl). Rada Miejska publikuje
w kategorii "Wyniki głosowania radnych → IX kadencja" (menu=684) per-sesja artykuł
"Głosowanie radych {RR} sesji Rady Miejskiej w Kłodzku z dnia ..." z jednym PDF
imiennych wyników głosowania (tekstowy, generowany z System Rada eSesja).

Format PDF (tekstowy, layout=True rozdziela kolumny):
    Kłodzko, dn.: 13.07.2026 r.
    1 1. 3. Wybór sekretarza sesji.
    GŁOSOWAŁO: 17
    głosowało ZA: 17
    głosowało PRZECIW: 0
    WSTRZYMAŁO się: 0
    LP. Nazwisko i Imię jak głosował
    1 Banyś Iwona głosował ZA
    ...
    (per radny: głosował ZA / głosował PRZECIW / WSTRZYMAŁ się / nie głosował)

37 sesji IX kadencji (I 2024-05-07 .. XXXVII 2026-07-13). 21 radnych.

Użycie:
    python scrape_klodzko.py --city-dir cities/klodzko --cache-dir .cache
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

BIP = "https://um.bip.klodzko.pl"
MENU_VOTES = 684              # Wyniki głosowania radnych IX kadencja
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

# Kanoniczny skład Rady (BIP "Radni IX kadencji 2024-2029" — BIP podaje nazwiska
# w kolejności "Nazwisko Imię"; Radoskop używa "Imię Nazwisko" -> poniżej przekład
# na kanoniczną kolejność). Weryfikacja z list imiennych w PDFach głosowań.
# Kluby: BIP Kłodzka nie publikuje listy klubów radnych (brak kategorii "Kluby").
ROSTER_NAMES = [
    "Iwona Banyś", "Piotr Bryła", "Zdzisław Duda", "Stanisław Ferenc",
    "Armin Jarosz", "Anna Karolczak", "Jolanta Kobak", "Adam Kowalski",
    "Mateusz Kubasiak", "Wojciech Łyszkiewicz", "Zbigniew Nowak",
    "Karolina Opalińska", "Bogusław Procak", "Magdalena Ptaszyńska",
    "Czesław Radwański", "Marta Szmyrko-Konieczyńska", "Iwona Sobczyk",
    "Damian Ślak", "Magdalena Taurogińska", "Józef Trocki", "Elżbieta Trybus",
]

REQ_DELAY = 0.35
_LAST_REQ = 0.0


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, cache_dir: Path | None = None, binary: bool = False):
    if cache_dir is not None and not binary:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = cache_dir / (key + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                        timeout=60, verify=False)
    resp.raise_for_status()
    data = resp.content if binary else resp.text
    if cache_dir is not None:
        if binary:
            pass
        else:
            cf = cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".html")
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_text(data, encoding="utf-8", errors="ignore")
    return data


# ---------------------------------------------------------------------------
# 1. Kolekcja sesji (menu=684) -> (article_id, date, session_roman)
# ---------------------------------------------------------------------------
MONTH_PL = {'stycznia': '01', 'lutego': '02', 'marca': '03', 'kwietnia': '04',
            'maja': '05', 'czerwca': '06', 'lipca': '07', 'sierpnia': '08',
            'września': '09', 'wrzysnia': '09', 'października': '10', 'pazdziernika': '10',
            'listopada': '11', 'grudnia': '12'}

ROMAN = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
         "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,
         "XIX":19,"XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,
         "XXVII":27,"XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33,
         "XXXIV":34,"XXXV":35,"XXXVI":36,"XXXVII":37,"XXXVIII":38,"XXXIX":39,"XL":40}


def _parse_label_date(label: str) -> str | None:
    # "...z dnia 13 lipca 2026" albo "...z dnia 07.05.2024 r"
    m = re.search(r'z dnia\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', label, re.I)
    if m:
        mon = MONTH_PL.get(m.group(2).lower())
        if mon:
            return f"{m.group(3)}-{mon}-{int(m.group(1)):02d}"
    m = re.search(r'z dnia\s+(\d{1,2})\.(\d{1,2})\.(\d{4})', label)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def discover_sessions(cache_dir):
    html = fetch(f"{BIP}/index.php?n=i&menu={MENU_VOTES}", cache_dir)
    arts = []
    for m in re.finditer(r"<a href='index\.php\?n=i\&(?:amp;)?id=(\d+)\&(?:amp;)?akcja=info\&(?:amp;)?menu=684[^']*'[^>]*?title='[^']*?Kliknij[^']*?'>(.*?)</a>",
                         html, re.S):
        aid = int(m.group(1))
        label = unicodedata.normalize("NFC", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if 'sesji' not in label.lower():
            continue
        date = _parse_label_date(label)
        rm = re.search(r'radych\s+([IVXLCDM]+)\s+sesji', label)
        roman = rm.group(1) if rm else ''
        arts.append({"id": aid, "label": label, "date": date or "0000-00-00",
                     "roman": roman, "num": ROMAN.get(roman)})
    arts.sort(key=lambda a: (a["date"], a["id"]))
    return arts


def _pdf_url_from_article(html):
    m = re.search(r"<a href='(?:\.\./)?(pi/[^']*\.pdf)'", html)
    if m:
        return f"{BIP}/{m.group(1)}"
    m = re.search(r"<a href='([^']*\.pdf)'", html, re.I)
    if m:
        h = m.group(1)
        return h if h.startswith('http') else f"{BIP}/{h}"
    return None


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF głosowań (format Kłodzko: modern "jak głosował" + legacy)
#    Parsowanie row-based: wiersze per-radny są czyste w ekstrakcji non-layout
#    dla obu formatów. Liczba głosowań w sesji = liczba grup spójnych wierszy.
# ---------------------------------------------------------------------------
def _vote_cat(tok):
    t = tok.strip().lower()
    if re.search(r'\bza\b', t):
        return 'za'
    if 'przeciw' in t:
        return 'przeciw'
    if 'wstrzym' in t:
        return 'wstrzymal_sie'
    if 'nie' in t and 'głosow' in t:
        return 'brak_glosu'
    if re.search(r'nieobecn', t):
        return 'nieobecni'
    return None


def _norm2(s):
    s = s.lower()
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's',
            'ź': 'z', 'ż': 'z'}
    for pl, a in repl.items():
        s = s.replace(pl, a)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"[^a-z]", "", s)
    return s


# canonical roster: display name  -> frozenset of normalized tokens (split na
# spacje ORAZ myślniki — "Szmyrko-Konieczyńska" -> {szmyrko, konieczynska})
_CANON_TOKENS = {}
for _nm in ROSTER_NAMES:
    _k = frozenset(_norm2(w) for w in re.split(r'[\s,-]+', _nm) if _norm2(w))
    if _k not in _CANON_TOKENS:
        _CANON_TOKENS[_k] = _nm


def _canonicalize(raw):
    """Znormalizuj nazwisko z PDF (kolejność 'Nazwisko Imię', legacy uppercase
    SURNAME NAME) do kanonicznego 'Imię Nazwisko' z rostera — dopasowanie
    niezależne od kolejności tokenów (frozenset)."""
    toks = [_norm2(w) for w in re.split(r'[\s,-]+', raw) if w.strip()]
    toks = [t for t in toks if t]
    for combo_len in (2, 3):
        for i in range(len(toks) - combo_len + 1):
            key = frozenset(toks[i:i + combo_len])
            if key in _CANON_TOKENS:
                return _CANON_TOKENS[key]
    return raw.strip()


# row patterns (layout=True: rozdziela kolumny; numer strony w lewej kolumnie
# w długich dokumentach -> opcjonalny wiodący numer: (?:\d+\s+)? )
_MODERN_ROW = re.compile(
    r'^\s*(?:\d+\s+)?\d+\s+([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż\'’,-]+(?:\s+[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż\'’,-]+)+?)\s+'
    r'(głosow[^\n]+|WSTRZYMA[ŁL]\S*\s*si\S*|Wstrzyma[łl]\S*\s*si\S*|nie\s+głosow\S*)\s*$')
_LEGACY_ROW = re.compile(
    r'^\s*(?:-?\s*\d+\s+)?(.+?)\s*[-–]\s*'
    r'(GŁOSOW\S*\s*(?:ZA|PRZECIW|SIĘ|SIE)|NIE\s+GŁOSOW\S*|WSTRZYMA[ŁL][A]?\s*SI[ĘÉE])\s*$', re.I)


def _extract_full(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        full = "\n".join((p.extract_text() or "") for p in pdf.pages)
    return full


def parse_pdf_votes(data):
    full = _extract_full(data)
    lines = full.split("\n")
    modern = ('jak głosował' in full) or bool(re.search(r'\blp\.?\s+nazwisko', full, re.I))
    legacy = (not modern) and bool(re.search(r'Za\s*[–-]\s*\d+\s*,\s*przeciw', full, re.I)
                                   or re.search(r'GŁOSOWAŁ[AŁ]?\s*ZA', full, re.I))
    if not modern and not legacy:
        return []  # empty/broken PDF (np. XXV)

    if modern:
        return _parse_modern(lines)
    return _parse_legacy(lines)


def _parse_modern(lines):
    votes = []
    cur_rows = []
    cur_topic = []
    # topic buffer: text since last rows block (excluding rows), we accumulate
    # non-row lines; when a row block starts we commit previous for a new vote.
    pending_topic = []

    def flush():
        nonlocal cur_rows, pending_topic
        if not cur_rows:
            return
        named = defaultdict(list)
        for name, tok in cur_rows:
            cat = _vote_cat(tok)
            if cat:
                named[cat].append(_canonicalize(name))
        topic = " ".join(t for t in pending_topic if t.strip())
        topic = re.sub(r'\s+', ' ', topic).strip()
        topic = re.sub(r'^.*?>>>', '', topic)  # usun "Wprowadź nazwę pliku >>>"
        votes.append({"topic": topic, "aggregate": None,
                      "named": {k: list(v) for k, v in named.items()}})
        cur_rows = []
        pending_topic = []

    for l in lines:
        m = _MODERN_ROW.match(l)
        if m:
            cur_rows.append((m.group(1).strip(), m.group(2)))
            continue
        # not a row line
        if cur_rows:
            # a new topic started after previous rows -> flush previous vote and
            # treat this line as beginning of the next topic
            flush()
        pending_topic.append(l)
    flush()
    return votes


def _parse_legacy(lines):
    """Legacy format: każdy blok zaczyna się od wiersza 'Za – N, przeciw - N, ...'
    (agregat głosowania). Wiersze po nim (aż do następnego agregatu / końca) to
    głosy radnych. Podział po agregacie radzi sobie z przerwami stron (nagłówek
    'WYNIKI GŁOSOWANIA...' w trakcie listy wierszy)."""
    agg = [i for i, l in enumerate(lines)
           if re.search(r'Za\s*[–-]\s*\d+\s*,\s*przeciw', l, re.I)]
    if not agg:
        agg = [-1]
    votes = []
    for k, ag in enumerate(agg):
        start = ag + 1
        end = agg[k + 1] if k + 1 < len(agg) else len(lines)
        rows = []
        topic = ""
        for l in lines[start:end]:
            m = _LEGACY_ROW.match(l)
            if m:
                rows.append((m.group(1).strip(), m.group(2).strip()))
        # topic: ostatnia niepusta linia przed agregatem (tekst wniosku/pktu)
        for j in range(ag - 1, -1, -1):
            t = re.sub(r'\s+', ' ', lines[j]).strip()
            if t and not _LEGACY_ROW.match(lines[j]) and 'WYNIKI GŁOSOWANIA' not in t.upper():
                topic = t
                break
        named = defaultdict(list)
        for raw, tok in rows:
            nm = _canonicalize(raw)
            cat = _vote_cat(tok)
            if cat:
                named[cat].append(nm)
        votes.append({"topic": topic, "aggregate": None,
                      "named": {kk: list(vv) for kk, vv in named.items()}})
    return votes



# ---------------------------------------------------------------------------
# 3. Budowanie outputu (wzorzec scrape_police.py)
# ---------------------------------------------------------------------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's',
            'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def _club_of(name):
    return ""  # Kłodzko: brak publikowanych klubów radnych w BIP


def _validate(v):
    """Walidacja: każde głosowanie powinno zawierać pełną listę radnych
    (wszyscy 21 głosujących/nie głosujących) — suma per-kategoria == 21."""
    named = v["named"]
    total = (len(named.get("za", [])) + len(named.get("przeciw", []))
             + len(named.get("wstrzymal_sie", []))
             + len(named.get("brak_glosu", []))
             + len(named.get("nieobecni", [])))
    return total == len(ROSTER_NAMES)


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    bad = 0
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        named = {k: list(v) for k, v in rec["named"].items()}
        ok = rec.get("valid", True)
        if not ok:
            bad += 1
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": rec.get("session_num", ""),
            "topic": rec.get("topic", ""), "named_votes": named,
            "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
        })

    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({
            "date": d, "number": s["number"], "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
            "speakers": [],
        })

    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    # kanoniczna kolejność/kontrola
    roster = set(ROSTER_NAMES)
    missing = roster - all_names
    extra = all_names - roster

    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {
            "name": name, "club": _club_of(name), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0,
            "votes_with_club": 0, "votes_against_club": 0, "rebellions": [],
        }
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm not in councilors_data:
                    continue
                c = councilors_data[nm]
                if cat == "za":
                    c["votes_za"] += 1
                elif cat == "przeciw":
                    c["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    c["votes_wstrzymal"] += 1
                elif cat == "nieobecni":
                    c["votes_nieobecny"] += 1
                else:
                    c["votes_brak"] += 1

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)

    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat != "nieobecni":
                for nm in names:
                    councillor_sess[nm].add(v["session_date"])

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None,
        })

    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_of(n) for n in all_names)
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "clubs": dict(club_counts),
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": all_votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {
        "generated": datetime.now().isoformat(),
        "default_kadencja": KADENCJA_ID,
        "kadencje": [kad],
        "_audit": {"total_sessions": total_sessions, "total_votes": total_votes,
                   "invalid_votes": bad, "roster_missing": sorted(missing),
                   "extra_names": sorted(extra)},
    }


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                key = "za" if cat == "za" else "przeciw" if cat == "przeciw" \
                    else "wstrzymal_sie" if cat == "wstrzymal_sie" else "nieobecny" if cat == "nieobecni" else "brak"
                cv[nm][key] += 1
                cv[nm]["votes"].append({"session": d, "vote": key})
    profiles = []
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        present_sess = len({v["session"] for v in vd["votes"] if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / all_sess if all_sess else 0.0
        profiles.append({
            "name": nm, "slug": make_slug(nm),
            "kadencje": {
                KADENCJA_ID: {
                    "club": _club_of(nm), "has_voting_data": True,
                    "has_activity_data": False, "frekwencja": round(frekw, 1),
                    "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                    "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                    "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                    "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                    "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                    "former": False, "mid_term": False,
                }
            }
        })
    return {"profiles": profiles, "total": len(profiles)}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {"generated": output.get("generated", ""),
             "default_kadencja": output.get("default_kadencja", ""),
             "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    sessions = discover_sessions(cache_dir)
    print(f"[klodzko] sesje: {len(sessions)}")
    records = []
    for s in sessions:
        try:
            html = fetch(f"{BIP}/index.php?n=i&id={s['id']}&akcja=info&menu={MENU_VOTES}", cache_dir)
            purl = _pdf_url_from_article(html)
            if not purl:
                print(f"  [skip {s['id']}] brak PDF linku")
                continue
            data = fetch(purl, cache_dir, binary=True)
            votes = parse_pdf_votes(data)
            for v in votes:
                ok = _validate(v)
                records.append({"session_date": s["date"], "session_num": s["roman"],
                                "topic": v["topic"], "named": v["named"], "valid": ok})
            print(f"  {s['date']} {s['roman']:>3} votes={len(votes)} (pdf {purl.split('/')[-1]})")
        except Exception as e:
            print(f"  [ERR {s['id']}] {type(e).__name__}: {e}")

    output = build_output(records)
    profiles = build_profiles(records)
    audit = output.pop("_audit")
    print("\n[AUDIT]", json.dumps(audit, ensure_ascii=False))
    out_path = city_dir / "docs" / "data.json"
    save_split(output, out_path, profiles)
    print(f"[klodzko] wrote {out_path} (+kadencja +profiles.json)")


if __name__ == "__main__":
    main()
