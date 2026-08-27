#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Strzelce Opolskie — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Żródło: BIP bip.strzelceopolskie.pl (CMS eBOI-ish "sesje_rady_miejskiej"),
kategoria "/rada_miejska/sesje_rady_miejskiej.html" (stronicowanie st:1..st:14).
Każda sesja IX kadencji to artykuł z linkiem /rada_miejska/sesje_rady_miejskiej/{TYTUL}/idn:{id}.html,
a w nim załącznik "/download/{fileId}.html" o nazwie zawierającej "Lista obecności + głosowania"
(lub wariant: "Lista obecności i głosowania radnych", "Głosowania radnych").

Ten załącznik to ZDJĘTY (scanned-image) PDF, 10-22 strony, BEZ warstwy tekstowej.
Strona 1 = "LISTA OBECNOŚCI" (obecność), kolejne strony = po jednym głosowaniu:
    "Wyniki głosowania" / "Głosowano w sprawie: <temat>" /
    "ZA: n, PRZECIW: n, WSTRZYMUJE SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n" /
    "Wyniki imienne:" + listy "ZA (n)" / "PRZECIW (n)" / "WSTRZYMUJE SIĘ (n)" / ...
Dlatego każdą stronę renderujemy do PNG (PyMuPDF, dpi=150) i OCR-ujemy tesseract -l pol.
OCR tekst jest cache-owany w ocr_cache/<idn>/p{00}.txt (szybkie ponowne uruchomienia).

Parser imienny jest odporny na szum OCR: normalizacja polskich znaków (ł->l, usunięcie
diakrytyków), tolerancja zawijania linii, dopasowanie nazwisk do kanonicznego rejestru
zbudowanego z samych danych (unia LISTA OBECNOŚCI + lista imienna).

Użycie:
    python scrape_strzelce.py [--city-dir work/strzelce] [--no-ocr]
