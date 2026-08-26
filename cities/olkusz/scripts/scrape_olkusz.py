#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Olkusz — imienne głosowania Rady Miejskiej.

Źródło: BIP Urzędu Miasta i Gminy w Olkuszu na platformie bip.malopolska.pl
(Madkom SPA, encja "umigolkusz"). Rada Miejska (IX kadencja 2024-2029, 21
radnych) publikuje sesje w kategoriach Rada -> Sesje -> Protokoły z sesji Rady
-> 2024/2025/2026. Każdy artykuł-protokół ma załącznik "Głosowania z {ROMAN}
sesji..." (PDF lub DOCX) z wynikami głosowań IMiennych (za/przeciw/wstrzymuje
się per radny + nieobecni).

DWA formaty załączników:
  * format S (PDF, przeważnie zeskanowany, bez warstwy tekstowej):
      "Wyniki głosowania / Głosowano w sprawie: {temat} /
       ZA: A, PRZECIW: B, WSTRZYMUJĘ SIĘ: C, BRAK GŁOSU: D, NIEOBECNI: E /
       Wyniki imienne: / ZA (A) / {imiona przecinkowo} / ... /
       Głosowanie zakończono..."
      -> wymaga OCR (tesseract -l pol).
  * format T (DOCX lub PDF tekstowy "Raport z głosowań"):
      "Rada Miejska w Olkuszu / Raport z głosowań / {ROMAN} Sesja w dniu ...
       / Przeprowadzone głosowania /
       N. Głosowanie w sprawie {temat}. - czas głosowania: ..., wyniki:
       ZA: A, PRZECIW: B, WSTRZYMUJĘ SIĘ: C, BRAK GŁOSU: D, NIEOBECNI: E
       / Wyniki imienne: {Nazwisko} (ZA), {Nazwisko} (PRZECIW), ..."
      -> parsuj tekst wprost.

Walidacja: dla KAŻDEGO głosowania liczba imion w ZA+PRZECIW+WSTRZYMUJĘ SIĘ+
BRAK GŁOSU+NIEOBECNI musi zgadzać się z agregatem (i = 21 radnych). Rekordy
niespełniające walidacji są odrzucane i liczone (pokrycie raportowane).

Skład Rady, kluby i role pochodzą z BIP (kategorie "Osoby i funkcje" oraz
"Kluby Radnych") — dane realne, nie fabrykowane.

Użycie:
    python scrape_olkusz.py --output docs/data.json --profiles docs/profiles.json
        [--cache-dir DIR] [--regen]
