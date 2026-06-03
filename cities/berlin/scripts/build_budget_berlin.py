#!/usr/bin/env python3
"""Adapter budżetu Berlina z daten.berlin.de (Doppelhaushalt CSV).

Berlin (kraj związkowy + dzielnice) publikuje Haushaltsplan jako maszynowy CSV
na portalu Open Data. Plik to płaska tabela tytułów budżetowych z kolumnami
(opisanymi na stronie datasetu):
  Jahr, Titelart (Einnahme-/Ausgabetitel), BetragTyp (Soll=plan / Ist=wykonanie),
  Betrag (EUR), Hauptfunktionsbezeichnung (plan funkcjonalny ~ polskie działy),
  Hauptgruppenbezeichnung (plan rodzajowy) i in.

Mapowanie na kanoniczny budget.json:
  - currency: "€"
  - totals[rok]: expenditure = Σ Betrag(Ausgabe), revenue = Σ Betrag(Einnahme);
    deficit = revenue - expenditure; estimated = True dla BetragTyp "Soll"
    (plan), False dla "Ist" (wykonanie)
  - categories[rok]: wydatki (Ausgabe) zgrupowane po Hauptfunktionsbezeichnung

Plan Berlina jest dwuletni (Doppelhaushalt). URL CSV zmienia się co edycję, więc
podawaj --csv-url albo --csv-file. Domyślny URL to edycja 2026/2027.

Uwaga zgodności z web_content_restrictions: to produkcyjny adapter pipeline,
nie obejście pobierania treści.
"""

import argparse
import csv
import io
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CSV_URL = "https://www.berlin.de/sen/finanzen/service/daten/260223_doppelhaushalt_2026_2027.csv"

# Nazwy kolumn (z opisu datasetu). DictReader matchuje po nagłówku, więc
# kolejność jest nieistotna; tu trzymamy oczekiwane klucze.
COL_YEAR = "Jahr"
COL_TITELART = "Titelart"
COL_BETRAGTYP = "BetragTyp"
COL_BETRAG = "Betrag"
COL_FUNCTION = "Hauptfunktionsbezeichnung"


def parse_de_amount(raw: str) -> Optional[float]:
    """Parsuje niemiecki zapis kwoty: '1.234.567,89' -> 1234567.89. Minus z przodu."""
    if raw is None:
        return None
    s = raw.strip().replace("\xa0", "").replace(" ", "")
    if not s or s in ("-", "."):
        return None
    neg = s.startswith("-")
    s = s.lstrip("+-")
    if "," in s:  # przecinek = separator dziesiętny, kropka = tysiące
        s = s.replace(".", "").replace(",", ".")
    else:  # brak przecinka: kropki to tysiące (kwoty budżetowe są całkowite)
        s = s.replace(".", "")
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def _is_expenditure(titelart: str) -> bool:
    return "usgabe" in (titelart or "").lower()  # Ausgabe


def _is_revenue(titelart: str) -> bool:
    return "innahme" in (titelart or "").lower()  # Einnahme


