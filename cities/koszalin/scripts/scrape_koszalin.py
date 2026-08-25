#!/usr/bin/env python3
"""
Radoskop Koszalin — imienne głosowania Rady Miejskiej.

Źródło: BIP Miasta Koszalina na platformie Logonet (bip.koszalin.pl), kategoria
"Uchwały Rady Miejskiej i głosowania radnych" (/artykuly/1744). Rada Miejska w
Koszalinie (IX kadencja 2024-2029, 23 radnych) publikuje dla każdej uchwały PDF
"Głosowanie do uchwały nr <XXX>/<nr>/<rok>", zawierający głosowanie imienne:
per radny ZA / PRZECIW / WSTRZYMUJĘ SIĘ / NIEODDANY / NIEOBECNY.

Struktura BIP (Logonet):
  /uchwaly/1744/{page}/15            — stronicowana lista uchwał (nr, data, tytuł)
  /uchwala/{id}/{slug}               — strona uchwały z załącznikami
  /attachments/download/{att_id}     — plik PDF (m.in. "Głosowanie do uchwały ...")

Głos w PDF mapujemy: ZA->za, PRZECIW->przeciw, WSTRZYMUJĘ SIĘ->wstrzymal_sie,
NIEODDANY->brak_glosu, NIEOBECNY/NIEOBECNA->nieobecni.

Kluby radnych skuratorowane z BIP "Kluby Radnych" (stan 2026-08 / aktualizacja
2025-12-22): KO (Koalicja Obywatelska), PiS (Prawo i Sprawiedliwość),
WDK (Wspólnie dla Koszalina), NZ (Niezrzeszeni).

Użycie:
    python scrape_koszalin.py --output docs/data.json
                              --profiles docs/profiles.json
                              [--cache-dir .cache]
"""

import argparse
import difflib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import pdfplumber

BIP = "https://bip.koszalin.pl"
LIST_CAT = "/uchwaly/1744"

KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
CUTOFF = "2024-05-07"  # początek IX kadencji

# Kuratorowane przypisanie klubów (canonical "Imię Nazwisko").
# Źródło: https://bip.koszalin.pl/artykuly/585/kluby-radnych (stan 2026-08).
CLUB_CURRENT = {
    "Dorota Chałat": "KO",
    "Magdalena Chałat": "KO",
    "Monika Foremna-Pilarska": "KO",
    "Piotr Iwat": "KO",
    "Krystyna Kościńska": "KO",
    "Małgorzata Leśniewska-Lorek": "KO",
    "Michał Listowski": "KO",
    "Bartosz Malinowski": "KO",
    "Agnieszka Połaniecka": "KO",
    "Teresa Tałaj": "KO",
    "Anetta Urbaniak": "KO",
    "Izabela Wesołowska": "KO",
    "Artur Wezgraj": "KO",
    "Jacek Wezgraj": "KO",
    "Andrzej Jakubowski": "PiS",
    "Miłosz Janczewski": "PiS",
    "Oliwia Skórka": "PiS",
    "Artur Wiśniewski": "PiS",
    "Piotr Jedliński": "WDK",
    "Przemysław Krzyżanowski": "WDK",
    "Żaneta Kwapisz": "WDK",
    "Błażej Papiernik": "WDK",
    "Wiktor Kamieniarz": "NZ",
}
CLUB_FALLBACK = "NZ"

CLUB_NAMES = {
    "KO": "Koalicja Obywatelska",
    "PiS": "Prawo i Sprawiedliwość",
    "WDK": "Wspólnie dla Koszalina",
    "NZ": "Niezrzeszeni",
}

REQ_DELAY = 0.4
_LAST_REQ = 0.0


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, cache_dir: Path | None = None, binary: bool = False):
    import hashlib
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".html"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 info@radoskop.eu"},
        timeout=40, verify=False)
    resp.raise_for_status()
    data = resp.content if binary else resp.text
    if cache_dir is not None:
        cf = cache_dir / (key + ext)
        cf.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            cf.write_bytes(data)
        else:
            cf.write_text(data, encoding="utf-8", errors="ignore")
    return data


# ---------------------------------------------------------------------------
# 1. Kolekcja uchwał IX kadencji z listingu
# ---------------------------------------------------------------------------

def iso(d: str) -> str:
    dd, mm, yy = d.split(".")
    return f"{yy}-{mm}-{dd}"


