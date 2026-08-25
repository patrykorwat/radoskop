#!/usr/bin/env python3
"""
Radoskop Gorzów Wielkopolski — imienne głosowania Rady Miasta.

Źródło: BIP Miasta Gorzowa Wielkopolskiego na platformie wrota/bip.um (custom CMS).
Rada Miasta Gorzowa Wielkopolskiego (IX kadencja 2024-2029) publikuje per sesja
dokument DOCX "wyniki głosowań z <RZYMSKA> sesji Rady Miasta z dnia <dd> <miesiąc> <rrrr> r."
(listowany jako załącznik `system/pobierz.php?id=...` na stronach rocznych
kategorii "Głosowania z sesji Rady Miasta"). Dokument zawiera głosowania imienne:
dla każdego punktu "Głosowano w sprawie: <temat>" + wynik ZA/PRZECIW/WSTRZYMUJĘ SIĘ/
BRAK GŁOSU/NIEOBECNI + "Wyniki imienne:" z listami nazwisk wg głosu.

Struktura BIP:
  /614/2026_rok/  /604/2025_rok/  /569/2024_rok/  — kategorie lat z załącznikami
  /system/pobierz.php?id=<hash>                   — pobranie DOCX
  /90/Sklad_Rady_Miasta/                          — skład rady (kluby, kuratorowane)

Pomijamy załączniki indywidualne (a nie "wyniki głosowań z ... sesji"), bo sesyjny
DOCX już zawiera pełne głosowania. Głosy mapujemy: ZA->za, PRZECIW->przeciw,
WSTRZYMUJĘ SIĘ->wstrzymal_sie, BRAK GŁOSU->brak_glosu, NIEOBECNI->nieobecni.

Kluby radnych skuratorowane z BIP "Skład Rady Miasta" (stan 2026-08):
  Gorzów Plus (GP), Koalicja Obywatelska (KO), Prawo i Sprawiedliwość (PiS),
  Niezrzeszeni/Niezależni (NZ).

Użycie:
    python scrape_gorzow_wielkopolski.py --output docs/data.json
                                          --profiles docs/profiles.json
                                          [--cache-dir .cache]
"""

import argparse
import io
import json
import re
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.um.gorzow.pl"
YEAR_CATS = {
    "2024": "569/2024_rok",
    "2025": "604/2025_rok",
    "2026": "614/2026_rok",
}

KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

# Kuratorowane przypisanie klubów (imię nazwisko wg formy z DOCX = "Imię Nazwisko").
# Źródło: https://bip.um.gorzow.pl/90/Sklad_Rady_Miasta/ (stan 2026-08).
CLUB_CURRENT = {
    "Maciej Buszkiewicz": "GP",
    "Albert Madej": "GP",
    "Piotr Paluch": "GP",
    "Jarosław Porwich": "PiS",
    "Aleksandra Sibińska-Szadna": "KO",
    "Agnieszka Cierach": "NZ",
    "Krzysztof Kielec": "PiS",
    "Halina Kunicka": "KO",
    "Paulina Szymotowicz": "KO",
    "Jan Kaczanowski": "GP",
    "Anna Kozak": "NZ",
    "Tomasz Rafalski": "PiS",
    "Robert Surowiec": "KO",
    "Jerzy Synowiec": "KO",
    "Grzegorz Ignatowicz": "KO",
    "Jerzy Sobolewski": "KO",
    "Roman Sondej": "PiS",
    "Milena Surowiec-Sikora": "KO",
    "Cezary Żołyński": "KO",
    "Artur Andruszczak": "KO",
    "Marta Krupa": "NZ",
    "Sebastian Pieńkowski": "NZ",
    "Maria Szupiluk": "KO",
    "Piotr Wilczewski": "KO",
    "Jarosław Baryła": "NZ",
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
    import hashlib
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
# 1. Kolekcja sesyjnych załączników DOCX
# ---------------------------------------------------------------------------

ROMAN = r"(?:I{1,3}|IV|V|VI{0,3}|IX|X|XI{0,3}|XXIX|XXXI{0,2}|XXXIV|XXXVI|XIII|XIV|XVI|XVII|XVIII|XIX|XX|XXI|XXII|XXIII|XXIV|XXV|XXVI|XXVII|XXVIII|XXX|XXXII|XXXIII|XXXV|L{0,3}(?:[IVX]))"

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11,
    "grudnia": 12,
}
MONTHS_PL.update({  # ASCII fallback
    "września": 9, "października": 10, "pazdziernika": 10, "wrzesnia": 9,
})


def _date_from_title(title: str):
    """'wyniki głosowań z XXIX sesji Rady Miasta z dnia 20 stycznia 2026 r.' -> '2026-01-20'."""
    m = re.search(r"z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})", title)
    if not m:
        return None
    day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    if mon not in MONTHS_PL:
        return None
    return f"{year:04d}-{MONTHS_PL[mon]:02d}-{day:02d}"


