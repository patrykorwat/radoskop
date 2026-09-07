#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Pajeczno — imienne glosowania Rady Miejskiej (platforma e-bip.pl / ABC PRO).

Zrodlo: http://www.e-bip.pl/Start/76/InformationModule/1372/824533
  kategoria "Wyniki glosowan" (Rada Miejska w Pajecznie), IX kadencja.
Kazdy artykul = jedna sesja; zalacznik PDF "Wyniki glosowan" = wydruk DSSS-like
per-glosowanie: naglowki 'Glosowanie n', 'Liczba uprawnionych', agregaty
(Glosy za/przeciw/wstrzymujace sie), a ponizej DWUKOLUMNOWA tabela imienna
'Lp | Nazwisko i imie | Glos' x2 — warstwa tekstowa idealna, glos =
ZA / PRZECIW / WSTRZYMUJE SIE / NIEOBECNA (token na koncu wiersza).
Parser pary wierszy lewa/prawa kolumna (x<290 lewa). Walidacja: kazde
glosowanie reconcilowane vs agregat, inaczej od rzucenia/odfiltrowane.

Uzycie:
    python scrape_pajeczno.py --city-dir cities/pajeczno [--cache-dir .cache]
"""

import argparse
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import requests

BASE = "http://www.e-bip.pl"
CAT_PATH = "/Start/76/InformationModule/1372/824533"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}
_MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
        'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10,
        'listopada': 11, 'grudnia': 12, 'wrzesnia': 9, 'pazdziernika': 10}
REQ_DELAY = 0.4
_LAST = 0.0


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=False):
    if cache is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = Path(cache) / (key + (".bin" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".html"))
        if binary:
            cf.write_bytes(resp.content)
        else:
            cf.write_text(resp.text, encoding="utf-8", errors="ignore")
    return resp.content if binary else resp.text


# ---------------------------------------------------------------------------
# 1. Sesje (artykuly kategorii) + attachment URL
# ---------------------------------------------------------------------------
def discover_sessions(cache):
    html = _fetch(BASE + CAT_PATH, cache)
    # rows: <td>YYYY-MM-DD</td><td><a href='/Start/76/Information/NID'>Tytuł</a></td>
    arts = re.findall(
        r"<td>(\d{4}-\d{2}-\d{2})\s*</td>\s*<td><a[^>]*href='(/Start/76/Information/(\d+))'>(Wyniki g\S+owa\S+\s*-\s*Sesja[^<]+)</a>",
        html)
    seen = set()
    sess = []
    for dcol, href, aid, title in arts:
        if href in seen:
            continue
        seen.add(href)
        t = title.strip()
        d = dcol
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d or ""):
            d = None
        if not d:
            m = re.search(r"z dnia (\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", t)
            if m:
                yr = m.group(3)
                if len(yr) == 2:
                    yr = "20" + yr
                d = f"{yr}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        sm = re.search(r"Sesja\s+(?:Nr\s+)?(\d+)", t)
        sess.append({"url": BASE + href, "article_id": aid, "title": t,
                     "date": d, "num": sm.group(1) if sm else ""})
    out = [s for s in sess if s["date"] and s["date"] >= KAD_START]
    out.sort(key=lambda s: s["date"])
    return out


def find_attachment_pdf(article_html):
    m = re.search(r'href="([^"]*file\.ashx\?hash=[^"]+)"', article_html)
    if not m:
        return None
    u = m.group(1).replace("&amp;", "&")
    if u.startswith("../"):
        u = BASE + "/Start/76/Information/" + u
    elif u.startswith("/"):
        u = BASE + u
    return u


# ---------------------------------------------------------------------------
# 2. Parser PDF (dwukolumnowa tabela imienna)
# ---------------------------------------------------------------------------
VOTE_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJE": "wstrzymal_sie",
            "WSTRZYMUJ": "wstrzymal_sie", "WSTRZYMUJĘ": "wstrzymal_sie",
            "NIEOBECNA": "nieobecny", "NIEOBECNY": "nieobecny",
            "OBECNA": "obecny_no", "OBECNY": "obecny_no", "BRAK": "obecny_no"}
_VOTE_RE = re.compile(r"\b(ZA|PRZECIW|WSTRZYMUJ[EĘ]*\s*SI[EĘ]|NIEOBECNA|NIEOBECNY|OBECNA|OBECNY|BRAK)\s*$")


def _rows_from_page(pg):
    """Return list of (lp:int, name:str, vote:str|None) from both columns."""
    words = pg.get_text("words")
    rows = {}
    for x0, y0, x1, y1, w, *_ in words:
        rows.setdefault(round(y0 / 5), []).append((x0, w))
    out = []
    for k in sorted(rows):
        toks = sorted(rows[k], key=lambda z: z[0])
        line = " ".join(t for _, t in toks)
        # split line at right column marker: second standalone "N." Lp>=9 pattern —
        # simpler: left col x<290, right x>=290
        left = [t for x, t in toks if x < 290]
        right = [t for x, t in toks if x >= 290]
        for seg in (left, right):
            if not seg:
                continue
            m = re.match(r"^(\d+)\.\s+(.*)$", " ".join(seg))
            if not m:
                continue
            lp = int(m.group(1))
            rest = m.group(2).strip()
            vote = None
            name = rest
            vm = _VOTE_RE.search(rest)
            if vm:
                key = vm.group(1).split()[0].upper()
                vote = VOTE_MAP.get(key)
                name = rest[:vm.start()].strip()
            # wiersz osoby = Lp + nazwisko + TOKEN GLOSU (bez tokstu -> temat obrad)
            if vote is not None and re.match(r"^[A-ZŁŚŹŻĆŃÓ]", name) and len(name) > 3:
                name = re.sub(r"\s*-\s*", "-", name).strip()
                # zrodlo: 'Nazwisko i imie' -> konwersja na 'Imie Nazwisko'
                parts = name.split(" ")
                if len(parts) >= 2:
                    name = " ".join(parts[1:] + [parts[0]])
                out.append((lp, name, vote))
    return out


def parse_doc(doc):
    """Group per-page rows into votes; page = one session header OR repeated vote blocks.

    Layout: each PDF page typically contains ONE vote block (header aggregate +
    table). Multi-page session: page 1 starts with '<N>\\n<roman> Sesja'.
    """
    votes = []
    cur = None
    for pno in range(doc.page_count):
        pg = doc[pno]
        t = pg.get_text()
        if "Uprawnieni do g" not in t and "Glosowanie" not in t and "Głosowanie" not in t:
            continue
        za = re.search(r"Glosy za\s+(\d+)|Głosy za\s+(\d+)", t)
        pr = re.search(r"Glosy przeciw\s+(\d+)|Głosy przeciw\s+(\d+)", t)
        wz = re.search(r"(?:Glosy|Głosy) wstrzymuj\S+e?\s*si\S*\s+(\d+)|(?:Glosy|Głosy) wstrzymujące się\s+(\d+)", t)
        if not za or not pr:
            continue
        zag = int(za.group(1) or za.group(2))
        prg = int(pr.group(1) or pr.group(2))
        wzg = 0
        if wz:
            wzg = int(wz.group(1) or wz.group(2) or 0)
        rows = _rows_from_page(pg)
        named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
        absent = []
        roster = []
        for lp, name, vote in rows:
            roster.append(name)
            if vote is None or vote == "obecny_no":
                continue
            if vote == "nieobecny":
                absent.append(name)
            else:
                named[vote].append(name)
        got = (len(named["za"]), len(named["przeciw"]), len(named["wstrzymal_sie"]))
        if got != (zag, prg, wzg):
            votes.append({"_bad": True, "agg": (zag, prg, wzg), "got": got,
                          "rows": rows, "page": pno})
            continue
        tm = re.search(r"Głosowanie\s*\n?\s*\d+\s*\n?\s*(.{4,200}?)(?:\nTyp|\nTyp|$)", t, re.S)
        topic = ""
        if tm:
            topic = re.sub(r"\s+", " ", tm.group(1)).strip()
        dt = re.search(r"(?:Data|Data) g\S+osowania:\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", t)
        vdate = ""
        if dt:
            vdate = f"{dt.group(3)}-{dt.group(2)}-{dt.group(1)}"
        votes.append({"topic": topic, "named": named, "absent": absent,
                      "roster": roster,
                      "counts": {k: len(v) for k, v in named.items()},
                      "session_date": vdate, "_bad": False})
    return votes


# ---------------------------------------------------------------------------
# 3. Output
# ---------------------------------------------------------------------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=".cache/pajeczno")
    ap.add_argument("--output", default=None)
    ap.add_argument("--profiles", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = args.cache_dir
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    sess = discover_sessions(cache)
    print(f"[pajeczno] sessions IX: {len(sess)}")
    all_votes = []
    sessions = []
    roster_union = {}
    vid = 0
    bad_total = 0
    for s in sess:
        try:
            art_html = _fetch(s["url"], cache)
        except Exception as e:
            print(f"  ! {s['date']} fetch art: {e}")
            continue
        pdf_url = find_attachment_pdf(art_html)
        if not pdf_url:
            print(f"  ! {s['date']} NO PDF")
            continue
        try:
            data = _fetch(pdf_url, cache, binary=True)
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as e:
            print(f"  ! {s['date']} pdf: {e}")
            continue
        vlist = parse_doc(doc)
        good = [v for v in vlist if not v.get("_bad")]
        bad = [v for v in vlist if v.get("_bad")]
        bad_total += len(bad)
        vc = 0
        for v in good:
            if not v["session_date"] or v["session_date"] < KAD_START:
                v["session_date"] = s["date"]
            vid += 1
            vc += 1
            for nm in v["roster"]:
                roster_union.setdefault(nm, 0)
            all_votes.append({
                "id": str(vid), "session_date": v["session_date"],
                "session_number": s["num"], "topic": v["topic"],
                "named_votes": v["named"], "counts": v["counts"],
                "source_url": s["url"],
            })
        sessions.append({"date": s["date"], "number": s["num"],
                         "label": f"Sesja {s['num']} ({s['date']})",
                         "vote_count": vc, "url": s["url"]})
        print(f"  {s['date']} sesja {s['num']}: {vc} glosowan" + (f" ({len(bad)} odrzucone/nierew.)" if bad else ""))
        doc.close()

    councilors = sorted(roster_union.keys())
    print(f"[pajeczno] votes={len(all_votes)} bad={bad_total} councilors={len(councilors)}")

    # kadencja
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "sessions": sessions, "votes": all_votes,
        "councilor_index": councilors,
        "councilors": [{"name": n, "slug": make_slug(n), "club": "", "role": ""} for n in councilors],
        "total_councilors": len(councilors),
        "total_votes": len(all_votes),
        "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {"scraped_at": datetime.now(timezone.utc).isoformat(),
                "total": len(councilors), "profiles": [
                    {"name": n, "slug": make_slug(n), "club": "", "role": "",
                     "photo_url": "", "bio": "", "email": "", "social_links": {},
                     "voting": None,
                     "kadencje": {KADENCJA_LABEL: {"club": "", "has_voting_data": True,
                                                   "role": ""}}}
                    for n in councilors]}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"city": "Pajeczno", "scraped_at": datetime.now(timezone.utc).isoformat(),
            # kontrakt generate_seo_pages: kadencje jako list [{id,label}]
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}], "sessions": len(sessions),
            "votes": len(all_votes), "councilors": len(councilors)}
    out = Path(args.output) if args.output else (docs / "data.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[pajeczno] wrote {docs}/kadencja-{KADENCJA_ID}.json profiles.json data.json")


if __name__ == "__main__":
    main()
