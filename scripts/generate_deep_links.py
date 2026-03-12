#!/usr/bin/env python3
"""
Generate lightweight static pages for /glosowanie/ and /sesja/ deep links.

These are tiny HTML files (~1KB each) that:
1. Return HTTP 200 (instead of 404 via GitHub Pages)
2. Have correct <link rel="canonical"> for the specific URL
3. Load the main SPA app which then renders the correct view

This fixes "Not found (404)" errors in Google Search Console.
"""

import json
import os
from pathlib import Path


DEEPLINK_TEMPLATE = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canonical}">
<meta property="og:url" content="{canonical}">
<meta name="robots" content="noindex">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script>
// SPA deep link: redirect to main app which handles routing
var path = location.pathname;
if (path !== '/') location.replace('/?p=' + encodeURIComponent(path));
</script>
</head>
<body>
<noscript><a href="/">Radoskop</a></noscript>
</body>
</html>
"""


def generate_city(city_dir: Path):
    docs = city_dir / "docs"
    if not docs.exists():
        return

    config_path = city_dir / "config.json"
    if not config_path.exists():
        print(f"  Skipping {city_dir.name}: no config.json")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    site_url = config["site_url"].rstrip("/")
    city_name = config["city_name"]

    data_path = docs / "data.json"
    if not data_path.exists():
        return

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    vote_count = 0
    session_count = 0

    for k in data.get("kadencje", []):
        # Generate glosowanie directories
        for v in k.get("votes", []):
            vid = v.get("id", "")
            if not vid:
                continue
            topic = v.get("topic", "")[:80]
            page_dir = docs / "glosowanie" / vid
            if not page_dir.exists():
                page_dir.mkdir(parents=True, exist_ok=True)

            canonical = f"{site_url}/glosowanie/{vid}"
            title = f"Glosowanie {vid} - Radoskop {city_name}"
            desc = topic if topic else f"Glosowanie {vid} Rady Miasta"

            html = DEEPLINK_TEMPLATE.format(
                title=title,
                description=desc.replace('"', '&quot;'),
                canonical=canonical,
            )
            with open(page_dir / "index.html", "w", encoding="utf-8") as f:
                f.write(html)
            vote_count += 1

        # Generate sesja directories
        for s in k.get("sessions", []):
            snum = s.get("number", "")
            if not snum:
                continue
            page_dir = docs / "sesja" / snum
            if not page_dir.exists():
                page_dir.mkdir(parents=True, exist_ok=True)

            canonical = f"{site_url}/sesja/{snum}"
            title = f"Sesja {snum} - Radoskop {city_name}"
            desc = f"Sesja {snum} Rady Miasta ({s.get('date', '')})"

            html = DEEPLINK_TEMPLATE.format(
                title=title,
                description=desc.replace('"', '&quot;'),
                canonical=canonical,
            )
            with open(page_dir / "index.html", "w", encoding="utf-8") as f:
                f.write(html)
            session_count += 1

    print(f"  Generated {vote_count} glosowanie + {session_count} sesja pages")


def main():
    base = Path("/sessions/busy-youthful-brown/mnt/gdansk-network")

    cities = [
        "radoskop-gdansk", "radoskop-warszawa", "radoskop-krakow",
        "radoskop-wroclaw", "radoskop-poznan", "radoskop-gdynia", "radoskop-sopot",
    ]

    for city in cities:
        city_dir = base / city
        if city_dir.exists():
            print(f"\n=== {city} ===")
            generate_city(city_dir)


if __name__ == "__main__":
    main()