def _session_number_from_title(title: str):
    """'XXIX' z 'wyniki głosowań z XXIX sesji ...'."""
    m = re.search(r"z\s+([IVXLCDM]+)\s+sesji", title)
    return m.group(1) if m else ""


def collect_attachments(cache_dir: Path | None = None):
    """Zwraca listę sesyjnych DOCX: [{url, date, num, title}] (dedupe po dacie)."""
    out = []
    seen = set()
    for year, cat in YEAR_CATS.items():
        html = fetch(f"{BIP}/{cat}/", cache_dir)
        for m in re.finditer(
                r'<a[^>]+href=["\']([^"\']*pobierz\.php\?id=[0-9a-f]+)["\'][^>]*>(.*?)</a>',
                html, re.S):
            url = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            tl = title.lower()
            if "wyniki głosowań" not in tl and "wyniki glosowan" not in tl:
                continue
            if "sesji" not in tl:
                continue
            date = _date_from_title(title)
            if not date:
                continue
            if date in seen:
                continue
            seen.add(date)
            out.append({
                "url": url if url.startswith("http") else BIP + url,
                "date": date,
                "num": _session_number_from_title(title),
                "title": re.sub(r"&nbsp;", "", title).strip(),
            })
    out.sort(key=lambda x: x["date"])
    return out


# ---------------------------------------------------------------------------
# 2. Parsowanie DOCX
# ---------------------------------------------------------------------------

def _docx_paragraphs(data: bytes):
    zf = zipfile.ZipFile(io.BytesIO(data))
    xml = zf.read("word/document.xml").decode("utf-8", "replace")
    lines = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        txt = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S))
        # odtwórz spacje/przerwy (break) jako separator
        txt = txt.replace("\u00a0", " ").strip()
        if txt:
            lines.append(txt)
    return lines


_VOTE_START = re.compile(
    r"^(?:(\d+)\.\s*)?Głosowano(?:\s+wniosek)?\s+(?:w sprawie|w sprawach|wniosek w sprawie|wniosek o)\s*:?\s*(.+)$",
    re.I)
_CAT_HEAD = re.compile(
    r"^(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI|NIEOBECN[YA]|OBECN[YA])\s*\((\d+)\)$",
    re.I)
_AGG = re.compile(
    r"ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJĘ SIĘ:\s*(\d+),\s*BRAK GŁOSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)", re.I)
_PARTICIPATION = re.compile(r"^\d+\.\s+\S.*\d+\s*/\s*\d+$")  # "N. Nazwisko Imię 26/26"
_PREPARED = re.compile(r"^Przygotowa", re.I)
_INLINE_SEG = re.compile(
    r"^\s*(.+?)\s*\((ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI|NIEOBECN[YA]|OBECN[YA])\)\s*$",
    re.I)


def _parse_inline(line, cur_named):
    """Format B: 'Nazwisko Imię (ZA), ...' — jedna linia par nazwa(głos)."""
    for seg in line.split(","):
        m = _INLINE_SEG.match(seg)
        if not m:
            continue
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        cat = _to_cat(m.group(2))
        if cat:
            cur_named[cat].append(name)
# Suffiksy daty/czasu w temacie głosowania (stary i nowy format)
_TS_NEW = re.compile(r"\s*[-–]\s*czas głosowania:\s*\d{1,2}\s+\w+\s+\d{4},?\s*\d{1,2}:\d{2}\s*$")  # nowy
_TS_OLD = re.compile(r"\s+\d{1,2}\s+\w+\s+\d{4},\s*godz\.\s*\d{1,2}:\d{2}\s*$")              # stary


def _clean_topic(topic: str) -> str:
    topic = _TS_NEW.sub("", topic)
    topic = _TS_OLD.sub("", topic)
    return re.sub(r"\s+", " ", topic).strip().rstrip(".,;:-")


def _to_cat(cat: str) -> str:
    c = cat.upper()
    if c == "ZA":
        return "za"
    if c == "PRZECIW":
        return "przeciw"
    if c == "WSTRZYMUJĘ SIĘ" or c == "WSTRZYMUJE SIE":
        return "wstrzymal_sie"
    if c == "BRAK GŁOSU":
        return "brak_glosu"
    if c == "NIEOBECNI" or c == "NIEOBECNY" or c == "NIEOBECNA":
        return "nieobecni"
    return None  # OBECNY (quorum) — pomijamy


def _split_names(text: str):
    names = [n.strip() for n in text.split(",")]
    return [n for n in names if n]


