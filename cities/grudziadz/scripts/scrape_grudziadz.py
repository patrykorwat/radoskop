#!/usr/bin/env python3
"""
Radoskop Grudziądz — imienne głosowania Rady Miejskiej Grudziądza (IX kadencja 2024-2029).

Źródło: custom BIP (https://bip.grudziadz.pl). Struktura:
  1. Artykuł "Wydruki głosowań" (/artykul/wydruki-glosowan) — lista sesji jako linki
     do artykułów /artykul/{slug} (każda sesja = jeden artykuł).
  2. Każdy artykuł sesji zawiera pobierany plik PDF "/pliki/grudziadz/zalaczniki/{id}/glosowaniesesja{N}.pdf"
     z głosowaniami imiennymi: dla każdego punktu nagłówek "Głosowanie N" + agregat
     (Liczba uprawnionych/obecnych, Głosy za/przeciw/wstrzymujące się/nieoddane) +
     tabela per radny "Lp  Nazwisko i imię  GŁOS" (dwie kolumny Lp 1-12 / 13-23).
  3. PDF -> parsujemy imienne głosowania (pdfplumber). Mapa głosów:
     ZA->za, PRZECIW->przeciw, NIEOBECNY->nieobecny, NIEODDANY->nieoddany.

Tylko IX kadencja (sesje od 2024-05-07). Imiona radnych (kolejność Radoskopa "Imię Nazwisko")
normalizowane z kuratorowanej listy IX kadencji z BIP (/artykul/radni-rady-miejskiej-grudziadza-ix-kadencji).
Kluby radnych: club_assignments PENDING (kuratorowane z BIP; brak bieżącej listy klubów IX kadencji).

Użycie (jak wywołuje scrape_all.sh / nas):
    python scrape_grudziadz.py --output docs/data.json --profiles docs/profiles.json
                [--config config.json]
"""

import argparse
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.grudziadz.pl"
VOTES_INDEX = "/artykul/wydruki-glosowan"

KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
IX_START = "2024-05-07"

REQ_DELAY = 0.6
_LAST_REQ = 0.0
UA = "Mozilla/5.0 (compatible; Radoskop/1.0 bot)"

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
    "wrzesnia": 9, "pazdziernika": 10,
}

# Kuratorowana lista radnych IX kadencji Grudziądza (z BIP /artykul/radni-...-ix-kadencji),
# format "NAZWISKO Imię [Imię...]". Dwóch radnych o nazwisku Kosiński rozróżnia drugie imię.
CANONICAL_ROSTER = [
    "CZEPEK Marek Józef",
    "DĄBROWSKA Ewelina",
    "DECKER Przemysław Marcin",
    "DROZDECKA Aleksandra Ewa",
    "GAWROŃSKI Krzysztof Janusz",
    "GBURCZYK Maciej",
    "GURBIN Szymon",
    "JEZIERSKI Piotr",
    "JONIEC Miłosz",
    "KOCIK Krzysztof Andrzej",
    "KOPKOWSKI Jakub Kornel",
    "KOSIŃSKI Krzysztof",
    "KOSIŃSKI Krzysztof Roman",
    "KOWAROWSKI Łukasz Mariusz",
    "KUPIS Dorota",
    "MAKOWSKA Marzena Maria",
    "MISIEWICZ Krzysztof Roman",
    "NAPOLSKI Paweł",
    "OGONOWSKA Edyta Renata",
    "POKORA Krzysztof Piotr",
    "SARNOWSKI Roman Bernard",
    "SZYMAŃSKI Sławomir Andrzej",
    "ŻEBROWSKI Mariusz Arkadiusz",
]


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
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=60,
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
    if mon.startswith("pazd"):
        return "października"
    return None


def _parse_date_from_slug(slug: str):
    """'...-24-czerwca-2026-r-1' -> '2026-06-24'."""
    m = re.search(r"-(\d{1,2})-(\w+)-(\d{4})(?:-r)?", slug)
    if not m:
        return None
    d, mon, y = int(m.group(1)), _norm_mon(m.group(2)), int(m.group(3))
    if mon:
        return f"{y:04d}-{MONTHS_PL[mon]:02d}-{d:02d}"
    return None


def collect_sessions():
    """Z artykułu 'Wydruki głosowań' — lista sesji IX kadencji [{slug,date,roman}]."""
    html = fetch(BIP + VOTES_INDEX)
    if not html:
        return []
    links = re.findall(r'href="(/artykul/([^"#]+))"', html)
    seen = []
    for _u, s in links:
        if s not in seen:
            seen.append(s)
    sessions = []
    for slug in seen:
        if not re.match(r"[ivxlcdm]+-sesja", slug):
            continue
        date = _parse_date_from_slug(slug)
        if not date or date < IX_START:
            continue
        roman = re.match(r"([ivxlcdm]+)", slug).group(1).upper()
        sessions.append({"slug": slug, "date": date, "roman": roman})
    sessions.sort(key=lambda x: (x["date"], x["roman"]))
    return sessions


