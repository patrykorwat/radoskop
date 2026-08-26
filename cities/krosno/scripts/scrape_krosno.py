#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Krosno — imienne głosowania Rady Miasta (IX kadencja 2024-2029).

Źródło: BIP UM Krosna (bip.umkrosno.pl, CMS z ../mdTresc-cmPokaz../),
podsekcja "Imienny wykaz głosowań radnych Miasta Krosna" (kategoria 234).
Per sesja publikowany jest artykuł "Imienny wykaz głosowań radnych Miasta
Krosna przeprowadzonych podczas N sesji RM odbytej w dniu ..." z PLIKIEM PDF
(wykaz imiennych głosowań, downloadFile/{id}). Każdy PDF zawiera bloki
"Głosowanie Nr N." z tytułem uchwały i tabelą imienną:
        Radny           Oddany głos
        Wojciech Byczyński  Jestem za   / Jestem przeciw / Wstrzymuję się
Głosy ZA/PRZECIW/WSTRZYMUJĘ SIĘ per radny. Część bloków ma DWA przebiegi
głosowania (nad poprawką + nad projektem) — każdy wydawany jako osobne
głosowanie. Parsowane pdfplumber z rekonstrukcją wierszy.

Zakres: 27 sesji (II..XXVIII, 2024-05-28 .. 2026-06-29), 848 głosowań,
21 radnych. I sesja (inauguracyjna, 2024-05) nie publikuje imiennego wykazu —
tylko wybór przewodniczącego. Radni zweryfikowani z BIP (wykaz ślubowania).

Użycie:
    python scrape_krosno.py --output docs/data.json --profiles docs/profiles.json
                            [--config config.json]
