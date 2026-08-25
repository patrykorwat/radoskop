#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Radoskop Rybnik — imienne głosowania Rady Miasta Rybnika.

Źródło: BIP Miasta Rybnika (ASP.NET, bip.um.rybnik.eu).
Rada Miasta Rybnika (IX kadencja 2024-2029, 25 radnych) publikuje w dziale
"Rada Miasta -> Sesje Rady Miasta -> Wyniki głosowań" (Default.aspx?Page=358)
dla każdej sesji obu wersję RTF "Wyniki głosowań z sesji nr <XXIX> Rady Miasta
z dnia <YYYY-MM-DD>.rtf" z głosowaniami imiennymi. Każdy punkt głosowania ma
strukturę:

    <temat>. Za <n> radnych  <Imię Nazwisko> (KLUB), ...
             Przeciw <n> radnych <Imię Nazwisko> (KLUB), ...
             Wstrzymało się <n> radnych <Imię Nazwisko> (KLUB), ...

Raport podaje per radnego jego klub (KO/PiS/WdR/BRR), co pozwala solidnie
wypełnić club_assignments. Pliki to RTF (ansicpg 1250; polskie znaki jako
\'HH escape'y + \uN? unicode), parsujemy ręcznie.

Struktura BIP:
  1. Rejestr:  Default.aspx?Page=358  (wiersze "Wyniki głosowań z sesji nr <nr>
     Rady Miasta z dnia <data>" + link Download.ashx?id=<id>)
  2. Plik:    Download.ashx?id=<id>  (RTF)

Użycie (jak wywołuje scrape_all.sh):
    python scrape_rybnik.py --output docs/data.json --profiles docs/profiles.json
                            [--cache-dir .cache]
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests")
    sys.exit(1)

BIP = "https://bip.um.rybnik.eu"
REJESTR_PAGE = f"{BIP}/Default.aspx?Page=358"   # Wyniki głosowań

KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
IX_START = "2024-05-07"

CLUB_FULL = {
    "KO": "Koalicja Obywatelska",
    "PiS": "Prawo i Sprawiedliwość",
    "WdR": "Wspólnie dla Rybnika",
    "BRR": "Bezpartyjni Radni Rybnika",
    "NZ": "Niezrzeszeni",
}

REQ_DELAY = 0.5
_LAST_REQ = 0.0


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url, cache_dir=None, binary=False):
    import hashlib
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".html"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                        timeout=60)
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
# RTF -> tekst (ansicpg 1250)
# ---------------------------------------------------------------------------

def _rom_to_int(s: str):
    val = 0
    prev = 0
    for ch in reversed(s.strip().upper()):
        cur = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}.get(ch, 0)
        if cur < prev:
            val -= cur
        else:
            val += cur
            prev = cur
    return val if val > 0 else None


def rtf_to_text(raw: bytes) -> str:
    t = raw.decode('cp1250', 'replace')

    def usub(m):
        try:
            return chr(int(m.group(1)) & 0xFFFF)
        except Exception:
            return ''
    t = re.sub(r'\\u(-?\d+)\?', usub, t)

    def hsub(m):
        try:
            return bytes([int(m.group(1), 16)]).decode('cp1250')
        except Exception:
            return ''
    t = re.sub(r"\\'([0-9a-fA-F]{2})", hsub, t)

    t = re.sub(r'\\(par|line|row)\b', '\n', t, flags=re.I)
    t = re.sub(r'\\(tab|cell)\b', '\t', t, flags=re.I)
    t = re.sub(r'\\[a-zA-Z]+-?\d* ?', '', t)
    t = re.sub(r'\\[|{}]', '', t)
    t = re.sub(r'\\([^a-zA-Z])', r'\1', t)
    t = t.replace('{', '').replace('}', '')
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t


