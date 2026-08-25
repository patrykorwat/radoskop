#!/usr/bin/env python3
"""
Radoskop Legnica — imienne głosowania Rady Miejskiej Legnicy (IX kadencja 2024-2029).

Źródło: custom CMS BIP-E.PL (https://um.bip.legnica.eu), sekcja
"Rada Miejska -> Sesje -> Wyniki głosowań". Dla każdej sesji publikowany jest
PDF "glosowanieSesja2.pdf" / "glosowania<N>.pdf" / "Wynikiglosowan...pdf"
z głosowaniami imiennymi: dla każdego punktu nagłówek + agregat
(Liczba uprawnionych / obecnych / Głosy za / przeciw / wstrzymujące się /
nieoddane) + tabela per radny "Lp  Nazwisko i imię  GŁOS" (dwie kolumny).

Struktura:
  1. /uml/rada-miejska/sesje/wyniki-glosowan  (lista sesji, paginacja ?page=N)
     -> linki /uml/rada-miejska/sesje/wyniki-glosowan/<id>,<slug>.html
  2. Każda sesja -> link /download/<k>/**/glosowanie*.pdf
  3. PDF -> parsujemy imienne głosowania (pdfplumber). Mapa głosów:
     ZA->za, PRZECIW->przeciw, WSTRZYMUJĘ SIĘ->wstrzymal_sie,
     NIEODDANY->nieoddany, NIEOBECNY/NIEOBECNA->nieobecny.

Tylko IX kadencja (sesje od 2024-05-07). Kluby radnych kuratorowane z BIP
"Kluby radnych" / "Skład osobowy" (config.json club_assignments).

Użycie (jak wywołuje scrape_all.sh / nas):
    python scrape_legnica.py --output docs/data.json --profiles docs/profiles.json
                [--config config.json]
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

import requests
import pdfplumber
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://um.bip.legnica.eu"
VOTES_INDEX = "/uml/rada-miejska/sesje/wyniki-glosowan"

KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
IX_START = "2024-05-07"

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
    "wrzesnia": 9, "pazdziernika": 10,
}

REQ_DELAY = 0.6
_LAST_REQ = 0.0
UA = "Mozilla/5.0 (compatible; Radoskop/1.0 bot)"


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, binary: bool = False, tries: int = 5):
    for t in range(tries):
        try:
            _rate()
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=40,
                                verify=False)
            if resp.status_code == 200:
                return resp.content if binary else resp.text
            time.sleep(1.0 + t)
        except Exception:
            time.sleep(1.0 + t)
    return None


# --------------------------------------------------------------------------
# 1. Lista sesji IX kadencji
# --------------------------------------------------------------------------
def _norm_mon(mon: str):
    mon = mon.lower()
    if mon in MONTHS_PL:
        return mon
    if mon.startswith("wrze"):
        return "września"
    if mon.startswith("pazd") or mon == "pazdziernika":
        return "października"
    return None


def _parse_date(slug: str):
    pats = [
        r"z[- ]+(?:dnia[- ]+)?(\d{1,2})[- ]+(\w+)[- ]+(\d{4})",
        r"w dniu[- ]+(\d{1,2})[- ]+(\w+)[- ]+(\d{4})",
        r"(\d{1,2})[- ]+(\w+)[- ]+(\d{4})",
    ]
    m = None
    for pat in pats:
        m = re.search(pat, slug)
        if m:
            break
    if m:
        d, mon, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mon = _norm_mon(mon)
        if mon:
            return f"{y:04d}-{MONTHS_PL[mon]:02d}-{d:02d}"
    # compact DDMMYYYY e.g. 23022026
    m2 = re.search(r"(\d{8})\b", slug.replace("-", ""))
    if m2:
        ds = m2.group(1)
        try:
            return f"{ds[4:8]}-{ds[2:4]}-{ds[0:2]}"
        except Exception:
            pass
    return None


def collect_sessions():
    """Zwraca listę unikalnych sesji [{uid,slug,date,roman}] z paginacji indexu."""
    seen = {}
    for page in range(0, 12):
        url = BIP + VOTES_INDEX if page == 0 else BIP + VOTES_INDEX + "?page=%d" % page
        html = fetch(url)
        if not html:
            break
        links = re.findall(r"wyniki-glosowan/(\d+),([^\"']+)\.html", html)
        if not links:
            break
        new = 0
        for uid, slug in links:
            if uid not in seen:
                seen[uid] = slug
                new += 1
        if new == 0:
            break
    sessions = []
    for uid, slug in seen.items():
        date = _parse_date(slug)
        if not date or date < IX_START:
            continue
        roman = re.match(r"([IVXLCDM]+)", slug)
        sessions.append({
            "uid": uid, "slug": slug, "date": date,
            "roman": roman.group(1) if roman else "",
        })
    sessions.sort(key=lambda x: x["date"])
    return sessions


def session_glosowanie_pdf(sess):
    """Ze strony sesji zwraca URL pliku glosowanie*.pdf."""
    url = f"{BIP}/uml/rada-miejska/sesje/wyniki-glosowan/{sess['uid']},{sess['slug']}.html"
    html = fetch(url)
    if not html:
        return None
    dl = [d for d in re.findall(r'href="(/download/[^\"]+\.pdf)"', html)]
    glos = [d for d in dl if "glos" in d.lower()]
    return glos[0] if glos else (dl[0] if dl else None)


# --------------------------------------------------------------------------
# 2. Parsowanie PDF glosowan
# --------------------------------------------------------------------------
_NAME_VOTE_RE = re.compile(
    r"([A-ZĄĆĘŁŃÓŚŹŻ][A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż\-\s]+?)\s+"
    r"(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|WSTRZYMUJE SIĘ|NIEODDANY|NIEOBECN[YA])\b"
)
_VOTE_CAT = {
    "ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
    "WSTRZYMUJE SIĘ": "wstrzymal_sie", "NIEODDANY": "nieoddany",
    "NIEOBECNY": "nieobecny", "NIEOBECNA": "nieobecny",
}


def parse_glosowanie_pdf(data: bytes):
    """Parsuje PDF glosowan na punkty imienne: [{topic, counts, named}]."""
    with pdfplumber.open(__import__("io").BytesIO(data)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    votes = []
    for page in pages:
        dline = re.search(r"Typ głosowania\s+\S+\s+Data głosowania:\s+([\d.]+)",
                          page)
        if not dline:
            continue
        # temat: linie przed linią "Typ głosowania" (od "Głosowanie" wstecz)
        head_lines = [l.strip() for l in page[:dline.start()].split("\n")]
        topic = []
        for l in reversed(head_lines):
            if (l == "Głosowanie"
                    or re.match(r"\d+\s+[IVXLCDM]+\s+Sesja", l)
                    or l.endswith("Sesja Rady Miejskiej Legnicy")):
                break
            topic.insert(0, l)
        # tabela imienna: po dacie do "Wydrukowano"
        table = page[dline.end():].split("Wydrukowano")[0]
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [],
                 "nieoddany": [], "nieobecny": []}
        for m in _NAME_VOTE_RE.finditer(table):
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            # usun wiodacy ewentualny "N." (liczba + kropka) jesli zostal przy imieniu
            name = re.sub(r"^\d+\.\s*", "", name).strip()
            named[_VOTE_CAT[m.group(2)]].append(_norm_fullname(name))
        agg = re.search(
            r"Głosy za\s+(\d+).*?Głosy przeciw\s+(\d+).*?"
            r"Głosy wstrzymujące się\s+(\d+)", page, re.S)
        votes.append({
            "topic": " ".join(topic).strip(),
            "counts": {
                "za": int(agg.group(1)) if agg else len(named["za"]),
                "przeciw": int(agg.group(2)) if agg else len(named["przeciw"]),
                "wstrzymal_sie": int(agg.group(3)) if agg else len(named["wstrzymal_sie"]),
            },
            "named": named,
        })
    return votes


# --------------------------------------------------------------------------
# 3. Budowanie data.json / kadencja-*/profiles.json
# --------------------------------------------------------------------------
def _make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    slug = slug.replace(" ", "-").replace("-", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def _norm_fullname(name: str) -> str:
    """Normalizuje 'Jaczewska - Szymkowiak' -> 'Jaczewska-Szymkowiak'."""
    return re.sub(r"\s+", " ", name).replace(" - ", "-").replace(" -", "-").replace("- ", "-").strip()


def _name_pdf_order(name: str) -> str:
    """Przekształca 'GivenName Surname' -> 'Surname GivenName' (kolejność jak w PDF).

    Dwuczłonowe nazwiska (Jaczewska-Szymkowiak, Janowska-Lascar, Śliwińska-Łokaj)
    to nazwisko i w wyniku lądują przed imieniem w całości, połączone '-'.
    Nazwy już w kolejności PDF (Nazwisko Imię) wracają niezmienione."""

    norm = _norm_fullname(name)  # ' - ' -> '-'
    # Nazwy z dwuczłonowym nazwiskiem: "<Imię> <A>-<B> [ew. drugie imię]"
    def flip(given: str, surname_part: str) -> str:
        # surname_part może zawierać cześć imienia (drugie imię) — usuń
        # traktuj: ostatni token to imię, reszta to nazwisko
        toks = surname_part.split(" ")
        # jeśli ostatni token zaczyna się od 'model' drugiego imienia (male litery)?
        # Heurystyka: dwuczłonowe nazwisko opakowane '-' → jedno słowo w surname_part
        return f"{surname_part} {given}"

    # Wzorzec 1: "Imię Nazwisko[ DrugieImię]" z dwuczłonowym nazwiskiem po '-'
    m = re.match(r"^(\S+)\s+((?:[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż]+-){0,1}[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż]+)(?:\s+(\S+))?$", norm)
    if m:
        given = m.group(1)
        sur = m.group(2)
        return f"{sur} {given}"
    # Wzorzec 2: "Imię1 Imię2 Nazwisko" — przenieś nazwisko (ostatnie słowo) na przód
    m2 = re.match(r"^(\S+)\s+(.+)$", norm)
    if m2:
        g = m2.group(1)
        rest = m2.group(2)
        words = rest.split(" ")
        if len(words) >= 1:
            surname = words[-1]
            return f"{surname} {g}"
    return norm


def build_output(records, club_map, councilor_names):
    """records: [{date, roman, votes:[{topic,counts,named}]}] ; club_map: name->club_key"""
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("roman", ""),
                                   "vote_count": 0, "attendees": set(),
                                   "speakers": []}
        for v in rec["votes"]:
            vid += 1
            named_clean = {k: list(vals) for k, vals in v["named"].items()}
            sessions_by_date[d]["vote_count"] += 1
            for cat in ("za", "przeciw", "wstrzymal_sie", "nieoddany"):
                sessions_by_date[d]["attendees"].update(v["named"].get(cat, []))
            all_votes.append({
                "id": str(vid),
                "session_date": d,
                "session_number": rec.get("roman", ""),
                "topic": v["topic"] or "",
                "named_votes": named_clean,
                "counts": {k: v["counts"][k]
                           for k in ("za", "przeciw", "wstrzymal_sie")},
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

    # Tylko realni radni: ci z aktualnego skladu IX + ci co kiedykolwiek glosowali
    real_names = {n for n in councilor_names if n} | {n for n in all_names
                  if n in club_map or n in councilor_names}

    councilors_data = {}
    for name in sorted(real_names):
        councilors_data[name] = {
            "name": name, "club": club_map.get(name, "NZ"), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0,
            "votes_with_club": 0, "votes_against_club": 0, "rebellions": [],
        }
    for rec in records:
        d = rec["date"]
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
                    elif cat == "nieobecny":
                        c["votes_nieobecny"] += 1
                    else:
                        c["votes_brak"] += 1

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)

    councillor_sess = defaultdict(set)
    for rec in records:
        d = rec["date"]
        if not d:
            continue
        for v in rec["votes"]:
            for cat, names in v["named"].items():
                if cat != "nieobecny":
                    for n in names:
                        if n in councilors_data:
                            councillor_sess[n].add(d)

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = (c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
                   + c["votes_brak"])
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions
                      * 100) if total_sessions else 0
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
                if name in councilors_data:
                    vectors[name][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        score = round(same / len(common) * 100, 1)
        pairs.append({"a": a, "b": b, "club_a": club_map.get(a, "NZ"),
                      "club_b": club_map.get(b, "NZ"), "score": score,
                      "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    clubs_count = Counter(club_map.get(n, "NZ") for n in real_names)

    kad = {
        "id": KADENCJA_ID, "label": KADENCJA_LABEL,
        "clubs": dict(clubs_count),
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


def build_profiles(records, club_map):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d:
            continue
        for v in rec["votes"]:
            for cat, names in v["named"].items():
                for name in names:
                    key = ("za" if cat == "za" else "przeciw" if cat == "przeciw"
                           else "wstrzymal_sie" if cat == "wstrzymal_sie"
                           else "nieobecny" if cat == "nieobecny" else "brak")
                    cv[name][key] += 1
                    cv[name]["votes"].append({"session": d, "vote": key})
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie",
                                    "nieobecny", "brak")) or 1
        present_sess = len({v["session"] for v in vd["votes"]
                            if v["vote"] != "nieobecny"})
        all_sess = len({v["session"] for v in vd["votes"]})
        frekw = 100.0 * present_sess / all_sess if all_sess else 0.0
        profiles.append({
            "name": name, "slug": _make_slug(name),
            "kadencje": {
                KADENCJA_ID: {
                    "club": club_map.get(name, "NZ"), "has_voting_data": True,
                    "has_activity_data": False, "frekwencja": round(frekw, 1),
                    "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                    "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                    "votes_wstrzymal": vd["wstrzymal_sie"],
                    "votes_brak": vd["brak"], "votes_nieobecny": vd["nieobecny"],
                    "votes_total": total,
                    "rebellion_count": 0, "rebellions": [], "roles": [],
                    "notes": "", "former": False, "mid_term": False,
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
        with open(out_path.parent / f"kadencja-{kid}.json", "w",
                  encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {"generated": output.get("generated", ""),
             "default_kadencja": output.get("default_kadencja", ""),
             "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def load_club_map(config_path):
    """club_assignments: nazwa->klucz klubu z config.json (source of truth).

    Klucze configu są w kolejności 'Imię Nazwisko'; głosowania w PDF w kolejności
    'Nazwisko Imię'. Mapujemy klucze na format PDF, by zgadzały się z radnymi."""
    if config_path:
        try:
            cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
            raw = cfg.get("club_assignments", {})
            return {_name_pdf_order(k): v for k, v in raw.items()}
        except Exception:
            pass
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, help="docs/data.json")
    ap.add_argument("--profiles", required=True, help="docs/profiles.json")
    ap.add_argument("--config", default=None, help="cities/{slug}/config.json")
    args = ap.parse_args()

    club_map = load_club_map(args.config)
    print("=== Scraper Rada Miejska Legnicy (BIP-E.PL wyniki-glosowan PDF) ===")

    sessions = collect_sessions()
    print(f"  Sesje IX kadencji w indexie: {len(sessions)}")

    if not sessions:
        print("  BŁĄD: brak sesji")
        sys.exit(1)

    # dedupe by date (wybierz pierwsza; sesje nadzwyczajne tego samego dnia)
    records = []
    ok = fail = 0
    seen_dates = set()
    for i, sess in enumerate(sessions):
        if sess["date"] in seen_dates:
            continue
        seen_dates.add(sess["date"])
        print(f"  [{i+1}/{len(sessions)}] {sess['roman']:5s} {sess['date']}")
        try:
            pdf_url = session_glosowanie_pdf(sess)
            if not pdf_url:
                print("    brak PDF glosowan; pomijam")
                fail += 1
                continue
            data = fetch(BIP + pdf_url, binary=True)
            if not data:
                print("    blad pobrania PDF")
                fail += 1
                continue
            votes = parse_glosowanie_pdf(data)
            print(f"    PDF {sess['date']}: {len(votes)} glosowan imiennych")
            if votes:
                records.append({"date": sess["date"], "roman": sess["roman"],
                                "votes": votes})
                ok += 1
            else:
                fail += 1
        except Exception as e:
            print(f"    BŁĄD: {e}")
            fail += 1

    print(f"\n  Sesje z danymi: {ok}, bez danych: {fail}")
    if not records:
        print("  BŁĄD: zero sesji z danymi")
        sys.exit(1)

    # Rada: lista radnych z aktualnego IX-kadencji skladu (config club_assignments keys)
    councilor_names = set(club_map.keys())
    output = build_output(records, club_map, councilor_names)
    profiles = build_profiles(records, club_map)
    save_split(output, args.output, profiles)

    total_votes = sum(len(r["votes"]) for r in records)
    print("\n=== PODSUMOWANIE ===")
    print(f"  sesji z danymi: {len(records)}")
    print(f"  glosowan imiennych: {total_votes}")
    print(f"  radnych (w kadencji): {len(output['kadencje'][0]['councilors'])}")
    print(f"  zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")


if __name__ == "__main__":
    main()
