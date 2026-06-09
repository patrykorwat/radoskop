#!/usr/bin/env python3
"""Builder budżetu miasta ze sprawozdań BeSTi@ (Ministerstwo Finansów).

Źródło: publiczne REST API https://bestia-api.mf.gov.pl (bez klucza). Dane to
oficjalne sprawozdania budżetowe JST, te same które nadzoruje RIO:
  - Rb-28S: wykonanie wydatków (pl=plan, ww=wydatki wykonane), per dział/rozdział/paragraf
  - Rb-27S: wykonanie dochodów (pl=plan, dw=dochody wykonane)
Deficyt = dochody wykonane - wydatki wykonane.

Output: cities/<slug>/docs/budget.json w TYM SAMYM kanonicznym schemacie co
build_budget_gus.py (totals[] + categories{}), więc frontend i build_compare_index
działają bez zmian. Dodatkowo pola plan/wykonanie i procent realizacji.

Mapowanie miasta -> jednostka BeSTi@ z kodu TERYT (config 'teryt', 7 cyfr):
  wk = teryt[0:2], pk = teryt[2:4]
  miasto na prawach powiatu (pk 61-99): gk = "00" (BeSTi@ koduje je powiatowo)
  zwykła gmina: gk = teryt[4:6]

Skrobacz produkcyjny (jak build_budget_gus.py), uruchamiany w pipeline.
Uwaga web_content_restrictions: to scraper produkcyjny, nie obejście pobierania.

Użycie:
  python build_budget_bestia.py --city gdansk
  python build_budget_bestia.py --city gdansk --years 6 --out /tmp/gd.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_dzial_cofog import COFOG_LABELS, map_to_cofog  # noqa: E402

API = "https://bestia-api.mf.gov.pl"
HERE = Path(__file__).resolve().parent.parent
PAGE_LIMIT = 100  # twardy max API (200+ -> HTTP 400)


def api_get(path: str, filters: dict[str, str], *, fields: str = "",
            page: int = 1, limit: int = PAGE_LIMIT, retries: int = 3) -> dict | None:
    """GET z deepObject filtrem (filter[<pole>]=wartość, literalne nawiasy)."""
    parts = [f"filter[{k}]={urllib.parse.quote(str(v))}" for k, v in filters.items()]
    parts.append("format=JSON")
    if fields:
        parts.append("fields=" + fields)
    parts.append(f"page={page}")
    parts.append(f"limit={limit}")
    url = f"{API}{path}?" + "&".join(parts)
    req = urllib.request.Request(url, headers={"User-Agent": "radoskop-budget-bestia"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status == 204:
                    return {"data": [], "paging": {"totalItems": 0, "totalPages": 0}}
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                print(f"  WARN: {path} p{page}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def num(s) -> float:
    return float(str(s or "0").replace(" ", "").replace("\xa0", "").replace(",", ".") or 0)


def resolve_unit(teryt: str) -> dict | None:
    """Zwraca {const_id, nazwa, wk, pk, gk} jednostki BeSTi@ dla kodu TERYT.

    Wiele jednostek dzieli ten sam kod wk/pk/gk (np. związek gmin z kodem siedziby).
    Dlatego pobieramy kandydatów i wybieramy WŁAŚCIWY samorząd:
    - grodzki: gk=00, pt=2 (miasto na prawach powiatu), odsiewa związki (pt 7/8),
    - gmina: gt = rodzaj z TERYT (1 miejska/2 wiejska/3 miejsko-wiejska), z wykluczeniem
      związków (gt=Z). Bez tego Bochnia łapała "Związek Gmin Dolnego Dorzecza Raby".
    """
    if not teryt or len(teryt) < 6:
        return None
    wk, pk, gk0 = teryt[0:2], teryt[2:4], teryt[4:6]
    rodzaj = teryt[6] if len(teryt) >= 7 else ""
    grodzki = pk.isdigit() and int(pk) >= 61
    filt = {"jednostka-wk": wk, "jednostka-pk": pk, "jednostka-gk": "00" if grodzki else gk0}
    if grodzki:
        filt["jednostka-pt"] = "2"  # miasto na prawach powiatu
    elif rodzaj:
        # Filtr po typie gminy odsiewa związki (gt=Z) dzielące ten sam kod (np.
        # związek z kodem siedziby Bochni). Miasto w gminie miejsko-wiejskiej
        # (rodzaj 4) sprawozdaje się jako gmina MW (gt=3).
        filt["jednostka-gt"] = "3" if rodzaj == "4" else rodzaj
    j = api_get("/api/sprawozdania", filt, limit=5)
    rows = (j or {}).get("data") or []
    if not rows:  # fallback bez filtra typu
        filt.pop("jednostka-gt", None)
        rows = (api_get("/api/sprawozdania", filt, limit=5) or {}).get("data") or []
    if not rows:
        return None
    r = rows[0]
    return {"const_id": r["jednostka-const-id"], "nazwa": r["jednostka-nazwa"],
            "wk": r["jednostka-wk"], "pk": r["jednostka-pk"], "gk": r["jednostka-gk"]}


def annual_years(const_id: str, kod: str, n: int) -> list[str]:
    """Lata, dla których jest roczne (okres 4) sprawozdanie danego typu."""
    years: set[str] = set()
    page, pages = 1, 1
    while page <= pages and page <= 30:
        j = api_get("/api/sprawozdania",
                    {"jednostka-const-id": const_id, "typ-sprawozdania-kod": kod,
                     "okres-okres": "4"}, fields="okres-rok", page=page)
        if not j:
            break
        pages = j["paging"]["totalPages"]
        for x in j.get("data", []):
            years.add(x["okres-rok"])
        page += 1
    return sorted(years)[-n:]


def sum_report(path: str, const_id: str, rok: str, value_keys: list[str],
               group_key: str | None = None) -> tuple[dict, dict]:
    """Sumuje pola value_keys z rocznego sprawozdania; opcjonalnie grupuje
    pierwszą wartość po group_key. Zwraca (sumy_globalne, grupowane_pierwszej)."""
    totals = {k: 0.0 for k in value_keys}
    grouped: dict[str, float] = {}
    fields = ",".join(([group_key] if group_key else []) + value_keys)
    page, pages = 1, 1
    while page <= pages and page <= 80:
        j = api_get(path, {"jednostka-const-id": const_id, "okres-rok": rok,
                           "okres-okres": "4"}, fields=fields, page=page)
        if not j:
            break
        pages = j["paging"]["totalPages"]
        for x in j.get("data", []):
            for k in value_keys:
                totals[k] += num(x.get(k))
            if group_key:
                grouped[x.get(group_key)] = grouped.get(x.get(group_key), 0.0) + num(x.get(value_keys[0]))
        page += 1
        time.sleep(0.05)
    return totals, grouped


def debt_total(const_id: str, rok: str) -> float | None:
    """Zadłużenie ogółem na koniec roku z Rb-Z (tytuły dłużne).

    Rb-Z td jest hierarchiczne: wiersz symbol="E" to ZOBOWIĄZANIA WG TYTUŁÓW
    DŁUŻNYCH (E1+E2+E3+E4) = total. Składowych (E1, E2, E2.2...) NIE sumujemy,
    bo to podwójne liczenie. Bierzemy wprost wiersz E.
    """
    j = api_get("/api/pozycje-rbztd",
                {"jednostka-const-id": const_id, "okres-rok": rok, "okres-okres": "4"},
                fields="symbol,z", limit=100)
    for x in (j or {}).get("data", []):
        if x.get("symbol") == "E":
            return round(num(x.get("z")), 2)
    return None


def aggregate_rb28s(const_id: str, rok: str) -> tuple[dict, dict, dict]:
    """Jeden przebieg po rocznym Rb-28S: sumuje wydatki wykonane po DZIALE
    (kategorie) i po funkcji COFOG (mapując każdy wiersz po dział+rozdział)."""
    tot = {"ww": 0.0, "pl": 0.0}
    by_dzial: dict[str, float] = {}
    by_cofog: dict[str, float] = {}
    page, pages = 1, 1
    while page <= pages and page <= 80:
        j = api_get("/api/pozycje-rb28s",
                    {"jednostka-const-id": const_id, "okres-rok": rok, "okres-okres": "4"},
                    fields="dzial,rozdzial,ww,pl", page=page)
        if not j:
            break
        pages = j["paging"]["totalPages"]
        for x in j.get("data", []):
            w = num(x.get("ww"))
            tot["ww"] += w
            tot["pl"] += num(x.get("pl"))
            by_dzial[x.get("dzial")] = by_dzial.get(x.get("dzial"), 0.0) + w
            gf = map_to_cofog(x.get("dzial"), x.get("rozdzial"))
            if gf:
                by_cofog[gf] = by_cofog.get(gf, 0.0) + w
        page += 1
        time.sleep(0.05)
    return tot, by_dzial, by_cofog


def dzialy_names() -> dict[str, str]:
    out: dict[str, str] = {}
    page = 1
    while page <= 4:
        j = api_get("/api/dzialy", {}, page=page, limit=25)
        if not j or not j.get("data"):
            break
        for x in j["data"]:
            out.setdefault(x["symbol"], x["tekst"])
        if page >= j["paging"]["totalPages"]:
            break
        page += 1
    return out


def build_budget(teryt: str, years_n: int) -> dict | None:
    unit = resolve_unit(teryt)
    if not unit:
        return None
    cid = unit["const_id"]
    print(f"  jednostka: {unit['nazwa']} ({unit['wk']}/{unit['pk']}/{unit['gk']})")
    years = annual_years(cid, "Rb-28s", years_n)
    if not years:
        return None
    dz_names = dzialy_names()

    totals = []
    categories: dict[str, list] = {}
    cofog: dict[str, list] = {}
    for y in years:
        exp_tot, exp_by_dz, exp_by_cofog = aggregate_rb28s(cid, y)
        rev_tot, _ = sum_report("/api/pozycje-rb27s", cid, y, ["dw", "pl"])
        debt = debt_total(cid, y)
        revenue, expenditure = rev_tot["dw"], exp_tot["ww"]
        totals.append({
            "year": y,
            "revenue": round(revenue, 2),
            "expenditure": round(expenditure, 2),
            "deficit": round(revenue - expenditure, 2),
            "debt": debt,
            "revenue_plan": round(rev_tot["pl"], 2),
            "expenditure_plan": round(exp_tot["pl"], 2),
            "expenditure_exec_pct": round(expenditure / exp_tot["pl"] * 100, 1) if exp_tot["pl"] else None,
            "estimated": False,
        })
        cats = [{"name": dz_names.get(d, d), "amount": round(v, 2)}
                for d, v in exp_by_dz.items() if v]
        cats.sort(key=lambda c: -(c["amount"] or 0))
        categories[str(y)] = cats
        # COFOG: udziały funkcji w wydatkach (% liczone do wydatków ogółem).
        cof = [{"code": gf, "name": COFOG_LABELS.get(gf, gf), "amount": round(v, 2),
                "pct": round(v / expenditure * 100, 1) if expenditure else None}
               for gf, v in exp_by_cofog.items() if v]
        cof.sort(key=lambda c: -(c["amount"] or 0))
        cofog[str(y)] = cof
        print(f"  {y}: dochody {revenue/1e6:.0f} mln, wydatki {expenditure/1e6:.0f} mln, "
              f"deficyt {(revenue-expenditure)/1e6:.0f} mln, dług "
              f"{debt/1e6:.0f} mln" if debt is not None else
              f"  {y}: dochody {revenue/1e6:.0f} mln, wydatki {expenditure/1e6:.0f} mln, "
              f"deficyt {(revenue-expenditure)/1e6:.0f} mln")

    return {
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": f"BeSTi@ MF (jednostka {unit['nazwa']}, sprawozdania Rb-27S/Rb-28S/Rb-Z)",
        "currency": "zł",
        "totals": totals,
        "categories": categories,
        "cofog": cofog,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Budżet miasta ze sprawozdań BeSTi@ (MF)")
    ap.add_argument("--city", required=True, help="slug miasta (cities/<slug>/)")
    ap.add_argument("--years", type=int, default=6, help="ile ostatnich lat (domyślnie 6)")
    ap.add_argument("--out", help="ścieżka wyjścia (domyślnie cities/<slug>/docs/budget.json)")
    args = ap.parse_args()

    cfg_path = HERE / "cities" / args.city / "config.json"
    if not cfg_path.is_file():
        print(f"ERROR: brak configu {cfg_path}", file=sys.stderr)
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    teryt = cfg.get("teryt")
    if not teryt:
        print(f"ERROR: {args.city} nie ma 'teryt' w config (uruchom build_units.py --write-teryt)",
              file=sys.stderr)
        return 1

    budget = build_budget(teryt, args.years)
    if not budget or not budget["totals"]:
        print(f"{args.city}: brak danych BeSTi@ — nie zapisuję.", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else cfg_path.parent / "docs" / "budget.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(budget, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.city}: zapisano {out} ({len(budget['totals'])} lat)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
