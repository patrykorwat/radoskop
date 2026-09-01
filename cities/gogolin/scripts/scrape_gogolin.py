#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Gogolin — imienne głosowania Rady Miejskiej w Gogolinie (IX kadencja).

Źródło: BIP bip.gogolin.pl (idcom/Stronets), kategoria 'Protokoły i wykazy głosowań
z Sesji Rady Miejskiej w Gogolinie (2024-2029)' (/18801/, paginacja ?Page=N).
Każdy artykuł sesji = tytuł 'Protokół z <ROM> Sesji ... która odbyła się DD mm YYYY r.
oraz wykaz głosowań' + załącznik /download/attachment/<id>/wykaz-glosowan-...-...pdf
(system Rada365, WARSTWA TEKSTOWA): temat '(HH:MM)' + 'Wyniki imienne:' + listy
ZA (n)/PRZECIW (n)/WSTRZYMUJĘ SIĘ (n)/NIE GŁOSOWALI (n)/NIEOBECNI (n) — nazwiska
przecinkiem, zawijane; ostatnia lista bywa zklejona z tematem następnego głosowania
→ parser dwuprzebiegowy (roster pass + atrybucja walidowana agregatami).
Skład/role: /18759/sklad-rady-miejskiej-w-gogolinie-2024-2029.html ('N) Nazwisko Imię - rola').

Użycie: python scrape_gogolin.py --city-dir <cities/gogolin> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from html import unescape
from itertools import combinations
from pathlib import Path

import pymupdf
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
try:
    from lib_names_pl import fix_all as _fix_all_names
except Exception:
    _fix_all_names = lambda xs: list(xs)

BASE = "https://bip.gogolin.pl"
CAT_PATH = "/18801/protokoly-i-wykazy-glosowan-z-sesji-rady-miejskiej-w-gogolinie-2024-2029.html"
SKLAD_URL = BASE + "/18759/sklad-rady-miejskiej-w-gogolinie-2024-2029.html"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
REQ_DELAY = 0.45
_LAST = 0.0

_MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
           "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "pazdziernika": 10,
           "października": 10, "listopada": 11, "grudnia": 12}
_ROM = {}
for _v, _r in [(1, "I"), (2, "II"), (3, "III"), (4, "IV"), (5, "V"), (6, "VI"), (7, "VII"),
               (8, "VIII"), (9, "IX"), (10, "X"), (11, "XI"), (12, "XII"), (13, "XIII"),
               (14, "XIV"), (15, "XV"), (16, "XVI"), (17, "XVII"), (18, "XVIII"), (19, "XIX"),
               (20, "XX"), (21, "XXI"), (22, "XXII"), (23, "XXIII"), (24, "XXIV"),
               (25, "XXV"), (26, "XXVI"), (27, "XXVII"), (28, "XXVIII"), (29, "XXIX"),
               (30, "XXX"), (31, "XXXI"), (32, "XXXII"), (33, "XXXIII")]:
    _ROM[_r] = _v

FOOTER_RE = re.compile(r"wydrukowano|wygenerowano|strona|systemu\s+Rada365|^Gmina\s", re.I)
NAME_RE = re.compile(r"^[A-ZŚŁŻŹĆŃÓĄĘ][\wŚŁŻŹĆŃÓĄĘ\-]*(?: [A-ZŚŁŻŹĆŃÓĄĘ][\wŚŁŻŹĆŃÓĄĘ\.\-]*){1,2}$")

LABEL_MAP = {"ZA": "za", "PRZECIW": "przeciw",
             "WSTRZYMUJE SIE": "wstrzymal_sie",
             "NIE GLOSOWALI": "brak_glosu",
             "NIE GLOSOWALI/NIEOBECNI": "brak_glosu",
             "NIEOBECNI": "nieobecni"}
