#!/usr/bin/env python3
"""Skutki polityki lokalnej: join uchwale podatkowo-finansowych z wskaznikami OSF MF.

Dla kazdego glosowania nad uchwala z katalogu "zdarzen politycznych" (stawki
podatkow lokalnych, oplaty, kredyty/obligacje, dotacje, budzet/WPF) liczy
zmiane wskaznikow sytuacji finansowej (definicje MF, patrz
build_fiscal_indicators.py) w oknie 2 lata po uchwale vs 2 lata przed, oraz
te sama zmiane dla mediany rowiesniczej (inne JST tej samej klasy) — czyli
sygnature skutku ponad trend równieśniczy.

Metoda WYŁACZNIE na wskaznikach MF — zadnych autorskich wag ani progow.
Brak interpretacji przyczynowej w danych: pokazujemy rownoleglost czasowa
(uchwala -> delta) z jasnym zastrzezeniem, ze to asocjacja, nie kauzacja.

Wejscie:
  --docs DIR      katalog docs miasta (kadencja-*.json i/lub data.json z votes)
  --fiscal FILE   fiscal-indicators.json tego miasta (build_fiscal_indicators)
  --peers FILE    (opcjonalnie) fiscal_peers.json — mediany klasy; jezeli brak,
                  mediane liczymy z --peers-glob (inne fiscal-indicators.json)
  --peers-glob G  glob pozostalych fiscal-indicators.json do mediany krajowej
WYJSCIE: policy-effects.json

Uzycie:
  python build_policy_effects.py --docs docs --fiscal docs/fiscal-indicators.json \
      --peers-glob '/repos/*/cities/*/docs/fiscal-indicators.json' --out docs/policy-effects.json
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone

# ---- katalog zdarzen politycznych (tytul uchwaly -> kategoria) ----
# Reguły na surowym temacie z BIP; kolejnosc istotna (pierwsza pasujaca).
CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("podatek_od_nieruchomosci",
     re.compile(r"podatku od nieruchomo", re.I)),
    ("podatek_od_srodkow_transportu",
     re.compile(r"podatku od (srodk|środk)ów transportu", re.I)),
    ("podatek_rolny",
     re.compile(r"podatku roln", re.I)),
    ("opłata_miejscowa_uzdrowiskowa",
     re.compile(r"opłat(y|a)? (miejscow|uzdrowiskow)|opłata (miejscowa|uzdrowiskowa)", re.I)),
    ("opłata_za_żłobki_przedszkola",
     re.compile(r"opłat(y|a)?.{0,40}(żłobk|przedszkol|wychowania przedszkolnego)", re.I)),
    ("opłata_parkingowa",
     re.compile(r"strefa płatnego parkowania|opłat(y|a)? za parkowanie|parkowania", re.I)),
    ("opłata_za_gospodarowanie_odpadami",
     re.compile(r"opłat(y|a)? za gospodarowanie odpadami", re.I)),
    ("opłata_targowa_reklamowa",
     re.compile(r"opłat(y|a)? (targow|reklamow)|opłata targowa", re.I)),
    ("pas_drogowy",
     re.compile(r"pasa drogowego", re.I)),
    ("bonifikaty_ulgi",
     re.compile(r"bonifikat|ulgi w spłacie|umorzen(i|ie) (odset|zaległo)", re.I)),
    ("kredyt_pozyczka_obligacje",
     re.compile(r"(kredyt|pożyczk|pożyczk|emituj|emisji obligacji|obligacj)", re.I)),
    ("dotacje_podmiotowe_przedmiotowe",
     re.compile(r"dotacj", re.I)),
    ("budzet",
     re.compile(r"budżet", re.I)),
    ("wieloletnia_prognoza_finansowa",
     re.compile(r"wieloletni(a|e|ej) prognoz", re.I)),
    ("wynagrodzenia_organow",
     re.compile(r"wynagrodzen(i|ia) (prezydenta|burmistrza|wójta|radnych)|staroś(c|t)y", re.I)),
]

# Wykluczenia (kuratura 2026-09-05): tematy, ktore regexy lapaja jako
# zdarzenia podatkowo-finansowe, a nimi nie sa. Testowane PRZED CATEGORIES.
EXCLUDE = re.compile(
    r"pokrycia cz(e|ę)ści koszt(o|ó)w gospodarowania odpadami"  # doplata, nie opłata
    r"|jednolitego tekstu|zmieniaj(a|ą)cej? uchwa(ł|l)y w sprawie wyboru"
    , re.I)

# wskazniki wlasciwe dla kategorii (klucz -> rola) z fiscal-indicators.json
EFFECTS: dict[str, list[str]] = {
    # strona dochodowa / zdolnosc do finansowania zadan wlasnych
    "podatek_od_nieruchomosci": ["WL1", "WL2", "WB3", "WB6"],
    "podatek_od_srodkow_transportu": ["WL1", "WL2", "WB3"],
    "podatek_rolny": ["WL1", "WL2", "WB3"],
    "opłata_miejscowa_uzdrowiskowa": ["WL1", "WL2"],
    "opłata_za_żłobki_przedszkola": ["WL1", "WB4"],
    "opłata_parkingowa": ["WL1", "WL2"],
    "opłata_za_gospodarowanie_odpadami": ["WB5", "WB4"],
    "opłata_targowa_reklamowa": ["WL1"],
    "pas_drogowy": ["WL1", "WL3"],
    "bonifikaty_ulgi": ["WB3", "WB8"],
    # strona dluzna
    "kredyt_pozyczka_obligacje": ["WZ1", "WZ3", "WL4", "WL5", "WB9", "WB11", "WB12"],
    # strona wydatkowa
    "dotacje_podmiotowe_przedmiotowe": ["WB4", "WB5"],
    "budzet": ["WB1", "WB3", "WB6", "WL2", "WZ1"],
    "wieloletnia_prognoza_finansowa": ["WB9", "WB10", "WB12", "WZ1"],
    "wynagrodzenia_organow": ["WB5"],
}

# Sygnaturowy wskaznik do chipa w feedzie (jedna liczba na zdarzenie).
# None = bez chipa kategoria zbyt czesta/ogolna (budzet, WPF, dotacje) —
# zdarzenia zostaja w pliku, ale feed promuje only mierzalne decyzje.
SIGNATURE: dict[str, str | None] = {
    "podatek_od_nieruchomosci": "WL2",
    "podatek_od_srodkow_transportu": "WL2",
    "podatek_rolny": "WL2",
    "opłata_miejscowa_uzdrowiskowa": "WL1",
    "opłata_za_żłobki_przedszkola": "WB4",
    "opłata_parkingowa": "WL2",
    "opłata_za_gospodarowanie_odpadami": "WB5",
    "opłata_targowa_reklamowa": "WL1",
    "pas_drogowy": "WL3",
    "bonifikaty_ulgi": "WB3",
    "kredyt_pozyczka_obligacje": "WB11",
    "dotacje_podmiotowe_przedmiotowe": None,
    "budzet": None,
    "wieloletnia_prognoza_finansowa": None,
    "wynagrodzenia_organow": None,
}

# Kategorie, ktorych skutek prawny wchodzi w zycie 1 stycznia roku
# nastepnego po uchwale (art. 24a ordynacji podatkowej: wciazanie w zycie
# przed dniem 1 stycznia nastepnego roku podatkowego). Wykres i okna
# liczone od effective_year, nie od roku glosowania.
EFFECTIVE_NEXT_YEAR = {
    "podatek_od_nieruchomosci", "podatek_od_srodkow_transportu", "podatek_rolny",
    "opłata_miejscowa_uzdrowiskowa", "opłata_targowa_reklamowa",
}
ALL_INDICATORS = sorted({i for v in EFFECTS.values() for i in v})


def load_votes(docs_dir: str) -> list[dict]:
    """Glosowania z data.json (votes per kadencja) + kadencja-*.json, dedup po id."""
    votes: dict[str, dict] = {}
    files = []
    dj = os.path.join(docs_dir, "data.json")
    if os.path.exists(dj):
        files.append(dj)
    files += sorted(globmod.glob(os.path.join(docs_dir, "kadencja-*.json")))
    for path in files:
        try:
            d = json.load(open(path))
        except (json.JSONDecodeError, OSError):
            continue
        kads: list = []
        if isinstance(d, dict):
            if "votes" in d and isinstance(d["votes"], list):
                kads = [d]
            k = d.get("kadencje", [])
            if isinstance(k, dict):
                k = list(k.values())
            kads += [x for x in k if isinstance(x, dict)]
        for kad in kads:
            for v in kad.get("votes") or []:
                if isinstance(v, dict) and v.get("id") and v.get("topic"):
                    votes.setdefault(v["id"], v)
    return list(votes.values())


def classify(topic: str) -> str | None:
    if EXCLUDE.search(topic):
        return None
    for cat, rx in CATEGORIES:
        if rx.search(topic):
            return cat
    return None


def adopted(v: dict) -> bool:
    """Pominiety tylko wyraznie nieprzyjety; resolution bywa null (brak pola)."""
    r = (v.get("resolution") or "").lower()
    if not r:
        return True
    return not any(w in r for w in ("odrzucon", "nie podj", "niepodj", "stwierdzenie braku"))


def fiscal_years(path: str) -> dict[str, dict]:
    d = json.load(open(path))
    return {y["year"]: y for y in d.get("years", [])}


def window(vals: dict[str, dict], inds: list[str], years: list[str]) -> dict[str, float | None]:
    """Srednia wskaznika po listie lat (null gdy brak)."""
    out = {}
    for ind in inds:
        xs = [vals[y][ind] for y in years if y in vals and isinstance(vals[y].get(ind), (int, float))]
        out[ind] = round(statistics.mean(xs), 4) if xs else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True)
    ap.add_argument("--fiscal", required=True)
    ap.add_argument("--peers")
    ap.add_argument("--peers-glob")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    fiscal = fiscal_years(args.fiscal)
    if not fiscal:
        print("puste fiscal-indicators", file=sys.stderr)
        return 1

    # mediana rowiesnicza per wskaznik per rok
    peer_years: dict[str, dict[str, list[float]]] = {}
    if args.peers:
        pj = json.load(open(args.peers))
        # forma build_fiscal_peers: {year: {indicator: median}} albo zagniezdzona
        for year, row in (pj.get("medians", pj) if isinstance(pj, dict) else {}).items():
            if isinstance(row, dict):
                peer_years[year] = {k: [v] for k, v in row.items() if isinstance(v, (int, float))}
    if not peer_years and args.peers_glob:
        for p in globmod.glob(args.peers_glob):
            if os.path.abspath(p) == os.path.abspath(args.fiscal):
                continue
            try:
                for y in fiscal_years(p).values():
                    for ind in ALL_INDICATORS:
                        val = y.get(ind)
                        if isinstance(val, (int, float)):
                            peer_years.setdefault(y["year"], {}).setdefault(ind, []).append(val)
            except Exception:
                continue
    peer_med = {yr: {ind: statistics.median(xs) for ind, xs in row.items()}
                for yr, row in peer_years.items()}

    votes = load_votes(args.docs)
    events = []
    for v in votes:
        if not adopted(v):
            continue
        cat = classify(v.get("topic", ""))
        if not cat:
            continue
        m = re.match(r"(\d{4})", str(v.get("session_date") or ""))
        if not m:
            continue
        y0 = int(m.group(1))
        # effective_year: podatki i oplaty wg ORP wchodza 1 stycznia roku
        # nastepnego po uchwale; reszta obowiazuje od glosowania.
        y_eff = y0 + 1 if cat in EFFECTIVE_NEXT_YEAR else y0
        inds = EFFECTS[cat]
        # Okno: 'po' obejmuje SAM rok wejscia w zycie (sprawozdanie BeSTi@ za
        # ten rok jest juz wykonywane pod rzad danej uchwaly), 'przed' konczy
        # sie rok wczesniej.
        pre = [str(y_eff - 2), str(y_eff - 1)]
        post = [str(y_eff), str(y_eff + 1)]
        base = window(fiscal, inds, pre)
        after = window(fiscal, inds, post)
        # OKNO ODCINTE: BeSTi@ w fiscal-indicators to okno przesuwne (~5 lat);
        # bez zadnej wartosci po obu stronach zdarzenie nie jest mierzalne —
        # pomin zamiast emitowac same null (widoczne jako "okno poza seria").
        if all(x is None for x in after.values()) and all(x is None for x in base.values()):
            continue
        pbase = window(peer_med, inds, pre) if peer_med else {}
        pafter = window(peer_med, inds, post) if peer_med else {}
        delta = {}
        for ind in inds:
            d = dv = None
            b_i, a_i = base.get(ind), after.get(ind)
            if isinstance(b_i, float) and isinstance(a_i, float):
                d = a_i - b_i
            pb_i, pa_i = pbase.get(ind), pafter.get(ind)
            if isinstance(pb_i, float) and isinstance(pa_i, float):
                dv = pa_i - pb_i
            delta[ind] = {"miasto": round(d, 4) if d is not None else None,
                          "rowiesnicy": round(dv, 4) if dv is not None else None,
                          "ponad_trend": round(d - dv, 4) if d is not None and dv is not None else None}
        # chip sygnaturowy: tylko kategorie mierzalne (SIGNATURE) i tylko gdy
        # ponad_trend sygnatury policzony — feed nie promuje niezmierzalnego.
        sig = SIGNATURE.get(cat)
        chip = None
        if sig and sig in delta and delta[sig]["ponad_trend"] is not None:
            chip = {"indicator": sig, "value": delta[sig]["ponad_trend"]}
        events.append({
            "vote_id": v["id"],
            "date": v.get("session_date"),
            "session_number": v.get("session_number"),
            "topic": v.get("topic"),
            "category": cat,
            "effective_year": y_eff,
            "counts": v.get("counts"),
            "source_url": v.get("source_url"),
            "signature": chip,
            "window": {"przed": pre, "po": post},
            "przed": base, "po": after, "delta": delta,
        })
    events.sort(key=lambda e: str(e["date"] or ""))

    # Szeregi do wykresu w panelu Radar (bez dowozenia calego fiscal-indicators):
    # wartosci per rok per wskaznik — miasto i mediana rowiesnicza.
    series_city = {ind: {y: r[ind] for y, r in fiscal.items()
                         if isinstance(r.get(ind), (int, float))}
                   for ind in ALL_INDICATORS}
    series_peer = {ind: {y: r[ind] for y, r in peer_med.items()
                         if isinstance(r.get(ind), (int, float))}
                   for ind in ALL_INDICATORS}

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "uchwaly z BIP (topic glosowania) x wskazniki OSF MF (BeSTi@) — patrz build_fiscal_indicators.py",
        "method": "delta srednia(eff..eff+1) vs srednia(eff-2..eff-1), wskaznik MF; rownanie z mediana rowiesnicza. Asocjacja czasowa, nie kauzacja.",
        "caveat": "Wspolwystepowanie wielu czynnikow; pojedyncza uchwala rzadko jest jedynym sprawca zmiany wskaznika.",
        "peer_basis": "mediana wskaznikowa" if peer_med else "brak rowiesnikow",
        "series": {"miasto": series_city, "mediana": series_peer},
        "events": events,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{args.out}: {len(events)} zdarzen z {len(votes)} glosowan, "
          f"wskazniki z {len(fiscal)} lat, peer-mediana: {bool(peer_med)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
