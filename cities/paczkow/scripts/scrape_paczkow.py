#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Paczków — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych: BIP paczkow.bip.net.pl (legacy Sputnik Software)
publikuje protokoły sesji jako PDF-y tekstowe z WYŁĄCZNIE agregatami
("w głosowaniu udział wzięło 13 radnych przy 13 głosach za..."); wykazy
imienny ("wykaz imienny głosujących zał. nr N") NIE jest publikowany
osobno — brak kategorii Wyniki głosowań w BIP, paczkow.esesja.pl =
wildcard dead, rada.paczkow.pl = HTTP 500/404 (AlfaTV nieaktywne).

Źródła Tier-2 (BIP Sputnik, nawigacja ?c=/:
  - skład rady: kategoria ?c=223 (Rada Miejska » Skład), artykuł a=11927
    "Skład Rady Miejskiej w Paczkowie kadencji 2024 - 2029" (15 radnych, role)
  - kalendarz sesji: ?c=227 Protokoły z sesji » Kadencja 2024-2029
    (?c=1374) » kategorie roczne (2024=1375, 2025=1409, 2026=1467);
    artykuły "Protokół nr XXX/RRRR" — data sesji z PDF-a protokołu
    ("odbytej w dniu D month YYYY r.") — pobieramy daty z nagłówków
    ostatniego protokołu każdego roku + listy tytułów (numery rzymskie).
has_voting_data:false.
"""
import io
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

BIP = "https://paczkow.bip.net.pl"
ROSTER_ART = 11927
PROTO_ROOT = 1374            # Protokoły z sesji » Kadencja 2024-2029
KAD_START = "2024-05-07"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0"}
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}
ROMAN_MAP = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
ROMAN_RE = re.compile(r"^(?=[IVXLCDM]+$)M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


def _http(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read()


def _roman_to_int(s: str):
    s = s.upper()
    if not s or not ROMAN_RE.match(s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        v = ROMAN_MAP.get(ch)
        if v is None:
            return None
        if v < prev:
            total -= v
        else:
            total += v
        prev = max(prev, v)
    return total


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def _norm_name(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw).strip(" .,-–")
    # entries appear as "Barabasz Wiesław Jan" or "Kamila Dróżdż" or
    # "Janik Stanisław" — normalize to First Last... (heuristic: if the
    # first token looks like a surname-only style we keep source order;
    # Radoskop only needs a stable unique name)
    return raw


def fetch_roster() -> list[dict]:
    import html as _html
    t = _html.unescape(_http(f"{BIP}/?a={ROSTER_ART}").decode("utf-8", "replace"))
    i = t.find("Skład Rady Miejskiej w Paczkowie kadencji 2024 - 2029")
    j = t.find("Metadane", i)
    seg = t[i:j] if 0 <= i < j else t[i:i + 6000]
    text = re.sub(r"<[^>]+>", " ", seg)
    text = re.sub(r"\s+", " ", text)
    # entries end with e-mail (space may appear before @)
    out = []
    NAME = r"[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż]+(?: [A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż]+){1,3}"
    for m in re.finditer(rf"({NAME})\s*[–-]\s*(.+?)\s*\S+\s*@\s*\S+", text):
        nm = _norm_name(m.group(1))
        role_raw = m.group(2)
        role = ""
        if "Przewodniczący Rady" in role_raw:
            role = "Przewodniczący Rady"
        elif "Wiceprzewodnicząc" in role_raw:
            role = "Wiceprzewodniczący Rady"
        elif "Radn" in role_raw:
            role = ""
        else:
            role = "Członek komisji"
        if nm and nm not in [x["name"] for x in out]:
            out.append({"name": nm, "role": role})
    if not (13 <= len(out) <= 25):
        raise RuntimeError(f"Suspect roster size: {len(out)}")
    # Źródło BIP miesza szyk: 'Barabasz Wiesław Jan' / 'Kamila Dróżdż'.
    # Odwracamy wpisy odwrotne (nazwisko-first) — potwierdzone inicjałami
    # e-mail: w.barabasz@brm.paczkow.pl, s.janik@brm.paczkow.pl.
    fixes = {"Barabasz Wiesław Jan": "Wiesław Jan Barabasz",
             "Janik Stanisław": "Stanisław Janik"}
    for e in out:
        if e["name"] in fixes:
            e["name"] = fixes[e["name"]]
    return out


def _category_articles(cid: int) -> list[tuple[int, str]]:
    t = _http(f"{BIP}/?c={cid}").decode("utf-8", "replace")
    pairs = re.findall(r'href="\?a=(\d+)"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{6,120})', t)
    return [(int(a), re.sub(r"\s+", " ", n).strip()) for a, n in pairs]


def _subcats(parent_html_cid: int) -> list[tuple[int, str]]:
    t = _http(f"{BIP}/?c={parent_html_cid}").decode("utf-8", "replace")
    j = t.find("Kategorie")
    seg = t[j:j + 2500] if j >= 0 else t
    return [(int(c), re.sub(r"\s+", " ", n).strip())
            for c, n in re.findall(r'\?c=(\d+)"[^>]*>\s*(?:<span>)?\s*(\d{4})', seg)]


def _proto_date(art_id: int) -> str | None:
    """Data sesji z nagłówka PDF-a protokołu: 'odbytej w dniu 30 kwietnia 2026 r.'"""
    t = _http(f"{BIP}/?a={art_id}").decode("utf-8", "replace")
    m = re.search(r'\?p=document&amp;action=save&amp;id=(\d+)&amp;bar_id=' + str(art_id), t)
    if not m:
        m = re.search(r'action=save&amp;id=(\d+)', t)
    if not m:
        return None
    raw = _http(f"{BIP}/?p=document&action=save&id={m.group(1)}&bar_id={art_id}", timeout=90)
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            head = (pdf.pages[0].extract_text() or "") + "\n" + (pdf.pages[1].extract_text() if len(pdf.pages) > 1 else "")
    except Exception:
        return None
    dm = re.search(r"odbytej w dniu (\d{1,2})\s+(\w+)\s+(\d{4})", head)
    if dm:
        mon = MONTHS.get(dm.group(2).lower())
        if mon:
            return f"{int(dm.group(3)):04d}-{mon:02d}-{int(dm.group(1)):02d}"
    return None


def fetch_sessions() -> list[dict]:
    sessions = []
    years = _subcats(PROTO_ROOT)
    for cid, yname in years:
        arts = _category_articles(cid)
        protos = [(a, n) for a, n in arts if re.match(r"Protokół", n)]
        # newest-first: parse roman from title to order
        def key(pair):
            m = re.search(r"Protokół nr ([IVXLCDM]+)", pair[1])
            return _roman_to_int(m.group(1)) or 0 if m else 0
        protos.sort(key=key, reverse=True)
        for art_id, title in protos[:4]:  # max 4 newest per year → date via PDF
            m = re.search(r"Protokół nr ([IVXLCDM]+)", title)
            num = m.group(1) if m else ""
            try:
                d = _proto_date(art_id)
            except Exception as e:
                print(f"  [warn] art {art_id}: {e}")
                d = None
            time.sleep(0.4)
            if d and d >= KAD_START:
                sessions.append({"date": d, "number": num})
    # dedupe by date, sort
    seen = {}
    for s in sessions:
        seen[s["date"]] = s
    return sorted(seen.values(), key=lambda s: s["date"])


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    kad = cfg["kadencja_active"]
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    roster = fetch_roster()
    sessions = fetch_sessions()
    print(f"  roster: {len(roster)}  sessions: {len(sessions)}")
    if sessions and sessions[-1]["date"] < "2026-01-01":
        print("  [warn] kalendarz sesji wygląda na nieświeży")

    names = sorted((r["name"] for r in roster))
    kadencja = {
        "id": kad,
        "label": cfg["kadencje"][kad]["label"],
        "clubs": {},
        "sessions": [
            {"date": s["date"], "number": s["number"], "vote_count": 0,
             "attendee_count": None, "attendees": [], "speakers": []}
            for s in sessions
        ],
        "total_sessions": len(sessions),
        "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [
            {"name": n, "club": "", "district": None,
             "frekwencja": None, "aktywnosc": None, "zgodnosc_z_klubem": None,
             "votes_total": 0, "rebellion_count": 0, "has_activity_data": False}
            for n in names
        ],
        "votes": [],
        "similarity_top": [],
        "similarity_bottom": [],
    }
    (docs / f"kadencja-{kad}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    roles = {r["name"]: r["role"] for r in roster if r["role"]}
    profiles = {
        "scraped_at": datetime.now().isoformat(),
        "profiles": [
            {"name": n, "slug": _slug(n),
             "kadencje": {kad: {"club": "", "role": roles.get(n, ""),
                                "has_voting_data": False,
                                "has_activity_data": False,
                                "former": False, "mid_term": False}}}
            for n in names
        ],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kad,
        "kadencje": [{"id": kad, "label": cfg["kadencje"][kad]["label"]}],
    }
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(__file__).resolve().parent.parent
    if len(sys.argv) > 1:
        city_dir = Path(sys.argv[1])
    raise SystemExit(build(city_dir))
