#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Starachowice — imienne głosowania Rady Miejskiej w Starachowicach (IX kadencja).

Źródło: BIP bip.um.starachowice.pl (stary CMS eBOI-podobny, index.php selectsite).
Kategoria "Uchwały i protokoły z sesji RM" -> podstrony roczne (2024/2025/2026, menu mnu4)
-> podstrona sesji "N/RRRR - <data> rok" z załącznikami PDF; wśród nich
"Głosowanie imienne" / "Głosowania imienne" — eSesja FORMAT TEKSTOWY:
    Wyniki głosowania
    Głosowano w sprawie: <temat>
    ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n
    Wyniki imienne:
    ZA (n)
    <imię nazwisko, …>
Część raportów to SKANY bez warstwy tekstowej -> OCR (render pdfplumber 200dpi +
tesseract -l pol --psm 6), tekst OCR cache'owany w cache-dir obok surowych bajtów.
Walidacja per głos: sumy list imiennych == agregaty nagłówka (zarówno dla tekstu jak i OCR).
Skład = unikalne nazwiska ze wszystkich poprawnych głosowań. Kluby PENDING.

Użycie:
    python scrape_starachowice.py --city-dir <cities/starachowice> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.parse import quote, unquote, urljoin

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.um.starachowice.pl/"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

# podstrony roczne kategorii sesji (menu mnu4) — odkrywane dynamicznie, te stałe jako fallback
YEAR_PAGES = {"2024": "663", "2025": "705", "2026": "738"}

_MONTHS = {
    "styczen":1,"styczeń":1,"stycznia":1,"luty":2,"lutego":2,"marzec":3,"marca":3,
    "kwiecień":4,"kwiecień":4,"kwietnia":4,"maj":5,"maja":5,"czerwiec":6,"czerwca":6,
    "lipiec":7,"lipca":7,"sierpien":8,"sierpień":8,"sierpnia":8,"wrzesien":9,"wrzesień":9,
    "września":9,"pazdziernik":10,"październik":10,"pazdziernika":10,"października":10,
    "listopad":11,"listopada":11,"grudzien":12,"grudzień":12,"grudnia":12,
}

REQ_DELAY = 0.6
_LAST = 0.0

def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()

def _get(url, cache_dir, binary=True):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + (".dat" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    from requests.exceptions import ConnectionError, Timeout
    for attempt in range(6):
        _rate()
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=90, verify=False)
            r.raise_for_status()
            data = r.content
            if cache_dir:
                cf = cache_dir / (key + (".dat" if binary else ".html"))
                cf.write_bytes(data) if binary else cf.write_text(
                    _decode(data), encoding="utf-8", errors="ignore")
            return data if binary else _decode(data)
        except (ConnectionError, Timeout, OSError):
            if attempt == 5:
                raise
            time.sleep(3 + attempt * 4)
    raise RuntimeError(f"GET failed: {url}")

def _decode(raw):
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:3000])
    enc = "utf-8"
    if m:
        try: enc = m.group(1).decode()
        except Exception: pass
    try: return raw.decode(enc, errors="replace")
    except Exception: return raw.decode("utf-8", errors="replace")

def _qurl(u):
    """Percent-quote path+query preserving structure (BIP pliki mają spacje/ogonki w ścieżkach)."""
    if u.startswith("http"):
        scheme, rest = u.split("://", 1)
        host, _, rest2 = rest.partition("/")
        return f"{scheme}://{host}/" + quote(rest2)
    return quote(u)

_ROMAN = r"[IVX]+"

