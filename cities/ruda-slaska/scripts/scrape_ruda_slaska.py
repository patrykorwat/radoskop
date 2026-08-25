#!/usr/bin/env python3
"""
Radoskop Ruda Śląska — imienne głosowania Rady Miejskiej.

Źródło: BIP na platformie bip.info.pl (https://rudaslaska.bip.info.pl).
Rada Miasta Ruda Śląska (IX kadencja 2024-2029, 25 radnych) publikuje per sesję
dokument "Raport z głosowań <data>.pdf" (generowany programem eSesja.pl) z
głosowaniami imiennymi: dla każdego punktu agregat ZA/PRZECIW/WSTRZYMUJĘ SIĘ/
BRAK GŁOSU/NIEOBECNI + lista per radny "Nazwisko Imię (GŁOS)".

Struktura BIP (platforma bip.info.pl, Sputnik-style):
  1. Sesje - kadencja 2024-2029:  index.php?idmp=3240&r=o  (lista sesji)
  2. Sesja w dniu ...:            index.php?idmp=<sesja>&r=o
     -> link "Sesja Rady Miasta ..." dokument.php?iddok=<iddok>&idmp=<sesja>&r=o
  3. Dokument sesji:              dokument.php?iddok=<iddok>&idmp=<sesja>&r=o
     -> załącznik "Raport z głosowań <data>.pdf"  plik.php?id=<plik>&wer=1
  4. PDF raportu:                 parsimy imienne głosowania (pdfplumber)

Format PDF "Raport z głosowań":
  "<RZYMSKA> Sesja w dniu <dd miesiąc yyyy>
   Przeprowadzone głosowania
   <n>. Głosowanie w sprawie <temat>. - czas głosowania: <data>, godz. <hh:mm>,
       wyniki: ZA: X, PRZECIW: Y, WSTRZYMUJĘ SIĘ: Z, BRAK GŁOSU: W, NIEOBECNI: V
       Wyniki imienne: <Imię Nazwisko> (ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU), ...
   ...
   Uczestnictwo w głosowaniach
   <n>. <Nazwisko> <Imię>: <x>/<total>"

Pomijamy głosowanie "Sprawdzenie obecności" (brak ZA/PRZECIW — to tylko kworum).
Głosy mapujemy: ZA->za, PRZECIW->przeciw, WSTRZYMUJĘ SIĘ->wstrzymal_sie,
BRAK GŁOSU->brak_glosu, NIEOBECNY/NIEOBECNA->nieobecni.

Użycie (jak wywołuje scrape_all.sh):
    python scrape_ruda_slaska.py --output docs/data.json --profiles docs/profiles.json
                                [--cache-dir .cache]
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests")
    sys.exit(1)

try:
    import pdfplumber
except ImportError:
    print("Zainstaluj: pip install pdfplumber")
    sys.exit(1)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://rudaslaska.bip.info.pl"
SESSIONS_IDMP = 3240  # "Sesje - kadencja 2024 - 2029"

KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11,
    "grudnia": 12,
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


def fetch(url: str, cache_dir: Path | None = None, binary: bool = False):
    """Pobiera URL (tekst lub binarne) z opcjonalnym cache-em dyskowym po MD5."""
    import hashlib
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".html"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            data = cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
            return data
    _rate()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                        timeout=40, verify=False)
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


def fetch_bytes(url: str, cache_dir: Path | None = None):
    d = fetch(url, cache_dir, binary=True)
    return d


# ---------------------------------------------------------------------------
# 1. Lista sesji kadencji 2024-2029
# ---------------------------------------------------------------------------

def parse_session_list(cache_dir: Path | None = None):
    """Zwraca listę sesji: [{idmp, label}] z menu 'Sesje - kadencja 2024-2029'."""
    url = f"{BIP}/index.php?idmp={SESSIONS_IDMP}&r=o"
    html = fetch(url, cache_dir)
    sessions = []
    for m in re.finditer(
            r"href=['\"]index\.php\?idmp=(\d+)&amp;r=o['\"][^>]*>\s*([^<]{4,90}?)\s*<", html):
        idmp = m.group(1)
        label = re.sub(r"\s+", " ", m.group(2)).strip()
        low = label.lower()
        if low.startswith("terminy sesji"):
            continue
        if "sesja" not in low:
            continue
        sessions.append({"idmp": idmp, "label": label})
    # dedupe (zachowaj kolejność)
    seen = set()
    out = []
    for s in sessions:
        if s["idmp"] in seen:
            continue
        seen.add(s["idmp"])
        out.append(s)
    return out


def session_document_iddok(session_idmp: str, cache_dir: Path | None = None):
    """Ze strony sesji znajduje iddok dokumentu 'Sesja Rady Miasta ...'."""
    url = f"{BIP}/index.php?idmp={session_idmp}&r=o"
    html = fetch(url, cache_dir)
    doc = None
    for m in re.finditer(
            r"href=['\"]dokument\.php\?iddok=(\d+)&amp;idmp=" + session_idmp +
            r"&amp;r=o['\"][^>]*>\s*([^<]{4,100}?)\s*<", html):
        iddok = m.group(1)
        label = re.sub(r"\s+", " ", m.group(2)).strip().lower()
        if "transmisj" in label or "terminy" in label:
            continue
        if "sesja" in label:
            doc = iddok
            break
    return doc


def _norm_name(txt: str) -> str:
    """Normalizuje nazwę pliku: lowercase + ł->l dla porównań diakrytyk-niezależnych."""
    return txt.lower().replace("ł", "l")


def raport_attachment(iddok: str, cache_dir: Path | None = None):
    """Ze strony dokumentu sesji zwraca URL pliku 'Raport z głosowań ... .pdf'."""
    url = f"{BIP}/dokument.php?iddok={iddok}&idmp=0&r=o"
    html = fetch(url, cache_dir)
    block = html
    m = re.search(r'<div[^>]*class="[^"]*doc-attachments[^"]*"(.*?)</div>', html, re.S)
    if m:
        block = m.group(1)
    for a in re.findall(r"<a href='(plik\.php\?id=\d+&wer=\d+)'[^>]*>\s*([^<]{3,80}?)\s*</a>", block):
        href, txt = a
        name = txt.strip()
        nn = _norm_name(name)
        # Raport głosowań = plik którego nazwa zawiera 'glosow' (odporne na
        # warianty: 'Raport z głosowań...', 'Głosowania.pdf', 'glosowan...').
        if "glosow" in nn:
            if nn.endswith((".doc", ".docx")):
                continue  # binaria — pomijamy (raport 2024 w starym .doc)
            return f"{BIP}/{href.replace('&', '&')}"
    return None


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF raportu
# ---------------------------------------------------------------------------

def _parse_dm(text):
    """'30 lipca 2026' -> '2026-07-30' (lub None)."""
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    if not m:
        return None
    day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    if mon not in MONTHS_PL:
        return None
    return f"{year:04d}-{MONTHS_PL[mon]:02d}-{day:02d}"


def parse_raport_pdf(data: bytes):
    """Parsuje PDF 'Raport z głosowań' na punkty imienne."""
    with pdfplumber.open(__import__("io").BytesIO(data)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    full = re.sub(r"[\s\u00a0]+", " ", "\n".join(pages)).strip()

    # data sesji z nagłówka: "<RZYMSKA> Sesja w dniu <dd month yyyy>"
    session_date = None
    hm = re.search(r"Sesja w dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})", full)
    if hm:
        session_date = _parse_dm(f"{hm.group(1)} {hm.group(2)} {hm.group(3)}")

    votes = []
    for m in re.finditer(
            r"(\d+)\.\s*Głosowanie w sprawie\s+(.+?)\s+-\s+czas głosowania:"
            r"\s+[\d \w:.,]+\s+wyniki:\s+(.+?)\s*Wyniki imienne:\s*(.+?)"
            r"(?=\s+\d+\.\s*Głosowanie w sprawie|\s+Uczestnictwo w głosowaniach|$)",
            full):
        num, topic, wyniki, names = m.group(1), m.group(2), m.group(3), m.group(4)
        topic = re.sub(r"\s+", " ", topic).strip()
        # pomiń głosowanie kworum (Sprawdzenie obecności) — brak ZA/PRZECIW
        if "ZA:" not in wyniki:
            continue
        # agregat
        counts = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
        for key, pat in [("za", r"ZA:\s*(\d+)"), ("przeciw", r"PRZECIW:\s*(\d+)"),
                         ("wstrzymal_sie", r"WSTRZYMUJĘ SIĘ:\s*(\d+)"),
                         ("brak_glosu", r"BRAK GŁOSU:\s*(\d+)"),
                         ("nieobecni", r"NIEOBECNI:\s*(\d+)")]:
            cm = re.search(pat, wyniki)
            if cm:
                counts[key] = int(cm.group(1))
        # imienne
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [],
                 "brak_glosu": [], "nieobecni": []}
        for nm in re.finditer(
                r"([A-ZĄĆĘŁŃÓŚŹŻ][\w\-ąćęłńóśźż ]+?)\s*\((ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECN[YA]|OBECN[YA])\)",
                names):
            name = re.sub(r"\s+", " ", nm.group(1)).strip()
            vote = nm.group(2)
            cat = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
                   "BRAK GŁOSU": "brak_glosu"}.get(vote, "nieobecni")
            named[cat].append(name)
        votes.append({
            "num": num,
            "topic": topic,
            "counts": counts,
            "named": named,
        })
    return {"session_date": session_date, "votes": votes}


# ---------------------------------------------------------------------------
# 3. Budowanie data.json / kadencja-*/profiles.json
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z', 'Ą': 'A', 'Ć': 'C', 'Ę': 'E',
            'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    slug = slug.replace(' ', '-').replace("'", "")
    return slug


def build_output(records):
    """records: [{session_date, votes:[{topic,counts,named}]}]"""
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
            named_clean = {k: list(vals) for k, vals in v["named"].items()}
            sessions_by_date[d]["vote_count"] += 1
            for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
                sessions_by_date[d]["attendees"].update(v["named"].get(cat, []))
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
            "name": name, "club": "Niezrzeszeni", "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0,
            "votes_with_club": 0, "votes_against_club": 0, "rebellions": [],
        }
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        for v in rec["votes"]:
            for cat, names in v["named"].items():
                for name in names:
                    if name not in councilors_data:
                        continue
                    c = councilors_data[name]
                    if cat == "za":
                        c["votes_za"] += 1
                    elif cat == "przeciw":
                        c["votes_przeciw"] += 1
                    elif cat == "wstrzymal_sie":
                        c["votes_wstrzymal"] += 1
                    elif cat == "nieobecni":
                        c["votes_nieobecny"] += 1
                    else:
                        c["votes_brak"] += 1

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
                        councillor_sess[n].add(d)

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": c["name"], "club": "Niezrzeszeni", "district": None,
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
        pairs.append({"a": a, "b": b, "club_a": "Niezrzeszeni", "club_b": "Niezrzeszeni",
                      "score": score, "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "clubs": {"Niezrzeszeni": len(councilors_list)},
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


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        for v in rec["votes"]:
            for cat, names in v["named"].items():
                for name in names:
                    key = "za" if cat == "za" else "przeciw" if cat == "przeciw" \
                        else "wstrzymal_sie" if cat == "wstrzymal_sie" \
                        else "nieobecny" if cat == "nieobecni" else "brak"
                    cv[name][key] += 1
                    cv[name]["votes"].append({"session": d, "vote": key})
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
                    "club": "Niezrzeszeni", "has_voting_data": True,
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
    import json as _json
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            _json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {"generated": output.get("generated", ""),
             "default_kadencja": output.get("default_kadencja", ""),
             "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        _json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        _json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


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

    print("=== Scraper Rada Miasta Ruda Śląska (bip.info.pl) ===")
    sessions = parse_session_list(cache_dir)
    print(f"  Sesje w menu kadencji 2024-2029: {len(sessions)}")
    if not sessions:
        print("  BŁĄD: brak sesji"); sys.exit(1)

    records = []
    ok = fail = 0
    for i, s in enumerate(sessions):
        print(f"  [{i+1}/{len(sessions)}] idmp={s['idmp']} {s['label'][:50]}")
        try:
            iddok = session_document_iddok(s["idmp"], cache_dir)
            if not iddok:
                print("    brak dokumentu sesji; pomijam")
                fail += 1
                continue
            pl = raport_attachment(iddok, cache_dir)
            if not pl:
                print("    brak 'Raport z głosowań'; pomijam")
                fail += 1
                continue
            pdf = fetch_bytes(pl, cache_dir)
            parsed = parse_raport_pdf(pdf)
            n = len(parsed["votes"])
            print(f"    raport {parsed['session_date']} : {n} głosowań imiennych")
            if n > 0:
                records.append(parsed)
                ok += 1
            else:
                fail += 1
        except Exception as e:
            print(f"    BŁĄD: {e}")
            fail += 1

    print(f"\n  Sesje z danymi: {ok}, bez danych: {fail}")
    if not records:
        print("  BŁĄD: zero sesji z danymi"); sys.exit(1)

    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)

    total_votes = sum(len(r["votes"]) for r in records)
    all_names = set()
    for r in records:
        for v in r["votes"]:
            for names in v["named"].values():
                all_names.update(names)
    print("\n=== PODSUMOWANIE ===")
    print(f"  sesji z danymi: {len(records)}")
    print(f"  głosowań imiennych: {total_votes}")
    print(f"  radnych: {len(all_names)}")
    print(f"  zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")


if __name__ == "__main__":
    main()
