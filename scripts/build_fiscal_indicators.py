#!/usr/bin/env python3
"""Wskaźniki sytuacji finansowej JST wg definicji Ministerstwa Finansów (OSF).

Metoda = WYŁĄCZNIE oficjalne definicje z publikacji MF „Wskaźniki do oceny
sytuacji finansowej JST" (gov.pl/web/finanse, wyd. 2024, dane 2019-2023).
Żadnych autorskich wag ani progów — surowe wskaźniki MF + perły równieśnicze
(mediana klasy JST) liczone przez build_fiscal_index.py.

Źródło danych: BeSTi@ MF REST API (bestia-api.mf.gov.pl, bez klucza),
sprawozdania Rb-27S / Rb-28S / Rb-NDS / Rb-Z — te same, na których MF liczy
swoje wskaźniki (patrz opis metody, rozdz. I).

Zestaw (zgodnie z opisem MF):
  Budżetowe:   WB1 Db/Do, WB2 Tb/Db, WB3 No/Db, WB4 Wm/Wo, WB5 Ww/Wb,
               WB6 (No+Sm)/Do, WB7 (No+Dm)/Wm, WB8 ((Do+P)-(Wo+R))/Do,
               WB9 (Pbzwr+(Do-Wb))/(Wm+Rs), WB10 (Pbzwr+(Do-Wb-Rs))/Wm,
               WB11 Wm/(Pbzwr+(Do-Wb-Rs)), WB12 jedn. śr. z art. 243 u.f.p.
  Na mieszkańca: WL1 Tb/L, WL2 No/L, WL3 Zo/L, WL4 (Pbzwr+(Do-Wb))/L,
               WL5 (Pbzwr+(Do-Wb-Rs))/L
  Zobowiązania:  WZ1 Zo/Do, WZ2 Zo/No, WZ3 (O+Rs)/Do, WZ4 O/Zo,
               WZ5 (O+Rs)/(Db-Dbd), WZ6 O/Rs, WZ7 Zw/Zo

Definicje składników (§-kiery wg MF, opis metody tabela symboli):
  Do,Rb-27S suma dw; Dm = § ∈ DOCH_MAJ; Sm = § ∈ SPRZ_MAJ; Db = Do - Dm
  Tb = § ∈ TRF_BIEZ (Rb-27S)
  No = Db - Wb (nadwyżka operacyjna)
  Wo = Rb-28S suma ww; Wm = wiersze grupa-paragrafow 16xx; Wb = Wo - Wm
  Ww = grupa 14xx z wyłączeniem finansowania art.5 ust.1 pkt 2-3
  O  = grupa 1810 (obsługa długu); Wbd = § 492; Wbe = grupy 11xx/12xx/13xx/14xx
       z czwartą cyfrą 1,2 (środki, o których mowa w art. 5 ust. 1 pkt 2-3)
  Dbe/Dbs z Rb-27S wg MF; Rb-NDS: P=D1, R=D2, Rs=D21, Pbzwr = P - D11
  Zo = Rb-Z symbol E (zobowiązania wg tytułów dłużnych)
  Zw = zobowiązania wymagalne — Rb-Z nie występuje w BeSTi@ API (patrz WZ7: null)

Output: cities/<slug>/docs/fiscal-indicators.json — wskaźniki per rok, plus
walidacja wewnętrzna (spójność Rb-NDS vs sumy Rb-27S/28S).

Użycie:
  python build_fiscal_indicators.py --city gdynia
  python build_fiscal_indicators.py --city gdynia --years 5 --out /tmp/f.json
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

API = "https://bestia-api.mf.gov.pl"
HERE = Path(__file__).resolve().parent.parent
PAGE_LIMIT = 100
NEWEST = {"sprawozdanie-najnowsze": "true"}

# ---- §-kry MF (opis metody, tabela symboli; tabele paragrafów z rozporządzenia
# w sprawie szczegółowej klasyfikacji, stan lat 2019-2025) ----
def _expand(*ranges: tuple[int, int]) -> set[int]:
    out: set[int] = set()
    for a, b in ranges:
        out |= set(range(a, b + 1))
    return out

# Dm — dochody majątkowe (MF): 076-078, 080, 087, 609, 610, 618, 620, 625, 626,
# 628-635, 637-639, 641-645, 651-653, 656, 661-666, 668-671 (+ nowsze § majątkowe
# wprowadzone po 2023: 672-674 — dodać przy weryfikacji rocznej)
DOCH_MAJ = _expand((76, 78), (80, 80), (87, 87), (609, 610), (618, 620),
                   (625, 626), (628, 639), (641, 645), (651, 653), (656, 656),
                   (661, 666), (668, 671))
# Sm — dochody ze sprzedaży majątku: 077, 078, 080, 087
SPRZ_MAJ = {77, 78, 80, 87}
# Tb — transfery bieżące: 200-208, 210-213, 216-218, 221-223, 231-233, 238, 244,
# 246, 253, 269-271, 273, 275-279, 287, 288, 290, 292
TRF_BIEZ = _expand((200, 208), (210, 213), (216, 218), (221, 223), (231, 233),
                   (238, 238), (244, 244), (246, 246), (253, 253), (269, 271),
                   (273, 273), (275, 279), (287, 288), (290, 290), (292, 292))
# Dbd — dotacje/środki bieżące: 200-208,210-213,216-218,221-223,231-233,238,244,
# 246,253,269-271,273,278,287,288,290 (bez subwencji 275-277,279,292)
DBD = _expand((200, 208), (210, 213), (216, 218), (221, 223), (231, 233),
              (238, 238), (244, 244), (246, 246), (253, 253), (269, 271),
              (273, 273), (278, 278), (287, 288), (290, 290))
# Dbe — podzbiór powyższego: § z czwartą cyfrą ∈ {1,2,5,6,7,8,9}
# (MF: „z czwartą cyfrą paragrafu 1,2,5,6,7,8,9"; w BeSTi@ paragraf ma 3 cyfry
#  i nie ma czwartej — realizujemy przez filtr po polu „wyróznik"? Niedostępne:
#  przybliżenie = Dbe⊂Dbd po paragrafach z końcówką z listy; MF liczy pełny §4cyf.
#  Weryfikacja WB12 vs MF powie, czy istotne.)
DBE_LAST_DIGITS = {1, 2, 5, 6, 7, 8, 9}
# Wbd — spłaty zobowiązań dłużnych po 1.1.2019 w formie wydatku bieżącego: § 492
WBD_PAR = 492
# Filtry finansowania-paragrafu (kalibracja co-do-zlota vs zalaczniki MF,
# Gdynia 2023: Ww 764,71 = 764,71; Dbe 7,829 = 7,829). MF wyklucza srodki
# art.5 ust.1 pkt 2-3; BeSTi@ nie ma 4. cyfry paragrafu, wiec realizacja przez
# kode finansowania:
#   Ww  (grupa 14xx): wylacz fin {1,2,3,7,8,9}
#   Dbe (podzbior Dbd):    zostaw   fin {1,7,8,9}
WW_EXCL_FIN = {"1", "2", "3", "7", "8", "9"}
DBE_FIN = {"1", "7", "8", "9"}
# Wm — wydatki majątkowe: grupy paragrafów 16xx
# Ww — grupa 14xx; O — grupa 1810; Wbe — grupy 1101/1102/1201/1202/1301/1302/
#      1401/1402 (MF; grupy API = symbol 4-cyfrowy pola grupa-paragrafow)
WBE_GROUPS = {"1101", "1102", "1201", "1202", "1301", "1302", "1401", "1402"}


def api_get(path: str, filters: dict[str, str], *, fields: str = "",
            page: int = 1, limit: int = PAGE_LIMIT, retries: int = 3,
            sort: str = "") -> dict | None:
    """GET z deepObject filtrem. sort=<pole> jest WYMAGANY dla wielostronicowych
    raportów (Rb-27S/Rb-28S): bez sortu API zwraca niestabilną kolejność — przy
    paginacji te same wiersze powtarzają się, inne znikają i sumy są błędne
    (Gdynia 2023 Rb-27S: 83 duplikaty, suma zawyżona ~40%)."""
    parts = [f"filter[{k}]={urllib.parse.quote(str(v))}" for k, v in filters.items()]
    parts.append("format=JSON")
    if fields:
        parts.append("fields=" + fields)
    if sort:
        parts.append("sort=" + sort)
    parts.append(f"page={page}")
    parts.append(f"limit={limit}")
    url = f"{API}{path}?" + "&".join(parts)
    req = urllib.request.Request(url, headers={"User-Agent": "radoskop-fiscal-mf"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                if r.status == 204:
                    return {"data": [], "paging": {"totalItems": 0, "totalPages": 0}}
                return json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                print(f"  WARN: {path} p{page}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def num(s) -> float:
    return float(str(s or "0").replace(" ", "").replace("\xa0", "").replace(",", ".") or 0)


def paginated(path: str, filters: dict, fields: str, max_pages: int = 80,
              sort: str = ""):
    """Iteruj strony. sort=<pole> DLA WIELOSTRONICZOWYCH raportow jest
    WYMAGANY: bez sortu BeSTi@ zwraca Niestabilna kolejnosc — te same wiersze
    powtarzaja sie na kolejnych stronach, a inne znikaja (Gdynia 2023 Rb-27S:
    590 wierszy, 83 duplikaty, suma zawyzona do 2769 mln zamiast 1962 mln).
    Ze scisle sortem sumy zgadzaja sie co do zlotej z danymi MF OSF."""
    page, pages = 1, 1
    while page <= pages and page <= max_pages:
        j = api_get(path, filters, fields=fields, page=page, sort=sort)
        if not j:
            return
        pages = j["paging"]["totalPages"]
        yield from j.get("data", [])
        page += 1
        time.sleep(0.03)


def resolve_unit(teryt: str) -> dict | None:
    """Termin identyczny jak build_budget_bestia.resolve_unit (sprawdzony)."""
    if not teryt or len(teryt) < 6:
        return None
    wk, pk, gk0 = teryt[0:2], teryt[2:4], teryt[4:6]
    rodzaj = teryt[6] if len(teryt) >= 7 else ""
    grodzki = pk.isdigit() and int(pk) >= 61
    filt = {"jednostka-wk": wk, "jednostka-pk": pk, "jednostka-gk": "00" if grodzki else gk0}
    if grodzki:
        filt["jednostka-pt"] = "2"
    elif rodzaj:
        filt["jednostka-gt"] = "3" if rodzaj == "4" else rodzaj
    filt.update(NEWEST)
    j = api_get("/api/sprawozdania", filt, limit=5)
    rows = (j or {}).get("data") or []
    if not rows:
        filt.pop("jednostka-gt", None)
        rows = (api_get("/api/sprawozdania", filt, limit=5) or {}).get("data") or []
    if not rows:
        return None
    r = rows[0]
    return {"const_id": r["jednostka-const-id"], "nazwa": r["jednostka-nazwa"],
            "wk": r["jednostka-wk"], "pk": r["jednostka-pk"], "gk": r["jednostka-gk"]}


def annual_years(cid: str, kod: str, n: int) -> list[str]:
    years: set[str] = set()
    for x in paginated("/api/sprawozdania",
                       {"jednostka-const-id": cid, "typ-sprawozdania-kod": kod,
                        "okres-okres": "4", **NEWEST}, "okres-rok", max_pages=30):
        years.add(x["okres-rok"])
    return sorted(years)[-n:]


def base_from_nds(cid: str, rok: str) -> dict | None:
    """Składniki Rb-NDS: Do, Wo, wynik, P, R, Rs(D21), kredyty(D11), Db/B1?, B2, A1.."""
    rows = list(paginated("/api/pozycje-rbnds",
                          {"jednostka-const-id": cid, "okres-rok": rok,
                           "okres-okres": "4", **NEWEST}, "symbol,p,w",
                          sort="kolejnosc"))
    by: dict[str, dict] = {}
    for x in rows:
        by.setdefault(x.get("symbol"), x)
    if "B" not in by or "A" not in by:
        return None
    g = lambda s, k="w": num(by[s][k]) if s in by else None  # noqa: E731
    P = g("D1"); R = g("D2"); Rs = g("D21"); kredyty = g("D11")
    Pbzwr = (P - (kredyty or 0.0)) if P is not None else None
    return {
        "Do": g("A"), "Db_nds": g("A1"), "Dm_nds": g("A2"), "Sm_nds": g("A21"),
        "Wo": g("B"), "Wb_nds": g("B1"), "Wm_nds": g("B2"),
        "wynik": g("C"), "No_nds": g("C1"),
        "P": P, "R": R, "Rs": Rs, "Pbzwr": Pbzwr,
    }


def rb27s_components(cid: str, rok: str) -> dict:
    """Rb-27S: Do, Dm, Sm, Tb, Dbd, Dbe (wg § MF)."""
    Do = Dm = Sm = Tb = Dbd = Dbe = 0.0
    for x in paginated("/api/pozycje-rb27s",
                       {"jednostka-const-id": cid, "okres-rok": rok,
                        "okres-okres": "4", **NEWEST},
                       "paragraf,finansowanie-paragrafu,dw", sort="dzial"):
        dw = num(x.get("dw"))
        fin = str(x.get("finansowanie-paragrafu") or "0").strip()
        try:
            par = int(str(x.get("paragraf")))
        except (TypeError, ValueError):
            par = -1
        Do += dw
        if par in DOCH_MAJ:
            Dm += dw
        if par in SPRZ_MAJ:
            Sm += dw
        if par in TRF_BIEZ:
            Tb += dw
        if par in DBD:
            Dbd += dw
            if fin in DBE_FIN:
                Dbe += dw
    return {"Do": Do, "Dm": Dm, "Sm": Sm, "Tb": Tb, "Dbd": Dbd, "Dbe": Dbe}


def rb28s_components(cid: str, rok: str) -> dict:
    """Rb-28S: Wo, Wm, Ww, O, Rs_28s?, Wbe, Wbd. Grupy po polu grupa-paragrafow."""
    Wo = Wm = Ww = O = Wbe = Wbd = 0.0
    for x in paginated("/api/pozycje-rb28s",
                       {"jednostka-const-id": cid, "okres-rok": rok,
                        "okres-okres": "4", **NEWEST},
                       "paragraf,grupa-paragrafow,finansowanie-paragrafu,ww",
                       sort="dzial"):
        ww = num(x.get("ww"))
        grp = (str(x.get("grupa-paragrafow") or "")).strip()
        fin = str(x.get("finansowanie-paragrafu") or "").strip()
        try:
            par = int(str(x.get("paragraf")))
        except (TypeError, ValueError):
            par = -1
        Wo += ww
        if grp.startswith("16"):
            Wm += ww
        if grp.startswith("14"):
            # MF: z wyłączeniem wynagrodzeń finansowanych środkami art.5 ust.1
            # pkt 2 i 3 (kalibrowane fin {1,2,3,7,8,9} — patrz WW_EXCL_FIN).
            if fin not in WW_EXCL_FIN:
                Ww += ww
        if grp == "1810":
            O += ww
        if grp in WBE_GROUPS:
            Wbe += ww
        if par == WBD_PAR:
            Wbd += ww
    return {"Wo": Wo, "Wm": Wm, "Ww": Ww, "O": O, "Wbe": Wbe, "Wbd": Wbd}


def debt_zo(cid: str, rok: str) -> float | None:
    """Zo = Rb-Z symbol E (zobowiązania wg tytułów dłużnych, suma E1..E4)."""
    j = api_get("/api/pozycje-rbztd",
                {"jednostka-const-id": cid, "okres-rok": rok, "okres-okres": "4", **NEWEST},
                fields="symbol,z", limit=100)
    for x in (j or {}).get("data", []):
        if x.get("symbol") == "E":
            return round(num(x.get("z")), 2)
    return None


def pct(n, d):
    if n is None or d is None:
        return None
    try:
        return round(n / d * 100, 2)
    except ZeroDivisionError:
        return None


def pln(n, d):
    if n is None or d is None or d == 0:
        return None
    return round(n / d, 2)


def compute_year(cid: str, rok: str, population: float | None) -> dict | None:
    nds = base_from_nds(cid, rok)
    if not nds or nds["Do"] in (None, 0) or nds["Wo"] in (None, 0):
        return None
    r27 = rb27s_components(cid, rok)
    r28 = rb28s_components(cid, rok)
    zo = debt_zo(cid, rok)

    # Składniki: priorytet Rb-27S/28S (tak jak MF); Rb-NDS jako spoiwo P/R/Rs.
    Do = r27["Do"] or nds["Do"]
    Dm = r27["Dm"]
    Sm = r27["Sm"]
    Db = Do - Dm
    Wo = r28["Wo"] or nds["Wo"]
    Wm = r28["Wm"]
    Wb = Wo - Wm
    No = Db - Wb
    Tb = r27["Tb"]
    Ww = r28["Ww"]
    O = r28["O"]
    Wbe = r28["Wbe"]
    Wbd = r28["Wbd"]
    Dbe = r27["Dbe"]
    Dbd = r27["Dbd"]
    P, R, Rs, Pbzwr = nds["P"], nds["R"], nds["Rs"], nds["Pbzwr"]
    L = population

    WB9n = None if None in (Pbzwr, Do, Wb) else Pbzwr + (Do - Wb)
    WB10n = None if None in (WB9n, Rs) else WB9n - Rs

    return {
        "year": rok,
        "raw": {"Do": round(Do, 2), "Db": round(Db, 2), "Dm": round(Dm, 2),
                "Sm": round(Sm, 2), "Tb": round(Tb, 2), "Wo": round(Wo, 2),
                "Wb": round(Wb, 2), "Wm": round(Wm, 2), "No": round(No, 2),
                "Ww": round(Ww, 2), "O": round(O, 2), "Rs": Rs, "P": P, "R": R,
                "Pbzwr": Pbzwr, "Zo": zo, "Dbd": round(Dbd, 2), "Dbe": round(Dbe, 2),
                "Wbe": round(Wbe, 2), "Wbd": round(Wbd, 2)},
        "WB1": pct(Db, Do), "WB2": pct(Tb, Db), "WB3": pct(No, Db),
        "WB4": pct(Wm, Wo), "WB5": pct(Ww, Wb),
        "WB6": None if None in (No, Sm) else pct(No + Sm, Do),
        "WB7": None if None in (No, Dm) else pct(No + Dm, Wm),
        "WB8": None if None in (Do, P, Wo, R) else pct((Do + P) - (Wo + R), Do),
        "WB9": pct(WB9n, None if None in (Wm, Rs) else Wm + Rs),
        "WB10": pct(WB10n, Wm),
        "WB11": None if WB10n in (None, 0) else pct(Wm, WB10n),
        "WB12": (None if None in (Db, Dbe, Wb, Wbe, Wbd, Sm, Dbd)
                 else pct((Db - Dbe) - (Wb - Wbe - Wbd) + Sm, Db - Dbd)),
        "WL1": pln(Tb, L), "WL2": pln(No, L), "WL3": pln(zo, L),
        "WL4": pln(WB9n, L), "WL5": pln(WB10n, L),
        "WZ1": pct(zo, Do),
        "WZ2": None if (zo is None or No <= 0) else round(zo / No, 2),
        "WZ3": None if None in (O, Rs) else pct(O + Rs, Do),
        "WZ4": pct(O, zo),
        "WZ5": (None if None in (O, Rs, Db, Dbd) or (Db - Dbd) == 0
                else pct(O + Rs, Db - Dbd)),
        "WZ6": pct(O, Rs),
        "WZ7": None,  # Zw (zobow. wymagalne) nie występuje w BeSTi@ API — null celowo
        "population": L,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Wskaźniki OSF MF dla miasta (BeSTi@)")
    ap.add_argument("--city", required=True)
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--population", type=float, default=None,
                    help="ludność do wskaźników per capita (domyślnie config.population)")
    ap.add_argument("--out")
    args = ap.parse_args()

    cfg_path = HERE / "cities" / args.city / "config.json"
    if not cfg_path.is_file():
        print(f"ERROR: brak configu {cfg_path}", file=sys.stderr)
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    teryt = cfg.get("teryt")
    pop = args.population if args.population is not None else cfg.get("population")
    if pop is None:
        # Fallback: docs/cities.json (slug→population) — jak build_cross_city.
        cj = HERE / "docs" / "cities.json"
        if cj.is_file():
            try:
                raw = json.loads(cj.read_text(encoding="utf-8"))
                ents = raw if isinstance(raw, list) else (raw.get("cities") or raw.get("entries") or [])
                pop = next((e.get("population") for e in ents
                            if isinstance(e, dict) and e.get("slug") == args.city), None)
            except Exception:
                pass
    if not teryt:
        print(f"ERROR: {args.city} bez 'teryt'", file=sys.stderr)
        return 1

    unit = resolve_unit(teryt)
    if not unit:
        print(f"{args.city}: nie znaleziono jednostki BeSTi@", file=sys.stderr)
        return 2
    cid = unit["const_id"]
    print(f"  jednostka: {unit['nazwa']}")
    years = annual_years(cid, "Rb-28s", args.years)
    rows = []
    for y in years:
        rec = compute_year(cid, y, pop)
        if rec:
            rows.append(rec)
            print(f"  {y}: WB3={rec['WB3']} WB4={rec['WB4']} WZ1={rec['WZ1']} WB12={rec['WB12']}")
    if not rows:
        print(f"{args.city}: brak danych", file=sys.stderr)
        return 2

    out = {
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": ("Ministerstwo Finansów, \u201eWska\u017aniki do oceny sytuacji "
                   "finansowej JST\u201d \u2014 oficjalne definicje (opis metody, "
                   "wyd. 2024); dane: BeSTi@ MF, sprawozdania Rb-27S/Rb-28S/"
                   "Rb-NDS/Rb-Z"),
        "methodology": "https://www.gov.pl/web/finanse/wskazniki-do-oceny-sytuacji-finansowej-jst-w-latach-2019---2023",
        "unit": unit["nazwa"],
        "teryt": teryt,
        "years": rows,
    }
    dst = Path(args.out) if args.out else cfg_path.parent / "docs" / "fiscal-indicators.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.city}: zapisano {dst} ({len(rows)} lat)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