def collect_uchwaly(cache_dir: Path | None = None):
    """Zwraca uchwały IX kadencji: [{id, href, number, date, title}]."""
    out = []
    seen = set()
    page = 1
    fully_past = 0
    while page <= 130:
        html = fetch(f"{BIP}{LIST_CAT}/{page}/15", cache_dir)
        # per-item rows: blok z "uchwała nr", "z dnia", "w sprawie" + link /uchwala/{id}
        # Parsujemy linki do uchwał oraz daty "z dnia"
        ids = {}
        date_seq = []
        # iterate over item blocks — each block contains the /uchwala/{id} link and a 'z dnia'
        # Split HTML on <article>/item containers loosely: find all uchwala links and all 'z dnia' dates
        for m in re.finditer(r'href="([^"]*/uchwala/(\d+)/[^"]*)"[^>]*>\s*((?:Uchwa|uchwa)[^<]*)<', html):
            iid = int(m.group(2))
            if iid not in ids:
                ids[iid] = m.group(1) if m.group(1).startswith("http") else BIP + m.group(1)
        for m in re.finditer(r'z dnia\s*</[^>]+>\s*(\d{2}\.\d{2}\.\d{4})', html):
            date_seq.append(m.group(1))
        # fallback: dates may be inline
        if not date_seq:
            # strip tags then find 'z dnia dd.mm.yyyy'
            body = re.sub(r'<[^>]+>', '\n', html)
            date_seq = re.findall(r'z dnia\s*\n\s*(\d{2}\.\d{2}\.\d{4})', body)
        page_dates = [iso(d) for d in date_seq]
        # also grab uchwała numbers + titles from text for each id
        body = re.sub(r'<[^>]+>', '\n', html)
        # build rows
        for iid in ids:
            if iid in seen:
                continue
            seen.add(iid)
            out.append({"id": iid, "href": ids[iid], "page": page})
        if page_dates:
            if all(d < CUTOFF for d in page_dates):
                fully_past += 1
            else:
                fully_past = 0
        if fully_past >= 2:
            break
        page += 1
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF głosowania
# ---------------------------------------------------------------------------

def _glos_attachment_id(html: str):
    """Znajdź id załącznika 'Głosowanie do uchwały ...'."""
    atts = re.findall(
        r'attachments-title[^>]*href="[^"]*/attachments/download/(\d+)"[^>]*>\s*(.*?)\s*</a>',
        html, re.S)
    for att_id, title in atts:
        t = re.sub(r'<[^>]+>', '', title).strip()
        if "łosowanie" in t or "Głosowanie" in t:
            return int(att_id)
    return None


_ROMAN = re.compile(r"([IVXLCDM]+)\s+Sesja")

_VOTE_TOKENS = ("ZA", "PRZECIW", "WSTRZYMUJĘ", "WSTRZYMUJE", "NIEODDANY",
                "NIEODDANA", "NIEOBECNY", "NIEOBECNA", "NIEOBECNI",
                "OBECNY", "OBECNA")

# numer porządkowy radnego: cyfry lub roman "i." (niektóre PDF-y renderują "1." jako "i.")
_LP_RE = re.compile(r"(?:\d{1,2}|[ivx]{1,2})[.,]?", re.I)


def _is_lp(tok: str) -> bool:
    return bool(_LP_RE.fullmatch(tok.strip()))


_MONTHS = {
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "września": "09", "września": "09", "wrzesnia": "09", "października": "10",
    "pazdziernika": "10", "listopada": "11", "grudnia": "12",
}


def _extract_session_date(txt: str) -> str | None:
    """Data sesji z pierwszego wiersza lub z 'Data głosowania:'. Odporna na
    separatory przecinek/kropka, miesięc po polsku lub brak daty w tytule."""
    # 1) "w dniu 17.07.2026" / "17,07.2026" / "17 07 2026"
    m = re.search(r"w dniu\s+(\d{1,2})[.,\s]+(\d{1,2})[.,\s]+(\d{4})", txt)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    # 2) "w dniu 14 maja 2026"
    m = re.search(r"w dniu\s+(\d{1,2})\s+([a-żA-Ż]+)\s+(\d{4})", txt)
    if m and m.group(2).lower() in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]}-{int(m.group(1)):02d}"
    # 3) fallback "Data głosowania: 29.01.2026" / "Data głosowania 17 07.2026"
    m = re.search(r"Data głosowania\s*:?\s*(\d{1,2})[.,\s]+(\d{1,2})[.,\s]+(\d{4})", txt)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def _vote_cat(token: str) -> str | None:
    t = re.sub(r"[.,]", "", token).upper()
    if t == "ZA":
        return "za"
    if t == "PRZECIW":
        return "przeciw"
    if t in ("WSTRZYMUJĘ", "WSTRZYMUJE"):
        return "wstrzymal_sie"
    if t in ("NIEODDANY", "NIEODDANA"):
        return "brak_glosu"
    if t in ("NIEOBECNY", "NIEOBECNA", "NIEOBECNI"):
        return "nieobecni"
    return None


