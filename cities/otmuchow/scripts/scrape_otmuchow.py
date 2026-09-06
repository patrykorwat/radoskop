#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Otmuchów — imienne głosowania Rady Miejskiej w Otmuchowie (IX kadencja).

Źródło: BIP bip.otmuchow.pl (CMS nazwa.pl 'system/pobierz.php'), kategorie
'Protokoły z głosowań' rocznikowe: /10378/ (2024), /10410/ (2025), /10464/ (2026).
Każdy plik = 'NN_Protokol_z_glosowania_<ROM> sesja - DD miesiąca YYYY r.pdf' —
raport app.esesja.pl w formacie eSesja standard (Głosowano w sprawie / Wyniki
glosowania / Wyniki imienne ZA (n)...), parser lib_voting_pdf_table.parse_voting_text.
Pobieranie przez href z /system/pobierz.php?plik=...&id=... (id wymagane).
Skład/role: /10055/Sklad_Rady_Miejskiej/ ('Nazwisko Imię', role Przewodniczący/
Wiceprzewodniczący przed tabelą) — merge token-set do 'Imię Nazwisko' z PDFów.
eSesja BIP martwa (0 sesji), AlfaTV 500, bip.net.pl 404.

Użycie: python scrape_otmuchow.py --city-dir <cities/otmuchow> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from html import unescape
from itertools import combinations
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_voting_pdf_table import parse_voting_text, extract_pdf_text  # noqa: E402
try:
    from lib_names_pl import fix_all as _fix_all_names
except Exception:
    _fix_all_names = lambda xs: list(xs)

BASE = "https://bip.otmuchow.pl"
VOTE_CATS = ["/10378/2024_/", "/10410/2025_/", "/10464/2026_/"]
SKLAD_URL = BASE + "/10055/Sklad_Rady_Miejskiej/"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
REQ_DELAY = 0.45
_LAST = 0.0

_MONTHS = {"stycznia": 1, "luty": 2, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
           "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
           "pazdziernika": 10, "października": 10, "listopada": 11, "grudnia": 12}
_ROM = {}
for _v, _r in [(1, "I"), (2, "II"), (3, "III"), (4, "IV"), (5, "V"), (6, "VI"), (7, "VII"),
               (8, "VIII"), (9, "IX"), (10, "X"), (11, "XI"), (12, "XII"), (13, "XIII"),
               (14, "XIV"), (15, "XV"), (16, "XVI"), (17, "XVII"), (18, "XVIII"), (19, "XIX"),
               (20, "XX"), (21, "XXI"), (22, "XXII"), (23, "XXIII"), (24, "XXIV"),
               (25, "XXV"), (26, "XXVI"), (27, "XXVII"), (28, "XXVIII"), (29, "XXIX"),
               (30, "XXX"), (31, "XXXI"), (32, "XXXII"), (33, "XXXIII")]:
    _ROM[_r] = _v


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None, binary=False, ext=".html"):
    cache_dir = Path(cache) if cache else None
    fp = None
    if cache_dir:
        import hashlib
        h = hashlib.md5(url.encode()).hexdigest()
        fp = cache_dir / (h + ext)
        if fp.exists() and time.time() - fp.stat().st_mtime < 7 * 86400:
            return fp.read_bytes() if binary else fp.read_text(encoding="utf-8", errors="replace")
    _rate()
    r = requests.get(url, headers=HEADERS, timeout=45)
    r.raise_for_status()
    data = r.content if binary else r.text
    if fp is not None and cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if binary:
            fp.write_bytes(data)
        else:
            fp.write_text(data, encoding="utf-8")
    return data


