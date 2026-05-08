#!/usr/bin/env python3
"""
Scraper listy Abgeordnetenów Berlin Abgeordnetenhaus.

Strona /das-parlament/abgeordnete/suche-nach-fraktionen renderuje pełną
listę deputowanych SSR (server-side rendering), z każdym anchor:
  <a href="/Abgeordnete/{slug}?groupStrategy=fraktion">Nazwisko, Imię, Fraktion[, Nachgerückt]</a>

Wystarcza 1 fetch HTML. Output: zaktualizowany config.json z club_assignments
i lista deputowanych w docs/abgeordnete.json (do scrape_sessions.py).

Mapowanie fraktion → club key (config.clubs):
  CDU-Fraktion       → CDU
  SPD-Fraktion       → SPD
  Bündnis 90/Die Grünen → GRUENE
  Die Linke          → LINKE
  AfD-Fraktion       → AFD
  FDP-Fraktion       → FDP (jeśli istnieje w danej kadencji)
  fraktionslos       → NZ

Użycie:
    python3 scrape_abgeordnete.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LIST_URL = "https://www.parlament-berlin.de/das-parlament/abgeordnete/suche-nach-fraktionen"


def http_get(url: str, timeout: int = 30) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "de"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def slugify(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s\-]", "", ascii_only.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "abgeordnet"


def map_fraktion(fraktion_text: str) -> str:
    """Mapuj nazwę frakcji na klucz w config.clubs."""
    f = fraktion_text.lower()
    if "cdu" in f:
        return "CDU"
    if "spd" in f:
        return "SPD"
    if "grün" in f or "gruen" in f or "bündnis 90" in f:
        return "GRUENE"
    if "linke" in f or "die linke" in f:
        return "LINKE"
    if "afd" in f:
        return "AFD"
    if "fdp" in f:
        return "FDP"
    return "NZ"  # fraktionslos albo nieznane


def parse_list(html: str) -> list[dict[str, str]]:
    """Wyciągnij listę abgeordnetów z HTML.

    Każdy anchor: <a href="/Abgeordnete/{slug}?groupStrategy=fraktion">
        Nazwisko, Imię, Fraktion[, Nachgerückt]</a>
    """
    out: list[dict[str, str]] = []
    pattern = re.compile(
        r'<a[^>]+href="(/Abgeordnete/[^"?]+)\?groupStrategy=fraktion"[^>]*>'
        r'\s*([^<]+?)\s*</a>',
        re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        href = m.group(1)
        text = re.sub(r"\s+", " ", m.group(2)).strip()
        # Format: "Nazwisko, Imię, Fraktion[, Nachgerückt]"
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 3:
            continue
        nachgerueckt = "Nachgerückt" in parts[-1] or "Nachgerueckt" in parts[-1]
        if nachgerueckt:
            parts = parts[:-1]
        # Last part po wytraceniu Nachgerückt to fraktion
        fraktion = parts[-1]
        # Reszta to nazwisko + imię
        if len(parts) == 3:
            last_name, first_name = parts[0], parts[1]
        else:
            last_name = parts[0]
            first_name = ", ".join(parts[1:-1])
        # Imię może mieć tytuł (Dr.) — zostawiamy
        full_name = f"{first_name} {last_name}".strip()
        out.append({
            "slug": href.split("/")[-1],
            "url_path": href,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "fraktion_text": fraktion,
            "club": map_fraktion(fraktion),
            "nachgerueckt": nachgerueckt,
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default=str(DEFAULT_DOCS / "abgeordnete.json"))
    parser.add_argument("--no-update-config", action="store_true",
                        help="Tylko pisz docs/abgeordnete.json, nie zmieniaj config.json")
    args = parser.parse_args()

    print(f"[abgeordnete] GET {LIST_URL}", file=sys.stderr)
    html = http_get(LIST_URL)
    print(f"  fetched {len(html)/1024:.1f} KB", file=sys.stderr)

    members = parse_list(html)
    print(f"  parsed {len(members)} abgeordnete", file=sys.stderr)

    # Counts per fraktion
    from collections import Counter
    fraktion_counts = Counter(m["club"] for m in members)
    print("  per fraktion:")
    for club, n in sorted(fraktion_counts.items(), key=lambda x: -x[1]):
        print(f"    {club:8} → {n}")

    # docs/abgeordnete.json
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(members),
        "abgeordnete": members,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[abgeordnete] wrote {out_path} ({len(members)} entries)", file=sys.stderr)

    # Update config.club_assignments
    if not args.no_update_config:
        config_path = Path(args.config)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assignments: dict[str, str] = {}
        for m in members:
            assignments[m["full_name"]] = m["club"]
        config["club_assignments"] = assignments
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[abgeordnete] updated {config_path} z {len(assignments)} club_assignments", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