def _session_pdf_url(html: str):
    """Ze strony sesji zwraca URL pliku glosowaniesesja*.pdf (jako absolutny)."""
    dl = [d for d in re.findall(r'href="([^"]+\.pdf)"', html)]
    glos = [d for d in dl if "glosowani" in d.lower()]
    pick = glos[0] if glos else (dl[0] if dl else None)
    if pick and pick.startswith("/"):
        return BIP + pick
    return pick


# --------------------------------------------------------------------------
# 2. Parsowanie PDF głosowań
# --------------------------------------------------------------------------
_VOTE_TOKEN = re.compile(
    r"(?:ZA|PRZECIW|NIEOBECNY|NIEOBECNA|NIEODDANY|WSTRZYMAŁ|WSTRZYMUJĘ|WSTRZYMUJE|WSTRZYMUJĄCY)\b"
)
_LP_TOKEN = re.compile(r"^\d{1,2}\.$")

_VOTE_CAT = {
    "ZA": "za", "PRZECIW": "przeciw",
    "WSTRZYMUJĄCY": "wstrzymal_sie", "WSTRZYMAŁ": "wstrzymal_sie",
    "WSTRZYMUJĘ": "wstrzymal_sie", "WSTRZYMUJE": "wstrzymal_sie",
    "NIEODDANY": "nieoddany", "NIEOBECNY": "nieobecny", "NIEOBECNA": "nieobecny",
}


def _extract_named_rows(words):
    """Z listy słów pdfplumber (region tabeli imiennej) odtwarza (lp, name, vote).

    Tabela ma dwie pozycje na wiersz: (Lp Nazwisko i imię Głos) (Lp Nazwisko i imię Głos).
    Kolumny głosów stoją między blokami, a w niektórych plikach (np. sesja XIV) nazwiska
    lewej kolumny są rozbite pionowo (nazwisko ~5 pkt nad linią Lp, imię ~5 pod).

    Dlatego: (1) grupujemy słowa w wiersze po współrzędnej Y (klastrowanie składowych,
    próg ~9 pkt — separuje sąsiednie wiersze ~17-24 pkt); (2) w obrębie wiersza czytamy
    słowa po X i odtwarzamy sekwencje 'Lp -> Nazwisko i imię -> Głos'.
    """
    if not words:
        return []
    ROW_GAP = 9.0
    words = sorted(words, key=lambda w: w["top"])
    clusters = []
    cur = [words[0]]
    for w in words[1:]:
        if w["top"] - cur[-1]["top"] <= ROW_GAP and w["top"] - cur[0]["top"] <= 26:
            cur.append(w)
        else:
            clusters.append(cur)
            cur = [w]
    clusters.append(cur)
    out = []
    for cluster in clusters:
        cur_entry = None
        entries = []
        srt = sorted(cluster, key=lambda w: w["x0"])
        i = 0
        while i < len(srt):
            w = srt[i]
            txt = w["text"]
            i += 1
            if _LP_TOKEN.match(txt):
                if cur_entry is not None:
                    entries.append(cur_entry)
                cur_entry = {"lp": int(txt[:-1]), "name": [], "vote": None}
            elif _VOTE_TOKEN.fullmatch(txt):
                if cur_entry is not None and cur_entry["vote"] is None:
                    cur_entry["vote"] = txt
                    # "WSTRZYMUJĘ SIĘ" / "WSTRZYMUJE SIĘ" — połykamy 'SIĘ'
                    if i < len(srt) and srt[i]["text"] == "SIĘ":
                        i += 1
            else:
                if txt == "SIĘ" and cur_entry is not None and cur_entry["vote"]:
                    # nagły 'SIĘ' po głosie (np. przeniesiony) — ignoruj
                    pass
                elif cur_entry is not None:
                    cur_entry["name"].append(txt)
        if cur_entry is not None:
            entries.append(cur_entry)
        for e in entries:
            nm = " ".join(e["name"]).strip()
            if e["lp"] is not None and nm:
                out.append({"lp": e["lp"], "name": nm, "vote": e["vote"]})
    return out