def slugify(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s


def discover_sessions(cache=None):
    """[{num, date, url}] z trzech rocznikowych kategorii; tylko IX kadencja (data >= KAD_START)."""
    out = []
    seen_urls = set()
    for cat in VOTE_CATS:
        html = _fetch(BASE + cat, cache=cache)
        for hm in re.finditer(r'href="([^"]*pobierz\.php[^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
            url = unescape(hm.group(1))
            label = re.sub(r"<[^>]+>", " ", hm.group(2))
            if url in seen_urls:
                continue
            fm = re.search(r"plik=([^&]+)\.pdf", url)
            if not fm:
                continue
            fname = fm.group(1)
            mm = re.search(r"glosowania_([IVXLCDM]+)_sesja[^_]*_-_?_?(\d{1,2})_([a-ząęółśżźćń]+?)_?(\d{4})", fname, re.I)
            if not mm:
                print(f"  [skip] nie do sparsowania nazwa: {fname[:70]}")
                continue
            roman = mm.group(1).upper()
            day, mon, year = int(mm.group(2)), mm.group(3).lower(), int(mm.group(4))
            if mon not in _MONTHS:
                mon = mon.rstrip(".")
            if mon not in _MONTHS:
                print(f"  [skip] miesiąc: {mon}")
                continue
            date = f"{year:04d}-{_MONTHS[mon]:02d}-{day:02d}"
            if date < KAD_START or roman not in _ROM:
                continue
            seen_urls.add(url)
            out.append({"num": str(_ROM[roman]), "date": date, "url": url, "fname": fname})
    # dedup po (date,num), sort
    uniq = {}
    for s in out:
        uniq[(s["date"], s["num"])] = s
    out = sorted(uniq.values(), key=lambda s: (s["date"], int(s["num"])))
    return out


def scrape_sklad(cache=None):
    html = _fetch(SKLAD_URL, cache=cache)
    body = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    txt = re.sub(r"<[^>]+>", "\n", body)
    lines = [l.strip().replace("&oacute;", "ó").replace("&Oacute;", "Ó") for l in txt.split("\n") if l.strip()]
    NAME_RE = re.compile(r"^[A-ZŁŚŻŹĆŃ][\wŁŚŻŹĆŃ\-]+\s+[A-ZŁŚŻŹĆŃ][\wŁŚŻŹĆŃ\.\-]+$")
    skl = {}
    role = ""
    started = False
    for l in lines:
        if l.startswith("Wykaz radnych"):
            started = True
            continue
        if not started:
            continue
        if "Przewodniczący Rady" in l:
            role = "Przewodniczący"
            continue
        if "Wiceprzewodniczący" in l:
            role = "Wiceprzewodniczący"
            continue
        if l in ("Lp.", "Nazwisko i Imię") or re.match(r"^\d+$", l) or l == "&nbsp;":
            continue
        if l.startswith("Skład Komisji") or l.startswith("Komisja"):
            break
        if NAME_RE.match(l):
            if l not in skl:
                skl[l] = role
            if role:
                role = ""
    return skl


def _full_text_from_bytes(raw):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "s.pdf"
        fp.write_bytes(raw)
        full, first = extract_pdf_text(fp, ocr_fallback=False)
    return full


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
    print(f"[otmuchow] sessions IX: {len(sessions)}")
    skl = scrape_sklad(cache=cache)
    print(f"[otmuchow] sklad: {len(skl)} names")

    votes_all = []
    roster_union = set(skl.keys())
    for s in sessions:
        raw = _fetch(s["url"], cache=cache, binary=True, ext=".pdf")
        text = _full_text_from_bytes(raw)
        sess = parse_voting_text(text, text[:2000], source_name=s["fname"])
        votes = sess.get("votes", [])
        for vi, v in enumerate(votes, 1):
            nv = {k: list(v.get("named_votes", {}).get(k, []))
                  for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
            roster_union |= set().union(*[set(x) for x in nv.values()]) if nv else set()
            att = sum(len(x) for x in nv.values())
            votes_all.append({
                "id": f"{s['date'].replace('-', '')}-{s['num']}-{vi}",
                "title": re.sub(r"\s+", " ", v.get("topic", "")).strip(),
                "date": s["date"],
                "session_num": s["num"],
                "session_date": s["date"],
                "attendee_count": att,
                "named_votes": {"za": nv["za"], "przeciw": nv["przeciw"],
                                 "wstrzymal_sie": nv["wstrzymal_sie"],
                                 "brak_glosu": nv["brak_glosu"], "nieobecni": nv["nieobecni"]},
                "result": "przyjete" if len(nv["za"]) > len(nv["przeciw"]) else "odrzucone",
            })
        print(f"  sesja {s['num']} {s['date']}: {len(votes)} glosowan")

    # merge 'Nazwisko Imię' sklad <-> 'Imię Nazwisko' PDF po token-set
    skl_norm = {unicodedata.normalize("NFKC", k): k for k in skl}
    key2roster = {frozenset(n.split()): n for n in skl}
    pre = {}
    merged_union = set(skl.keys())
    for n in roster_union:
        k = unicodedata.normalize("NFKC", n)
        if k in skl_norm:
            pre[n] = skl_norm[k]
            continue
        r = key2roster.get(frozenset(n.split()))
        pre[n] = r if r else n
        merged_union.add(pre[n])
    canon_in = sorted(merged_union)
    fixed = _fix_all_names(canon_in)
    if len(fixed) != len(canon_in):
        fixed = canon_in
    swap = {canon_in[i]: fixed[i] for i in range(len(canon_in))}
    swap = {n: swap.get(pre[n], pre[n]) for n in roster_union}
    for vv in votes_all:
        vv["named_votes"] = {k: [swap.get(x, x) for x in lst] for k, lst in vv["named_votes"].items()}
    names_union = {swap.get(n, n) for n in roster_union}
    skl2 = {swap.get(k, k): v for k, v in skl.items()}
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
        for n in nvk.get("brak_glosu", []):
            council_stats[n]["brak"] += 1
        for n in nvk.get("nieobecni", []):
            council_stats[n]["nieobecny"] += 1

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
            "name": n, "slug": slugify(n), "club": "", "role": skl2.get(n, ""),
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
        {"scraped_at": datetime.now(timezone.utc).isoformat(), "profiles": profiles,
         "total": len(profiles)}, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {
        "city": "Otmuchów", "rada": "Rada Miejska w Otmuchowie",
        "kadencja_active": KADENCJA_ID,
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {"total_votes": len(votes_all), "total_sessions": len(sess_list),
                  "total_councilors": len(all_names)},
        "source": {"bip": BASE, "type": "eSesja standard Raport z glosowan PDF (BIP)"},
    }
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[otmuchow] DONE: {len(sess_list)} sesji, {len(votes_all)} glosowan, {len(all_names)} radnych")


if __name__ == "__main__":
    main()
