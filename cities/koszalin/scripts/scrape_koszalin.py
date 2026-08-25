#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Koszalin — imienne głosowania Rady Miejskiej.

Źródło: BIP Miasta Koszalina na własnym CMS (bip.koszalin.pl).
Rada Miejska w Koszalinie (IX kadencja 2024-2029) publikuje w kategorii
"Uchwały Rady Miejskiej i głosowania radnych" (/artykuly/1744) listę uchwał;
każda uchwała (/uchwala/{id}/...) ma załącznik PDF "Głosowanie do uchwały nr ..."
z tabelą imienną: Lp | Nazwisko i imię | Głos (ZA / PRZECIW / WSTRZYMUJĘ SIĘ /
NIEOBECNY/-A / NIEODDANY) oraz nagłówkiem sesji ("XXXV Sesja Rady Miejskiej w
Koszalinie w dniu 17.07.2026 r.") i tematem punktu.

Sesje grupujemy po dacie głosowania; każda uchwała = jeden punkt głosowania.
Kluby radnych skuratorowane z BIP "Kluby radnych" (stan 2026-08):
  KO = Koalicja Obywatelska (14), PiS = Prawo i Sprawiedliwość (4),
  WDK = Wspólnie dla Koszalina (4), NZ = Niezrzeszeni (1, Kamieniarz Wiktor).

Użycie:
    python scrape_koszalin.py --output docs/data.json --profiles docs/profiles.json
                               [--cache-dir .cache]
"""

import argparse
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.koszalin.pl"
LIST_CAT = 1744          # /artykuly/1744/nowe-uchwaly, paginacja /uchwaly/1744/{page}/10
KAD_START = "2024-05-07"  # początek IX kadencji
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
PAGE_CAP = 48

def _norm(s: str) -> str:
    s = s.lower().replace("\u0142", "l").replace("\u0141", "L")
    s = s.replace("\u00b3", "3")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


# ---- Kuratorowany skład rady (nazwisko-imie wg formy z PDF) + kluby ----
# Źródło klubów: https://bip.koszalin.pl/artykuly/585/kluby-radnych (stan 2026-08).
# Obok 23 obecnych radnych uwzględniamy radnych, którzy zasiadali w pierwszych
# sesjach IX kadencji (2024) i później zostali zastąpieni — ich klub jest
# nieznany (NZ), głosy są autentyczne z PDF.
ROSTER = [
    ("Chałat Dorota", "KO"), ("Chałat Magdalena", "KO"),
    ("Foremna-Pilarska Monika", "KO"), ("Iwat Piotr", "KO"),
    ("Jakubowski Andrzej", "PiS"), ("Janczewski Miłosz", "PiS"),
    ("Jedliński Piotr", "WDK"), ("Kamieniarz Wiktor", "NZ"),
    ("Kościńska Krystyna", "KO"), ("Krzyżanowski Przemysław", "WDK"),
    ("Kwapisz Żaneta", "WDK"), ("Leśniewska-Lorek Małgorzata", "KO"),
    ("Listowski Michał", "KO"), ("Malinowski Bartosz", "KO"),
    ("Papiernik Błażej", "WDK"), ("Połaniecka Agnieszka", "KO"),
    ("Skórka Oliwia", "PiS"), ("Tałaj Teresa", "KO"),
    ("Urbaniak Anetta", "KO"), ("Wesołowska Izabela", "KO"),
    ("Wezgraj Artur", "KO"), ("Wezgraj Jacek", "KO"),
    ("Wiśniewski Artur", "PiS"),
    # Radni z pierwszych sesji IX kadencji (2024), później zastąpieni (klub nieznany -> NZ)
    ("Grygorcewicz Barbara", "NZ"), ("Tałaj Sebastian", "NZ"),
    ("Reinholz Marek", "NZ"), ("Czarkowska Katarzyna", "NZ"),
    ("Sendlewski Łukasz", "NZ"), ("Sokalski Andrzej", "NZ"),
    ("Tarnowski Ryszard", "NZ"), ("Tiece Bogumiła", "NZ"),
    ("Kowalik Jakub", "NZ"), ("Twardowski Marek", "NZ"),
    ("Kuriata Jan", "NZ"), ("Mętlewicz Anna", "NZ"),
    ("Nastarowski Mariusz", "NZ"), ("Waszkiewicz Marcin", "NZ"),
    ("Ostrowski Leopold", "NZ"), ("Bernacki Tomasz", "NZ"),
    ("Kaczmarek Bożena", "NZ"),
]
CLUB_BY_NORM = {}
DISPLAY_BY_NORM = {}
for _name, _club in ROSTER:
    _key = _norm(_name)
    CLUB_BY_NORM[_key] = _club
    DISPLAY_BY_NORM.setdefault(_key, _name)

CLUBS_META = {
    "KO":  {"name": "Koalicja Obywatelska", "color": "#0ea5e9",
            "bg": "rgba(14,165,233,0.12)", "avatar_bg": "#0369a1"},
    "PiS": {"name": "Prawo i Sprawiedliwość", "color": "#1d4ed8",
            "bg": "rgba(29,78,216,0.12)", "avatar_bg": "#1e40af"},
    "WDK": {"name": "Wspólnie dla Koszalina", "color": "#16a34a",
            "bg": "rgba(22,163,74,0.12)", "avatar_bg": "#15803d"},
    "NZ":  {"name": "Niezrzeszeni", "color": "#6b7280",
            "bg": "rgba(107,114,128,0.12)", "avatar_bg": "#505560"},
}

REQ_DELAY = 0.4
_LAST_REQ = 0.0


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, cache_dir: Path | None = None, binary: bool = False):
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".html"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
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


# ---------------------------------------------------------------------------
# 1. Kolekcja uchwał + PDF "Głosowanie do uchwały"
# ---------------------------------------------------------------------------

def collect_uchwaly(cache_dir: Path | None = None):
    """Iteruje kategorie uchwał, zwraca [{uid, url, glos_pdf}] dla IX kadencji
    (daty >= KAD_START). Zatrzymuje się, gdy strona nie ma już żadnej uchwały
    IX kadencji (strony są malejąco wg daty)."""
    out = []
    seen_uids = set()
    for page in range(1, PAGE_CAP + 1):
        url = f"{BIP}/uchwaly/{LIST_CAT}/{page}/10"
        try:
            html = fetch(url, cache_dir)
        except Exception as e:
            print(f"    [warn] page {page} fetch: {e}")
            break
        items = []
        for m in re.finditer(r'href=["\'](https://bip\.koszalin\.pl/uchwala/(\d+)/[^"\']*)["\']', html):
            if m.group(2) not in seen_uids:
                items.append((m.group(2), m.group(1)))
        if not items:
            break
        page_all_old = True
        for uid, u in items:
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            try:
                uh = fetch(f"{BIP}/uchwala/{uid}/x", cache_dir)
            except Exception:
                continue
            gl = None
            for m in re.finditer(r'<a[^>]+href=["\']([^"\']*attachments/download/\d+)[^"\']*["\']>(.*?)</a>',
                                 uh, re.S):
                label = re.sub(r"<[^>]+>", "", m.group(2))
                if "g\u0142osow" in label.lower() or "osowan" in label.lower():
                    gl = m.group(1)
                    break
            if not gl:
                continue  # uchwała bez odrębnego głosowania (bez tabeli imiennej)
            gurl = gl if gl.startswith("http") else BIP + gl.split('//')[-1]
            out.append({"uid": uid, "url": u, "glos_pdf": gurl})
        # Zatrzymaj, gdy cała strona to już stare (poza IX kadencją) — ale to
        # ustalamy dopiero w fazie parsowania. Tu jedynie wykrywamy brak nowych.
        if page >= PAGE_CAP:
            break
        print(f"    page {page}: collected {len(items)} uchwal (total {len(out)})")
    return out


# ---------------------------------------------------------------------------
# 2. Parsowanie PDF głosowania
# ---------------------------------------------------------------------------

_VOTE_TOK = (r"WSTRZYMUJ\u0118 SI\u0118|WSTRZYMUJE SI\u0118|WSTRZYMUJ\u0118|WSTRZYMUJE|"
             r"PRZECIW|NIEOBECNE|NIEOBECNI|NIEOBECNA|NIEOBECNY|"
             r"NIEODDANE|NIEODDANI|NIEODDANA|NIEODDANY|ZA")
_CELL_RE = re.compile(r"(\d+)\s*\.?\s*([^\d]+?)\s*(" + _VOTE_TOK + r")(?=\s+\d+|\s*$)", re.I)
_VOTE_FIND = re.compile(r"\b(" + _VOTE_TOK + r")\b", re.I)

_GOSY_ZA = re.compile(r"G\u0142osy za\s+(\d+)", re.I)
_GOSY_PRZECIW = re.compile(r"G\u0142osy przeciw\s+(\d+)", re.I)
_GOSY_WSTRZ = re.compile(r"G\u0142osy wstrzymuj\u0105ce si\u0119\s+(\d+)", re.I)
_SESJA = re.compile(r"Sesja Rady Miejskiej w Koszalinie w dniu\s+([\d\.]+)", re.I)
_SESJA_NUM = re.compile(r"^([IVXLCDM]+)\s+Sesja", re.I)


def _to_cat(tok: str) -> str:
    t = tok.upper().replace("\u0118", "E").replace("\u0104", "A").replace("\u0106", "C") \
        .replace("\u0141", "L").replace("\u0143", "N").replace("\u00D3", "O") \
        .replace("\u015A", "S").replace("\u0179", "Z").replace("\u017B", "Z")
    if t == "ZA":
        return "za"
    if t == "PRZECIW":
        return "przeciw"
    if t.startswith("WSTRZYMUJ"):
        return "wstrzymal_sie"
    if t.startswith("NIEOBEC"):
        return "nieobecni"
    if t.startswith("NIEODDAN"):
        return "brak_glosu"
    return None


def _topic_from(pdf_text: str) -> str:
    """Temat = tekst między nagłówkiem 'Głosowanie' a 'Typ głosowania',
    po usunięciu wiodącego 'Nr Lp' + 'punkt sesji' (np. '2.10.')."""
    m = re.search(r"G\u0142osowanie\s*\n(.*?)\n\s*Typ g\u0142osowania", pdf_text, re.S)
    if not m:
        m2 = re.search(r"G\u0142osowanie\s+(.*?)\s+Typ g\u0142osowania", pdf_text, re.S)
        raw = m2.group(1) if m2 else ""
    else:
        raw = m.group(1)
    raw = re.sub(r"\s+", " ", raw).strip()
    # wiodący numer Lp / numer punktu (np. "21 6.17." / "6.17." / "7. 18.")
    raw = re.sub(r"^\d+\.\s+\d+\.\d+[a-z]?\.\s*", "", raw)
    raw = re.sub(r"^\d+\s+\d+\.\d+[a-z]?\.\s*", "", raw)
    raw = re.sub(r"^\d+\.\d+[a-z]?\.\s*", "", raw)
    # Usunięcie artefaktu pdfplumber: samotny numer (1-3 cyfry = nr głosowania)
    # w środku tematu, np. "planu 10 zagospodarowania". Nie rusza lat 4-cyfrowych
    # ani numerów przy "nr"/"/".
    raw = re.sub(r"\s\d{1,3}(?=\s)", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip().strip(".,;:-")
    return raw


def parse_vote_pdf(data: bytes):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    sm = re.search(r"w dniu\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if sm:
        session_date = f"{sm.group(3)}-{sm.group(2)}-{sm.group(1)}"
    else:
        sm2 = re.search(r"Data g\u0142osowania:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        session_date = f"{sm2.group(3)}-{sm2.group(2)}-{sm2.group(1)}" if sm2 else None
    snm = _SESJA_NUM.search(text)
    session_num = snm.group(1) if snm else ""
    topic = _topic_from(text)

    named = defaultdict(list)
    seen_cells = set()
    on = False
    for ln in text.split("\n"):
        if "Uprawnieni" in ln:
            on = True
            continue
        if "Wydrukowano" in ln or "Kworum" in ln:
            on = False
            continue
        if not on:
            continue
        # Normalizacja artefaktów pdfplumber: "1 7." -> "17.", "WSTRZYMUJĘ SIĘ !" -> "WSTRZYMUJĘ SIĘ"
        ln = re.sub(r"(?<=\d)\s+(?=\d)", "", ln)
        ln = re.sub(r"\s*[!\u2026\u2022.]+\s*$", "", ln)
        if not re.search(r"(WSTRZYMUJ\u0118|WSTRZYMUJE|PRZECIW|NIEOBEC|NIEODDAN|ZA)", ln, re.I):
            continue
        # Dopasowanie po nazwisku z rosteru (Lp bywa uszkodzony: "i.", "u.", "L9-").
        vms = list(_VOTE_FIND.finditer(ln))
        if not vms:
            continue
        for i, vm in enumerate(vms):
            seg_start = 0 if i == 0 else vms[i - 1].end()
            seg = _norm(ln[seg_start:vm.start()])
            best = None
            bestlen = -1
            for key in CLUB_BY_NORM:
                if key and seg.find(key) != -1 and len(key) > bestlen:
                    best = key
                    bestlen = len(key)
            if best is None or best in seen_cells:
                continue
            seen_cells.add(best)
            cat = _to_cat(vm.group(0))
            if cat:
                named[cat].append(DISPLAY_BY_NORM[best])

    # counts (nagłówek) — do walidacji
    counts = {
        "za": int(m.group(1)) if (m := _GOSY_ZA.search(text)) else 0,
        "przeciw": int(m.group(1)) if (m := _GOSY_PRZECIW.search(text)) else 0,
        "wstrzymal_sie": int(m.group(1)) if (m := _GOSY_WSTRZ.search(text)) else 0,
    }
    return {
        "session_date": session_date,
        "session_num": session_num,
        "topic": topic,
        "named": {k: list(v) for k, v in named.items()},
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# 3. Budowanie outputu
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    repl = {'\u0105': 'a', '\u0107': 'c', '\u0119': 'e', '\u0142': 'l', '\u0144': 'n',
            '\u00f3': 'o', '\u015b': 's', '\u017a': 'z', '\u017c': 'z',
            '\u0104': 'A', '\u0106': 'C', '\u0118': 'E', '\u0141': 'L', '\u0143': 'N',
            '\u00d3': 'O', '\u015a': 'S', '\u0179': 'Z', '\u017b': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def _club_of(name: str) -> str:
    return CLUB_BY_NORM.get(_norm(name), "NZ")


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d,
            "session_number": rec.get("session_num", ""),
            "topic": rec["topic"] or "", "named_votes": named,
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
    for name in sorted(all_names):
        councilors_data[name] = {
            "name": name, "club": _club_of(name), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0,
            "votes_with_club": 0, "votes_against_club": 0, "rebellions": [],
        }
    for v in all_votes:
        for cat, names in v["named_votes"].items():
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
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat != "nieobecni":
                for n in names:
                    councillor_sess[n].add(v["session_date"])

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
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": score, "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    club_counts = Counter(_club_of(n) for n in all_names)
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


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        for v in [rec]:
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
                    "club": _club_of(name), "has_voting_data": True,
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, help="docs/data.json")
    ap.add_argument("--profiles", required=True, help="docs/profiles.json")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    print("=== Scraper Rada Miejska Koszalin (bip.koszalin.pl) ===")
    uchwaly = collect_uchwaly(cache_dir)
    print(f"  Uchwał z PDF głosowania: {len(uchwaly)}")

    records = []
    ok = fail = 0
    total_votes = 0
    validated = 0
    mismatch = 0
    for it in uchwaly:
        try:
            data = fetch(it["glos_pdf"], cache_dir, binary=True)
            parsed = parse_vote_pdf(data)
            sd = parsed["session_date"]
            if not sd or sd < KAD_START:
                continue  # poza IX kadencją
            # walidacja liczebności (ZA/PRZECIW/WSTRZYM) vs nazwiska
            c = parsed["counts"]
            nn = {k: len(parsed["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}
            if all(c[k] == nn[k] for k in ("za", "przeciw", "wstrzymal_sie")):
                validated += 1
            else:
                mismatch += 1
                print(f"    [walidacja] {sd} #{it['uid']}: nagłówek {c} imienne {nn}")
            records.append(parsed)
            ok += 1
            total_votes += 1
        except Exception as e:
            print(f"    BŁĄD {it['uid']}: {e}")
            fail += 1

    print(f"  PDF OK: {ok}, błędy: {fail}, głosowań (IX kad): {total_votes}, "
          f"walidacja zgodna: {validated}, niezgodna: {mismatch}")

    filter_records = [r for r in records if r.get("session_date")]
    output = build_output(filter_records)
    profiles = build_profiles(filter_records)
    save_split(output, args.output, profiles)

    kad = output["kadencje"][0]
    print(f"  SESJE: {kad['total_sessions']}, GŁOSOWANIA: {kad['total_votes']}, "
          f"RADNYCH: {kad['total_councilors']}")
    print(f"  KLUBY: {kad['clubs']}")
    print("  OK — zapisano data.json / kadencja-2024-2029.json / profiles.json")


if __name__ == "__main__":
    main()
