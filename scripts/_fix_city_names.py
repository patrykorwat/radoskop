#!/usr/bin/env python3
"""Naprawa odmienionych nazw miast w 112 configach dodanych dziś przez
add_city.py. Bug: fetch_composition_esesja brał title eSesja
("Rada Miejska w Bolesławcu") i wyciągał token po "w " (locative),
zamiast nominative.

Słownik (slug → (nominative, genitive)) hardcoded ręcznie dla 112 miast.
Dla każdego: update config.json (city_name, city_genitive, site_*,
rada_name, rada_name_genitive) + docstring scrape_{slug}.py.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CITIES = {
    "aleksandrow-kujawski": ("Aleksandrów Kujawski", "Aleksandrowa Kujawskiego"),
    "augustow": ("Augustów", "Augustowa"),
    "bartoszyce": ("Bartoszyce", "Bartoszyc"),
    "bedzin": ("Będzin", "Będzina"),
    "belchatow": ("Bełchatów", "Bełchatowa"),
    "bielawa": ("Bielawa", "Bielawy"),
    "bierun": ("Bieruń", "Bierunia"),
    "bochnia": ("Bochnia", "Bochni"),
    "boguszow-gorce": ("Boguszów-Gorce", "Boguszowa-Gorc"),
    "boleslawiec": ("Bolesławiec", "Bolesławca"),
    "braniewo": ("Braniewo", "Braniewa"),
    "bransk": ("Brańsk", "Brańska"),
    "brodnica": ("Brodnica", "Brodnicy"),
    "bukowno": ("Bukowno", "Bukowna"),
    "chodziez": ("Chodzież", "Chodzieży"),
    "chojnice": ("Chojnice", "Chojnic"),
    "chorzow": ("Chorzów", "Chorzowa"),
    "ciechocinek": ("Ciechocinek", "Ciechocinka"),
    "czarnkow": ("Czarnków", "Czarnkowa"),
    "czeladz": ("Czeladź", "Czeladzi"),
    "czluchow": ("Człuchów", "Człuchowa"),
    "debica": ("Dębica", "Dębicy"),
    "deblin": ("Dęblin", "Dęblina"),
    "dynow": ("Dynów", "Dynowa"),
    "dzierzoniow": ("Dzierżoniów", "Dzierżoniowa"),
    "elk": ("Ełk", "Ełku"),
    "gozdnica": ("Gozdnica", "Gozdnicy"),
    "grajewo": ("Grajewo", "Grajewa"),
    "ilawa": ("Iława", "Iławy"),
    "imielin": ("Imielin", "Imielina"),
    "inowroclaw": ("Inowrocław", "Inowrocławia"),
    "jawor": ("Jawor", "Jawora"),
    "jedlina-zdroj": ("Jedlina-Zdrój", "Jedliny-Zdroju"),
    "jozefow": ("Józefów", "Józefowa"),
    "ketrzyn": ("Kętrzyn", "Kętrzyna"),
    "kobylka": ("Kobyłka", "Kobyłki"),
    "kolo": ("Koło", "Koła"),
    "kolobrzeg": ("Kołobrzeg", "Kołobrzega"),
    "koscian": ("Kościan", "Kościana"),
    "kowary": ("Kowary", "Kowar"),
    "krasnik": ("Kraśnik", "Kraśnika"),
    "krasnystaw": ("Krasnystaw", "Krasnegostawu"),
    "kutno": ("Kutno", "Kutna"),
    "lancut": ("Łańcut", "Łańcuta"),
    "leba": ("Łeba", "Łeby"),
    "ledziny": ("Lędziny", "Lędzin"),
    "legionowo": ("Legionowo", "Legionowa"),
    "leszno": ("Leszno", "Leszna"),
    "lezajsk": ("Leżajsk", "Leżajska"),
    "lipno": ("Lipno", "Lipna"),
    "lomza": ("Łomża", "Łomży"),
    "lowicz": ("Łowicz", "Łowicza"),
    "lubaczow": ("Lubaczów", "Lubaczowa"),
    "luban": ("Lubań", "Lubania"),
    "lubartow": ("Lubartów", "Lubartowa"),
    "lubawa": ("Lubawa", "Lubawy"),
    "lubon": ("Luboń", "Lubonia"),
    "lukow": ("Łuków", "Łukowa"),
    "marki": ("Marki", "Marek"),
    "mikolow": ("Mikołów", "Mikołowa"),
    "milanowek": ("Milanówek", "Milanówka"),
    "mragowo": ("Mrągowo", "Mrągowa"),
    "myszkow": ("Myszków", "Myszkowa"),
    "nieszawa": ("Nieszawa", "Nieszawy"),
    "olesnica": ("Oleśnica", "Oleśnicy"),
    "orzesze": ("Orzesze", "Orzesza"),
    "ostroda": ("Ostróda", "Ostródy"),
    "ostroleka": ("Ostrołęka", "Ostrołęki"),
    "oswiecim": ("Oświęcim", "Oświęcimia"),
    "otwock": ("Otwock", "Otwocka"),
    "piastow": ("Piastów", "Piastowa"),
    "piechowice": ("Piechowice", "Piechowic"),
    "pionki": ("Pionki", "Pionek"),
    "polanica-zdroj": ("Polanica-Zdrój", "Polanicy-Zdroju"),
    "poreba": ("Poręba", "Poręby"),
    "przemysl": ("Przemyśl", "Przemyśla"),
    "pruszkow": ("Pruszków", "Pruszkowa"),
    "puck": ("Puck", "Pucka"),
    "puszczykowo": ("Puszczykowo", "Puszczykowa"),
    "pyskowice": ("Pyskowice", "Pyskowic"),
    "raciborz": ("Racibórz", "Raciborza"),
    "radlin": ("Radlin", "Radlina"),
    "radymno": ("Radymno", "Radymna"),
    "reda": ("Reda", "Redy"),
    "sanok": ("Sanok", "Sanoka"),
    "sieradz": ("Sieradz", "Sieradza"),
    "sierpc": ("Sierpc", "Sierpca"),
    "slawkow": ("Sławków", "Sławkowa"),
    "slawno": ("Sławno", "Sławna"),
    "sulejowek": ("Sulejówek", "Sulejówka"),
    "sulmierzyce": ("Sulmierzyce", "Sulmierzyc"),
    "suwalki": ("Suwałki", "Suwałk"),
    "swietochlowice": ("Świętochłowice", "Świętochłowic"),
    "szczyrk": ("Szczyrk", "Szczyrku"),
    "tarnobrzeg": ("Tarnobrzeg", "Tarnobrzega"),
    "ustka": ("Ustka", "Ustki"),
    "ustron": ("Ustroń", "Ustronia"),
    "wagrowiec": ("Wągrowiec", "Wągrowca"),
    "walcz": ("Wałcz", "Wałcza"),
    "wegrow": ("Węgrów", "Węgrowa"),
    "wejherowo": ("Wejherowo", "Wejherowa"),
    "wisla": ("Wisła", "Wisły"),
    "wojcieszow": ("Wojcieszów", "Wojcieszowa"),
    "zagan": ("Żagań", "Żagania"),
    "zakopane": ("Zakopane", "Zakopanego"),
    "zambrow": ("Zambrów", "Zambrowa"),
    "zamosc": ("Zamość", "Zamościa"),
    "zary": ("Żary", "Żar"),
    "zgierz": ("Zgierz", "Zgierza"),
    "zgorzelec": ("Zgorzelec", "Zgorzelca"),
    "zlotoryja": ("Złotoryja", "Złotoryi"),
    "zlotow": ("Złotów", "Złotowa"),
    "zyrardow": ("Żyrardów", "Żyrardowa"),
}

CITIES_DIR = Path(__file__).resolve().parent.parent / "cities"


def fix_config(slug: str, name: str, genitive: str) -> bool:
    cfg_path = CITIES_DIR / slug / "config.json"
    if not cfg_path.is_file():
        return False
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["city_name"] = name
    cfg["city_genitive"] = genitive
    cfg["site_title"] = f"Radoskop {name} — Jak głosują radni?"
    cfg["site_description"] = (
        f"Radoskop — otwarte narzędzie monitoringu Rady Miasta {genitive}. "
        "Sprawdź frekwencję, głosowania imienne i aktywność radnych."
    )
    cfg["site_description_short"] = (
        f"Otwarte narzędzie monitoringu Rada Miasta {genitive}."
    )
    cfg["rada_name"] = f"Rada Miasta {genitive}"
    cfg["rada_name_genitive"] = f"Rady Miasta {genitive}"
    cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def fix_scraper_docstring(slug: str, name: str) -> bool:
    """Aktualizuj nazwę w docstring Radoskop {Old} → Radoskop {Name}.
    Plus 'Radoskop {old} ({url})' w run_cli prog_name."""
    script = CITIES_DIR / slug / "scripts" / f"scrape_{slug.replace('-', '_')}.py"
    if not script.is_file():
        return False
    text = script.read_text(encoding="utf-8")
    # Pattern: "Radoskop XXX — eSesja scraper" lub "Radoskop XXX (https..."
    # Replace any "Radoskop {word(s)}" przed em-dash lub nawiasem
    text = re.sub(
        r"Radoskop\s+[A-ZŁŚĄĘĆŃÓŻŹ][\w\s\-]+?(?=(\s*—|\s*\())",
        f"Radoskop {name}",
        text,
    )
    script.write_text(text, encoding="utf-8")
    return True


def main():
    import sys
    fixed = 0
    not_found = []
    for slug, (name, gen) in CITIES.items():
        if fix_config(slug, name, gen):
            fix_scraper_docstring(slug, name)
            fixed += 1
        else:
            not_found.append(slug)
    print(f"Fixed: {fixed}/{len(CITIES)}")
    if not_found:
        print(f"Brak configu: {not_found}")


if __name__ == "__main__":
    main()