def _cluster_rows(words):
    """Grupuje słowa (top, x0, x1, text) w wiersze po współrzędnej top."""
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows = []
    cur = None
    cur_top = None
    for w in words:
        if cur is None or abs(w["top"] - cur_top) > 4:
            if cur:
                rows.append(cur)
            cur = [w]
            cur_top = w["top"]
        else:
            cur.append(w)
    if cur:
        rows.append(cur)
    return rows


_AC = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
       'ś': 's', 'ź': 'z', 'ż': 'z'}


def _norm_letters(s: str) -> str:
    t = s.lower()
    for pl, a in _AC.items():
        t = t.replace(pl, a)
    return re.sub(r"[^a-z]", "", t)


def _match_canonical(raw: str) -> str | None:
    """Fuzzy-dopasowuje surowe 'Nazwisko Imię' do jednego z 23 radnych (roster).

    Najpierw zamieniamy na kolejność 'Imię Nazwisko' (w PDF-ie jest 'Nazwisko
    Imię'), potem porównujemy normalizowane literowo — odporne na brakujące
    litery (pdfplumber gubi 'i') i na artefakty (kropki, myślniki, apostrofy).
    Odpada śmieć (nie-radny).
    """
    tokens = raw.split()
    if len(tokens) >= 2:
        swapped = " ".join(tokens[1:]) + " " + tokens[0]
    else:
        swapped = raw
    rl = _norm_letters(swapped)
    if len(rl) < 6:
        return None
    best = None
    br = 0.0
    for c in CLUB_CURRENT:
        cl = _norm_letters(c)
        ratio = difflib.SequenceMatcher(None, rl, cl).ratio()
        if ratio > br:
            br = ratio
            best = c
    return best if br >= 0.72 else None


def _parse_vote_col(col_tokens):
    """Z listy tokenów jednej kolumny wyciąga (canonical_name, cat) albo None."""
    tokens = [t for t in col_tokens if t.strip()]
    if not tokens:
        return None
    if not _is_lp(tokens[0]):
        return None
    vote_idx = None
    for i, tok in enumerate(tokens[1:], start=1):
        if _vote_cat(tok) is not None:
            vote_idx = i
            break
    if vote_idx is None:
        return None
    raw = " ".join(tokens[1:vote_idx])
    raw = _LP_RE.sub("", raw, count=1).strip()
    if not raw:
        return None
    canonical = _match_canonical(raw)
    cat = _vote_cat(tokens[vote_idx])
    if cat and canonical:
        return canonical, cat
    return None