def _norm_date_str(ds):
    m = re.match(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", ds.strip().lower())
    if not m:
        return None
    mon = _MONTHS.get(m.group(2))
    if not mon:
        return None
    return f"{int(m.group(3))}-{mon:02d}-{int(m.group(1)):02d}"

# ---------------- discovery ----------------
def discover_sessions(cache_dir):
    """Roczne podstrony -> sesje 'N/RRRR - data' (URL-e po pełnym unquote)."""
    sessions = []
    seen = set()
    for year, cid in YEAR_PAGES.items():
        url = BIP + f"index.php?type=4&name=bt3&func=selectsite&value%5B0%5D=mnu4&value%5B1%5D={cid}"
        try:
            t = _get(url, cache_dir, binary=False)
        except Exception as e:
            print(f"[disc] year {year} fetch fail: {e}")
            continue
        for href, txt in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', t, re.S | re.I):
            tt = re.sub(r"<[^>]+>|\s+", " ", txt).strip()
            m = re.match(r"^(" + _ROMAN + r")/(\d{4}) - (.+?)\s*$", tt)
            if not m:
                continue
            if m.group(2) != year:
                continue
            u = href.replace("&amp;", "&")
            prev = None
            while prev != u:
                prev = u; u = unquote(u)
            if not u.startswith("http"):
                u = urljoin(BIP, u)
            if u in seen:
                continue
            seen.add(u)
            date = _norm_date_str(m.group(3))
            if not date:
                continue
            sessions.append({"url": u, "num": f"{m.group(1)}/{m.group(2)}", "date": date})
    sessions = [s for s in sessions if s["date"] >= KAD_START]
    sessions.sort(key=lambda s: s["date"])
    return sessions

_IMIENNA_RE = re.compile(r"g[łl]osow[ąa]n\w*\s+imien|imien\w*\s+g[łl]osow", re.I)

def session_attachments(session_url, cache_dir):
    t = _get(session_url, cache_dir, binary=False)
    out = []
    for href, txt in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', t, re.S | re.I):
        title = re.sub(r"<[^>]+>|\s+", " ", txt).strip()
        if not _IMIENNA_RE.search(title):
            continue
        h = href.replace("&amp;", "&")
        if not h.startswith("http"):
            h = urljoin(BIP, h)
        out.append((title, _qurl(h)))
    return out

# ---------------- OCR ----------------
def _ocr_pdf(data, cache_dir, url):
    """OCR skanu sekwencyjnie (NIE równolegle — pitfall tesseract). Cache po md5(url)+'.ocr'."""
    if cache_dir:
        cf = Path(cache_dir) / (hashlib.md5(url.encode()).hexdigest() + ".ocr")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, p in enumerate(pdf.pages):
            png = f"/tmp/stara_ocr_{os.getpid()}_{i}.png"
            try:
                p.to_image(resolution=200).save(png)
                r = subprocess.run(["tesseract", png, "-", "-l", "pol", "--psm", "6"],
                                   capture_output=True, text=True, timeout=180)
                parts.append(r.stdout or "")
            finally:
                if os.path.exists(png):
                    os.remove(png)
    full = "\n".join(parts)
    if cache_dir:
        (Path(cache_dir) / (hashlib.md5(url.encode()).hexdigest() + ".ocr")).write_text(full, encoding="utf-8")
    return full

import os

# ---------------- eSesja imienne parsing ----------------
_LABEL_RE = re.compile(r"\b(ZA|PRZECIW|WSTRZYMUJ[EĘ]\s+SI[EĘ]|BRAK\s+G[ŁL]OSU|NIEOBECNI)\s*\((\d+)\)")
_COUNTS_RE = re.compile(
    r"ZA:\s*(\d+),?\s*PRZECIW:\s*(\d+),?\s*WSTRZYMUJ[EĘ]\s+SI[EĘ]:\s*(\d+),?\s*"
    r"BRAK\s+G[ŁL]OSU:\s*(\d+),?\s*NIEOBECNI:\s*(\d+)")

_FOOTER_TOKENS = re.compile(
    r"(zakończono|godz|wygenerowano|za\s*pomocą|app\.esesja\.pl|strona\s*\d+\s*z\s*\d+|"
    r"g[łl]osowanie\s*z\s*dnia|w\s*dnia:|\d{1,2}:\d{2}:\d{2}|\||\b\d{2}\.\d{2}\.\d{4}\b)", re.I)

def _clean_name(s):
    s = s.strip()
    if not s or not any(c.isalpha() for c in s):
        return None
    # OCR leak: nazwisko sklejone z etykietą następnego bloku ('Zuba BRAK GŁOŚU (1')
    s = re.split(r"\s(?:BRAK|NIEOBE\w*|PRZECI\w*|WSTRZ\w*)\b", s)[0].strip()
    if _FOOTER_TOKENS.search(s):
        return None
    s = re.sub(r"\s+", " ", s)
    # OCR szum: odrzuć tokeny z wieloma znakami nietypowymi
    weird = sum(1 for c in s if not (c.isalpha() or c in " -.'"))
    if weird > max(1, len(s) // 4):
        return None
    return s

_CAT_MAP = {"ZA": "za", "PRZECIW": "przeciw", "BRAK": "brak", "NIEOBECNI": "nieobecni"}

def _cat_key(label):
    lab = re.sub(r"\s+", " ", label.upper())
    if lab.startswith("WSTRZYMUJ"):
        return "wstrzymal_sie"
    for k, v in _CAT_MAP.items():
        if lab.startswith(k):
            return v
    return None

def parse_imienne_text(text):
    if "wyniki imienne" not in text.lower():
        return []
    text = text.replace("\x0c", "\n")
    records = []
    blocks = re.split(r"(?=Wyniki g[łl]osowania)", text, flags=re.I)
    for blk in blocks:
        wl = re.search(r"wyniki imienne", blk, re.I)
        if not wl:
            continue
        labels = list(_LABEL_RE.finditer(blk, wl.end()))
        if not labels:
            continue
        named = defaultdict(list)
        counts = {}
        for i, m in enumerate(labels):
            cat = _cat_key(m.group(1))
            if cat is None:
                continue
            start = m.end()
            end = labels[i + 1].start() if i + 1 < len(labels) else len(blk)
            chunk = blk[start:end]
            for cut in ("G[łl]osowanie z dnia", "G[łl]osowanie zakończono",
                        "Wygenerowano", "za\\s*pomoc[aą]", "Wyniki g[łl]osowania", "\\|"):
                mm = re.search(cut, chunk, re.I)
                if mm:
                    chunk = chunk[:mm.start()]
                    break
            chunk = re.sub(r"\s+", " ", chunk)
            named[cat] = [t for t in (_clean_name(x) for x in chunk.split(",")) if t]
            counts[cat] = int(m.group(2))
        if not named.get("za") and not named.get("przeciw"):
            continue
        gs = re.search(r"G[łl]osowano w sprawie[:\s]*", blk, re.I)
        topic = "(glosowanie)"
        if gs:
            topic = re.sub(r"\s+", " ", blk[gs.end():labels[0].start()]).strip(" .,:;-") or topic
        cm = _COUNTS_RE.search(blk)
        hdr = None
        if cm:
            hdr = {"za": int(cm.group(1)), "przeciw": int(cm.group(2)),
                   "wstrzymal_sie": int(cm.group(3)), "brak": int(cm.group(4)),
                   "nieobecni": int(cm.group(5))}
        records.append({"topic": topic, "counts": counts, "hdr": hdr, "named": dict(named)})
    return records

def parse_imienne_payload(data, url, cache_dir):
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return []
    ocr = False
    if len(text.strip()) < 200:
        text = _ocr_pdf(data, cache_dir, url)
        ocr = True
    recs = parse_imienne_text(text)
    for r in recs:
        r["src"] = "ocr" if ocr else "text"
    return recs

def validate_vote(rec):
    for cat, expected in rec["counts"].items():
        got = len(rec["named"].get(cat, []))
        if got != expected:
            return False, f"{cat}: got {got} expect {expected}"
    return True, ""

# ---------------- roster anchoring (OCR warianty nazwisk) ----------------
def _norm_name(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c)).replace(" ", "")

def canonicalize_records(records):
    """Kotwiczy OCR-warianty nazwisk do czystego składu (z PDF-ów tekstowych, źródło
    'text'), przez clustering wariantów OCR + fuzzy-match do składu (difflib na
    znormalizowanych pełnych nazwiskach). Recordy, które po remapie tracą spójność
    z agregatami, są odrzucane (bezpieczny kierunek — nigdy fabrykowania)."""
    from difflib import SequenceMatcher
    # 1. czysty skład = nazwiska z recordów tekstowych (walidowanych wcześniej)
    roster = Counter()
    for rec in records:
        if rec.get("src") != "text":
            continue
        for cat, lst in rec["named"].items():
            if cat == "nieobecni":
                continue
            for nm in lst:
                roster[nm] += 1
    if not roster:
        # fallback: clustering bez składu (wszystko z OCR)
        print("[roster] BRAK tekstowych źródeł — klastrowanie OCR-owe")
        roster = Counter()
        for rec in records:
            for cat, lst in rec["named"].items():
                if cat == "nieobecni":
                    continue
                for nm in lst:
                    roster[nm] += 1
    canon = {}
    firsts = Counter()
    for rn in roster:
        firsts[rn.split(" ", 1)[0]] += 1
    for nm in sorted({n for rec in records for lst in rec["named"].values() for n in lst},
                     key=lambda n: -roster.get(n, 0)):
        if nm in roster:
            canon[nm] = nm
            continue
        b = _norm_name(nm)
        best, best_r = None, 0.0
        for rn in roster:
            a = _norm_name(rn)
            if abs(len(a) - len(b)) > max(3, len(a) // 3):
                continue
            r = SequenceMatcher(None, a, b).ratio()
            if r > best_r:
                best, best_r = rn, r
        if best and best_r >= 0.72:
            canon[nm] = best
            continue
        # reguła 2: imię jednoznaczne w składzie -> dopasuj po nazwisku
        parts = nm.split(" ", 1)
        if len(parts) == 2:
            fn, sn = parts
            cands = [rn for rn in roster if rn.split(" ", 1)[0] == fn and " " in rn]
            if len(cands) == 1:
                rn_sn = cands[0].split(" ", 1)[1]
                if SequenceMatcher(None, _norm_name(sn), _norm_name(rn_sn)).ratio() >= 0.5:
                    canon[nm] = cands[0]
                    continue
            # reguła 3: nazwisko identyczne, imię zbliżone (OCR 'lena'~'Ilona')
            exact_sn = [rn for rn in roster if " " in rn and rn.split(" ", 1)[1] == sn]
            if len(exact_sn) == 1 and SequenceMatcher(
                    None, _norm_name(fn), _norm_name(exact_sn[0].split(" ", 1)[0])).ratio() >= 0.6:
                canon[nm] = exact_sn[0]
                continue
            # reguła 4: imię zbliżone (>=0.75) + nazwisko najlepsze (>=0.5)
            fn_best = max(roster, key=lambda rn: SequenceMatcher(
                None, _norm_name(fn), _norm_name(rn.split(" ", 1)[0])).ratio())
            fn_r = SequenceMatcher(None, _norm_name(fn),
                                   _norm_name(fn_best.split(" ", 1)[0])).ratio()
            if fn_r >= 0.75:
                sn_scores = sorted(
                    (SequenceMatcher(None, _norm_name(sn),
                                     _norm_name(rn.split(" ", 1)[1])).ratio(), rn)
                    for rn in roster if " " in rn)
                if sn_scores and sn_scores[-1][0] >= 0.5:
                    top_fn = sn_scores[-1][1].split(" ", 1)[0]
                    margin_ok = (len(sn_scores) < 2 or sn_scores[-1][0] - sn_scores[-2][0] >= 0.2)
                    fn_top_ok = SequenceMatcher(
                        None, _norm_name(fn), _norm_name(top_fn)).ratio() >= 0.75
                    if margin_ok or fn_top_ok:
                        canon[nm] = sn_scores[-1][1]
                        continue
            # reguła 5: imię identyczne i JEDNOZNACZNE (w składzie i na liście głosu),
            # radny nieobecny na liście -> nazwisko jest zmasakrowane przez OCR
            same_first_roster = [rn for rn in roster if " " in rn
                                 and rn.split(" ", 1)[0] == fn]
            if len(same_first_roster) == 1 and len(cands) == 1:
                canon[nm] = same_first_roster[0]
                continue
        canon[nm] = nm  # pozostaw jako kandydata na radnego spomiędzy skanu
    merged = sum(1 for k, v in canon.items() if k != v)
    kept, dropped = [], 0
    for rec in records:
        new_named = {}
        for cat, lst in rec["named"].items():
            seen_local = []
            for nm in lst:
                c = canon.get(nm, nm)
                if c not in seen_local:
                    seen_local.append(c)
            new_named[cat] = seen_local
        rec2 = dict(rec)
        rec2["named"] = new_named
        ok, msg = validate_vote(rec2)
        if ok:
            kept.append(rec2)
        else:
            dropped += 1
            print(f"    [CANON-DROP {rec.get('date','?')}] {rec['topic'][:40]!r}: {msg}")
    print(f"[roster] skład kotwiczący: {len(roster)}, remapowań OCR->skład: {merged}, "
          f"recordy odrzucone po canon: {dropped}")
    return kept

# ---------------- output ----------------
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
                                   "vote_count": 0, "attendees": set()}
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
                key = {"nieobecni": "votes_nieobecny", "brak": "votes_brak", "za": "votes_za",
                       "przeciw": "votes_przeciw", "wstrzymal_sie": "votes_wstrzymal"}.get(cat)
                if key:
                    councilors_data[nm][key] += 1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for names in v["named_votes"].values():
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
    pairs = []
    for a, b in combinations(sorted(vectors.keys()), 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for v in common if vectors[a][v] == vectors[b][v])
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
    sess_set = {r["date"] for r in records if r["date"] >= KAD_START}
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
                "votes_nieobecny": 0, "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else (city_dir / "work")
    cache.mkdir(parents=True, exist_ok=True)

    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    sessions = discover_sessions(cache)
    print(f"[starachowice] {len(sessions)} sesji IX kad.")

    records = []
    for se in sessions:
        n_votes = 0; n_skip = 0
        try:
            for title, href in session_attachments(se["url"], cache):
                data = _get(href, cache)
                recs = parse_imienne_payload(data, href, cache)
                if not recs:
                    n_skip += 1
                    continue
                for r in recs:
                    ok, msg = validate_vote(r)
                    if ok:
                        r["date"] = se["date"]; r["num"] = se["num"]
                        records.append(r); n_votes += 1
                    else:
                        print(f"    [VAL-FAIL {se['date']} {r['topic'][:40]!r}] {msg}")
            print(f"  [{'ok' if n_votes else '-'}] {se['date']} {se['num']} votes={n_votes} skip={n_skip}", flush=True)
        except Exception as e:
            print(f"  [ERR {se['date']}] {type(e).__name__}: {e}")

    records = canonicalize_records(records)
    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"; docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[starachowice] DONE votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])}")

if __name__ == "__main__":
    main()