def parse_rows(rows) -> Dict[str, Any]:
    """Agreguje wiersze CSV (iterowalne dictów) do kanonicznego budżetu."""
    exp: Dict[int, float] = {}
    rev: Dict[int, float] = {}
    estimated_year: Dict[int, bool] = {}
    cats: Dict[int, Dict[str, float]] = {}

    for r in rows:
        try:
            year = int(str(r.get(COL_YEAR, "")).strip()[:4])
        except (ValueError, TypeError):
            continue
        amount = parse_de_amount(r.get(COL_BETRAG, ""))
        if amount is None:
            continue
        betrag_typ = (r.get(COL_BETRAGTYP) or "").strip().lower()
        is_soll = betrag_typ.startswith("soll") or betrag_typ == "" or "plan" in betrag_typ
        titelart = r.get(COL_TITELART, "")

        if _is_expenditure(titelart):
            exp[year] = exp.get(year, 0.0) + amount
            func = (r.get(COL_FUNCTION) or "Sonstige").strip() or "Sonstige"
            cats.setdefault(year, {})[func] = cats.setdefault(year, {}).get(func, 0.0) + amount
        elif _is_revenue(titelart):
            rev[year] = rev.get(year, 0.0) + amount
        else:
            continue
        # Rok jest "estimated" gdy którykolwiek wiersz to Soll (plan). Wykonanie
        # (Ist) nadpisuje na False tylko gdy wszystkie wiersze roku to Ist.
        estimated_year[year] = estimated_year.get(year, True) and is_soll

    years = sorted(set(exp) | set(rev))
    totals: List[Dict[str, Any]] = []
    for y in years:
        rv = round(rev[y]) if y in rev else None
        ev = round(exp[y]) if y in exp else None
        totals.append({
            "year": y,
            "revenue": rv,
            "expenditure": ev,
            "deficit": (rv - ev) if (rv is not None and ev is not None) else None,
            "estimated": estimated_year.get(y, True),
        })

    categories: Dict[str, List[Dict[str, Any]]] = {}
    for y, d in cats.items():
        items = [{"name": k, "amount": round(v)} for k, v in d.items() if v]
        items.sort(key=lambda c: -c["amount"])
        categories[str(y)] = items

    return {
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "daten.berlin.de Doppelhaushalt (Senatsverwaltung für Finanzen)",
        "currency": "€",
        "totals": totals,
        "categories": categories,
    }


def _read_csv_text(text: str):
    # Sniff separatora (Berlin używa ';'); fallback ','.
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delim = dialect.delimiter
    except csv.Error:
        delim = ";"
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def load_csv(path: Optional[str], url: Optional[str]) -> List[Dict[str, str]]:
    if path:
        data = Path(path).read_bytes()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "radoskop-budget/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return _read_csv_text(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return _read_csv_text(data.decode("latin-1", errors="replace"))


def main() -> int:
    p = argparse.ArgumentParser(description="Build Berlin budget.json from Doppelhaushalt CSV")
    p.add_argument("--csv-url", default=DEFAULT_CSV_URL)
    p.add_argument("--csv-file", help="Lokalny plik CSV zamiast pobierania")
    p.add_argument("--out", help="Domyślnie cities/berlin/docs/budget.json (względem repo)")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        return _self_test()

    rows = load_csv(args.csv_file, args.csv_url)
    budget = parse_rows(rows)
    if not budget["totals"]:
        print("Berlin: brak danych po parsowaniu CSV — sprawdź kolumny/URL.", file=sys.stderr)
        return 1

    if args.out:
        out = Path(args.out)
    else:
        # cities/berlin/scripts/ -> cities/berlin/docs/
        out = Path(__file__).resolve().parent.parent / "docs" / "budget.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(budget, f, ensure_ascii=False, indent=2)
    yrs = ", ".join(str(t["year"]) for t in budget["totals"])
    print(f"berlin: zapisano {out} (lata {yrs})")
    return 0


def _self_test() -> int:
    assert parse_de_amount("1.234.567,00") == 1234567.0
    assert parse_de_amount("2.000") == 2000.0
    assert parse_de_amount("-500,5") == -500.5
    sample = [
        {"Jahr": "2026", "Titelart": "Ausgabetitel", "BetragTyp": "Soll",
         "Betrag": "1.000", "Hauptfunktionsbezeichnung": "Bildung"},
        {"Jahr": "2026", "Titelart": "Ausgabetitel", "BetragTyp": "Soll",
         "Betrag": "400", "Hauptfunktionsbezeichnung": "Verkehr- und Nachrichtenwesen"},
        {"Jahr": "2026", "Titelart": "Einnahmetitel", "BetragTyp": "Soll",
         "Betrag": "1.200", "Hauptfunktionsbezeichnung": ""},
    ]
    b = parse_rows(sample)
    t = b["totals"][0]
    assert t["expenditure"] == 1400 and t["revenue"] == 1200, t
    assert t["deficit"] == -200 and t["estimated"] is True, t
    assert b["categories"]["2026"][0]["name"] == "Bildung", b["categories"]
    assert b["currency"] == "€"
    print("self-test OK: parsowanie kwot DE, agregacja Ausgabe/Einnahme, kategorie funkcyjne")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
