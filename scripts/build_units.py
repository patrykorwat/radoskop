#!/usr/bin/env python3
"""Import wszystkich jednostek podziału administracyjnego Polski do portalu.

Buduje kanoniczne drzewo hierarchii województwo -> powiat -> gmina oraz warstwy
geometrii (granice) na podstawie Państwowego Rejestru Granic (PRG, GUGiK).

Źródło geometrii: pochodne PRG w formacie GeoJSON (WGS84, kod TERYT w
properties.JPT_KOD_JE). Domyślnie repo public-domain waszkiewiczja, ale można
podać własny katalog z plikami PRG przez --from-dir.

Wyjścia (radoskop/docs/units/):
  wojewodztwa.geojson   16 obiektów, properties {teryt, name}
  powiaty.geojson       ~380 obiektów, properties {teryt, name, woj, grodzki}
  gminy.geojson         ~2477 obiektów, properties {teryt, name, powiat, woj, rodzaj}
  teryt_tree.json       pełne drzewo hierarchii (bez geometrii) jako manifest
  city_teryt_map.csv    propozycja dopasowania 166 miast Radoskopu do kodów TERYT

Re-run raz na rok (GUS aktualizuje TERYT co kwartał, powiaty zmieniają się rzadko).

Użycie:
  python build_units.py                 # pobiera GeoJSON z sieci, buduje wszystko
  python build_units.py --from-dir DIR  # używa lokalnych wojewodztwa/powiaty/gminy.json
  python build_units.py --no-download    # tylko lokalne, błąd jeśli brak plików
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import unicodedata
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
RADOSKOP_DIR = SCRIPTS_DIR.parent
DOCS_DIR = RADOSKOP_DIR / "docs"
UNITS_DIR = DOCS_DIR / "units"
CITIES_DIR = RADOSKOP_DIR / "cities"
ASSEMBLIES_DIR = RADOSKOP_DIR / "assemblies"
# Cache surowych plików PRG trzymamy POZA docs/, bo deploy_main_s3.py mirroruje
# całe docs/ rekursywnie na S3. Surowe 17 MB nie ma trafiać na portal.
SRC_CACHE = RADOSKOP_DIR / ".units_src"

RAW_BASE = (
    "https://raw.githubusercontent.com/waszkiewiczja/"
    "GeoJSON-Polska-Wojewodztwa-Powiaty-Gminy/main"
)
LAYERS = {"wojewodztwa": "wojewodztwa.json", "powiaty": "powiaty.json", "gminy": "gminy.json"}

# Oczekiwane liczby jednostek (2025). Asercje walidacyjne z tolerancją, bo GUS
# aktualizuje co kwartał i pojedyncze gminy bywają tworzone/łączone.
EXPECT = {"wojewodztwa": (16, 16), "powiaty": (376, 384), "gminy": (2470, 2485)}

RODZAJ = {
    "1": "gmina miejska",
    "2": "gmina wiejska",
    "3": "gmina miejsko-wiejska",
    "4": "miasto w gminie miejsko-wiejskiej",
    "5": "obszar wiejski w gminie miejsko-wiejskiej",
    "8": "dzielnica m. st. Warszawy",
    "9": "delegatura (Kraków/Łódź/Poznań/Wrocław)",
}
# Rodzaje gmin które są realnym ośrodkiem miejskim (kandydaci do match z miastem
# Radoskopu): miejska, miejsko-wiejska, miasto w gminie miejsko-wiejskiej.
CITY_RODZAJE = {"1", "3", "4"}

# Dodatkowe uproszczenie geometrii dla ciężkich warstw (mapshaper, Visvalingam).
# percentage = ułamek usuwalnych wierzchołków do ZACHOWANIA. Region (16 woj)
# zostaje w pełnej rozdzielczości. Local (gminy) 5.3->~1.6 MB raw, ~450 KB gzip.
SIMPLIFY_PCT = {"district": 0.40, "local": 0.20}


def find_mapshaper() -> list[str] | None:
    """Znajdź mapshaper: $MAPSHAPER_BIN, PATH, albo lokalny node_modules."""
    env = os.environ.get("MAPSHAPER_BIN")
    if env and Path(env).exists():
        return [env]
    found = shutil.which("mapshaper")
    if found:
        return [found]
    local = SCRIPTS_DIR / "node_modules" / ".bin" / "mapshaper"
    if local.exists():
        return [str(local)]
    return None


def simplify_layer(path: Path, pct: float, ms: list[str]) -> None:
    """Uprość geometrię w miejscu przez mapshaper (zachowuje atrybuty i kształty)."""
    tmp = path.with_suffix(".simpl.geojson")
    subprocess.run(
        ms + [str(path), "-simplify", "visvalingam", f"percentage={pct}",
              "keep-shapes", "-o", str(tmp), "force"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp.replace(path)
    print(f"  uproszczono {path.name} (pct={pct}, {path.stat().st_size//1024} KB)")


def norm(name: str) -> str:
    """Normalizacja do porównań: bez diakrytyków, lower, tylko alfanumeryk."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def fetch_layer(key: str, download: bool) -> Path:
    """Zwraca ścieżkę do lokalnego pliku warstwy, pobierając jeśli trzeba."""
    SRC_CACHE.mkdir(parents=True, exist_ok=True)
    dest = SRC_CACHE / LAYERS[key]
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    if not download:
        sys.exit(f"BŁĄD: brak {dest} a --no-download. Wrzuć plik albo pozwól pobrać.")
    url = f"{RAW_BASE}/{LAYERS[key]}"
    print(f"  pobieram {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "radoskop-build-units"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    dest.write_bytes(data)
    print(f"  zapisano {dest} ({len(data)//1024} KB)")
    return dest


def load_features(path: Path) -> list[dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    feats = d["features"] if isinstance(d, dict) else d
    if not feats:
        sys.exit(f"BŁĄD: brak features w {path}")
    return feats


def code_of(props: dict) -> str:
    for k in ("JPT_KOD_JE", "teryt", "kod", "TERYT"):
        if k in props and props[k]:
            return str(props[k]).strip()
    sys.exit(f"BŁĄD: nie znaleziono kodu TERYT w properties: {list(props)[:6]}")


def name_of(props: dict) -> str:
    for k in ("JPT_NAZWA_", "name", "nazwa", "NAZWA"):
        if k in props and props[k]:
            return str(props[k]).strip()
    return ""


def check_count(key: str, n: int) -> None:
    lo, hi = EXPECT[key]
    flag = "OK" if lo <= n <= hi else "UWAGA poza zakresem"
    print(f"  {key}: {n} jednostek (oczekiwano {lo}-{hi}) [{flag}]")


def write_geojson(path: Path, features: list[dict]) -> None:
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  zapisano {path.relative_to(RADOSKOP_DIR)} ({path.stat().st_size//1024} KB)")


def build(download: bool, src_dir: Path | None, write_teryt: bool = False,
          simplify: bool = True) -> None:
    UNITS_DIR.mkdir(parents=True, exist_ok=True)
    ms = find_mapshaper() if simplify else None
    if simplify and not ms:
        print("UWAGA: mapshaper nie znaleziony, warstwy zostaną w pełnym rozmiarze "
              "(npm install mapshaper albo MAPSHAPER_BIN=...).")

    def layer_path(key: str) -> Path:
        if src_dir:
            p = src_dir / LAYERS[key]
            if not p.exists():
                sys.exit(f"BŁĄD: brak {p}")
            return p
        return fetch_layer(key, download)

    print("1) Warstwy źródłowe")
    woj_feats = load_features(layer_path("wojewodztwa"))
    pow_feats = load_features(layer_path("powiaty"))
    gmi_feats = load_features(layer_path("gminy"))
    check_count("wojewodztwa", len(woj_feats))
    check_count("powiaty", len(pow_feats))
    check_count("gminy", len(gmi_feats))

    print("2) Drzewo TERYT + geometria (poziomy generyczne: region/district/local)")
    tree: dict[str, dict] = {}

    # Generyczne nazwy plików i poziomów = model wielokrajowy. Polska wnosi
    # region<-województwo, district<-powiat, local<-gmina. Inny kraj dołoży
    # swoje feature'y do tych samych plików (z innym `country`), bez zmian w
    # kodzie mapy. Każdy feature ma: code (kanoniczny ID kraju, tu TERYT),
    # name, country, level, parent (kod rodzica), plus pola specyficzne.
    region_out, district_out, local_out = [], [], []
    for f in woj_feats:
        code = code_of(f["properties"])[:2]
        nm = name_of(f["properties"])
        tree[code] = {"teryt": code, "name": nm, "level": "region", "powiaty": {}}
        region_out.append({"type": "Feature",
                           "properties": {"code": code, "name": nm, "country": "PL",
                                          "level": "region", "type": "województwo"},
                           "geometry": f["geometry"]})

    # Inne kraje na poziomie regionu. Niemcy: istniejący docs/lander-de.geojson
    # (na razie 1 land, MV). Kod = iso_3166_2 (np. DE-MV), nie koliduje z 2-cyfr.
    # kodami PL. Dowód że poziom region jest wielokrajowy. Kolejne kraje analogicznie.
    de_name_to_code: dict[str, str] = {}
    lander_de = DOCS_DIR / "lander-de.geojson"
    if lander_de.exists():
        dd = json.loads(lander_de.read_text(encoding="utf-8"))
        for f in dd.get("features", []):
            p = f["properties"]
            code = p.get("iso_3166_2") or p.get("id") or ""
            nm = p.get("name") or p.get("nazwa") or ""
            de_name_to_code[norm(nm)] = code
            region_out.append({"type": "Feature",
                               "properties": {"code": code, "name": nm, "country": "DE",
                                              "level": "region", "type": "kraj związkowy"},
                               "geometry": f["geometry"]})
        print(f"  dołożono {len(dd.get('features', []))} land(ów) DE do region.geojson")
    write_geojson(UNITS_DIR / "region.geojson", region_out)

    for f in pow_feats:
        code = code_of(f["properties"])[:4]
        woj = code[:2]
        nm = name_of(f["properties"])
        grodzki = code[2:4].isdigit() and int(code[2:4]) >= 61
        node = {"teryt": code, "name": nm, "level": "district", "woj": woj,
                "grodzki": grodzki, "gminy": {}}
        tree.setdefault(woj, {"teryt": woj, "name": "?", "level": "region",
                              "powiaty": {}})["powiaty"][code] = node
        district_out.append({"type": "Feature",
                             "properties": {"code": code, "name": nm, "country": "PL",
                                            "level": "district", "parent": woj,
                                            "grodzki": grodzki,
                                            "type": ("miasto na prawach powiatu"
                                                     if grodzki else "powiat")},
                             "geometry": f["geometry"]})
    write_geojson(UNITS_DIR / "district.geojson", district_out)

    for f in gmi_feats:
        code = code_of(f["properties"])[:7]
        powc = code[:4]
        woj = code[:2]
        rodzaj_digit = code[6] if len(code) >= 7 else ""
        nm = name_of(f["properties"])
        gnode = {"teryt": code, "name": nm, "level": "local", "powiat": powc,
                 "woj": woj, "rodzaj": RODZAJ.get(rodzaj_digit, rodzaj_digit)}
        parent_pow = tree.get(woj, {}).get("powiaty", {}).get(powc)
        if parent_pow is not None:
            parent_pow["gminy"][code] = gnode
        local_out.append({"type": "Feature",
                          "properties": {"code": code, "name": nm, "country": "PL",
                                         "level": "local", "parent": powc,
                                         "type": RODZAJ.get(rodzaj_digit, "gmina")},
                          "geometry": f["geometry"]})
    write_geojson(UNITS_DIR / "local.geojson", local_out)

    if ms:
        for layer, pct in SIMPLIFY_PCT.items():
            simplify_layer(UNITS_DIR / f"{layer}.geojson", pct, ms)

    (UNITS_DIR / "teryt_tree.json").write_text(
        json.dumps(tree, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  zapisano units/{{region,district,local}}.geojson + teryt_tree.json")

    print("3) Dopasowanie miast Radoskopu do gmin TERYT")
    # Indeks gmin-ośrodków miejskich po znormalizowanej nazwie.
    gmina_index: dict[str, list[dict]] = {}
    for woj in tree.values():
        for powc in woj["powiaty"].values():
            for g in powc["gminy"].values():
                if g["rodzaj"] in {RODZAJ[r] for r in CITY_RODZAJE}:
                    gmina_index.setdefault(norm(g["name"]), []).append(g)

    rows = []
    # Pokrycie: które jednostki Radoskop realnie obejmuje. Steruje podświetleniem
    # i klikalnością warstw na mapie (jednostka z danymi = klikalna, reszta = tło).
    cov_gmina: dict[str, dict] = {}
    cov_powiat: dict[str, list] = {}
    matched = ambiguous = unmatched = foreign = 0
    miejska = RODZAJ["1"]
    for cfg in sorted(CITIES_DIR.glob("*/config.json")):
        d = json.loads(cfg.read_text(encoding="utf-8"))
        slug = cfg.parent.name
        cname = d.get("city_name", "")
        if d.get("samorzad_type") and d["samorzad_type"] != "miasto":
            continue
        # Miasta zagraniczne nie mają polskiego TERYT.
        if (d.get("locale") or "pl").lower() != "pl":
            foreign += 1
            rows.append([slug, cname, "", "", "", "", "foreign"])
            continue
        cand = gmina_index.get(norm(cname), [])
        if len(cand) > 1:
            # Tie-break: rada miasta = gmina miejska. Preferuj ją nad miejsko-wiejską.
            pref = [g for g in cand if g["rodzaj"] == miejska]
            if len(pref) == 1:
                cand = pref
        if len(cand) == 1:
            g = cand[0]
            matched += 1
            rows.append([slug, cname, g["teryt"], g["name"], g["powiat"], g["woj"], "matched"])
            url = d.get("site_url", "")
            cov_gmina[g["teryt"]] = {"name": cname, "url": url}
            cov_powiat.setdefault(g["powiat"], []).append({"name": cname, "url": url})
            if write_teryt and d.get("teryt") != g["teryt"]:
                d["teryt"] = g["teryt"]
                cfg.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        elif len(cand) > 1:
            ambiguous += 1
            opts = "|".join(f"{g['teryt']}:{g['powiat']}" for g in cand)
            rows.append([slug, cname, "", "", "", "", f"ambiguous:{opts}"])
        else:
            unmatched += 1
            rows.append([slug, cname, "", "", "", "", "unmatched"])

    with (UNITS_DIR / "city_teryt_map.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["slug", "city_name", "teryt", "gmina_name", "powiat", "woj", "status"])
        w.writerows(rows)
    print(f"  miasta: {matched} matched, {ambiguous} ambiguous, "
          f"{unmatched} unmatched, {foreign} foreign (zagraniczne)")
    print(f"  zapisano units/city_teryt_map.csv (przejrzyj ambiguous/unmatched ręcznie)")

    # Pokrycie sejmików: województwa z aktywnym sejmikiem (assemblies/*/config.json,
    # samorzad_type=wojewodztwo). Mapowanie nazwa województwa -> kod TERYT z drzewa.
    woj_by_name = {norm(w["name"]): w["teryt"] for w in tree.values()}
    cov_woj: dict[str, dict] = {}
    for acfg in sorted(ASSEMBLIES_DIR.glob("*/config.json")):
        a = json.loads(acfg.read_text(encoding="utf-8"))
        if a.get("scrape_status") != "active":
            continue
        st = a.get("samorzad_type")
        if st == "wojewodztwo":          # PL sejmik -> kod TERYT województwa
            code = woj_by_name.get(norm(a.get("voivodeship_name", "")))
        elif st == "land":               # DE Landtag -> kod iso z lander-de.geojson
            code = de_name_to_code.get(norm(a.get("voivodeship_name", "")))
        else:
            continue
        if code:
            cov_woj[code] = {"name": a.get("voivodeship_name"), "url": a.get("site_url", "")}

    # Coverage kluczowane generycznym poziomem (region/district/local), nie
    # polskimi nazwami, żeby mapa była wielokrajowa.
    coverage = {"local": cov_gmina, "district": cov_powiat, "region": cov_woj}
    (UNITS_DIR / "coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False), encoding="utf-8")
    print(f"  zapisano units/coverage.json (local {len(cov_gmina)}, "
          f"district {len(cov_powiat)}, region z sejmikiem {len(cov_woj)})")

    # Manifest: mapa czyta go zamiast hardkodować poziomy. Kolejność = wchodzenie
    # wyżej (lokalny -> powiat -> region), więc przyciski: Miasta / Gminy /
    # Powiaty / Województwa. label per poziom (przy wielu krajach można zmienić
    # na ponadkrajowe Regiony itd. bez ruszania kodu). color: region = teal
    # (sejmik), niższe = indygo (miasto).
    manifest = {
        "default_label": "Miasta",
        "join_property": "code",
        # Etykiety uogólnione (wielokrajowo). "Regiony" zamiast "Województwa", bo
        # poziom region obejmuje też niemieckie kraje związkowe itd. "Powiaty" i
        # "Gminy" to w polszczyźnie ogólne odpowiedniki średniego i lokalnego
        # szczebla (Kreis->powiat, Gemeinde->gmina). Rodzimy typ jednostki jest
        # w properties.type i pokazuje się w dymku ("kraj związkowy" dla MV).
        "levels": [
            {"key": "local", "label": "Gminy", "file": "local.geojson", "color": "#4f46e5"},
            {"key": "district", "label": "Powiaty", "file": "district.geojson", "color": "#4f46e5"},
            {"key": "region", "label": "Regiony", "file": "region.geojson", "color": "#0d9488"},
        ],
        "countries": ["PL", "DE"],
    }
    (UNITS_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  zapisano units/manifest.json")

    print("4) Walidacja integralności prefiksów")
    orphan_pow = sum(1 for w in tree.values() for p in w["powiaty"].values()
                     if p["woj"] not in tree)
    orphan_gmi = sum(1 for w in tree.values() for p in w["powiaty"].values()
                     for g in p["gminy"].values() if g["powiat"] != p["teryt"])
    print(f"  powiaty bez województwa-rodzica: {orphan_pow}")
    print(f"  gminy źle przypięte do powiatu: {orphan_gmi}")
    grodzkie = sum(1 for w in tree.values() for p in w["powiaty"].values() if p["grodzki"])
    print(f"  miasta na prawach powiatu (grodzkie): {grodzkie}")
    print("GOTOWE.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-dir", type=Path, default=None,
                    help="katalog z lokalnymi plikami wojewodztwa/powiaty/gminy.json")
    ap.add_argument("--no-download", action="store_true",
                    help="nie pobieraj z sieci, użyj tylko cache/--from-dir")
    ap.add_argument("--write-teryt", action="store_true",
                    help="wpisz dopasowany kod teryt do cities/*/config.json (matched)")
    ap.add_argument("--no-simplify", action="store_true",
                    help="nie upraszczaj warstw mapshaperem (pełna rozdzielczość)")
    args = ap.parse_args()
    build(download=not args.no_download, src_dir=args.from_dir,
          write_teryt=args.write_teryt, simplify=not args.no_simplify)


if __name__ == "__main__":
    main()
