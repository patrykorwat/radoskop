#!/usr/bin/env python3
"""
Generator sitemap.xml dla Radoskop.

Skanuje katalog docs/ danego miasta, znajduje wszystkie index.html
i generuje sitemap.xml z poprawnymi URLami (trailing slash).

Uzycie:
    python generate_sitemap.py /path/to/radoskop-gdansk

Wymaga config.json w katalogu miasta (pole site_url).
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path


# Priorytety per typ strony. Po migracji 2026-05 angielskie URL slugi.
# Ten skrypt to legacy fallback (scrape_all.sh używa go tylko gdy
# generate_seo_pages.py nie ma) — kanoniczny sitemap robi
# generate_seo_pages.py.
PRIORITIES = {
    "": 1.0,           # strona glowna
    "budget": 0.8,
    "term": 0.7,
    "profile": 0.6,
    "session": 0.5,
    "vote": 0.3,
}

CHANGEFREQS = {
    "": "weekly",
    "budget": "monthly",
    "term": "weekly",
    "profile": "weekly",
    "session": "weekly",
    "vote": "monthly",
}


def get_priority(rel_path: str) -> float:
    parts = rel_path.strip("/").split("/")
    first = parts[0] if parts and parts[0] else ""
    return PRIORITIES.get(first, 0.5)


def get_changefreq(rel_path: str) -> str:
    parts = rel_path.strip("/").split("/")
    first = parts[0] if parts and parts[0] else ""
    return CHANGEFREQS.get(first, "weekly")


def find_pages(docs_dir: Path) -> list[str]:
    """Znajdz wszystkie sciezki z index.html w docs/."""
    pages = []
    for root, dirs, files in os.walk(docs_dir):
        if "index.html" in files:
            rel = os.path.relpath(root, docs_dir)
            if rel == ".":
                pages.append("/")
            else:
                pages.append("/" + rel.replace(os.sep, "/") + "/")
    return sorted(pages)


def generate_sitemap(site_url: str, pages: list[str]) -> str:
    """Generuj XML sitemap."""
    site_url = site_url.rstrip("/")
    today = datetime.now().strftime("%Y-%m-%d")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    for page in pages:
        url = site_url + page
        priority = get_priority(page)
        changefreq = get_changefreq(page)

        lines.append("  <url>")
        lines.append(f"    <loc>{url}</loc>")
        lines.append(f"    <lastmod>{today}</lastmod>")
        lines.append(f"    <changefreq>{changefreq}</changefreq>")
        lines.append(f"    <priority>{priority}</priority>")
        lines.append("  </url>")

    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generator sitemap.xml dla Radoskop")
    parser.add_argument("city_dir", help="Katalog miasta (np. radoskop-gdansk)")
    parser.add_argument("--out", help="Sciezka wyjsciowa (domyslnie: docs/sitemap.xml)")
    args = parser.parse_args()

    city_dir = Path(args.city_dir)
    docs_dir = city_dir / "docs"
    config_path = city_dir / "config.json"

    if not docs_dir.is_dir():
        print(f"BLAD: Brak katalogu {docs_dir}", file=sys.stderr)
        sys.exit(1)

    if not config_path.is_file():
        print(f"BLAD: Brak {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    site_url = config.get("site_url", "")
    if not site_url:
        print(f"BLAD: Brak site_url w {config_path}", file=sys.stderr)
        sys.exit(1)

    pages = find_pages(docs_dir)
    sitemap = generate_sitemap(site_url, pages)

    out_path = Path(args.out) if args.out else docs_dir / "sitemap.xml"
    out_path.write_text(sitemap)

    print(f"Sitemap: {out_path} ({len(pages)} URLi)")


if __name__ == "__main__":
    main()
