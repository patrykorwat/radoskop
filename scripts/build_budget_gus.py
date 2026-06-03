#!/usr/bin/env python3
"""Scraper budżetów miast PL z GUS Bank Danych Lokalnych (BDL) API v1.

Jedno źródło, wszystkie polskie miasta. BDL publikuje per gmina (level 6,
łącznie z miastami na prawach powiatu) roczne szeregi w zł:
  - wydatki ogółem            -> subject P2633 (grupa G425)
  - wydatki wg działów Klas.  -> subject P2920 (jedna zmienna na dział)
  - dochody ogółem            -> subject P2693 (grupa dochodów, analogicznie)

Skalowalność: dodanie miasta wymaga tylko jego BDL unit-id (12-cyfrowy,
oparty na TERYT). Skrypt sam odkrywa zmienne w temacie, więc nie trzeba
pinować ID-ków per dział. Output trafia do cities/<slug>/docs/budget.json
w kanonicznym schemacie, który czyta frontend i build_compare_index.py.
Capability "budget" zapala się automatycznie, gdy plik istnieje.

API jest publiczne (bez klucza dla małego wolumenu). Limity: ~5 req/s bez
klucza. Dla pełnego runu wielomiastowego warto ustawić nagłówek X-ClientId
(env BDL_CLIENT_ID).

Uwaga zgodności z web_content_restrictions: ten skrypt to produkcyjny scraper
uruchamiany w pipeline (jak pozostałe scrapery miast), nie obejście pobierania.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BDL_BASE = "https://bdl.stat.gov.pl/api/v1"

# Tematy (subject) w grupie G425 "Wydatki budżetów gmin i miast na prawach
# powiatu". Odkryte z /subjects?parent-id=G425. Wartości to czytelne nazwy
# działów do wyświetlenia w porównywarce.
SUBJECT_EXPENDITURE_TOTAL = "P2633"   # Wydatki z budżetu ogółem
SUBJECT_EXPENDITURE_BY_DIVISION = "P2920"  # Wydatki ogółem wg działów Klas. Budż.
SUBJECT_REVENUE_TOTAL = "P2693"       # Dochody budżetu ogółem (grupa dochodów)

# Preferujemy wariant zmiennej dla miast na prawach powiatu.
PREFER_N1 = "gminy łącznie z miastami na prawach powiatu"


def _req(path: str, params: Dict[str, Any]) -> Any:
    params = {**params, "format": "json"}
    url = f"{BDL_BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    client_id = os.environ.get("BDL_CLIENT_ID")
    if client_id:
        req.add_header("X-ClientId", client_id)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    return None


# Priorytet typu jednostki (pole "kind" w BDL) dla budżetu miasta:
#   1 = gmina miejska, 3 = miejsko-wiejska (budżet całej gminy = rada miasta),
#   4 = miasto w gminie miejsko-wiejskiej (zwykle bez własnych danych budżet.),
#   2 = gmina wiejska (odrzucamy — to nie miasto mimo tej samej nazwy).
_KIND_PRIORITY = {"1": 0, "3": 1, "4": 2}


def resolve_unit_id(name: str) -> Optional[str]:
    """Znajduje BDL unit-id (level 6) po nazwie miasta.

    Dla nazw dwuznacznych (np. Brańsk: gmina miejska kind=1 i wiejska kind=2)
    preferuje wariant miejski. Wynik warto zacacheować w config (bdl_unit_id),
    co robi --cache-config w main().
    """
    data = _req("units/search", {"name": name, "level": 6, "page-size": 25})
    results = (data or {}).get("results") or []
    exact = [r for r in results if r.get("name", "").strip().lower() == name.strip().lower()]
    pool = exact or results
    if not pool:
        return None
    urban = [r for r in pool if r.get("kind") in _KIND_PRIORITY]
    pick = sorted(urban or pool, key=lambda r: _KIND_PRIORITY.get(r.get("kind"), 9))
    return pick[0]["id"]


def _list_variables(subject_id: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    page = 0
    while True:
        data = _req("variables", {"subject-id": subject_id, "page-size": 100, "page": page})
        res = (data or {}).get("results") or []
        out.extend(res)
        if not (data or {}).get("links", {}).get("next"):
            break
        page += 1
    return out


def _pick_total_variable(variables: List[Dict[str, Any]]) -> Optional[int]:
    """Wybiera zmienną 'ogółem' w zł, preferując wariant miast na prawach powiatu."""
    zl = [v for v in variables if v.get("measureUnitName") == "zł"]
    def score(v: Dict[str, Any]) -> tuple:
        n1 = (v.get("n1") or "")
        n2 = (v.get("n2") or "")
        return (
            1 if PREFER_N1 in n1 else 0,
            1 if n2.strip().lower() == "ogółem" or not n2 else 0,
        )
    if not zl:
        return None
    return sorted(zl, key=score, reverse=True)[0]["id"]


def _by_unit_values(unit_id: str, var_id: int) -> Dict[int, float]:
    data = _req(f"data/by-unit/{unit_id}", {"var-id": var_id, "page-size": 60})
    out: Dict[int, float] = {}
    for series in (data or {}).get("results") or []:
        for v in series.get("values") or []:
            # attrId 0 zwykle = brak danych (val 0 nic nie znaczy). Bierzemy
            # tylko wartości z atrybutem != 0 (dana faktyczna).
            if v.get("attrId", 0) == 0 and (v.get("val") or 0) == 0:
                continue
            try:
                out[int(v["year"])] = float(v["val"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _clean_division_name(var: Dict[str, Any]) -> str:
    """Czytelna nazwa działu z n-tupli zmiennej P2920 ('Dział 600 - Transport...')."""
    for k in ("n2", "n1", "n3"):
        val = (var.get(k) or "").strip()
        if val and val.lower() not in ("ogółem", PREFER_N1):
            # "Dział 600 - Transport i łączność" -> "Transport i łączność"
            if " - " in val:
                val = val.split(" - ", 1)[1]
            return val
    return var.get("n1") or "Inne"


def build_budget(unit_id: str, max_years: int = 8) -> Dict[str, Any]:
    # 1) Totale: wydatki + dochody ogółem
    exp_vars = _list_variables(SUBJECT_EXPENDITURE_TOTAL)
    exp_var = _pick_total_variable(exp_vars)
    rev_vars = _list_variables(SUBJECT_REVENUE_TOTAL)
    rev_var = _pick_total_variable(rev_vars)

    exp_by_year = _by_unit_values(unit_id, exp_var) if exp_var else {}
    rev_by_year = _by_unit_values(unit_id, rev_var) if rev_var else {}

    years = sorted(set(exp_by_year) | set(rev_by_year))[-max_years:]
    totals = []
    for y in years:
        rev = rev_by_year.get(y)
        exp = exp_by_year.get(y)
        totals.append({
            "year": y,
            "revenue": rev,
            "expenditure": exp,
            "deficit": (rev - exp) if (rev is not None and exp is not None) else None,
            "estimated": False,
        })

    # 2) Kategorie: wydatki wg działów (P2920) — każda zmienna to jeden dział
    div_vars = [v for v in _list_variables(SUBJECT_EXPENDITURE_BY_DIVISION)
                if v.get("measureUnitName") == "zł" and PREFER_N1 in (v.get("n1") or "")]
    categories: Dict[str, List[Dict[str, Any]]] = {}
    for var in div_vars:
        name = _clean_division_name(var)
        vals = _by_unit_values(unit_id, var["id"])
        for y, amount in vals.items():
            if y not in years:
                continue
            categories.setdefault(str(y), []).append({"name": name, "amount": amount})
    for y in categories:
        categories[y].sort(key=lambda c: -(c["amount"] or 0))

    return {
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": f"GUS BDL API v1 (unit {unit_id})",
        "currency": "zł",
        "totals": totals,
        "categories": categories,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Build budget.json from GUS BDL for a PL city")
    p.add_argument("--city", help="Slug miasta (cities/<slug>/)")
    p.add_argument("--unit-id", help="BDL unit-id (12 cyfr). Gdy brak, rozwiązywany po --name")
    p.add_argument("--name", help="Nazwa miasta do resolve_unit_id, gdy brak --unit-id")
    p.add_argument("--years", type=int, default=8)
    p.add_argument("--out", help="Ścieżka wyjścia (domyślnie cities/<slug>/docs/budget.json)")
    p.add_argument("--cache-config", action="store_true",
                   help="Zapisz rozwiązany unit-id do cities/<slug>/config.json (bdl_unit_id)")
    p.add_argument("--self-test", action="store_true", help="Test logiki na próbkach, bez sieci")
    args = p.parse_args()

    if args.self_test:
        return _self_test()

    if not args.city:
        print("Wymagane --city (albo --self-test).", file=sys.stderr)
        return 2

    here = Path(__file__).resolve().parent.parent
    config_path = here / "cities" / args.city / "config.json"

    # Kolejność źródeł unit-id: --unit-id > config.bdl_unit_id > resolve(name).
    unit_id = args.unit_id
    resolved_by_name = False
    if not unit_id and config_path.exists():
        try:
            unit_id = json.load(open(config_path, encoding="utf-8")).get("bdl_unit_id")
        except Exception:  # noqa: BLE001
            unit_id = None
    name = args.name
    if not unit_id and not name and config_path.exists():
        try:
            cfg = json.load(open(config_path, encoding="utf-8"))
            name = cfg.get("city_name") or cfg.get("voivodeship_name")
        except Exception:  # noqa: BLE001
            name = None
    if not unit_id and name:
        unit_id = resolve_unit_id(name)
        resolved_by_name = bool(unit_id)
    if not unit_id:
        print(f"{args.city}: brak unit-id (podaj --unit-id lub --name).", file=sys.stderr)
        return 2

    budget = build_budget(unit_id, max_years=args.years)
    if not budget["totals"]:
        print(f"{args.city}: brak danych budżetowych dla unit {unit_id} — nie zapisuję budget.json.")
        return 0

    out = Path(args.out) if args.out else here / "cities" / args.city / "docs" / "budget.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(budget, f, ensure_ascii=False, indent=2)
    print(f"{args.city}: zapisano {out} ({len(budget['totals'])} lat, {len(budget['categories'])} lat z działami)")

    if args.cache_config and resolved_by_name and config_path.exists():
        try:
            cfg = json.load(open(config_path, encoding="utf-8"))
            if cfg.get("bdl_unit_id") != unit_id:
                cfg["bdl_unit_id"] = unit_id
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                print(f"{args.city}: zacacheowano bdl_unit_id={unit_id} w config.json")
        except Exception as e:  # noqa: BLE001
            print(f"{args.city}: nie udało się zapisać bdl_unit_id: {e}", file=sys.stderr)
    return 0


def _self_test() -> int:
    """Waliduje parsowanie na próbkach JSON z BDL (kształty potwierdzone na żywym API)."""
    sample_vars = [
        {"id": 1, "n1": PREFER_N1, "n2": "ogółem", "measureUnitName": "zł"},
        {"id": 2, "n1": "gminy bez miast na prawach powiatu", "n2": "ogółem", "measureUnitName": "zł"},
        {"id": 3, "n1": PREFER_N1, "n2": "wydatki majątkowe", "measureUnitName": "zł"},
    ]
    assert _pick_total_variable(sample_vars) == 1, "powinien wybrać wariant MnPP/ogółem"
    div_var = {"n1": PREFER_N1, "n2": "Dział 600 - Transport i łączność", "measureUnitName": "zł"}
    assert _clean_division_name(div_var) == "Transport i łączność", _clean_division_name(div_var)
    # Priorytet kind: miejska (1) przed wiejską (2) dla tej samej nazwy
    pool = [{"id": "wiejska", "kind": "2"}, {"id": "miejska", "kind": "1"}]
    urban = [r for r in pool if r.get("kind") in _KIND_PRIORITY]
    best = sorted(urban or pool, key=lambda r: _KIND_PRIORITY.get(r.get("kind"), 9))[0]
    assert best["id"] == "miejska", best
    print("self-test OK: wybór zmiennej, czyszczenie nazw działów, priorytet kind")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
