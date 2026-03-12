#!/usr/bin/env python3
"""
Radoskop -- orchestrator scrapowania wszystkich miast.

Uruchamia istniejące skrypty per miasto (scrape_*.py) jako subprocesy,
zbiera wyniki i raportuje status.

Użycie:
  python scrape_all.py                          # wszystkie miasta
  python scrape_all.py gdansk krakow            # wybrane miasta
  python scrape_all.py --dry-run                # przekaż --dry-run do scraperów
  python scrape_all.py --max-sessions 3         # ogranicz sesje
  python scrape_all.py --interpelacje           # tylko interpelacje (Gdańsk)
  python scrape_all.py --skip-venv              # nie twórz venv / nie instaluj deps
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Konfiguracja miast
# ---------------------------------------------------------------------------

CITIES = {
    "gdansk": {
        "name": "Gdańsk",
        "scraper": "scrape_uchwaly.py",  # głosowania via eSesja pipeline
        "interpelacje": "scrape_interpelacje.py",
        "deps": ["requests", "beautifulsoup4"],
        "extra_args": [],
    },
    "krakow": {
        "name": "Kraków",
        "scraper": "scrape_krakow.py",
        "deps": ["requests", "beautifulsoup4", "lxml"],
        "extra_args": [],
    },
    "wroclaw": {
        "name": "Wrocław",
        "scraper": "scrape_wroclaw.py",
        "deps": ["requests", "beautifulsoup4", "lxml", "pymupdf"],
        "extra_args": [],
    },
    "poznan": {
        "name": "Poznań",
        "scraper": "scrape_poznan.py",
        "deps": ["requests", "pymupdf"],
        "extra_args": [],
    },
    "gdynia": {
        "name": "Gdynia",
        "scraper": "scrape_gdynia.py",
        "deps": ["requests", "pymupdf", "playwright"],
        "needs_playwright": True,
        "extra_args": lambda proj: ["--pdf-dir", str(proj / "scripts" / "pdfs")],
    },
    "sopot": {
        "name": "Sopot",
        "scraper": "scrape_sopot.py",
        "deps": ["requests", "pymupdf", "playwright"],
        "needs_playwright": True,
        "extra_args": lambda proj: ["--cache-dir", str(proj / ".cache")],
    },
    "warszawa": {
        "name": "Warszawa",
        "scraper": "scrape_warszawa.py",
        "deps": ["requests", "beautifulsoup4", "lxml", "playwright", "python-docx", "pymupdf"],
        "needs_playwright": True,
        "extra_args": ["--kadencja", "all"],
    },
}

ALL_CITY_IDS = list(CITIES.keys())


def find_base_dir() -> Path:
    """Znajdź katalog gdansk-network/ (rodzic radoskop/)."""
    script_path = Path(__file__).resolve()
    # scrape_all.py jest w radoskop/scripts/ -> parent.parent = radoskop -> parent = gdansk-network
    candidate = script_path.parent.parent.parent
    if (candidate / "radoskop-gdansk").exists():
        return candidate
    return Path.cwd()


def setup_venv(project_dir: Path, deps: list[str], needs_playwright: bool = False):
    """Utwórz venv i zainstaluj zależności jeśli potrzeba."""
    venv_dir = project_dir / ".venv"
    python = venv_dir / "bin" / "python"

    if not venv_dir.exists():
        print(f"    Tworzę venv w {venv_dir}...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    # Instaluj deps
    print(f"    Instaluję: {', '.join(deps)}")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet"] + deps,
        check=True,
    )

    if needs_playwright:
        print(f"    Instaluję Chromium (Playwright)...")
        subprocess.run(
            [str(python), "-m", "playwright", "install", "chromium"],
            capture_output=True,
        )

    return python


def run_scraper(
    city_id: str,
    config: dict,
    base_dir: Path,
    args: argparse.Namespace,
) -> bool:
    """Uruchom scraper dla jednego miasta."""
    project_dir = base_dir / f"radoskop-{city_id}"
    scripts_dir = project_dir / "scripts"
    docs_dir = project_dir / "docs"

    scraper_file = scripts_dir / config["scraper"]
    if not scraper_file.exists():
        print(f"    BRAK: {scraper_file}")
        return False

    # Python executable
    if args.skip_venv:
        python = sys.executable
    else:
        python = str(setup_venv(
            project_dir,
            config.get("deps", []),
            config.get("needs_playwright", False),
        ))

    docs_dir.mkdir(parents=True, exist_ok=True)

    # Zbuduj komendę
    cmd = [
        str(python),
        str(scraper_file),
        "--output", str(docs_dir / "data.json"),
        "--profiles", str(docs_dir / "profiles.json"),
    ]

    # Dodatkowe argumenty per miasto
    extra = config.get("extra_args", [])
    if callable(extra):
        extra = extra(project_dir)
    cmd.extend(extra)

    # Flagi globalne
    if args.dry_run:
        cmd.append("--dry-run")
    if args.max_sessions:
        cmd.extend(["--max-sessions", str(args.max_sessions)])

    print(f"    CMD: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode == 0


def run_interpelacje(
    city_id: str,
    config: dict,
    base_dir: Path,
    args: argparse.Namespace,
) -> bool:
    """Uruchom scraper interpelacji (jeśli istnieje)."""
    interp_script_name = config.get("interpelacje")
    if not interp_script_name:
        print(f"    {config['name']}: brak scrapera interpelacji")
        return True

    project_dir = base_dir / f"radoskop-{city_id}"
    scripts_dir = project_dir / "scripts"
    docs_dir = project_dir / "docs"
    interp_file = scripts_dir / interp_script_name

    if not interp_file.exists():
        print(f"    BRAK: {interp_file}")
        return False

    python = sys.executable if args.skip_venv else str(
        (project_dir / ".venv" / "bin" / "python")
    )
    if not Path(python).exists():
        python = sys.executable

    cmd = [
        str(python),
        str(interp_file),
        "--output", str(docs_dir / "interpelacje.json"),
    ]

    print(f"    CMD: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(
        description="Radoskop -- orchestrator scrapowania wszystkich miast",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "cities", nargs="*", default=[],
        help=f"Miasta do scrapowania (domyślnie: wszystkie). Dostępne: {', '.join(ALL_CITY_IDS)}",
    )
    parser.add_argument("--base-dir", default=None, help="Katalog nadrzędny z radoskop-*/")
    parser.add_argument("--dry-run", action="store_true", help="Przekaż --dry-run do scraperów")
    parser.add_argument("--max-sessions", type=int, default=None, help="Ogranicz do N sesji")
    parser.add_argument("--interpelacje", action="store_true", help="Tylko interpelacje")
    parser.add_argument("--skip-sesje", action="store_true", help="Pomiń sesje/głosowania")
    parser.add_argument("--skip-venv", action="store_true", help="Nie twórz venv / nie instaluj deps")

    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve() if args.base_dir else find_base_dir()
    print(f"Katalog bazowy: {base_dir}")

    cities = args.cities if args.cities else ALL_CITY_IDS
    invalid = [c for c in cities if c not in CITIES]
    if invalid:
        print(f"Nieznane miasta: {', '.join(invalid)}")
        print(f"Dostępne: {', '.join(ALL_CITY_IDS)}")
        sys.exit(1)

    print(f"Miasta: {', '.join(cities)}")
    print(f"Tryb: {'interpelacje' if args.interpelacje else 'sesje' if args.skip_sesje else 'sesje + interpelacje'}")
    print()

    succeeded = []
    failed = []

    for city_id in cities:
        config = CITIES[city_id]
        print(f"{'='*50}")
        print(f"  {config['name']} ({city_id})")
        print(f"{'='*50}")

        ok = True

        # Sesje / głosowania
        if not args.interpelacje and not args.skip_sesje:
            print(f"  --- Sesje i głosowania ---")
            if not run_scraper(city_id, config, base_dir, args):
                ok = False

        # Interpelacje
        if args.interpelacje or (not args.skip_sesje and not args.interpelacje):
            # Uruchom interpelacje jeśli --interpelacje lub domyślny tryb pełny
            if config.get("interpelacje"):
                print(f"  --- Interpelacje ---")
                if not run_interpelacje(city_id, config, base_dir, args):
                    ok = False

        if ok:
            succeeded.append(city_id)
        else:
            failed.append(city_id)
        print()

    # Podsumowanie
    print(f"{'='*50}")
    print("  PODSUMOWANIE")
    print(f"{'='*50}")
    print(f"  OK:    {', '.join(succeeded) if succeeded else 'brak'}")
    print(f"  Błąd:  {', '.join(failed) if failed else 'brak'}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
