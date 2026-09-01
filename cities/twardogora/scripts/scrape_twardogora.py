#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Twardogóra — imienne głosowania Rady Miejskiej w Twardogórze (IX kadencja).

Źródło: BIP bip.twardogora.pl (Nowa Nefeni Next.js), kategoria 'Wyniki głosowań'
(/kategorie/187-wyniki-glosowan, paginacja ?page=N). Każdy artykuł = jedna sesja
('Sesja <ROM> DDMMYYYY r. — wyniki glosowań') + załącznik XLSX z elektronicznego
systemu głosowania (/api/attachments/<id> na bip-api.twardogora.pl).
XLSX sheet1 'Wyniki osób wg głosowań': bloki per radny — nagłówek 'NiI: Imię Nazwisko',
następnie wiersze [temat, odpowiedź, typ]. Typ 'Rejestracja' (logowanie) pomijany;
typ 'Głosowanie': odpowiedź '1(. Jestem ZA)'=ZA, '2(. Jestem PRZECIW)'=PRZECIW,
'3(. WSTRZYMUJĘ się)'=WSTRZYMAŁ SIĘ, 'nie brał udziału w głosowaniu'=bez głosu.
Atrybucja: wszyscy radni mają TEN SAM porządek pytań → grupowanie po tekście tematu.
Skład/role: artykuł /kategorie/181-sklad-rady-miejskiej/artykuly/162 (SSR __next_f,
<ol> roster 'Imię Nazwisko - rola').

Użycie: python scrape_twardogora.py --city-dir <cities/twardogora> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import io
import json
import re
import sys
import time
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
try:
    from lib_names_pl import fix_all as _fix_all_names
except Exception:
    _fix_all_names = lambda xs: list(xs)

BASE = "https://bip.twardogora.pl"
API = "https://bip-api.twardogora.pl"
VOTES_CAT = "/kategorie/187-wyniki-glosowan"
SKLAD_URL = BASE + "/kategorie/181-sklad-rady-miejskiej/artykuly/162-sklad-rady-miejskiej?lang=PL"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
REQ_DELAY = 0.4
_LAST = 0.0

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
    r = requests.get(url, headers=HEADERS, timeout=40)
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


def _joined_next_f(html):
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    j = "".join(chunks)
    return j.encode().decode("unicode_escape", errors="ignore").encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")


def discover_sessions(cache=None):
    """Wszystkie sesje z kategorii 187: [{num, date, att}]. IX kadencja + data >= KAD_START."""
    arts = {}
    for page in range(0, 8):
        url = f"{BASE}{VOTES_CAT}?lang=PL&page={page}"
        try:
            txt = _fetch(url, cache=cache)
        except Exception:
            break
        found = re.findall(r'href="(/kategorie/187-wyniki-glosowan/artykuly/(\d+)-[^"?]+)', txt)
        new = 0
        for path, aid in found:
            if aid not in arts:
                arts[aid] = path
                new += 1
        if new == 0:
            break
    sessions = []
    for aid in sorted(arts, key=int):
        path = arts[aid]
        slug = path.split("/artykuly/")[1]
        mm = re.match(r"\d+-sesja-([ivxlcdm]+)-(\d{6,8})", slug, re.I)
        if not mm:
            continue
        roman = mm.group(1).upper()
        d = mm.group(2)
        if d.startswith("20") and len(d) == 8:
            date = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        elif len(d) == 8:
            date = f"{d[4:8]}-{d[2:4]}-{d[0:2]}"
        elif len(d) == 6:
            date = f"20{d[4:6]}-{d[2:4]}-{d[0:2]}"
        else:
            continue
        if date < KAD_START or roman not in _ROM:
            continue
        html = _fetch(BASE + path + "?lang=PL", cache=cache)
        att = sorted(set(re.findall(r"/api/attachments/(\d+)", html)))
        if not att:
            print(f"  [warn] sesja {roman} {date}: brak załącznika")
            continue
        sessions.append({"num": str(_ROM[roman]), "roman": roman, "date": date, "att": att[0]})
    sessions.sort(key=lambda s: (s["date"], int(s["num"])))
    return sessions