def parse_glosowanie_pdf(data: bytes):
    """Parsuje PDF 'Głosowanie do uchwały' na {session_date, session_number, topic, named}.

    Tabela per-radny jest dwukolumnowa, więc każdy wiersz dzielimy w miejscu
    największej poziomej przerwy między słowami. Głos to token po nazwisku.
    """
    import io
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        txt = "\n".join((p.extract_text() or "") for p in pdf.pages)
        words = []
        for p in pdf.pages:
            words.extend(p.extract_words(
                use_text_flow=False, keep_blank_chars=False,
                x_tolerance=1.5, y_tolerance=3))

    session_date = _extract_session_date(txt)
    rm = _ROMAN.search(txt.split("\n")[0] if txt.split("\n") else "")
    session_number = rm.group(1) if rm else ""

    # topic between 'Głosowanie' and 'Typ głosowania'
    topic = ""
    ti = txt.find("Głosowanie")
    tj = txt.find("Typ głosowania")
    if ti != -1 and tj != -1 and tj > ti:
        seg = txt[ti + len("Głosowanie"):tj]
        seg = "\n".join(l for l in seg.split("\n") if not re.fullmatch(r"\s*\d+\s*", l))
        seg = re.sub(r"\s+", " ", seg).strip()
        seg = re.sub(r"^\d+(\.\d+)*\.?\s*", "", seg)
        topic = seg.strip().rstrip(".,;:-")

    # Tabela per-radny jest dwukolumnowa. Każdy wiersz dzielimy na kolumny
    # w miejscach, gdzie zaczyna się kolejny numer porządkowy (Lp) radnego,
    # a następnie parsujemy każdą kolumnę jako "Lp Imię Nazwisko GŁOS".
    # Ograniczamy się do regionu tabeli ("Uprawnieni do głosowania" → "Wydrukowano").
    y_start = None
    y_end = None
    for w in words:
        if w["text"].strip() == "Uprawnieni":
            y_start = w["top"]
        if w["text"].strip() == "Wydrukowano":
            y_end = w["top"]
    named = defaultdict(list)
    for row in _cluster_rows(words):
        if y_start is not None and row[0]["top"] < y_start - 2:
            continue
        if y_end is not None and row[0]["top"] > y_end:
            continue
        ws = sorted(row, key=lambda w: w["x0"])
        toks = [w["text"] for w in ws]
        # pozycje numerów Lp — początek każdej kolumny
        lp_idx = [i for i, t in enumerate(toks) if _is_lp(t)]
        if not lp_idx:
            continue
        for si, start in enumerate(lp_idx):
            end = lp_idx[si + 1] if si + 1 < len(lp_idx) else len(toks)
            seg = toks[start:end]
            pair = _parse_vote_col(seg)
            if pair:
                canonical, cat = pair
                named[cat].append(canonical)
    named = {k: v for k, v in named.items() if v}
    return {"session_date": session_date, "session_number": session_number,
            "topic": topic, "named": dict(named)}


def _to_cat(vote: str) -> str | None:
    if vote == "ZA":
        return "za"
    if vote == "PRZECIW":
        return "przeciw"
    if vote == "WSTRZYMUJĘ SIĘ":
        return "wstrzymal_sie"
    if vote in ("NIEODDANY", "NIEODDANA"):
        return "brak_glosu"
    if vote in ("NIEOBECNY", "NIEOBECNA"):
        return "nieobecni"
    return None  # OBECNY/OBECNA — quorum


# ---------------------------------------------------------------------------
# 3. Budowanie kadencja-*/data.json / profiles.json
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z', 'Ą': 'A', 'Ć': 'C', 'Ę': 'E',
            'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    slug = re.sub(r"[^a-z0-9]+", "", slug)
    return slug


def _club_of(name):
    return CLUB_CURRENT.get(name, CLUB_FALLBACK)