LABEL_RE = re.compile(r"\b(ZA|PRZECIW|WSTRZYMUJ[EĘ] SI[EĘ]|NIE G\u0141OSOWALI(?:/NIEOBECNI)?|NIE GLOSOWALI(?:/NIEOBECNI)?|NIEOBECNI)\s*\((\d+)\)")
OCC_RE = re.compile(r"Wyniki imienne\s*:")


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=False):
    ext = ".bin" if binary else ".html"
    cf = None
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + 2 * attempt)
    data = r.content
    if cf is not None:
        if binary:
            cf.write_bytes(data)
        else:
            cf.write_text(data.decode("utf-8", "ignore"), encoding="utf-8")
    return data if binary else data.decode("utf-8", "ignore")


def _clean_name(tok):
    tok = re.sub(r"\s+", " ", tok).strip(" ,;.")
    if not tok or FOOTER_RE.search(tok):
        return None
    if not NAME_RE.match(tok):
        return None
    return tok


def pdf_text(data):
    doc = pymupdf.open(stream=data, filetype="pdf")
    raw = "\n".join(p.get_text() for p in doc)
    doc.close()
    lines = [l for l in raw.split("\n")
             if not re.search(r"wydrukowano|Strona\s|Strona$|systemu\s+Rada365|^Gmina\s+\S+\s*$|^(X*[IVX]*)?\s*Wykaz głosowań", l, re.I)]
    return "\n".join(lines)


def _chunks(body):
    lab_iter = list(LABEL_RE.finditer(body))
    for j, lm in enumerate(lab_iter):
        label = lm.group(1).upper().replace("Ę", "E").replace("Ł", "L")
        norm = LABEL_MAP.get(label)
        if norm is None:
            continue
        chunk_end = lab_iter[j + 1].start() if j + 1 < len(lab_iter) else len(body)
        chunk = re.sub(r"\s+", " ", body[lm.end():chunk_end]).strip()
        yield norm, int(lm.group(2)), chunk, (j == len(lab_iter) - 1)


def roster_pass(texts, seed):
    roster = set(seed)
    for text in texts:
        if "Wyniki imienne" not in text:
            continue
        occ = [m.start() for m in OCC_RE.finditer(text)]
        for i, pos in enumerate(occ):
            nxt = occ[i + 1] if i + 1 < len(occ) else len(text)
            body = text[pos:nxt]
            for norm, expect, chunk, is_last in _chunks(body):
                if is_last or expect <= 0:
                    continue
                for tok in chunk.split(","):
                    n = _clean_name(tok)
                    if n:
                        roster.add(n)
    return sorted(roster, key=len, reverse=True)


def parse_text(text, roster):
    if "Wyniki imienne" not in text:
        return []
    rosters = sorted(roster, key=len, reverse=True)
    votes = []
    occ = [m.start() for m in OCC_RE.finditer(text)]
    for i, pos in enumerate(occ):
        nxt = occ[i + 1] if i + 1 < len(occ) else len(text)
        body = text[pos:nxt]
        prev_start = occ[i - 1] if i > 0 else 0
        pre = text[prev_start:pos]
        anchors = list(re.finditer(r"\((\d{1,2}):(\d{2})\)", pre))
        when = ""
        topic = ""
        if anchors:
            a = anchors[-1]
            when = f"{a.group(1)}:{a.group(2)}"
            zone = pre[:a.start()]
            end = -1
            for nm in rosters:
                j = zone.rfind(nm)
                if j != -1 and j + len(nm) > end:
                    end = j + len(nm)
            topic = zone[end:] if end != -1 else zone[-250:]
        else:
            topic = pre
        topic = re.sub(r"\s+", " ", topic).strip(" .")
        topic = re.sub(r"^(Wykaz głosowań sesji\s*-\s*)?(X*[IVX]*\s*Sesja [^.]*?\.)?\s*", "", topic)
        topic = re.sub(r"^Wyniki imienne:?\s*", "", topic).strip(" .")
        prev_t = None
        while prev_t != topic:
            prev_t = topic
            topic = re.sub(r"^(ZA|PRZECIW|WSTRZYMUJ[EĘ] SI[EĘ]|NIE G\u0141OSOWALI(?:/NIEOBECNI)?|NIE GLOSOWALI(?:/NIEOBECNI)?|NIEOBECNI)\s*\(\d+\)[:\s]*", "", topic, flags=re.I).strip()
        named = {}
        ok = True
        seen_last = False
        for norm, expect, chunk, is_last in _chunks(body):
            names = []
            if expect > 0:
                hits = []
                for nm in rosters:
                    p = chunk.find(nm)
                    if p != -1:
                        hits.append((p, nm))
                hits.sort()
                ded = []
                last_end = -1
                for p, nm in hits:
                    if p >= last_end:
                        ded.append((p, nm))
                        last_end = p + len(nm)
                names = [nm for _p, nm in ded][:expect]
                if len(names) != expect:
                    names = [n for n in (_clean_name(t) for t in chunk.split(",")) if n]
            if is_last:
                seen_last = True
            named[norm] = names[:expect]
            if len(named[norm]) != expect:
                ok = False
        if not ok or not seen_last:
            continue
        if not all(k in named for k in ("za", "przeciw", "wstrzymal_sie")):
            continue
        votes.append({"topic": topic or "głosowanie", "time": when, "named": named})
    return votes