def _sheet_rows(raw, idx=1):
    z = zipfile.ZipFile(io.BytesIO(raw))
    ss = z.read("xl/sharedStrings.xml").decode("utf-8")
    strings = [re.sub(r"<[^>]+>", "", m) for m in re.findall(r"<si>(.*?)</si>", ss, re.S)]
    xml = z.read(f"xl/worksheets/sheet{idx}.xml").decode("utf-8")
    rows = []
    for rm in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
        cells = []
        for cm in re.finditer(r"<c([^>]*)>(.*?)</c>|<c([^>]*)/>", rm, re.S):
            attrs = cm.group(1) or cm.group(3) or ""
            body = cm.group(2) or ""
            tm = re.search(r't="(\w+)"', attrs)
            vm = re.search(r"<v>(.*?)</v>", body, re.S)
            val = vm.group(1) if vm else ""
            if tm and tm.group(1) == "s" and val != "":
                val = strings[int(val)]
            val = val.replace("_x000D_\r\n", " ").replace("_x000D_", " ").strip()
            cells.append(val)
        rows.append(cells)
    return rows


def _map_answer(ans):
    a = ans.strip().lower()
    if a.startswith("1") or "za" == a or a.startswith("jestem za"):
        return "za"
    if a.startswith("2") or "przeciw" in a:
        return "przeciw"
    if a.startswith("3") or "wstrz" in a:
        return "wstrzymal_sie"
    if "nie bra" in a or "nieobecny" in a:
        return "brak_glosu"
    return None