def parse_raport_docx(data: bytes):
    """Parsuje DOCX 'wyniki głosowań' na sesję {'session_date', 'votes':[{topic,counts,named}]}."""
    lines = _docx_paragraphs(data)

    session_date = None
    for ln in lines:
        m = re.search(r"sesja Rady Miasta w dniu\s+(\d{4}-\d{2}-\d{2})", ln, re.I)
        if m:
            session_date = m.group(1)
            break

    votes = []
    cur = None          # current vote being built
    cur_cat = None      # current block-imienne category being filled
    inline = False      # format B — inline "Nazwisko (GŁOS)" list
    mode = None         # 'agg' | 'imienne'

    def _commit_section(v):
        pass

    for ln in lines:
        # Koniec raportu głosowań — dalej "Uczestnictwo..." / "Przygotował(a):"
        if _PARTICIPATION.match(ln) or _PREPARED.match(ln) or \
                re.match(r"^Uczestnictwo w głosowaniach", ln, re.I):
            if cur is not None:
                votes.append(cur)
                cur = None
            break

        vm = _VOTE_START.match(ln)
        if vm:
            if cur is not None:
                votes.append(cur)
            topic = _clean_topic(vm.group(2))
            cur = {"num": vm.group(1) or "", "topic": topic,
                   "counts": None, "named": defaultdict(list)}
            cur_cat = None
            inline = False
            mode = None
            continue

        if cur is None:
            continue

        if re.match(r"^Wyniki głosowania(?: \(Radni\))?:?\s*$", ln, re.I):
            mode = "agg"
            continue
        if re.match(r"^Wyniki imienne:?\s*$", ln, re.I):
            mode = "imienne"
            continue

        if mode == "agg":
            am = _AGG.search(ln)
            if am:
                cur["counts"] = {
                    "za": int(am.group(1)), "przeciw": int(am.group(2)),
                    "wstrzymal_sie": int(am.group(3)), "brak_glosu": int(am.group(4)),
                    "nieobecni": int(am.group(5)),
                }
            continue

        if mode == "imienne":
            cm = _CAT_HEAD.match(ln)
            if cm:
                cat = _to_cat(cm.group(1))
                cur_cat = cat  # None-for OBECNY quorum simply doesn't accumulate
                inline = False
                continue
            if not inline and _INLINE_SEG.match(ln.split(",")[0]):
                # Format B: "Nazwisko (GŁOS), Nazwisko (GŁOS), ..." inline
                inline = True
                _parse_inline(ln, cur["named"])
                continue
            if inline:
                _parse_inline(ln, cur["named"])
                continue
            if cur_cat is not None:
                # lines of names (comma-separated) — append
                for n in _split_names(ln):
                    if n:
                        cur["named"][cur_cat].append(n)

    if cur is not None:
        votes.append(cur)

    # normalize named: filled dicts; drop empty category if not used
    for v in votes:
        if v["named"] is not None and isinstance(v["named"], defaultdict):
            v["named"] = {k: list(val) for k, val in v["named"].items()}

    return {"session_date": session_date, "votes": votes}


# ---------------------------------------------------------------------------
# 3. Budowanie kadencja-*/data.json / profiles.json
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z', 'Ą': 'A', 'Ć': 'C', 'Ę': 'E',
            'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    slug = re.sub(r"[^a-z0-9]+", "", slug)
    return slug


def _club_of(name):
    return CLUB_CURRENT.get(name, "NZ")


def build_output(records):
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
            if v.get("counts") is None:
                continue  # głosowanie kworum (Sprawdzenie obecności) — bez ZA/PRZECIW
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
                "counts": {k: (v["counts"] or {}).get(k, 0)
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

    councilors_data = {}
    for name in sorted(all_names):
        councilors_data[name] = {
            "name": name, "club": _club_of(name), "district": None,
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

    print("=== Scraper Rada Miasta Gorzów Wielkopolski (bip.um.gorzow.pl) ===")
    atts = collect_attachments(cache_dir)
    print(f"  Sesyjnych DOCX głosowań: {len(atts)}")
    if not atts:
        print("  BŁĄD: brak sesji"); sys.exit(1)
    for a in atts:
        print(f"    {a['date']}  {a['num']:6s} {a['title'][:70]}")

    records = []
    ok = fail = 0
    total_votes = 0
    for a in atts:
        try:
            data = fetch(a["url"], cache_dir, binary=True)
            parsed = parse_raport_docx(data)
            parsed["session_date"] = a["date"]
            records.append(parsed)
            ok += 1
            total_votes += len(parsed["votes"])
        except Exception as e:
            print(f"    BŁĄD parsowania {a['date']}: {e}")
            fail += 1

    print(f"  DOCX OK: {ok}, błędy: {fail}, głosowań: {total_votes}")

    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)

    kad = output["kadencje"][0]
    print(f"  SESJE: {kad['total_sessions']}, GŁOSOWANIA: {kad['total_votes']}, "
          f"RADNYCH: {kad['total_councilors']}")
    print(f"  KLUBY: {kad['clubs']}")
    print("  OK — zapisano data.json / kadencja-2024-2029.json / profiles.json")


if __name__ == "__main__":
    main()