def clean_text(t: str) -> str:
    i = t.lower().find('504b0304')
    if i != -1:
        t = t[:i]
    else:
        last = max(t.rfind('radnych'), t.rfind('Wstrzymało'))
        m = re.search(r'\n[0-9a-fA-F]{40,}', t[last:] if last != -1 else t)
        if m and last != -1:
            t = t[:last + m.start()]
    # polącz łamania wiersza tnące wyraz (mała litera -> mała litera)
    t = re.sub(r'(?<=[a-ząćęłńóśźż0-9])\n(?=[a-ząćęłńóśźż])', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t


MONTHS_PL = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5, 'czerwca': 6,
             'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10, 'listopada': 11, 'grudnia': 12}

CLUB_RE = (r'\(\s*([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+(?:\s+[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)*)\s*\)')


def norm_club(c: str) -> str:
    return re.sub(r'\s+', '', c)


def clean_display_name(name: str) -> str:
    return re.sub(r'\s+', ' ', name).strip()


def parse_leading_names(section: str):
    """Parsuje wiodące pozycje radnych 'Imię Nazwisko (KLUB), ...'; zatrzymuje się
    na pierwszej pozycji niebędącej radnym. Zwraca (lista {'name','club'}, end_pos)."""
    out = []
    text = section
    if not text or not text.strip():
        return out, 0
    pos, N = 0, len(text)
    while pos < N:
        m = re.match(r'\s*([A-Za-zĄĆĘŁŃÓŚŹŻ][A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż\.\- ]*?)\s*' + CLUB_RE,
                     text[pos:])
        if not m:
            break
        name = clean_display_name(m.group(1))
        club = norm_club(m.group(2))
        if not name or not club:
            break
        out.append({"name": name, "club": club})
        pos += m.end()
        if pos < N and text[pos] == ',':
            pos += 1
            while pos < N and text[pos] in ' \t':
                pos += 1
        else:
            break
    return out, pos


def parse_votes(raw: bytes):
    t = clean_text(rtf_to_text(raw))
    sd = None
    sm = re.search(r'z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})\s*r?\.?', t)
    if sm and sm.group(2).lower() in MONTHS_PL:
        d, mo, yr = int(sm.group(1)), MONTHS_PL[sm.group(2).lower()], int(sm.group(3))
        sd = f"{yr:04d}-{mo:02d}-{d:02d}"

    za = list(re.finditer(r'Za\s+(\d+)\s+radny(?:ch)?', t))
    if not za:
        return sd, []
    body_start = t.find('Wyniki głosowań')
    if body_start == -1:
        body_start = 0
    votes = []
    wz_prev_remainder = t[body_start:za[0].start()]
    for i, zm in enumerate(za):
        seg_start = zm.end()
        seg_end = za[i + 1].start() if i + 1 < len(za) else len(t)
        seg = t[seg_start:seg_end]
        pr = re.search(r'Przeciw\s+(\d+)\s+radny(?:ch)?', seg)
        wz = re.search(r'Wstrzymało\s+się\s+(\d+)\s+radny(?:ch)?', seg)
        za_sec = seg[:pr.start()] if pr else seg
        pr_sec = seg[pr.end():wz.start()] if (pr and wz) else ''
        wz_sec = seg[wz.end():] if wz else ''
        za_names, _ = parse_leading_names(za_sec)
        pr_names, _ = parse_leading_names(pr_sec)
        wz_names, wz_consumed = parse_leading_names(wz_sec)
        topic = wz_prev_remainder
        # usuń nagłówek sesji z pierwszego tematu (wariant z/bez "nr XXVII")
        topic = re.sub(r'^Wyniki głosowań z sesji\s*(?:nr\s*\S*\s*)?Rady Miasta Rybnika\s*z dnia.*?r?\.\s*',
                       '', topic, flags=re.I)
        topic = re.sub(r'^[\s,.;:_]*', '', topic)
        topic = re.sub(r'Gł\s*osowanie\s*\d+\s*[:.]?\s*', '', topic, flags=re.I)
        topic = re.sub(r'Gł\s*osowanie\s*\*[A-Za-zА-Я]*\s*[:.]?\s*', '', topic, flags=re.I)
        topic = re.sub(r'Głosowanie\s*\*[A-Za-z]*\s*', '', topic)
        topic = re.sub(r'_Hlk\d+\s*:\s*', '', topic)
        topic = re.sub(r'\s+', ' ', topic).strip()
        wz_prev_remainder = re.sub(r'\s+', ' ', wz_sec[wz_consumed:]).strip()
        votes.append({
            "topic": topic,
            "counts": {"za": int(zm.group(1)),
                       "przeciw": int(pr.group(1)) if pr else 0,
                       "wstrzymal_sie": int(wz.group(1)) if wz else 0},
            "named": {"za": za_names, "przeciw": pr_names, "wstrzymal_sie": wz_names},
        })
    return sd, votes


# ---------------------------------------------------------------------------
# 1. Rejestr sesji (IX kadencja)
# ---------------------------------------------------------------------------

def parse_registry(html: str):
    """z HTML Default.aspx?Page=358 -> [{'sesja','date','id'}], IX kadencja."""
    sessions = []
    for m in re.finditer(
            r'<tr>\s*<td>(Wyniki głosowań z sesji nr\s+(.+?)\s+Rady Miasta z dnia\s+([\d-]+))'
            r'</td><td><a[^>]+href="[^"]*Download\.ashx\?id=(\d+)"',
            html, re.S):
        full, nr, date, did = m.groups()
        rn = _rom_to_int(nr)
        if rn is None:
            continue
        sessions.append({"sesja": rn, "date": date, "id": did})
    # dedupe (sesje bywają 2x) + tylko IX kadencja
    seen, out = set(), []
    for s in sessions:
        if (s["sesja"], s["date"]) in seen:
            continue
        seen.add((s["sesja"], s["date"]))
        if s["date"] >= IX_START:
            out.append(s)
    out.sort(key=lambda x: x["date"])
    return out


def fetch_registry(cache_dir=None):
    html = fetch(REJESTR_PAGE, cache_dir)
    return parse_registry(html)


# ---------------------------------------------------------------------------
# 2. Kanoniczna lista radnych (rezolucja nazw z rozbitymi spacjami)
# ---------------------------------------------------------------------------

def _key(name: str) -> str:
    s = unicodedata.normalize('NFD', re.sub(r'\s+', '', name).lower())
    return ''.join(ch for ch in s if unicodedata.category(ch) != 'Mn')


SKLAD = ("Białous Tadeusz, Brzózka Joanna, Florczyk Marek, Głupczyk Grzegorz, Kaim Marian, "
         "Kazek Krzysztof, Kłosek Łukasz, Knesz Radosław, Kurpanik Franciszek, Lazar Jerzy, "
         "Małek Mirosław, Mularczyk Jacek, Nowara Aleksandra, Pałka Tadeusz, Piaskowy Małgorzata, "
         "Pierchała Kamil, Sączek Andrzej, Szafraniec Krzysztof, Szutka Mirela, Szymura Karol, "
         "Śmieja Sebastian, Twardawa Damian, Wawrzyn Paweł, Węglorz Mariusz, Wojaczek Andrzej")


def build_roster():
    roster = {}
    for nazw in SKLAD.split(', '):
        p = nazw.strip().split()
        display = f"{p[-1]} {p[0]}" if len(p) == 2 else nazw.strip()
        roster[_key(display)] = display
    return roster


def resolve(name: str, roster: dict):
    k = _key(name)
    if k in roster:
        return roster[k]
    for rk, disp in roster.items():
        if rk in k or k in rk:
            return disp
    return None


# ---------------------------------------------------------------------------
# 3. Budowanie data.json / kadencja-*/profiles.json
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z', 'Ą': 'A', 'Ć': 'C', 'Ę': 'E',
            'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = _key(name)
    for pl, a in repl.items():
        slug = slug.replace(pl.lower(), a)
    slug = slug.replace(' ', '-').replace("'", "")
    return slug


def current_club_for_councilor(name, club_records):
    """club_records: {councilor_name: {club: count}} -> klub występujący w
    najbardziej aktualnych data (użyj rekordu z najpóźniejszą sesją)."""
    # club_records already aggregated per session; take most common in latest session
    return None


def build_output(records, roster, club_current):
    """records: [{session_date, votes:[{topic,counts,named:[{name,club}]}]}]
    club_current: {name: club_code}"""
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": "", "vote_count": 0,
                                   "attendees": set(), "speakers": []}
        for v in rec["votes"]:
            vid += 1
            named_clean = {k: [n["name"] for n in v["named"][k]] for k in ("za", "przeciw", "wstrzymal_sie")}
            sessions_by_date[d]["vote_count"] += 1
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                sessions_by_date[d]["attendees"].update(named_clean[cat])
            all_votes.append({
                "id": str(vid),
                "session_date": d,
                "session_number": "",
                "topic": v["topic"] or "",
                "named_votes": named_clean,
                "counts": {k: v["counts"][k] for k in ("za", "przeciw", "wstrzymal_sie")},
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
    for v in all_votes:
        for cat_names in v["named_votes"].values():
            all_names.update(cat_names)

    councilors_data = {}
    for name in sorted(all_names):
        councilors_data[name] = {
            "name": name, "club": club_current.get(name, "NZ"), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0,
            "votes_with_club": 0, "votes_against_club": 0, "rebellions": [],
        }
    # nowy klub policzymy po zebraniu głosów (votes_with_club itd.) — pomijamy
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)

    councillor_sess = defaultdict(set)
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        for v in rec["votes"]:
            for cat, names in v["named"].items():
                if cat != "nieobecni":
                    for n in names:
                        councillor_sess[n["name"]].add(d)

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False,
            "activity": None,
        })

    # similarity
    vectors = defaultdict(dict)
    for v in all_votes:
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
        pairs.append({"a": a, "b": b, "club_a": club_current.get(a, "NZ"),
                      "club_b": club_current.get(b, "NZ"),
                      "score": score, "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(club_current.get(n, "NZ") for n in all_names)
    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "clubs": dict(club_counts),
        "sessions": sessions_data, "total_sessions": total_sessions,
        "total_votes": total_votes, "total_councilors": len(councilors_list),
        "councilors": councilors_list, "votes": all_votes,
        "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1],
    }
    return {
        "generated": datetime.now().isoformat(),
        "default_kadencja": KADENCJA_ID,
        "kadencje": [kad],
    }


def build_profiles(records, club_current):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        for v in rec["votes"]:
            for cat, names in v["named"].items():
                for n in names:
                    key = cat
                    cv[n["name"]][key] += 1
                    cv[n["name"]]["votes"].append({"session": d, "vote": key})
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
                    "club": club_current.get(name, "NZ"), "has_voting_data": True,
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, help="docs/data.json")
    ap.add_argument("--profiles", required=True, help="docs/profiles.json")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    roster = build_roster()

    print("=== Scraper Rada Miasta Rybnika (bip.um.rybnik.eu, RTF) ===")
    sessions = fetch_registry(cache_dir)
    print(f"  Sesje IX kadencji w rejestrze: {len(sessions)}")
    if not sessions:
        print("  BŁĄD: brak sesji"); sys.exit(1)

    records = []
    ok = fail = 0
    club_latest = {}                       # name -> club z ostatniej (najnowszej) sesji
    for i, s in enumerate(sessions):
        url = f"{BIP}/Download.ashx?id={s['id']}"
        print(f"  [{i+1}/{len(sessions)}] {s['date']} sesja {s['sesja']}")
        try:
            rtf = fetch(url, cache_dir, binary=True)
            sd, votes = parse_votes(rtf)
            print(f"    data={sd}  głosowań={len(votes)}")
            if len(votes) == 0:
                fail += 1
                continue
            # rezonuj nazwy radnych do kanonicznej listy (usuwa rozbite spacją
            # warianty typu "K łosek" -> "Łukasz Kłosek"); odrzuć nieodczytane.
            for v in votes:
                for cat in ("za", "przeciw", "wstrzymal_sie"):
                    kept = []
                    for nm in v["named"][cat]:
                        r = resolve(nm["name"], roster)
                        if not r:
                            continue
                        kept.append({"name": r, "club": nm["club"]})
                    v["named"][cat] = kept
            records.append({"session_date": sd, "votes": votes})
            ok += 1
            # klub bieżący = klub z najbardziej aktualnej sesji (iterujemy wg daty)
            for v in votes:
                for cat in ("za", "przeciw", "wstrzymal_sie"):
                    for nm in v["named"][cat]:
                        club_latest[nm["name"]] = nm["club"]
        except Exception as e:
            print(f"    BŁĄD: {e}")
            fail += 1

    print(f"\n  Sesje z danymi: {ok}, bez: {fail}")
    if not records:
        print("  BŁĄD: zero sesji z danymi"); sys.exit(1)

    # mapuj etykiety klubu do kanonicznych kodów (niezrzeszony/BSR/LepszyRybnik -> NZ)
    RAW2CODE = {"KO": "KO", "PiS": "PiS", "WdR": "WdR", "BRR": "BRR"}
    def code(raw):
        return RAW2CODE.get(raw, "NZ")
    club_current = {name: code(club) for name, club in club_latest.items()}

    output = build_output(records, None, club_current)
    profiles = build_profiles(records, club_current)
    save_split(output, args.output, profiles)

    # raport klubu dla config
    print("\n=== KLUBY (per radny, do club_assignments) ===")
    for name in sorted(club_current):
        print(f"  {name:28s} -> {club_current[name]}")
    print(f"\n  kluby zbiorczo: {dict(Counter(club_current.values()))}")

    total_votes = sum(len(r["votes"]) for r in records)
    print("\n=== PODSUMOWANIE ===")
    print(f"  sesji z danymi: {len(records)}")
    print(f"  głosowań imiennych: {total_votes}")
    print(f"  radnych: {len(club_current)}")
    print(f"  zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")


if __name__ == "__main__":
    main()