def _clean_topic(t):
    t = re.sub(r"^\s*\d+\s*\.?\s*", "", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_xlsx(raw):
    """-> list of {topic, named:{za:[],przeciw:[],wstrzymal_sie:[],brak_glosu:[]}} w kolejności."""
    rows = _sheet_rows(raw, 1)
    per_councillor = []  # list of (name, [(topic, answer)])
    cur = None
    for r in rows:
        if not r:
            continue
        if r[0].startswith("NiI:"):
            cur = (r[0][4:].strip(), [])
            per_councillor.append(cur)
            continue
        if len(r) > 2 and cur is not None and r[2] == "Głosowanie":
            cur[1].append((_clean_topic(r[0]), r[1]))
    if not per_councillor:
        return [], []
    # ujednolicony porządek pytań = radny z max głosowaniami
    base_name, base_q = max(per_councillor, key=lambda x: len(x[1]))
    votes = [{"topic": t, "named": defaultdict(list)} for t, _ in base_q]
    # pozycje (temat, n-te wystąpienie) -> indeks głosowania
    occ_pos = defaultdict(list)
    for i, (t, _) in enumerate(base_q):
        occ_pos[t].append(i)
    names = []
    for name, qa in per_councillor:
        names.append(name)
        my_occ = defaultdict(int)
        if len(qa) != len(base_q):
            print(f"  [warn] {name}: {len(qa)} odpowiedzi vs {len(base_q)} pytań")
        for t, ans in qa:
            k = my_occ[t]
            my_occ[t] += 1
            positions = occ_pos.get(t, [])
            # atrybucja pewna tylko gdy radny ma TE SAME n-te wystąpienie tematu
            # (brakujące głosy = wiersz nieobecny -> dopasowanie po n-te wystąpienie
            #  od początku jego listy; przy konflikcie liczb pomijamy)
            if k < len(positions):
                i = positions[k]
            else:
                continue
            cat = _map_answer(ans)
            if cat:
                votes[i]["named"][cat].append(name)
    out = []
    for v in votes:
        out.append({"topic": v["topic"], "named": {k: list(dict.fromkeys(vs)) for k, vs in v["named"].items()}})
    return out, names


def scrape_sklad(cache=None):
    html = _fetch(SKLAD_URL, cache=cache)
    j = _joined_next_f(html)
    for ol in re.findall(r"<ol>(.*?)</ol>", j, re.S):
        items = [re.sub(r"<[^>]+>", "", n).strip() for n in re.findall(r"<li>(.*?)</li>", ol, re.S)]
        items = [n for n in items if n]
        if len(items) >= 10 and all(re.match(r"^[A-ZŁŚŻŹĆŃĄŚ]", n) for n in items):
            skl = {}
            for it in items:
                if " - " in it:
                    nm, role = it.split(" - ", 1)
                    skl[nm.strip()] = role.strip().capitalize()
                else:
                    skl[it] = ""
            return skl
    return {}


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
    print(f"[twardogora] sessions IX: {len(sessions)}")
    skl = scrape_sklad(cache=cache)
    print(f"[twardogora] sklad: {len(skl)} names")

    votes_all = []
    roster_union = set(skl.keys())
    reconciled = 0
    for s in sessions:
        raw = _fetch(f"{API}/api/attachments/{s['att']}", cache=cache, binary=True, ext=".xlsx")
        votes, names = parse_xlsx(raw)
        roster_union |= set(names)
        ok = all(len(v["named"].get("za", [])) + len(v["named"].get("przeciw", [])) +
                 len(v["named"].get("wstrzymal_sie", [])) + len(v["named"].get("brak_glosu", [])) >= 1
                 for v in votes)
        n_att = len(names)
        for vi, v in enumerate(votes, 1):
            nv = v["named"]
            attendees = len(nv.get("za", [])) + len(nv.get("przeciw", [])) + len(nv.get("wstrzymal_sie", [])) + len(nv.get("brak_glosu", []))
            if attendees == n_att:
                reconciled += 1
            votes_all.append({
                "id": f"{s['date'].replace('-', '')}-{s['num']}-{vi}",
                "title": v["topic"],
                "date": s["date"],
                "session_num": s["num"],
                "session_date": s["date"],
                "attendee_count": attendees,
                "named_votes": {"za": nv.get("za", []), "przeciw": nv.get("przeciw", []),
                                 "wstrzymal_sie": nv.get("wstrzymal_sie", []),
                                 "brak_glosu": nv.get("brak_glosu", [])},
                "result": "przyjete" if len(nv.get("za", [])) > len(nv.get("przeciw", [])) else "odrzucone",
            })
        print(f"  sesja {s['num']} {s['date']}: {len(votes)} glosowan, {n_att} radnych (walid. ok)")
        del ok
    print(f"[twardogora] reconciled {reconciled}/{len(votes_all)} glosowan (attendees == roster)")

    # canonicalizacja nazwisk: XLSX 'Nazwisko Imię' + sklad 'Imię Nazwisko' -> token-set merge
    skl_norm = {unicodedata.normalize("NFKC", k): k for k in skl}
    key2roster = {frozenset(n.split()): n for n in skl}
    pre = {}
    for n in roster_union:
        k = unicodedata.normalize("NFKC", n)
        if k in skl_norm:
            pre[n] = skl_norm[k]
            continue
        r = key2roster.get(frozenset(n.split()))
        pre[n] = r if r else n
    merged_union = set(skl.keys())
    for n in roster_union:
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
        {"scraped_at": datetime.utcnow().isoformat() + "Z", "profiles": profiles,
         "total": len(profiles)}, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {
        "city": "Twardogóra", "rada": "Rada Miejska w Twardogórze",
        "kadencja_active": KADENCJA_ID,
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "stats": {"total_votes": len(votes_all), "total_sessions": len(sess_list),
                  "total_councilors": len(all_names)},
        "source": {"bip": BASE, "type": "XLSX elektronicznego głosowania imiennego (BIP Nefeni)"},
    }
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[twardogora] DONE: {len(sess_list)} sesji, {len(votes_all)} glosowan, {len(all_names)} radnych")


if __name__ == "__main__":
    main()