def parse_glosowanie_pdf(data: bytes):
    """Parsuje PDF głosowań na punkty imienne: [{date,topic,counts,named}].

    Każda strona PDF = jeden punkt głosowania (blok kończy się liniami
    'Wydrukowano:'). Tabela imienna (dwie kolumny Lp) odtwarzana po współrzędnych.
    """
    votes = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "Data głosowania" not in text or "Uprawnieni do głosowania" not in text:
                continue
            dline = re.search(r"Data głosowania:\s*([\d.]+)", text)
            if not dline:
                continue
            date = dline.group(1)  # DD.MM.YYYY
            # temat: od nagłówka 'Głosowanie [N]' do 'Typ głosowania'
            gt = re.search(r"Głosowanie\s+(\d+)\s*(.*?)\s*Typ głosowania", text, re.S)
            topic = ""
            if gt:
                topic = re.sub(r"\s+", " ", gt.group(2)).strip()

            # region tabeli: słowa między 'Uprawnieni...' a 'Wydrukowano'
            words = page.extract_words()
            up_i = None
            for i, w in enumerate(words):
                if w["text"].startswith("Uprawnieni"):
                    up_i = i
                    break
            end_i = len(words)
            for i, w in enumerate(words):
                if w["text"] == "Wydrukowano":
                    end_i = i
                    break
            tbl = words[up_i + 1:end_i] if up_i is not None else words[:end_i]

            named = {"za": [], "przeciw": [], "wstrzymal_sie": [],
                     "nieoddany": [], "nieobecny": []}
            for r in _extract_named_rows(tbl):
                cat = _VOTE_CAT.get(r["vote"])
                if cat:
                    named[cat].append(r["name"])
            # Sesje tajne / listy obecności nie niosą głosów imiennych (same
            # OBECNY/OBECNA lub jedna NIEOBECNY) — bez ZA/PRZECIW/WSTRZYMUJĘ brak
            # danych per radny; pomijamy.
            if not any(named[c] for c in ("za", "przeciw", "wstrzymal_sie")):
                continue
            agg = re.search(
                r"Głosy za\s+(\d+).*?Głosy przeciw\s+(\d+).*?"
                r"Głosy wstrzymujące się\s+(\d+)", text, re.S)
            votes.append({
                "date": date, "topic": topic,
                "counts": {
                    "za": int(agg.group(1)) if agg else len(named["za"]),
                    "przeciw": int(agg.group(2)) if agg else len(named["przeciw"]),
                    "wstrzymal_sie": int(agg.group(3)) if agg else len(named["wstrzymal_sie"]),
                },
                "named": named,
            })
    return votes


# --------------------------------------------------------------------------
# 3. Normalizacja nazwisk (PDF "Nazwisko Imię" -> Radoskop "Imię Nazwisko")
# --------------------------------------------------------------------------
def _build_canonical_map():
    """kanon: lower(surname) -> lista {out_name, given_lower, initials}."""
    canon = defaultdict(list)
    for entry in CANONICAL_ROSTER:
        toks = entry.split()
        surname = toks[0].lower()
        given = " ".join(toks[1:])          # "Marek Józef"
        out = f"{given} {toks[0].title()}"  # "Marek Józef Czepek"
        initials = "".join(w[0].lower() for w in toks[1:])  # "mr"
        canon[surname].append({
            "out": out, "given_lower": given.lower(),
            "first_given": toks[1].lower(),
            "initials": initials,
        })
    return canon, {e: f"{' '.join(e.split()[1:])} {e.split()[0].title()}" for e in CANONICAL_ROSTER}


_CANON, _CANON_BY_RAW = _build_canonical_map()


def _normalize_name(pdf_name: str):
    """PDF 'Nazwisko Imię [Init.]' -> 'Imię [Imię...] Nazwisko' z kuratorowanej listy."""
    n = re.sub(r"\s+", " ", pdf_name).strip()
    toks = n.split()
    if len(toks) < 2:
        return n
    surname = toks[0].lower()
    given = " ".join(toks[1:])               # "Krzysztof R."
    initials = "".join(w[0].lower() for w in toks[1:])   # "kr"
    # mapa initials do drugiego imienia: "Krzysztof r." -> kandidat KOSIŃSKI Krzysztof Roman
    cands = _CANON.get(surname, [])
    if len(cands) == 0:
        # poza kuratorowaną listą (np. radny zmieniony w trakcie kadencji) -> flip
        return f"{given} {toks[0]}"
    if len(cands) == 1:
        return cands[0]["out"]
    # wiele osób o tym samym nazwisku: rozróżnij po inicjałach / imieniu
    # inicjały z PDF: "r." -> tylko litera r; porównaj do inicjałów kandyata
    pdf_init = "".join(re.findall(r"\b([a-ząćęłńóśźż])\.", given.lower()))
    first = given.lower().split()[0] if given else ""
    if pdf_init:
        for c in cands:
            if (pdf_init in c["initials"]
                    and c["first_given"].startswith(first[:1])
                    and c["given_lower"].startswith(first)):
                return c["out"]
    # bez inicjałów: wybierz kandydata, którego imię odpowiada i bez drugiego imienia
    for c in cands:
        if c["first_given"] == first and c["initials"] == first[0]:
            return c["out"]
    return f"{given} {toks[0]}"