def build_output(records):
    votes = []
    sessions_by_date = {}
    for rec in records:
        d = rec["session_date"]
        if not d:
            continue
        num = rec["session_number"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": num, "vote_count": 0,
                                   "attendees": set()}
        vid = len(votes) + 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        votes.append({
            "id": str(vid),
            "session_date": d,
            "session_number": num,
            "topic": rec["topic"] or "",
            "named_votes": {k: list(v) for k, v in rec["named"].items()},
            "counts": {
                "za": len(rec["named"].get("za", [])),
                "przeciw": len(rec["named"].get("przeciw", [])),
                "wstrzymal_sie": len(rec["named"].get("wstrzymal_sie", [])),
            },
        })

    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({
            "date": d, "number": s["number"], "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]),
            "attendees": sorted(s["attendees"]), "speakers": [],
        })

    all_names = set()
    for v in votes:
        for names in v["named_votes"].values():
            all_names.update(names)

    councilors = {}
    for name in all_names:
        councilors[name] = {"name": name, "club": _club_of(name), "district": None,
                            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                            "votes_brak": 0, "votes_nieobecny": 0}
    for v in votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                c = councilors.get(name)
                if not c:
                    continue
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

    total_votes = len(votes)
    total_sessions = len(sessions_data)

    councillor_sess = defaultdict(set)
    for v in votes:
        for cat, names in v["named_votes"].items():
            if cat == "nieobecni":
                continue
            for n in names:
                councillor_sess[n].add(v["session_date"])

    # zgodność z klubem: w każdym głosowaniu dla każdego klubu wyznaczamy
    # stanowisko większości (wśród radnych klubu, którzy oddali głos ZA/PRZECIW/wstrz.).
    club_majority = []
    for vidx, v in enumerate(votes):
        for club in set(_club_of(n) for n in v["named_votes"].get("za", [])
                        + v["named_votes"].get("przeciw", [])
                        + v["named_votes"].get("wstrzymal_sie", [])):
            cnt = Counter()
            members = set()
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                for n in v["named_votes"].get(cat, []):
                    if _club_of(n) == club:
                        cnt[cat] += 1
                        members.add(n)
            if not cnt or len(members) < 1:
                continue
            top = cnt.most_common(2)
            if len(top) == 1 or top[0][1] > top[1][1]:
                club_majority.append((club, vidx, top[0][0]))

    zgod = defaultdict(lambda: [0, 0])  # name -> [matched, participated]
    for club, vidx, cat in club_majority:
        for n in councilors:
            if _club_of(n) != club:
                continue
            nv = votes[vidx]["named_votes"]
            voted = nv.get(cat, [])
            if n in voted:
                zgod[n][0] += 1
                zgod[n][1] += 1
            else:
                # czy radny w ogóle głosował (Z/P/W) w tym głosowaniu?
                participated = any(n in nv.get(k, []) for k in ("za", "przeciw", "wstrzymal_sie"))
                if participated:
                    zgod[n][1] += 1

    councilors_list = []
    for c in sorted(councilors.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        m, p = zgod.get(c["name"], [0, 0])
        zk = round(m / p * 100, 1) if p else 0.0
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": zk,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False,
            "activity": None,
        })

    # similarity
    vectors = defaultdict(dict)
    for v in votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        score = round(same / len(common) * 100, 1)
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": score, "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_of(n) for n in all_names)
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "clubs": {k: club_counts[k] for k in CLUB_NAMES},
        "club_names": CLUB_NAMES,
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {"generated": datetime.now().isoformat(),
            "default_kadencja": KADENCJA_ID, "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec["session_date"]
        if not d:
            continue
        for cat, names in rec["named"].items():
            for name in names:
                key = "za" if cat == "za" else "przeciw" if cat == "przeciw" \
                    else "wstrzymal_sie" if cat == "wstrzymal_sie" \
                    else "nieobecny" if cat == "nieobecni" else "brak"
                cv[name][key] += 1
                cv[name]["votes"].append({"session": d, "vote": key})
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        present_sess = len({v["session"] for v in vd["votes"] if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / all_sess if all_sess else 0.0
        profiles.append({
            "name": name, "slug": make_slug(name),
            "kadencje": {
                KADENCJA_ID: {
                    "club": _club_of(name), "has_voting_data": True,
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
    return {"profiles": profiles}


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
    ap.add_argument("--output", required=True, help="docs/data.json")
    ap.add_argument("--profiles", required=True, help="docs/profiles.json")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    print("=== Scraper Rada Miejska w Koszalinie (bip.koszalin.pl, Logonet) ===")
    uchwaly = collect_uchwaly(cache_dir)
    print(f"  Uchwał IX kadencji (>={CUTOFF}): {len(uchwaly)}")

    records = []
    ok = noglos = fail = 0
    for u in uchwaly:
        try:
            html = fetch(u["href"], cache_dir)
            att = _glos_attachment_id(html)
            if att is None:
                noglos += 1
                continue
            data = fetch(f"{BIP}/attachments/download/{att}", cache_dir, binary=True)
            parsed = parse_glosowanie_pdf(data)
            if not parsed["session_date"]:
                fail += 1
                continue
            if parsed["session_date"] < CUTOFF:  # poza IX kadencją
                continue
            # tajne głosowania (protokoły komisji skrutacyjnej) nie mają głosów imiennych
            if not any(parsed["named"].values()):
                continue
            records.append(parsed)
            ok += 1
        except Exception as e:
            print(f"    BŁĄD uchwała {u['id']}: {e}")
            fail += 1
        time.sleep(0.05)

    print(f"  PDF głosowań OK: {ok}, bez załącznika głosowania: {noglos}, błędy: {fail}")

    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)

    kad = output["kadencje"][0]
    print(f"  SESJE: {kad['total_sessions']}, GŁOSOWANIA: {kad['total_votes']}, "
          f"RADNYCH: {kad['total_councilors']}")
    print(f"  KLUBY: {kad['clubs']}")
    print("  OK — zapisano data.json / kadencja-2024-2029.json / profiles.json")


if __name__ == "__main__":
    main()