Zapisuje docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json.
"""
import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

BIP = "https://bip.strzelceopolskie.pl"
CAT_LIST = "/rada_miejska/sesje_rady_miejskiej.html"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 1.0
_LAST = 0.0

def _nk(s):
    """normalizacja klucza: lower, ł->l, usunięcie akcentów i znaków."""
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s)

# Ręczna kanonizacja wariantów OCR (zweryfikowane w głosowaniach: w każdym takim głosie
# prawidłowy radny jest NIEOBECNY, a wariant zajmuje jego slot -> czysty dedup, nie fabrykacja).
_NAME_ALIAS = {
    "Henryk Ridner": "Henryk Rudner",
    "Piotr Rudner": "Henryk Rudner",
    "tieńtyki Rudner": "Henryk Rudner",
    "Jadwiga Wdewik": "Jadwiga Wdowik",
    "Jadwiga Wwdowik": "Jadwiga Wdowik",
    "Marek Goc": "Tadeusz Goc",
    "Marek Imie": "Marek Zarębski",
}
# Finalna, odporna na różne pisownie OCR — stosowana na POZIOMIE rekordów (po map_name),
# więc łapie warianty niezależnie od dokładnej pisowni: merguje je do prawidłowego radnego.
_RENAME = {
    "Henryk Ridner": "Henryk Rudner",
    "Piotr Rudner": "Henryk Rudner",
    "tieńtyki Rudner": "Henryk Rudner",
    "Jadwiga Wdewik": "Jadwiga Wdowik",
    "Jadwiga Wwdowik": "Jadwiga Wdowik",
    "Marek Goc": "Tadeusz Goc",
    "Marek Imie": "Marek Zarębski",
    "Marek K": "Marek Zarębski",
}

# ---------------- HTTP (grzecznie, z backoff) ----------------
def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()

def _get_bytes(url):
    for i in range(6):
        try:
            _rate()
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=120, verify=False)
            r.raise_for_status()
            return r.content
        except Exception as e:
            if i == 5:
                raise
            time.sleep(2 * (i + 1))
    return None

# ---------------- zarzadzanie cache OCR ----------------
_cache_dir = None
def _cache_path(idn, page):
    return Path(_cache_dir) / str(idn) / f"p{page:02d}.txt"

def _ocr_worker(args):
    pdf_path, idn, p = args
    import subprocess
    d = fitz.open(pdf_path)
    pix = d[p].get_pixmap(dpi=150)
    f = f"/tmp/rc_{idn}_{p}.png"
    pix.save(f)
    d.close()
    r = subprocess.run(["tesseract", f, "-", "-l", "pol"], capture_output=True, text=True, timeout=300)
    return p, r.stdout

def ocr_pdf(pdf_path, idn):
    """OCR kazdej strony PDF do ocr_cache/<idn>/p{NN}.txt (serialnie — tesseract jest
    wielowątkowy, więc Pool tylko by przesycił 4 rdzenie). Zwraca {page: text}."""
    import subprocess
    doc = fitz.open(pdf_path)
    n = len(doc)
    doc.close()
    out = {}
    for p in range(n):
        cp = _cache_path(idn, p)
        if cp.is_file():
            out[p] = cp.read_text(encoding="utf-8")
            continue
        pout = _ocr_worker((pdf_path, idn, p))
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(pout[1], encoding="utf-8")
        out[p] = pout[1]
    return {p: out[p] for p in sorted(out)}

# ---------------- discovery ---------------
_MONTHS = {"stycznia":1,"lutego":2,"luty":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,
    "lipca":7,"sierpnia":8,"września":9,"października":10,"pazdziernika":10,"listopada":11,"grudnia":12}

def discover_sessions(max_pages=15):
    sessions = {}
    for i in range(1, max_pages + 1):
        u = f"{BIP}/rada_miejska/sesje_rady_miejskiej/st:{i}.html" if i > 1 else BIP + CAT_LIST
        t = _get_bytes(u).decode("utf-8", "ignore")
        for m in re.finditer(r'<div class="aktualnosc">(.*?)</div>', t, re.S):
            blk = m.group(1)
            am = re.search(r'<a href="([^"]+)"[^>]*>\s*(.*?)\s*</a>', blk, re.S)
            if not am:
                continue
            href = am.group(1)
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", am.group(2))).strip()
            if "Sesja Rady Miejskiej" not in title:
                continue
            idm = re.search(r"idn:(\d+)\.html", href)
            idn = idm.group(1) if idm else None
            dm = re.search(r"\b(\d{1,2})\s+([a-ząćęłńóśźż]+?)(?:\.|\s|,)\s*(\d{4})", title)
            dt = None
            if dm and dm.group(2) in _MONTHS:
                dt = f"{dm.group(3)}-{_MONTHS[dm.group(2)]:02d}-{int(dm.group(1)):02d}"
            sessions[idn] = {"idn": idn, "title": title, "date": dt, "url": BIP + "/" + href.lstrip("/")}
        time.sleep(0.5)
    return [s for s in sessions.values() if s["date"] and s["date"] >= KAD_START]

def get_rollcall_url(session):
    t = _get_bytes(session["url"]).decode("utf-8", "ignore")
    arts = [(a.group(1), re.sub(r"<[^>]+>", "", a.group(2)).strip())
            for a in re.finditer(r'<a[^>]+href="([^"]*download/[^"]+)"[^>]*>(.*?)</a>', t, re.S)]
    rc = next(((u, txt) for u, txt in arts if re.search(r"lista\s*obecno[śs]ci|g[łl]osowan", txt, re.I)), None)
    return rc

# ---------------- OCR parsing ---------------
_CAT_RE = re.compile(r"^\s*(ZA|PRZECIW|WSTRZYMUJE\s*SIĘ|WSTRZYMUJEŃ|WSTRZYMUJĘ\s*SIĘ|BRAK\s*GŁOSU|NIEOBECNI)\s*\(\s*(\d+)\s*\)\s*$", re.I)

def _looks_vote_header(line):
    """pojedyncza linia-nagłówek listy imiennej jak 'ZA (10)' (z tolerancją szumu OCR)."""
    line = line.strip()
    m = _CAT_RE.match(line.replace("E\u0301", "Ę").replace("E\u0300", "Ę").replace("Ē", "Ę"))
    if not m:
        # usuń końcowe nie-alfanumeryczne znaki (np. '*' z OCR), spróbuj ponownie
        stripped = re.sub(r"[^\w\s]*\s*$", " ", line)
        m = _CAT_RE.match(stripped.replace("E\u0301", "Ę").replace("E\u0300", "Ę").replace("Ē", "Ę"))
    if not m:
        m = _CAT_RE.match(line)
    return m

def _norm_cat(s):
    k = _nk(s)
    if k == "za":
        return "za"
    if k.startswith("przeciw"):
        return "przeciw"
    if k.startswith("wstrzym"):
        return "wstrzymal_sie"
    if k.startswith("brak"):
        return "brak_glosu"
    if k.startswith("nieobecn"):
        return "nieobecni"
    return None

def _extract_agg(line):
    """'ZA: 10, PRZECIW: 3, WSTRZYMUJE SIĘ: 6, BRAK GŁOSU: 0, NIEOBECNI: 2' -> dict"""
    out = {}
    m = re.search(r"ZA\s*:?\s*(\d+)", line)
    if m: out["za"] = int(m.group(1))
    m = re.search(r"PRZECIW\s*:?\s*(\d+)", line)
    if m: out["przeciw"] = int(m.group(1))
    m = re.search(r"WSTRZYMUJE\s*SIĘ\s*:?\s*(\d+)", line)
    if m: out["wstrzymal_sie"] = int(m.group(1))
    m = re.search(r"BRAK\s*GŁOSU\s*:?\s*(\d+)", line)
    if m: out["brak_glosu"] = int(m.group(1))
    m = re.search(r"NIEOBECNI\s*:?\s*(\d+)", line)
    if m: out["nieobecni"] = int(m.group(1))
    return out

def parse_document(pages_text):
    """pages_text: list[str] w kolejności stron. Zwraca listę glosowań w formacie:
       {topic, agg{...}, named{cat:[names]}}"""
    full = "\n".join(f"@@PAGE@@ {i}\n{t}" for i, t in enumerate(pages_text))
    lines = full.split("\n")
    # znajdź znaczniki startu głosowania "Głosowano w sprawie:"
    starts = []
    for idx, ln in enumerate(lines):
        if re.search(r"G[łl]osowano w sprawie", ln, re.I):
            starts.append(idx)
    votes = []
    for si, st in enumerate(starts):
        end = starts[si + 1] if si + 1 < len(starts) else len(lines)
        block = lines[st:end]
        # temat: wszystko między "sprawie:" a linią agregatów
        topic_lines = []
        agg = {}
        imi_idx = None
        for ln in block:
            if re.search(r"G[łl]osowano w sprawie", ln, re.I):
                m = re.search(r"sprawie\s*:\s*(.*)$", ln)
                if m and m.group(1).strip():
                    topic_lines.append(m.group(1).strip())
                continue
            a = _extract_agg(ln)
            if a and "za" in a:
                agg = a
                break  # agregaty kończą temat
            topic_lines.append(ln.strip())
        topic = re.sub(r"\s+", " ", " ".join(topic_lines)).strip(" :.,;-")
        if topic.startswith("(") and ")" in topic:
            topic = topic[topic.index(")") + 1:].strip()
        topic = re.sub(r"^\d+[\.\)]?\s*", "", topic).strip()
        if not topic:
            topic = "(glosowanie)"
        # parsuj listy imienne — zbieramy surowe linie per kategoria, potem dzielimy po przecinkach
        # (nazwisko zawinięte w nowej linii bez przecinka łączy się z poprzednim tokenem)
        named = defaultdict(list)
        cur = None
        in_imi = False
        pending = defaultdict(list)  # cat -> surowe linie tekstu
        for j in range(st, end):
            ln = lines[j]
            mh = _looks_vote_header(ln)
            if mh:
                # category header (ZA (N) / PRZECIW (N) / …) — enters imienne mode even when
                # the "Wyniki imienne:" marker line is missing from the source (observed on
                # Strzelce pages where the header got dropped; names still follow).
                cur = _norm_cat(mh.group(1))
                in_imi = True
                continue
            if re.search(r"Wyniki imienne", ln, re.I):
                in_imi = True
                continue
            if not in_imi:
                continue
            if re.search(r"(Głosowanie zakończono|Wygenerowano|Głosowanie z dnia|Wyniki głosowania)", ln, re.I):
                # jeśli stopka występuje W TEJ SAMEJ linii co reszta ostatniego nazwiska,
                # zachowaj fragment przed markerem zanim przerwiemy
                if cur is not None and in_imi:
                    frag = re.split(r"(Głosowanie zakończono|Wygenerowano|Głosowanie z dnia|Wyniki głosowania)", ln, 1, re.I)[0].strip()
                    if frag and not re.match(r"^\(?\d{1,2}[\.\)]?\s*\*?$", frag):
                        pending[cur].append(frag)
                break
            if cur is None:
                continue
            ln = ln.strip()
            if not ln or ln in ("@@PAGE@@"):
                continue
            lk = _nk(ln)
            if (lk.startswith("wygenerowan") or "|" in ln or
                re.match(r"^\(?\d{1,2}[\.\)]?\s*\*?$", ln) or
                lk.startswith("strona")):
                continue
            pending[cur].append(ln)
        # przetwórz zebrane linie -> nazwiska
        for cat, raw_lines in pending.items():
            text = " ".join(raw_lines)
            for part in re.split(r",\s*", text):
                part = part.strip(" .")
                if not part or re.match(r"^\(?\d+\)?\s*$", part) or _nk(part) in ("sie", "sier"):
                    continue
                named[cat].append(part)
        if agg or named:
            votes.append({"topic": topic, "agg": agg, "named": {k: v for k, v in named.items()}})
    return votes

# ---------------- roster ---------------
def build_roster(sessions_pages, votes_all):
    """zbuduj kanoniczny rejestr: surname->lista display.
    Źródło 1: nazwy z głosowań (''Imię Nazwisko'', czyste — PRIMARY).
    Źródło 2: LISTA OBECNOŚCI (NAZWISKO Imię, str.0) — supplementary (kolumna podpisu czasem
    zanieczyszcza linię, więc attendance samo nie wystarcza).
    Szum OCR normalizowany TUTAJ: _NAME_ALIAS (zweryfikowane warianty -> prawidłowy radny) oraz
    surname.title() (zgadza 'KATARZYNA KOZŁOWSKA'/'Kozłowska', odrzuca 'k'/'Imie').
    Duplikaty po nazwisku (np. Anita i Pelagia Ochwat) trzymane osobno, rozróżniane po imieniu."""
    bysur = defaultdict(list)
    def add(display):
        if not display:
            return
        alias = _NAME_ALIAS.get(display)
        if alias:
            display = alias
        toks = display.split()
        if len(toks) < 2:
            return
        surname = toks[-1]
        if not re.fullmatch(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż-]+", surname):
            return
        display = f"{toks[0]} {surname.title()}"
        key = _nk(surname)
        # unikaj duplikatów dokładnie tego samego display
        if all(_nk(d) != _nk(display) for d in bysur[key]):
            bysur[key].append(display)
    # contrib 2: attendance "NAZWISKO Imię" -> "Imię Nazwisko"
    for pages in sessions_pages.values():
        txt = pages.get(0, "")
        for ln in txt.split("\n"):
            m = re.match(r"^\s*\d{1,2}[\.\)]?\s+([A-ZĄĆĘŁŃÓŚŹŻ][A-ZĄĆĘŁŃÓŚŹŻ-]+)\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)\s*$", ln)
            if m:
                add(f"{m.group(2)} {m.group(1).title()}")
    # contrib 1 (primary): nazwy z głosowań
    for v in votes_all:
        for names in v["named"].values():
            for nm in names:
                t = nm.strip().strip(",")
                if not t or len(t.split()) < 2:
                    continue
                add(t)
    return dict(bysur)

def map_name(raw, bysur):
    """Mapuj surowe 'Imię Nazwisko' z OCR na kanoniczne display. Zwraca display lub None."""
    raw = raw.strip().strip(",")
    if not raw:
        return None
    canon = _NAME_ALIAS.get(raw)
    if canon:
        return canon
    toks = raw.split()
    surname = _nk(toks[-1])
    cands = bysur.get(surname)
    if not cands:
        # OCR gubi końcówkę nazwiska — szukaj prefiksu
        for key, clist in bysur.items():
            if _nk(clist[0].split()[-1]).startswith(surname) or surname.startswith(_nk(clist[0].split()[-1])):
                cands = clist
                break
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    # wiele osób o tym samym nazwisku — rozróżnij po imieniu (pierwszy token)
    given = _nk(toks[0])
    for d in cands:
        if _nk(d.split()[0]) == given:
            return d
    return cands[0]

def parse_rollcall(pages, bysur):
    """Zwraca (records, warns) - records = {topic, named:{cat:[display]}}, warns=list str."""
    # pages moze byc dict {nr: text} albo list; parse_document oczekuje listy w kolejnosci
    if isinstance(pages, dict):
        pages = [pages[k] for k in sorted(pages.keys())]
    votes = parse_document(pages)
    warns = []
    records = []
    for v in votes:
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": [], "brak_glosu": []}
        ok = True
        for cat, names in v["named"].items():
            for raw in names:
                disp = map_name(raw, bysur)
                if disp:
                    if disp not in named[cat]:
                        named[cat].append(disp)
                else:
                    ok = False
                    warns.append(f"UNMAPPED[{cat}]: {raw!r}")
        if not ok:
            warns.append(f"  topic: {v['topic'][:70]}")
        # walidacja licznikow: imienne vs header agregat (gdzie czytelny)
        cnt = {c: len(named[c]) for c in named}
        agg = v.get("agg", {})
        for c, key in (("za", "za"), ("przeciw", "przeciw"), ("wstrzymal_sie", "wstrzymal_sie"),
                       ("nieobecni", "nieobecni"), ("brak_glosu", "brak_glosu")):
            if key in agg and agg[key] != cnt[c]:
                warns.append(f"COUNT-MISMATCH {c}: header={agg[key]} imienne={cnt[c]} topic={v['topic'][:50]!r}")
        records.append({"topic": v["topic"], "named": named})
    return records, warns

# ---------------- output (wzorowane na goleniow) ---------------
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
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""), "vote_count": 0, "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]), "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat == "nieobecni":
                for nm in names:
                    if nm in councilors_data:
                        councilors_data[nm]["votes_nieobecny"] += 1
                continue
            if cat == "brak_glosu":
                for nm in names:
                    if nm in councilors_data:
                        councilors_data[nm]["votes_brak"] += 1
                continue
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "za": councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw": councilors_data[nm]["votes_przeciw"] += 1
                else: councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"], "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"], "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    from itertools import combinations
    pairs = []; names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "", "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}, total_votes, total_sessions

def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "brak_glosu": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1), "aktywnosc": round(aktywn, 1),
                "zgodnosc_z_klubem": 0.0, "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak_glosu"], "votes_nieobecny": 0,
                "votes_total": total, "rebellion_count": 0, "rebellions": [], "roles": [],
                "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", default=".")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--no-ocr", action="store_true", help="JSON-owo pisze tylko, bez pobierania/OCR")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    global _cache_dir
    _cache_dir = args.cache_dir or str(city_dir / "ocr_cache")
    out_docs = city_dir / "docs"; out_docs.mkdir(parents=True, exist_ok=True)
    pdf_dir = city_dir / "pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)

    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    if args.no_ocr:
        # użyj wcześniej cichego OCR z cache
        pass

    print("[strzelce] discover sessions ...")
    sessions = discover_sessions()
    sessions.sort(key=lambda s: s["date"])
    print(f"[strzelce] {len(sessions)} sesji IX kad. (z załącznikiem do sprawdzenia)")
    all_warns = []
    session_pages = {}
    records = []
    for se in sessions:
        rc = get_rollcall_url(se)
        if not rc:
            print(f"  [no-rollcall] {se['date']} {se['title'][:50]}")
            continue
        dl_url, dl_txt = rc
        fileid = re.search(r"/download/(\d+)\.html", dl_url).group(1)
        pdf_path = pdf_dir / f"{se['idn']}_{fileid}.pdf"
        if not pdf_path.is_file():
            data = _get_bytes(dl_url)
            pdf_path.write_bytes(data)
            print(f"  [downloaded] {se['date']} {fileid} {len(data)}B")
        try:
            pages = ocr_pdf(pdf_path, se["idn"])
            session_pages[se["idn"]] = pages
        except Exception as e:
            print(f"  [ERR ocr {se['date']}] {e}")
            continue
        time.sleep(0.3)

    # roster z danych
    votes_all_incl = []
    for idn, pages in session_pages.items():
        vv = parse_document(list(pages.values()))
        for v in vv:
            votes_all_incl.append(v)
    bysur = build_roster(session_pages, votes_all_incl)
    print(f"[strzelce] roster: {len(bysur)} nazwisk")

    # przypisz sesje -> records
    for se in sessions:
        if se["idn"] not in session_pages:
            continue
        pages = session_pages[se["idn"]]
        recs, warns = parse_rollcall(pages, bysur)
        for w in warns:
            all_warns.append((se["date"], se["idn"], w))
        for r in recs:
            r["date"] = se["date"]
            r["num"] = _roman_num(se["title"])
        records += recs
        print(f"  [ok] {se['date']} idn={se['idn']} votes={len(recs)}")

    # finałowa kanonizacja nazw (merga warianty OCR do prawidłowych radnych)
    rename_hits = 0
    for rec in records:
        for cat, names in rec["named"].items():
            newn = list(dict.fromkeys(_RENAME.get(n, n) for n in names))
            if newn != names:
                rename_hits += 1
            rec["named"][cat] = newn
    if rename_hits:
        print(f"[strzelce] rename: {rename_hits} kategorii z mergowanymi wariantami OCR")

    # odrzuć głosowania bez danych imiennych (brak naprawialnych list ZA/PRZECIW/WSTRZYMUJĘ —
    # np. zepsute bloki 'Wyniki imienne' na skanach); do Radoskopa liczą się tylko głosowania z danymi
    n_drop = sum(1 for rec in records if not (rec["named"].get("za") or rec["named"].get("przeciw") or rec["named"].get("wstrzymal_sie")))
    records = [rec for rec in records if rec["named"].get("za") or rec["named"].get("przeciw") or rec["named"].get("wstrzymal_sie")]
    if n_drop:
        print(f"[strzelce] odrzucam {n_drop} głosowań bez danych imiennych")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    (out_docs / "kadencja-2024-2029.json").write_text(json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID, "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (out_docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[strzelce] DONE votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])}")
    print(f"  warnings: {len(all_warns)}")
    for w in all_warns[:60]:
        print("   ", w)
    # statystyka
    n_unmapped = sum(1 for w in all_warns if "UNMAPPED" in w)
    print(f"  (UNMAPPED:{n_unmapped}, COUNT-MISMATCH:{sum(1 for w in all_warns if 'COUNT-MISMATCH' in w)})")
    # zapisz sesje listę
    (city_dir / "sessions_used.json").write_text(json.dumps([{"idn": s["idn"], "date": s["date"], "title": s["title"]} for s in sessions], ensure_ascii=False, indent=1), encoding="utf-8")

_ROM = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
        "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,"XIX":19,
        "XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,"XXVII":27,
        "XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33,"XXXIV":34,"XXXV":35,"XXXVI":36}
def _roman_num(title):
    m = re.search(r"^\s*([XLIVC]+)\s+Sesja", title)
    return _ROM.get(m.group(1), "") if m else ""

if __name__ == "__main__":
    main()