"""

import argparse, hashlib, io, json, re, subprocess, sys, time, unicodedata, zipfile, html as _html
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API = "https://bip.malopolska.pl/api"
CONTEXT = "umigolkusz"
# kategorie protokołów z sesji Rady: 2024 / 2025 / 2026
PROTO_CATS = ["432629", "461740", "471603"]
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
REQ_DELAY = 0.7
_LAST_REQ = 0.0

# Kanoniczny skład radnych IX kadencji — z BIP "Osoby i funkcje" + zgodny z
# wszystkimi kartami głosowań (realne źródło).
COUNCILORS = [
    'Anna Kwaśniewska','Apolinary Ćwięczek','Grzegorz Gruca','Henryk Gamrat',
    'Julita Mikucka','Kamil Czopek','Katarzyna Kamionka','Małgorzata Postołek',
    'Maria Beszterdo','Mariusz Gaszczyk','Michał Zasucha','Paweł Piasny',
    'Paulina Polak','Piotr Grabarczyk','Piotr Ziarnik','Renata Jurczyk',
    'Sebastian Tomsia','Tomasz Babiuch','Tomasz Witecki','Wojciech Panek',
    'Zbigniew Stach',
]
_COUNC = set(COUNCILORS)

# Kluby z BIP (kategoria "Kluby Radnych") + niezrzeszeni.
CLUB = {
    **{n: "SW" for n in ['Apolinary Ćwięczek','Tomasz Babiuch','Henryk Gamrat','Grzegorz Gruca','Katarzyna Kamionka']},
    **{n: "PP" for n in ['Sebastian Tomsia','Paulina Polak','Mariusz Gaszczyk','Piotr Ziarnik']},
    **{n: "BP" for n in ['Paweł Piasny','Małgorzata Postołek','Tomasz Witecki']},
    **{n: "PiS" for n in ['Michał Zasucha','Maria Beszterdo','Kamil Czopek','Zbigniew Stach','Renata Jurczyk','Wojciech Panek']},
    **{n: "NZ" for n in ['Anna Kwaśniewska','Julita Mikucka','Piotr Grabarczyk']},
}

_MONTHS_PL = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,
              "lipca":7,"sierpnia":8,"września":9,"października":10,"listopada":11,"grudnia":12}

_SLUG_REPL = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}

def make_slug(name):
    slug = name.lower()
    for pl, a in _SLUG_REPL.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)

def norm_name(s):
    """Normalizuje nazwisko (OCR noise-tolerant) do postaci kanonicznej z listy."""
    s = s.strip().strip(',').strip()
    s = re.sub(r'\s+', ' ', s)
    # popraw OCR-owe przekłamania typowe (O vs 0 nie dotyczy nazwisk)
    s = re.sub(r'\s*-\s*', '-', s)
    low = s.lower()
    for c0, c1 in [('ł','l'),('ę','e'),('ś','s'),('ą','a'),('ć','c'),('ó','o'),('ń','n'),('ź','z'),('ż','z')]:
        low = low.replace(c0, c1)
    low = re.sub(r'[^a-z -]', '', low)
    low = re.sub(r'\s+', ' ', low).strip()
    # mapuj na kanoniczną listę
    for c in _COUNC:
        cl = c.lower()
        for a,b in [('ł','l'),('ę','e'),('ś','s'),('ą','a'),('ć','c'),('ó','o'),('ń','n'),('ź','z'),('ż','z')]:
            cl = cl.replace(a,b)
        cl = re.sub(r'[^a-z -]', '', cl)
        cl = re.sub(r'\s+', ' ', cl).strip()
        if cl == low:
            return c
    return s  # nieznane — zostaw oryginał

def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()

def _fetch_json(url, cache_dir=None):
    if cache_dir:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = cache_dir / (key + ".json")
        if cf.is_file():
            return json.loads(cf.read_text(encoding="utf-8"))
    _rate()
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 Radoskop/1.0", "Accept": "application/json"}, timeout=90, verify=False)
    r.raise_for_status()
    j = r.json()
    if cache_dir:
        cf = cache_dir / (key + ".json"); cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
    return j

def _fetch_bin(url, cache_dir=None):
    if cache_dir:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = cache_dir / (key + ".bin")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 Radoskop/1.0"}, timeout=120, verify=False)
    r.raise_for_status()
    data = r.content
    if cache_dir:
        cf = cache_dir / (key + ".bin"); cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_bytes(data)
    return data

# --------------------------------------------------------------------------
# 1. Sesje IX kadencji z kategorii protokołów
# --------------------------------------------------------------------------
_ROMAN = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
def _roman_to_int(roman):
    n = 0; prev = 0
    for ch in reversed(roman.upper()):
        v = _ROMAN.get(ch, 0)
        n += -v if v < prev else v
        prev = v
    return n

_SESSION_RE = re.compile(
    r'\b([IVXLCDM]{1,7})\s+sesj\w*\s+Rady\s+Miejskiej[^.]*?(?:w\s+dniu|z\s+dnia)\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})',
    re.IGNORECASE)

def _parse_title(title):
    for m in _SESSION_RE.finditer(title):
        roman, dd, mon, yyyy = m.group(1), int(m.group(2)), m.group(3).lower(), m.group(4)
        mm = _MONTHS_PL.get(mon)
        if mm:
            return roman, f"{yyyy}-{mm:02d}-{dd:02d}"
    return None

def collect_sessions(cache_dir=None):
    """Zwraca sesje IX kadencji (date, roman, article_id, att_id, att_name, att_ext)."""
    sess = {}
    for cat in PROTO_CATS:
        url = f"{API}/menu/{cat}/articles?limit=200&offset=0"
        try:
            j = _fetch_json(url, cache_dir)
        except Exception as e:
            print(f"  [warn] kategoria {cat}: {e}")
            continue
        for it in (j.get("articles") or []):
            aid = it.get("id")
            title = _title_of(it)
            parsed = _parse_title(title)
            if not parsed:
                continue
            roman, date = parsed
            if date < KAD_START:
                continue
            sess[aid] = {"date": date, "roman": roman, "num": _roman_to_int(roman), "article": aid, "title": title}
    # pobierz załączniki głosowań dla każdej sesji
    out = []
    for aid, s in sess.items():
        try:
            a = _fetch_json(f"{API}/articles/{aid}", cache_dir)
        except Exception as e:
            print(f"  [warn] article {aid}: {e}")
            continue
        att = _pick_glosowanie(a.get("attachments") or [])
        if not att:
            print(f"  [warn] {s['roman']} {s['date']}: brak załącznika głosowań")
            continue
        s["att_id"] = att.get("id"); s["att_name"] = att.get("name")
        s["att_ext"] = (att.get("extension") or "").lower()
        out.append(s)
    out.sort(key=lambda s: (s["date"], s["num"]))
    return out

def _title_of(it):
    al = it.get("aliasFields") or []
    for f in al:
        if f.get("alias") == "title":
            return f.get("value") or ""
    cf = it.get("columnFields") or []
    for f in cf:
        if f.get("fieldId") == "title":
            return f.get("value") or ""
    return it.get("title") or ""

def _pick_glosowanie(atts):
    cand = [a for a in atts
            if re.search(r'osow|imienne|wyniki', (a.get("name") or "").lower())
            and not re.search(r'protokol', (a.get("name") or "").lower())]
    if not cand:
        cand = [a for a in atts if not re.search(r'protokol', (a.get("name") or "").lower())]
    cand.sort(key=lambda a: -(a.get("size") or 0))
    return cand[0] if cand else None

# --------------------------------------------------------------------------
# 2. Wyciąganie tekstu z załącznika (PDF / DOCX / PDF-skan -> OCR)
# --------------------------------------------------------------------------
def _docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "word/document.xml" in z.namelist():
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        else:
            return ""
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'<[^>]+>', '', xml)
    return _html.unescape(xml)

def _pdf_text_pages(data):
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for p in pdf.pages:
            pages.append(p.extract_text() or "")
    return pages

def _pdf_ocr_pages(data, dpi=180, cache_dir=None, name=""):
    """OCR zeskanowanego PDF-a. Zwraca listę tekstów stron."""
    key = hashlib.md5((name or "x").encode()).hexdigest()
    store = None
    if cache_dir:
        store = cache_dir / ("ocr_" + key + ".txt")
        if store.is_file():
            return store.read_text(encoding="utf-8").split("\n@@PAGE@@\n")
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i in range(len(pdf.pages)):
            im = pdf.pages[i].to_image(resolution=dpi)
            png = cache_dir / f"_ocr_{key}_{i}.png" if cache_dir else Path(f"/tmp/_ocr_{key}_{i}.png")
            png.parent.mkdir(parents=True, exist_ok=True)
            im.save(str(png))
            r = subprocess.run(["tesseract", str(png), "-", "-l", "pol", "--psm", "4"],
                               capture_output=True, text=True)
            pages.append(r.stdout)
            if cache_dir:
                png.unlink(missing_ok=True)
    if cache_dir:
        store.write_text("\n@@PAGE@@\n".join(pages), encoding="utf-8")
    return pages

def _extract_text(data, ext, cache_dir=None, name=""):
    """Zwraca (tekst, czy_zeskanowano)."""
    if ext == "docx":
        return _docx_text(data), False
    pages = _pdf_text_pages(data)
    joined = "\n".join(pages)
    if joined.strip():
        # tekstowy PDF (Raport z głosowań)
        return joined, False
    # skan -> OCR
    ocr_pages = _pdf_ocr_pages(data, cache_dir=cache_dir, name=name)
    return "\n".join(ocr_pages), True

# --------------------------------------------------------------------------
# 3. Parsowanie głosowań
# --------------------------------------------------------------------------
_AGGR = re.compile(r'ZA\s*[:]\s*(\d+)\s*,\s*PRZECIW\s*[:]\s*(\d+)\s*,\s*WSTRZYM[^,:]{0,20}?[:]\s*(\d+)\s*,\s*BRAK\s+GŁOSU\s*[:]\s*(\d+)\s*,\s*NIEOBECNI\s*[:]\s*(\d+)')
_AGGR2 = re.compile(r'ZA\s*[:]\s*(\d+)\s*[|]\s*przeciw\s*[:]\s*(\d+)\s*[|]\s*wstrzym\w*\s*[:]\s*(\d+)', re.I)

def _clean_num(x):
    return int(re.sub(r'[Oo]', '0', x))

# normalizacja do ASCII dla odpornego dopasowania nazwisk (OCR-noise tolerant)
def _norm(s):
    s = s.lower()
    for a, b in [('ł','l'),('ę','e'),('ś','s'),('ą','a'),('ć','c'),('ó','o'),('ń','n'),('ź','z'),('ż','z')]:
        s = s.replace(a, b)
    s = re.sub(r'[^a-z]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

# alternacja wszystkich 21 radnych (długie pierwsze) — do wykrywania imion w tekście sekcji
_COUNC_ALT = "|".join(sorted((re.escape(_norm(c)) for c in COUNCILORS), key=len, reverse=True))

def _section_members(body):
    """Zwraca obecnych w `body` radnych (dopasowanie kanoniczne, odporne na
    złamania linii bez przecinka / śmieciowe znaki OCR)."""
    bn = _norm(body)
    out = []
    used = set()
    for vm in re.finditer(r'\b(?:' + _COUNC_ALT + r')\b', bn):
        nm = next(c for c in COUNCILORS if _norm(c) == vm.group(0))
        if nm not in used:
            used.add(nm); out.append(nm)
    return out

def _parse_imienne_block(lines, agg_za, agg_pc, agg_ws, agg_br, agg_ne):
    """Parsuje sekcję imienną 'ZA (N)/names... NIEOBECNI (N)/names' z listy linii."""
    cat_headers = {
        'za': re.compile(r'^ZA\s*\(\s*(\d+)\s*\)'),
        'przeciw': re.compile(r'^PRZECIW\s*\(\s*(\d+)\s*\)'),
        'wstrzymal_sie': re.compile(r'^WSTRZYM[^\n(]*\(\s*(\d+)\s*\)'),
        'brak': re.compile(r'^BRAK\s+GŁOSU\s*\(\s*(\d+)\s*\)'),
        'nieobecni': re.compile(r'^NIEOBECNI\s*\(\s*(\d+)\s*\)'),
    }
    cur = None
    collect = {k: [] for k in cat_headers}
    for raw in lines:
        l = raw.strip()
        if not l:
            continue
        matched = None
        for k, rx in cat_headers.items():
            if rx.match(l):
                cur = k
                matched = True
                break
        if matched:
            continue
        if cur is None:
            continue
        # skończ sekcję na "Głosowanie zakończono"/"Wyniki głosowania"
        if l.lower().startswith("głosowanie zakończono") or l.startswith("Wyniki głosowania"):
            break
        collect[cur].append(l)
    names = {k: _section_members(" ".join(collect[k])) for k in cat_headers}
    # walidacja — Radoskop liczy trzy kategorie głosowania (za/przeciw/wstrzymuję);
    # NIEOBECNI/BRAK GŁOSU to wolne dane frekwencyjne, pomijamy je w walidacji.
    ok = (len(names['za']) == agg_za and len(names['przeciw']) == agg_pc
          and len(names['wstrzymal_sie']) == agg_ws)
    return {'za': names['za'], 'przeciw': names['przeciw'], 'wstrzymal_sie': names['wstrzymal_sie']}, ok

def _parse_format_s(text, session_date, session_num):
    """PDF-skan: bloki 'Wyniki głosowania / Głosowano w sprawie...'."""
    votes = []
    # podziel po markerze "Wyniki głosowania" (każde głosowanie ma dokładnie jeden)
    blocks = re.split(r'(?m)^\s*Wyniki\s+głosowania\s*$', text)
    for bi in range(1, len(blocks)):
        b = blocks[bi]
        m = _AGGR.search(b.replace('STYZYMUJĄCY','WSTRZYMUJĘ').replace('STYZYMUJ','WSTRZYM'))
        if not m:
            continue
        agg = [_clean_num(x) for x in m.groups()]
        agg_za, agg_pc, agg_ws, agg_br, agg_ne = agg
        lines = b.split('\n')
        # temat: od "Głosowano w sprawie" do agregatu
        tl = []
        in_topic = False
        for l in lines:
            if 'Głosowano w sprawie' in l:
                in_topic = True
            if _AGGR.search(l.replace('STYZYMUJĄCY','WSTRZYMUJĘ').replace('STYZYMUJ','WSTRZYM')):
                break
            if in_topic:
                tl.append(l)
        topic = re.sub(r'\s+', ' ', ' '.join(tl)).replace('Głosowano w sprawie:', '').strip().strip(':').strip()
        named, ok = _parse_imienne_block(lines, agg_za, agg_pc, agg_ws, agg_br, agg_ne)
        if not ok:
            print(f"    [warn]{session_num} {session_date} skan: sums za{len(named['za'])}/{agg_za} pc{len(named['przeciw'])}/{agg_pc} ws{len(named['wstrzymal_sie'])}/{agg_ws} nieob{agg_ne} — pominięto")
            continue
        votes.append({"session_date": session_date, "session_num": session_num,
                      "topic": topic, "named": named})
    return votes

def _parse_format_t(text, session_date, session_num):
    """DOCX/PDF-tekst 'Raport z głosowań': 'Wyniki imienne: N (ZA), N2 (PRZECIW), ...'"""
    votes = []
    # bloki zaczynają się od (opcjonalnie numerowanego) "Głosowanie w sprawie"
    matches = list(re.finditer(r'(?m)^(?:\d+[.)]\s*)?Głosowanie w sprawie\s*', text))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        chunk = text[start:end]
        # agregat
        ag = _AGGR.search(chunk.replace('STYZYMUJĄCY','WSTRZYMUJĘ').replace('STYZYMUJ','WSTRZYM'))
        if not ag:
            continue
        agg = [_clean_num(x) for x in ag.groups()]
        agg_za, agg_pc, agg_ws, agg_br, agg_ne = agg
        # imienne: "Nazwisko (VOTE)"
        named = {'za': [], 'przeciw': [], 'wstrzymal_sie': []}
        im = re.search(r'Wyniki\s*imienne\s*[:](.*)$', chunk, re.S)
        if im:
            # zwijamy białe znaki do spacji — nazwisko potrafi łamać się na granicy
            # linii ("Renata\nJurczyk (ZA)") i wtedy bez tego regex łapie samo "Jurczyk"
            body = re.sub(r'\s+', ' ', im.group(1))
            for vm in re.finditer(r'([A-ZĄĆĘŁŃÓŚŹŻ][\w\- ]+?)\s*\(\s*(?:ZA|PRZECIW|WSTRZYM[^)]*?)\s*\)', body):
                nm = norm_name(vm.group(1))
                v = body[vm.start():vm.end()]
                v = v[v.find('(')+1:v.find(')')].upper()
                if nm not in _COUNC:
                    continue
                if v == 'ZA':
                    named['za'].append(nm)
                elif 'PRZECIW' in v:
                    named['przeciw'].append(nm)
                elif 'STRZYM' in v:
                    named['wstrzymal_sie'].append(nm)
            # dedupe (w razie powtórzeń)
            named = {k: list(dict.fromkeys(v)) for k, v in named.items()}
        ok = (len(named['za']) == agg_za and len(named['przeciw']) == agg_pc
              and len(named['wstrzymal_sie']) == agg_ws)
        if not ok:
            print(f"    [warn]{session_num} {session_date} tekst: sums za{len(named['za'])}/{agg_za} pc{len(named['przeciw'])}/{agg_pc} ws{len(named['wstrzymal_sie'])}/{agg_ws} — pominięto")
            continue
        tm = re.search(r'Głosowanie\s+w\s+sprawie\s*(.*?)\s*[-–]\s*czas\s+głosowania', chunk, re.S)
        topic = (re.sub(r'\s+', ' ', tm.group(1)).strip().strip('.') if tm else '')
        votes.append({"session_date": session_date, "session_num": session_num,
                      "topic": topic, "named": named})
    return votes

def parse_attachment(data, ext, session_date, session_num, cache_dir=None, name=""):
    text, scanned = _extract_text(data, ext, cache_dir=cache_dir, name=name)
    if not scanned or 'Wyniki głosowania' not in text:
        # spróbuj formatu T (tekstowy) — DOCX/tekst-PDF, ale też skan zawiera "Raport z głosowań"? nie
        if 'Wyniki imienne' in text and re.search(r'\(\s*ZA\s*\)', text):
            v = _parse_format_t(text, session_date, session_num)
            if v:
                return v
    # format S (zeskanowany)
    if 'Wyniki głosowania' in text:
        return _parse_format_s(text, session_date, session_num)
    # ostatecznie format T
    return _parse_format_t(text, session_date, session_num)

# --------------------------------------------------------------------------
# 4. Budowa danych Radoskop (wzorzec jak skierniewice/konin)
# --------------------------------------------------------------------------
def _club_of(name):
    return CLUB.get(name, "")

def _compute_consensus(all_votes):
    stats = defaultdict(lambda: {"za":0,"przeciw":0,"wstrzymal":0,"brak":0,"nieobecny":0,
                                 "with":0,"against":0,"sess":set()})
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                key = "za" if cat=="za" else "przeciw" if cat=="przeciw" else "wstrzymal"
                stats[name][key] += 1
                stats[name]["sess"].add(v["session_date"])
    return stats

def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num",""),
                                   "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za","przeciw","wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d,
                          "session_number": rec.get("session_num",""),
                          "topic": rec.get("topic","") or "",
                          "named_votes": named,
                          "counts": {k: len(named.get(k,[])) for k in ("za","przeciw","wstrzymal_sie")}})
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
    for name in sorted(all_names):
        councilors_data[name] = {"name": name, "club": _club_of(name), "district": None,
            "votes_za":0,"votes_przeciw":0,"votes_wstrzymal":0,"votes_brak":0,"votes_nieobecny":0,
            "votes_with_club":0,"votes_against_club":0,"rebellions":[]}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                c = councilors_data.get(name)
                if not c: continue
                if cat=="za": c["votes_za"]+=1
                elif cat=="przeciw": c["votes_przeciw"]+=1
                else: c["votes_wstrzymal"]+=1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    stats = _compute_consensus(all_votes)
    councilors_list = []
    for name in sorted(councilors_data.keys()):
        c = councilors_data[name]
        present = c["votes_za"]+c["votes_przeciw"]+c["votes_wstrzymal"]
        aktywnosc = (present/total_votes*100) if total_votes else 0
        st = stats[name]
        frekwencja = (len(st["sess"])/total_sessions*100) if total_sessions else 0
        dec = st["with"]+st["against"]
        zgodnosc = (st["with"]/dec*100) if dec else 0.0
        councilors_list.append({"name": name, "club": c["club"], "district": None,
            "frekwencja": round(frekwencja,1), "aktywnosc": round(aktywnosc,1),
            "zgodnosc_z_klubem": round(zgodnosc,1),
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    global NAME_AGG, _all_session_dates
    NAME_AGG = {name: dict(stats[name], sess=len(stats[name]["sess"])) for name in stats}
    _all_session_dates = [s["date"] for s in sessions_data]
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za","przeciw","wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same/len(common)*100,1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}

NAME_AGG = {}
_all_session_dates = []

def build_profiles(records):
    cv = defaultdict(lambda: {"za":0,"przeciw":0,"wstrzymal_sie":0,"sess":set()})
    for rec in records:
        d = rec.get("session_date")
        if not d: continue
        for cat, names in rec["named"].items():
            for name in names:
                key = "za" if cat=="za" else "przeciw" if cat=="przeciw" else "wstrzymal_sie"
                cv[name][key] += 1
                cv[name]["sess"].add(d)
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za","przeciw","wstrzymal_sie"))
        agg = NAME_AGG.get(name, {})
        frekw = 100.0*len(vd["sess"])/len(_all_session_dates) if _all_session_dates else 0.0
        dec = agg.get("with",0)+agg.get("against",0)
        zgod = 100.0*agg.get("with",0)/dec if dec else 0.0
        profiles.append({"name": name, "slug": make_slug(name),
            "kadencje": {KADENCJA_ID: {
                "club": _club_of(name), "has_voting_data": True, "has_activity_data": False,
                "frekwencja": round(frekw,1),
                "aktywnosc": round(float(vd["za"]+vd["przeciw"]+vd["wstrzymal_sie"])/total*100,1) if total else 0.0,
                "zgodnosc_z_klubem": round(zgod,1),
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}}),
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
    index = {"generated": output.get("generated",""), "default_kadencja": output.get("default_kadencja",""),
             "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    print("=== Scraper Rady Miejskiej w Olkuszu (bip.malopolska.pl /umigolkusz) ===")
    sessions = collect_sessions(cache_dir)
    print(f"  Sesji IX kadencji: {len(sessions)}")
    if not sessions:
        print("  BRAK SESJI — koniec."); sys.exit(1)
    records = []
    drops = 0
    for s in sessions:
        try:
            data = _fetch_bin(f"{API}/files/{s['att_id']}", cache_dir)
        except Exception as e:
            print(f"  [warn] {s['roman']} {s['date']}: fetch {e}")
            continue
        vs = parse_attachment(data, s["att_ext"], s["date"], s["roman"],
                              cache_dir=cache_dir, name=f"{s['roman']}/{s['date']}")
        if not vs:
            print(f"  [warn] {s['roman']:5s} {s['date']}: 0 głosowań")
        else:
            print(f"  {s['roman']:5s} {s['date']}: {len(vs)} głosowań")
        records.extend(vs)
    print(f"  Razem głosowań (zwalidowanych): {len(records)}")
    if not records:
        print("  BRAK DANYCH"); sys.exit(1)
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    t = output["kadencje"][0]
    print(f"  Zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")
    print(f"  Sesji: {t['total_sessions']}, głosowań: {t['total_votes']}, radnych: {t['total_councilors']}")

if __name__ == "__main__":
    main()
