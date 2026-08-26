#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Środa Wielkopolska — imienne głosowania Rady Miejskiej (gov.pl BIP bip.umsroda.pl).

Źródło: BIP UM Środy Wielkopolskiej (IDcom platform, bip.umsroda.pl) — kategoria
    /organy/1691/dokumenty/16577/lista/N  "Imienne wykazy głosowań radnych".
Każdy dokument = jedna sesja Rady Miejskiej (tytuł: "Imienny wykaz głosowań radnych
z <NR> sesji Rady Miejskiej w Środzie Wielkopolskiej [IX kadencji] <DATA> roku").
Treść dokumentu: lista głosowań, każde z beczką temat + "Imienne wyniki głosowania:"
i wewnętrzną listą kategorii <strong>Za (N)</strong> / <strong>Przeciw (N)</strong> /
<strong>Wstrzymał się (N)</strong> / <strong>Nieobecni (N)</strong> + nazwiska.

IX kadencja (2024-2029) — 24 sesje (I 2024-05-06 .. XXVII 2026-06-25), 21 radnych.
(kadencja IX zaczyna się 2024-05-07; I sesja 06-05-2024 to kadencja start.)

Użycie:
    python scrape_sroda_wielkopolska.py --city-dir cities/sroda-wielkopolska [--cache-dir .cache]
"""

import argparse
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip.umsroda.pl"
CAT = f"{BASE}/organy/1691/dokumenty/16577/lista"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
REQ_DELAY = 0.35
_LAST_REQ = 0.0

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
}


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, cache_dir: Path | None = None):
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = cache_dir / (key + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    resp = requests.get(url, headers=HEADERS, timeout=60, verify=False)
    resp.raise_for_status()
    if cache_dir is not None:
        cf = cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".html")
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(resp.text, encoding="utf-8", errors="ignore")
    return resp.text


def parse_date_from_title(title: str) -> str:
    """'... IX kadencji 25 czerwca 2026 roku' -> '2026-06-25'."""
    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})\s+roku", title)
    if not m:
        return ""
    mon = _MONTHS.get(m.group(2).lower())
    if not mon:
        return ""
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"


def parse_session_num(title: str) -> str:
    """Roman numeral from title ('z XXVII sesji') -> 'XXVII'."""
    m = re.search(r"z\s+(?:nadzwyczajnej\s+)?([IVXLCDM]+)\s+[Ss]esji", title)
    return m.group(1) if m else ""


def discover_sessions(cache_dir):
    """Iterate category pages until no new docs; collect (href,title)."""
    seen = set()
    docs = []
    for page in range(1, 20):
        html = fetch(f"{CAT}/{page}", cache_dir)
        found = 0
        for m in re.finditer(
            r'<a href="(%s/organy/1691/dokumenty/16577/wiadomosc/\d+/[^"]+)"[^>]*>(.*?)</a>' % BASE,
            html, re.S):
            h = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2))
            title = re.sub(r"\s+", " ", title).strip()
            if not title or h in seen:
                continue
            seen.add(h)
            found += 1
            docs.append({"href": h, "title": title})
        if found == 0:
            break
    # keep only IX-kadencja (date >= KAD_START)
    out = []
    for d in docs:
        d["date"] = parse_date_from_title(d["title"])
        d["num"] = parse_session_num(d["title"])
        if d["date"] and d["date"] >= KAD_START:
            out.append(d)
    out.sort(key=lambda d: d["date"])
    return out


def parse_session_votes(html):
    """Parse the detail page: list of votes, each {topic, named:{za,przeciw,wstrzymal_sie,nieobecni}}."""
    soup = BeautifulSoup(html, "html.parser")
    votes = []
    # main content area: find the <li> tags after the heading 'Przeprowadzone głosowania'
    # The whole vote list lives in a <ul>; each vote is a top-level <li> that itself
    # contains (a) topic text and (b) an inner <ul> of <li><strong>Cat (N)</strong><br>names</li>
    # Strategy: find all <strong>Category (N)</strong>, walk up to the vote <li>.
    content = soup.find(id="content") or soup.find(class_="tresc") or soup
    # Find all strong category labels
    items = []
    for strong in content.find_all("strong", string=re.compile(r"^(Za|Przeciw|Wstrzymał się|Wstrzymał|Nieobecni)\s*\(\d+\)")):
        label = strong.get_text(" ", strip=True)
        # the inner <li> containing this strong
        li = strong.find_parent("li")
        if not li:
            continue
        # names = text after <br>
        br = strong.find_next("br")
        names_txt = ""
        if br:
            rest = ""
            for sib in br.next_siblings:
                if getattr(sib, "name", None) in ("li", "ul", "strong"):
                    break
                rest += sib.string if isinstance(sib, str) else (sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else "")
            names_txt = rest
        names = [n.strip() for n in re.split(r"[.,;]\s*", names_txt) if n.strip()]
        # vote li = the outer li (parent of the inner ul)
        outer_li = li.find_parent("li")
        if outer_li is None:
            outer_li = li
        items.append({"cat": label, "names": names, "vote_li": li, "outer_li": outer_li})

    # Group category labels into votes by outer_li identity
    votes_by_outer = defaultdict(dict)
    outer_to_idx = {}
    for it in items:
        key = id(it["outer_li"])
        if key not in outer_to_idx:
            outer_to_idx[key] = it["outer_li"]
        votes_by_outer[key][it["cat"]] = it["names"]

    for key, catnames in votes_by_outer.items():
        outer = outer_to_idx[key]
        # topic = text of outer li minus the inner ul and minus 'Imienne wyniki głosowania:'
        # clone
        clone_text = outer.get_text(" ", strip=True)
        # remove inner ul text
        for inner_ul in outer.find_all("ul"):
            inner_text = inner_ul.get_text(" ", strip=True)
            clone_text = clone_text.replace(inner_text, "", 1)
        topic = re.sub(r"\s*Imienne wyniki głosowania:\s*$", "", clone_text).strip()
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
        for cat, names in catnames.items():
            low = re.sub(r"\s*\(\d+\)$", "", cat).strip().lower()
            if low == "za":
                named["za"] = names
            elif low == "przeciw":
                named["przeciw"] = names
            elif "wstrzym" in low:
                named["wstrzymal_sie"] = names
            elif "nieobecn" in low:
                named["nieobecni"] = names
        if topic:
            votes.append({"topic": topic, "named": named})
    return votes


def _clean_name_token(tok: str) -> str:
    """Per-token cleanup: NBSP->space, strip Word paste markers, fix source typos."""
    tok = tok.replace("\xa0", " ").replace("\u200b", "")
    tok = tok.replace("StartFragment", "").replace("EndFragment", "")
    tok = re.sub(r"\s+", " ", tok).strip()
    if tok.lower() == "marek wieland":
        tok = "Marek Wieland"
    return tok


def normalize_records(records):
    """Fix merged/typo'd councilor names using the union roster.

    The source occasionally merges two names into one comma-less token
    (e.g. "Artur Forycki Róża Frąckowiak") or has a case typo
    (e.g. "Marek WIeland"). We canonicalise against the roster of all
    councilor names that appear alone elsewhere."""

    # first pass: collect clean single tokens across all records
    clean = {}
    for rec in records:
        for cat, names in rec["named"].items():
            for n in names:
                c = _clean_name_token(n)
                if c:
                    clean[c] = clean.get(c, 0) + 1

    # build roster of full-name tokens (2+ words, no digits)
    roster = [n for n in clean if " " in n and n.isalpha() or (" " in n and re.match(r"^[\wżźćńółśąęŻŹĆŃÓŁŚĄĘ -]+$", n))]
    # longer merge candidates: 3+ words that match two roster names
    merge_map = {}
    for tok, cnt in clean.items():
        words = tok.split()
        if len(words) < 3:
            continue
        # try to split into two names with a space boundary
        for i in range(1, len(words)):
            left, right = " ".join(words[:i]), " ".join(words[i:])
            if left in clean and right in clean:
                merge_map[tok] = (left, right)
                break

    for rec in records:
        for cat, names in rec["named"].items():
            out = []
            for n in names:
                c = _clean_name_token(n)
                if c in merge_map:
                    out.extend(merge_map[c])
                elif c:
                    out.append(c)
            rec["named"][cat] = out
    return records



def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        # attendees = za + przeciw + wstrzymal
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
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

    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": "", "district": None,
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat == "nieobecni":
                for nm in names:
                    if nm in councilors_data:
                        councilors_data[nm]["votes_nieobecny"] += 1
                continue
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                else:
                    councilors_data[nm]["votes_wstrzymal"] += 1

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)

    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
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
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": all_votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["session_date"] for r in records if r["session_date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({
            "name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {
                "club": "", "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                "aktywnosc": round(aktywn, 1),
                "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                "votes_nieobecny": 0, "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
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
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated": output.get("generated", ""),
                   "default_kadencja": output.get("default_kadencja", ""),
                   "kadencje": stubs}, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    sessions = discover_sessions(cache)
    print(f"[sroda] sesje IX kad.: {len(sessions)}")
    records = []
    for s in sessions:
        try:
            html = fetch(s["href"], cache)
            vs = parse_session_votes(html)
            for v in vs:
                records.append({"session_date": s["date"], "session_num": s["num"],
                                "topic": v["topic"], "named": v["named"]})
            print(f"  {s['date']} {s['num']:<8} votes={len(vs)}")
        except Exception as e:
            print(f"  [ERR {s['href'][-40:]}] {type(e).__name__}: {e}")

    output = build_output(normalize_records(records))
    profiles = build_profiles(normalize_records(records))
    save_split(output, city_dir / "docs" / "data.json", profiles)
    print(f"[sroda] total votes={output['kadencje'][0]['total_votes']} "
          f"sessions={output['kadencje'][0]['total_sessions']} "
          f"councilors={output['kadencje'][0]['total_councilors']}")


if __name__ == "__main__":
    main()