"""

import argparse
import io
import json
import re
import ssl
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber

BASE = "https://bip.umkrosno.pl"
VOTES_CAT = "/articles/234/imienny-wykaz-glosowan-radnych-miasta-krosna"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (Radoskop/1.0)"}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# Kanoniczna lista radnych IX kadencji (wykaz ślubowania z BIP + Sołek mid-term).
ROSTER = ["Daria Balon", "Michał Baran", "Sławomir Bęben", "Wojciech Byczyński",
          "Anna Dubiel", "Michał Finfa", "Anna Galert", "Piotr Grudysz",
          "Robert Hanusek", "Janusz Hejnar", "Tomasz Józefowicz", "Paweł Krzanowski",
          "Zbigniew Kubit", "Kazimierz Mazur", "Marcin Niepokój", "Adam Przybysz",
          "Agnieszka Raś", "Małgorzata Szeliga", "Gabriel Zajdel", "Tomasz Zajdel",
          "Tomasz Soliński"]
ROSTER_SET = set(ROSTER)
RTOK = {"".join(c for c in unicodedata.normalize("NFKD", r) if not unicodedata.combining(c)).lower(): r
        for r in ROSTER}

# Etykiety głosów — wiersze używają "Wstrzymuje się" (bez 'j'), nagłówek "Wstrzymuję się".
VOTES = {"Jestem przeciw": "przeciw", "Wstrzymuję się": "wstrzymal_sie",
         "Wstrzymuje się": "wstrzymal_sie", "Jestem za": "za"}
VOTE_LABS = ("Jestem przeciw", "Wstrzymuję się", "Wstrzymuje się", "Jestem za")

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
          "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
         "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
         "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21,
         "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27,
         "XXVIII": 28}
ROMAN_REV = {v: k for k, v in ROMAN.items()}

CLUB_ASSIGN = None


def _norm(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


def _resolve_name(raw):
    raw = raw.strip()
    if raw in ROSTER_SET:
        return raw
    k = _norm(raw)
    if k in RTOK:
        return RTOK[k]
    parts = raw.split()
    if len(parts) >= 2:
        rk = _norm(" ".join(reversed(parts)))
        if rk in RTOK:
            return RTOK[rk]
    return None


def _fetch(url, cache_dir, timeout=40):
    if cache_dir:
        name = urllib.parse.quote(url, safe="") + (".bin" if url.endswith(".pdf") else ".html")
        fp = Path(cache_dir) / name
        if fp.exists():
            return fp.read_bytes()
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            r = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
            data = r.read()
            if cache_dir:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(data)
            return data
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(url)


def _raw_lines(data):
    lines = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pg in pdf.pages:
            t = pg.extract_text()
            if t:
                for ln in t.split("\n"):
                    lines.append(ln.rstrip())
    return lines


def _parse_pdf(data):
    """Zwraca bloki głosowań: [{no, title_lines, rounds:[{suffix,counts,votes:[(nm,cat)]}]}]."""
    lines = _raw_lines(data)
    clean = [s.strip() for s in lines if not re.fullmatch(r"\d{1,3}", s.strip())]
    blocks = []
    cur = None
    i, n = 0, len(clean)
    while i < n:
        s = clean[i]
        if re.match(r"Głosowanie (?:Nr\s*(\d+)|nad)", s):
            if cur:
                blocks.append(cur)
            mm = re.match(r"Głosowanie Nr\s*(\d+)|Głosowanie nad", s)
            cur = {"no": int(mm.group(1)) if (mm and mm.group(1)) else 1,
                   "title_lines": [], "rounds": [], "pending": None, "pending_vote": None}
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        is_counts_line = re.match(r"(Jestem za|Jestem przeciw|Wstrzymuj[ęe] się)\s+\d+\s+\d", s)
        round_ = cur["rounds"][-1] if cur["rounds"] else None
        if s.startswith("Wynik głosowania") or (round_ and round_["in_votes"] and is_counts_line):
            suf_parts = []
            j = i
            if s.startswith("Wynik głosowania"):
                rest = s[len("Wynik głosowania"):].strip()
                if rest.startswith("nad "):
                    suf_parts.append(rest[len("nad "):])
                elif rest:
                    suf_parts.append(rest)
                j = i + 1
                while j < n and not re.match(r"(Odpowiedź|Jestem za|Jestem przeciw|Wstrzymuj[ęe] się|Radny)", clean[j]):
                    suf_parts.append(clean[j])
                    j += 1
            suffix = " ".join(suf_parts)
            suffix = re.sub(r"\s*przedstawia się następująco\s*:?\s*$", "", suffix).strip()
            if round_ and not round_["counts"] and not round_["votes"] and round_["suffix"] == "":
                round_["suffix"] = suffix
            else:
                cur["rounds"].append({"suffix": suffix, "counts": {}, "votes": [], "in_votes": False,
                                      "pending": None, "pending_vote": None})
            round_ = cur["rounds"][-1]
            if is_counts_line and s.startswith("Jestem"):
                mm = re.match(r"(Jestem za|Jestem przeciw|Wstrzymuj[ęe] się)\s+(\d+)", s)
                round_["counts"][VOTES[mm.group(1)]] = int(mm.group(2))
            i = j
            continue
        round_ = cur["rounds"][-1] if cur["rounds"] else None
        if round_ is None:
            cur["title_lines"].append(s)
            i += 1
            continue
        if not round_["in_votes"]:
            mm = re.match(r"(Jestem za|Jestem przeciw|Wstrzymuj[ęe] się)\s+(\d+)", s)
            if mm:
                round_["counts"][VOTES[mm.group(1)]] = int(mm.group(2))
                i += 1
                continue
            if s.startswith("Radny") and re.search(r"Oddan\d?y\s*", s):
                round_["in_votes"] = True
                i += 1
                continue
            i += 1
            continue
        # wiersz głosowania (nazwisko może być łamane między wiersze)
        vote = None
        namepart = None
        for lab in VOTE_LABS:
            if s.endswith(lab):
                vote = lab
                namepart = s[:-len(lab)].strip()
                break
        if vote:
            namepart = re.sub(r"\s+\d+\s*$", "", namepart).strip()
            cat = VOTES[vote]
            nm = _resolve_name(namepart)
            if nm:
                round_["votes"].append((nm, cat))
                round_["pending"] = None
                round_["pending_vote"] = None
                i += 1
                continue
            if round_.get("pending"):
                cand = (round_["pending"] + " " + namepart).strip()
                nm2 = _resolve_name(cand)
                if nm2:
                    round_["votes"].append((nm2, round_.get("pending_vote") or cat))
                    round_["pending"] = None
                    round_["pending_vote"] = None
                    i += 1
                    continue
            round_["pending"] = namepart
            round_["pending_vote"] = cat
            i += 1
            continue
        else:
            if round_.get("pending"):
                test = (round_["pending"] + " " + s).strip()
                nn = _resolve_name(test)
                if nn:
                    round_["votes"].append((nn, round_.get("pending_vote")))
                round_["pending"] = None
                round_["pending_vote"] = None
            i += 1
            continue
    if cur:
        blocks.append(cur)
    return blocks


def _parse_to_votes(data):
    out = []
    for b in _parse_pdf(data):
        base = "\n".join(b["title_lines"]).strip()
        for rnd in b["rounds"]:
            vd = {}
            for nm, cat in rnd["votes"]:
                if cat:
                    vd[nm] = cat
            suf = (" (" + rnd["suffix"] + ")") if rnd["suffix"] else ""
            out.append({"no": b["no"], "suffix": rnd["suffix"], "title": base + suf,
                        "counts": rnd["counts"], "votes": vd})
    return out


def _session_date_from_title(title):
    m = re.search(r"w dniu (\d{1,2})\s+([a-ząćężźćśńłó]+)\s+(\d{4})", title)
    if m:
        d, mo, y = m.group(1), m.group(2).lower(), m.group(3)
        if mo in MONTHS:
            return f"{y}-{MONTHS[mo]:02d}-{int(d):02d}"
    return None


def _roman_of_title(title):
    m = re.search(r"(?:podczas|na)\s+([IVXLC]+)(?:\s*-?\s*[a-ząćężźłóśń]+)?\s+sesji", title)
    return m.group(1) if m else None


def _harvest(cache_dir):
    """Artykuły sesji IX kadencji z kategorii 234 -> [{date, roman, url}]."""
    html = _fetch(BASE + VOTES_CAT, cache_dir).decode("utf-8", "replace")
    arts = []
    seen = set()
    for href, txt in re.findall(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S):
        href = href.strip()
        if "article/imienny-wykaz-glosowan" not in href and "article/wyniki-glosowania-i-sesja" not in href:
            continue
        full = href if href.startswith("http") else BASE + href.split("?")[0]
        if full in seen:
            continue
        seen.add(full)
        title = re.sub(r"<[^>]+>", "", txt).strip()
        a = {"url": full, "title": title,
             "date": _session_date_from_title(title), "roman": _roman_of_title(title)}
        if a["date"]:
            arts.append(a)
    # I sesja (inauguracyjna) nie ma wykazu imiennego — pomijam (data < start IX).
    arts = [a for a in arts if a["date"]]
    arts.sort(key=lambda x: x["date"])
    return arts


def _session_pdf(session, cache_dir):
    html = _fetch(session["url"], cache_dir).decode("utf-8", "replace")
    names = re.findall(r'href=["\']([^"\']*downloadFile/(\d+))[^"\']*["\'][^>]*>(?:<[^>]+>)*([^<]{0,60})', html)
    cand = None
    for p, fid, nm in names:
        n = re.sub(r"\s+", " ", nm).strip()
        if cand is None:
            cand = fid
        if re.search(r"\d{1,2}\.\d{2}\.\d{2}|^\s*[IVXL]+\s*-", n):
            cand = fid
            break
    if cand is None:
        pdfs = re.findall(r'href=["\']([^"\']*downloadFile/\d+)[^"\']*["\']', html)
        cand = pdfs[-1].split("/")[-1] if pdfs else None
    return cand


def _club_of(name):
    if CLUB_ASSIGN:
        return CLUB_ASSIGN.get(name, "NZ")
    return ""


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()


def build_output(records):
    """records: [{date, roman, votes:[{no,suffix,title,counts,votes}]}]."""
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if d not in sessions_by_date:
            num = ROMAN_REV.get(rec["roman"], rec["roman"]) if isinstance(rec["roman"], int) else rec["roman"]
            sessions_by_date[d] = {"date": d, "number": num or "", "vote_count": 0, "attendees": set()}
        for raw in rec["votes"]:
            named = defaultdict(list)
            for _nm, _cat in raw["votes"].items():
                named[_cat].append(_nm)
            named = dict(named)
            vid += 1
            sessions_by_date[d]["vote_count"] += 1
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                sessions_by_date[d]["attendees"].update(named.get(cat, []))
            all_votes.append({
                "id": str(vid), "session_date": d,
                "session_number": sessions_by_date[d]["number"],
                "topic": raw.get("title") or "",
                "named_votes": dict(named),
                "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
            })
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
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0,
                                 "votes_with_club": 0, "votes_against_club": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                c = councilors_data.get(name)
                if not c:
                    continue
                c["votes_za"] += cat == "za"
                c["votes_przeciw"] += cat == "przeciw"
                c["votes_wstrzymal"] += cat == "wstrzymal_sie"

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat != "nieobecni":
                for nme in names:
                    councillor_sess[nme].add(v["session_date"])

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
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})

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
        same = sum(1 for vv in common if vectors[a][vv] == vectors[b][vv])
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_of(x) for x in all_names)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": dict(club_counts),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        for raw in rec["votes"]:
            for name, cat in raw["votes"].items():
                cv[name][cat] += 1
                cv[name]["votes"].append({"session": d, "vote": cat})
    all_sess = {rec["date"] for rec in records}
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        present_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / len(all_sess) if all_sess else 0.0
        profiles.append({
            "name": name, "slug": _slug(name),
            "kadencje": {KADENCJA_ID: {
                "club": _club_of(name), "has_voting_data": True, "has_activity_data": False,
                "frekwencja": round(frekw, 1), "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"], "votes_wstrzymal": vd["wstrzymal_sie"],
                "votes_brak": 0, "votes_nieobecny": 0, "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
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
             "default_kadencja": output.get("default_kadencja", ""), "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    global CLUB_ASSIGN
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    if args.config and Path(args.config).exists():
        CLUB_ASSIGN = json.loads(Path(args.config).read_text(encoding="utf-8")).get("club_assignments") or {}

    print("=== Scraper Rada Miasta Krosna (bip.umkrosno.pl, kat. 234) ===")
    sessions = _harvest(cache_dir)
    print(f"  Sesje IX kadencji z wykazem imiennym: {len(sessions)}")
    records = []
    for s in sessions:
        fid = _session_pdf(s, cache_dir)
        if not fid:
            print(f"    sesja {s['date']}: brak PDF"); continue
        data = _fetch(f"{BASE}/downloadFile/{fid}", cache_dir)
        votes = _parse_to_votes(data)
        if not votes:
            print(f"    sesja {s['date']}: 0 głosowań"); continue
        records.append({"date": s["date"], "roman": s.get("roman"), "votes": votes})
        time.sleep(0.2)
    print(f"  Sesje z głosowaniami: {len(records)}, głosowań: "
          f"{sum(len(r['votes']) for r in records)}")

    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    kad = output["kadencje"][0]
    print(f"  Zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")
    print(f"  Głosowań: {kad['total_votes']}, sesji: {kad['total_sessions']}, radnych: {kad['total_councilors']}")


if __name__ == "__main__":
    main()