def discover_sessions(cache=None):
    """Artykuły sesji z kategorii 18801 (paginacja ?Page=N) + załącznik wykaz-glosowan."""
    articles = {}
    for pg in range(1, 12):
        url = BASE + CAT_PATH + (f"?Page={pg}" if pg > 1 else "")
        t = _fetch(url, cache=cache)
        new = set()
        for m in re.finditer(r'href="(https://bip\.gogolin\.pl/(\d+)/protokol-z-([ivxlcdm]+)-sesji-rady-miejskiej[^"]*)"', t):
            if m.group(2) == "6607":
                continue
            new.add((m.group(2), m.group(1), m.group(3)))
        fresh = {a for a in new if a[0] not in articles}
        if not fresh and not new:
            break
        for aid, u, rom in new:
            articles.setdefault(aid, (u, rom))
        if not fresh:
            break
    sessions = []
    for aid, (u, rom) in articles.items():
        t = _fetch(u, cache=cache)
        t = unescape(t)
        m = re.search(r"Protok\u00f3\u0142 z ([IVXLCDM]+) Sesji[^<]*?odby\u0142a si\u0119 (\d{1,2})\s+([a-zó-ż]+)\s*(\d{4})", t)
        if not m:
            continue
        month = _MONTHS.get(m.group(3).lower().replace("ś", "s"))
        if not month:
            continue
        date = f"{m.group(4)}-{month:02d}-{int(m.group(2)):02d}"
        num = _ROM.get(m.group(1).upper())
        att = None
        for am in re.finditer(r'href="(https://bip\.gogolin\.pl/download/attachment/\d+/[^"]*glosowa[^"]*\.pdf[^"]*)"', t, re.I):
            att = am.group(1)
            break
        if date < KAD_START or not att or num is None:
            continue
        sessions.append({"num": num, "date": date, "att": att, "art": aid})
    out = []
    seen = set()
    for s in sorted(sessions, key=lambda x: x["date"]):
        k = (s["num"], s["date"])
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def scrape_sklad():
    t = unescape(_fetch(SKLAD_URL))
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", "", t, flags=re.S)
    txt = re.sub(r"<[^>]+>", "\n", txt)
    lines = [re.sub(r"\s+", " ", l.replace("\xa0", " ")).strip() for l in txt.splitlines()]
    roster = {}
    nm2 = re.compile(r"^[A-ZŁŚŻŹĆŃÓĄĘ][a-złśżźćńóąę\-]+(?: [A-ZŁŚŻŹĆŃÓĄĘ][a-złśżźćńóąę\.\-]+)+$")
    for l in lines:
        m = re.match(r"^\d{1,2}\)\s*(.+)$", l)
        if not m:
            continue
        rest = m.group(1)
        # 'Nazwisko Imię - rola' / 'Imię Nazwisko- rola' — name = tokens before ' - '
        parts = re.split(r"\s+-\s*|(?<=[a-złśżźćńóąę])-+\s+", rest, maxsplit=1)
        name = re.sub(r"\s+", " ", parts[0]).strip(" -,.;")
        role = (parts[1] if len(parts) > 1 else "").strip(" -.")
        role = re.sub(r"\s+-\s*$", "", role)
        if not nm2.match(name):
            continue
        roster[name] = role
    return roster