# --------------------------------------------------------------------------
# 4. Budowanie data.json / kadencja-*/profiles.json
# --------------------------------------------------------------------------
def _make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def _norm_fullname(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def build_output(records, club_map):
    records = list(records)
    for rec in records:
        for v in rec["votes"]:
            v["named"] = {k: [_normalize_name(x) for x in vals]
                          for k, vals in v["named"].items()}

    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d:
            continue
        if d not in sessions_by_date:
            session_label = f"Sesja {rec.get('roman','')}" if rec.get("roman") else ""
            sessions_by_date[d] = {"date": d, "number": rec.get("roman", ""),
                                   "label": session_label, "vote_count": 0,
                                   "attendees": set(), "speakers": []}
        for v in rec["votes"]:
            vid += 1
            named_clean = v["named"]
            sessions_by_date[d]["vote_count"] += 1
            for cat in ("za", "przeciw", "wstrzymal_sie", "nieoddany"):
                sessions_by_date[d]["attendees"].update(named_clean.get(cat, []))
            all_votes.append({
                "id": str(vid),
                "session_date": d,
                "session_number": rec.get("roman", ""),
                "topic": v.get("topic") or "",
                "named_votes": named_clean,
                "counts": {k: v["counts"][k]
                           for k in ("za", "przeciw", "wstrzymal_sie")},
            })

    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({
            "date": d, "number": s["number"], "label": s["label"],
            "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]),
            "attendees": sorted(s["attendees"]), "speakers": [],
        })

    all_names = set()
    for v in all_votes:
        for cat_names in v["named_votes"].values():
            all_names.update(cat_names)

    club_by_name = {_norm_fullname(n): c for n, c in club_map.items()}
    real_names = set(_CANON_BY_RAW.values()) | all_names | set(club_by_name.keys())

    councilors_data = {}
    for name in sorted(real_names):
        councilors_data[name] = {
            "name": name, "club": club_by_name.get(name, ""), "district": None,
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
                elif cat == "nieobecny":
                    c["votes_nieobecny"] += 1
                else:
                    c["votes_brak"] += 1

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)

    councillor_sess = defaultdict(set)
    for v in all_votes:
        d = v["session_date"]
        for cat, names in v["named_votes"].items():
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
        pairs.append({"a": a, "b": b, "club_a": club_by_name.get(a, ""),
                      "club_b": club_by_name.get(b, ""), "score": score,
                      "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    clubs_count = Counter(club_by_name.get(n, "") or "NZ" for n in real_names)

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
    # records są już znormalizowane (build_output mutuje named w miejscu)
    club_by_name = {_norm_fullname(n): c for n, c in club_map.items()}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "votes": []})
    for rec in records:
        d = rec["date"]
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
                    "club": club_by_name.get(name, ""), "has_voting_data": True,
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
    if config_path:
        try:
            cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
            return dict(cfg.get("club_assignments", {}))
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
    print("=== Scraper Rada Miejska Grudziądza (BIP wydruki-głosowań PDF) ===")

    sessions = collect_sessions()
    print(f"  Sesje IX kadencji w indexie: {len(sessions)}")

    if not sessions:
        print("  BŁĄD: brak sesji")
        sys.exit(1)

    records = []
    ok = fail = 0
    for i, sess in enumerate(sessions):
        print(f"  [{i+1}/{len(sessions)}] {sess['roman']:5s} {sess['date']}")
        try:
            page = fetch(BIP + "/artykul/" + sess["slug"])
            if not page:
                print("    brak strony sesji")
                fail += 1
                continue
            pdf_url = _session_pdf_url(page)
            if not pdf_url:
                print("    brak PDF głosowań; pomijam")
                fail += 1
                continue
            data = fetch(pdf_url, binary=True)
            if not data:
                print("    błąd pobrania PDF")
                fail += 1
                continue
            votes = parse_glosowanie_pdf(data)
            print(f"    PDF {sess['date']}: {len(votes)} głosowań imiennych")
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

    output = build_output(records, club_map)
    profiles = build_profiles(records, club_map)
    save_split(output, args.output, profiles)

    total_votes = sum(len(r["votes"]) for r in records)
    print("\n=== PODSUMOWANIE ===")
    print(f"  sesji z danymi: {len(records)}")
    print(f"  głosowań imiennych: {total_votes}")
    print(f"  radnych (w kadencji): {len(output['kadencje'][0]['councilors'])}")
    print(f"  zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")


if __name__ == "__main__":
    main()