def slugify(name):
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else city_dir / ".cache"
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    sessions = discover_sessions(cache=cache)
    print(f"[gogolin] sessions IX: {len(sessions)}")
    pdfs = {}
    for s in sessions:
        pdfs[s["att"]] = pdf_text(_fetch(s["att"], cache=cache, binary=True))
    skl = scrape_sklad()
    print(f"[gogolin] sklad page: {len(skl)} names")
    roster = roster_pass(list(pdfs.values()), list(skl.keys()))
    print(f"[gogolin] roster pass-1: {len(roster)}")

    votes_all = []
    for s in sessions:
        votes = parse_text(pdfs[s["att"]], roster)
        print(f"  sesja {s['num']} {s['date']}: {len(votes)} glosowan")
        for vi, v in enumerate(votes, 1):
            nv = v["named"]
            votes_all.append({
                "id": f"{s['date'].replace('-', '')}-{s['num']}-{vi}",
                "title": v["topic"],
                "date": s["date"],
                "session_num": s["num"],
                "session_date": s["date"],
                "attendee_count": len(set(nv.get("za", [])) | set(nv.get("przeciw", [])) | set(nv.get("wstrzymal_sie", [])) | set(nv.get("brak_glosu", []))),
                "named_votes": {"za": nv.get("za", []), "przeciw": nv.get("przeciw", []),
                                 "wstrzymal_sie": nv.get("wstrzymal_sie", []),
                                 "nie_glosowali": nv.get("brak_glosu", []), "nieobecni": nv.get("nieobecni", [])},
                "result": "przyjete" if len(nv.get("za", [])) > len(nv.get("przeciw", [])) else "odrzucone",
            })
    names_union = set(skl.keys())
    for vv in votes_all:
        for lst in vv["named_votes"].values():
            names_union |= set(lst)
    # merge 'Nazwisko Imię' PDF order -> 'Imię Nazwisko' roster order (same token set)
    key2roster = {frozenset(n.split()): n for n in skl}
    pre = {}
    merged_union = set(skl.keys())
    for n in names_union:
        if n in skl:
            pre[n] = n
            continue
        r = key2roster.get(frozenset(n.split()))
        if r:
            pre[n] = r
        else:
            merged_union.add(n)
            pre[n] = n
    canon = {}
    for n in _fix_all_names(sorted(merged_union)):
        canon.setdefault(n, n)
    swap = {n: canon.get(_fix_all_names([pre[n]])[0], pre[n]) for n in names_union}
    for vv in votes_all:
        vv["named_votes"] = {k: [swap.get(x, x) for x in lst] for k, lst in vv["named_votes"].items()}
    names_union = {swap.get(n, n) for n in names_union}
    skl = {swap.get(k, k): v for k, v in skl.items()}
    all_names = sorted(names_union)

    council_stats = defaultdict(lambda: defaultdict(int))
    for vv in votes_all:
        nvk = vv["named_votes"]
        for n in nvk["za"]:
            council_stats[n]["za"] += 1
        for n in nvk["przeciw"]:
            council_stats[n]["przeciw"] += 1
        for n in nvk["wstrzymal_sie"]:
            council_stats[n]["wstrzymal_sie"] += 1
        for n in nvk["nie_glosowali"]:
            council_stats[n]["brak"] += 1
        for n in nvk["nieobecni"]:
            council_stats[n]["nieobecny"] += 1

    print(f"[gogolin] votes: {len(votes_all)}, names: {len(all_names)}")

    by_sess_date = defaultdict(list)
    for vv in votes_all:
        by_sess_date[vv["session_date"]].append(vv)
    sess_list = []
    for s in sessions:
        sv = by_sess_date.get(s["date"], [])
        if not sv:
            continue
        sess_list.append({"id": f"sesja-{s['num']}", "number": str(s["num"]), "date": s["date"],
                          "label": f"{s['num']} Sesja Rady Miejskiej ({s['date']})",
                          "vote_count": len(sv)})

    pairs = defaultdict(lambda: [0, 0])
    for vv in votes_all:
        v = {}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for n in vv["named_votes"].get(cat, []):
                v[n] = cat
        ns = sorted(v)
        for a, b in combinations(ns, 2):
            pairs[(a, b)][1] += 1
            if v[a] == v[b]:
                pairs[(a, b)][0] += 1
    sim = {}
    for n in all_names:
        vals = [100.0 * c[0] / c[1] for k, c in pairs.items() if n in k and c[1] >= 5]
        sim[n] = round(sum(vals) / len(vals), 1) if vals else None

    councilors = []
    for n in all_names:
        st = council_stats.get(n, {})
        cast = st.get("za", 0) + st.get("przeciw", 0) + st.get("wstrzymal_sie", 0)
        present = cast + st.get("brak", 0)
        councilors.append({
            "name": n, "slug": slugify(n), "club": "", "role": skl.get(n, ""),
            "frekwencja": round(100.0 * present / len(votes_all), 1) if votes_all else 0,
            "aktywnosc": round(100.0 * cast / len(votes_all), 1) if votes_all else 0,
            "votes": cast,
            "zgodnosc_z_izba": sim.get(n),
        })

    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "sessions": sess_list, "votes": votes_all,
        "councilor_index": all_names, "councilors": councilors,
        "total_councilors": len(all_names), "total_votes": len(votes_all),
        "total_sessions": len(sess_list),
        "similarity_top": sorted([{"name": n, "value": s} for n, s in sim.items() if s is not None],
                                  key=lambda x: -x["value"])[:10],
        "similarity_bottom": sorted([{"name": n, "value": s} for n, s in sim.items() if s is not None],
                                     key=lambda x: x["value"])[:10],
    }
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = []
    for c in councilors:
        profiles.append({
            "name": c["name"], "slug": c["slug"], "club": c["club"], "role": c["role"],
            "photo_url": "", "bio": "", "email": "", "social_links": {}, "voting": None,
            "kadencje": {KADENCJA_ID: {
                "club": "", "has_voting_data": True, "role": c["role"],
                "frekwencja": c["frekwencja"], "aktywnosc": c["aktywnosc"],
                "zgodnosc_z_klubem": None, "zgodnosc_z_izba": c["zgodnosc_z_izba"],
                "rebellion_count": 0,
            }},
        })
    (docs / "profiles.json").write_text(json.dumps(
        {"scraped_at": datetime.utcnow().isoformat() + "Z", "profiles": profiles,
         "total": len(profiles)}, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {
        "city": "Gogolin", "rada": "Rada Miejska w Gogolinie",
        "kadencja_active": KADENCJA_ID,
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "stats": {"total_votes": len(votes_all), "total_sessions": len(sess_list),
                  "total_councilors": len(all_names)},
        "source": {"bip": BASE, "type": "Rada365 wykaz glosowan PDF (warstwa tekstowa)"},
    }
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[gogolin] DONE: {len(sess_list)} sesji, {len(votes_all)} glosowan, {len(all_names)} radnych")


if __name__ == "__main__":
    main()
