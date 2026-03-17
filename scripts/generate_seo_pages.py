#!/usr/bin/env python3
"""
Fix SEO issues for Radoskop city instances.

Problems found in Google Search Console:
1. All sub-pages (profil/, budzet/, kadencja/) have <link rel="canonical"> pointing to homepage
   → Google reports "Page with redirect" for 20+ profile pages
2. /budzet canonical points to homepage → "Redirect error"
3. Missing directories for sitemap URLs (uchwaly, interpelacje in kadencja)
4. Sub-page index.html files are outdated (pre-uchwaly/interpelacje code)

Fix: Update all sub-page index.html files with current code + unique SEO meta tags.
"""

import json
import re
import os
import shutil
from pathlib import Path


def fix_city(city_dir: Path):
    """Fix SEO for a single city instance."""
    docs = city_dir / "docs"
    if not docs.exists():
        print(f"  Skipping {city_dir.name}: no docs/ directory")
        return

    config_path = city_dir / "config.json"
    if not config_path.exists():
        print(f"  Skipping {city_dir.name}: no config.json")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    site_url = config["site_url"].rstrip("/")
    city_name = config["city_name"]
    city_gen = config["city_genitive"]

    # Read current main index.html (most up-to-date version)
    main_html_path = docs / "index.html"
    with open(main_html_path, "r", encoding="utf-8") as f:
        main_html = f.read()

    # Read profiles
    profiles_path = docs / "profiles.json"
    if not profiles_path.exists():
        print(f"  WARNING: no profiles.json in {city_dir.name}")
        profiles = []
    else:
        with open(profiles_path, "r", encoding="utf-8") as f:
            profiles = json.load(f).get("profiles", [])

    # Read data.json for kadencje info
    data_path = docs / "data.json"
    kadencje = []
    if data_path.exists():
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            kadencje = data.get("kadencje", [])

    # ──────────────────────────────────────────
    # Helper: replace meta tags in HTML
    # ──────────────────────────────────────────
    def make_page(canonical_url, title, description):
        """Create a page variant with unique SEO tags."""
        html = main_html

        # Replace canonical
        html = re.sub(
            r'<link rel="canonical" href="[^"]*">',
            f'<link rel="canonical" href="{canonical_url}">',
            html
        )

        # Replace <title>
        html = re.sub(
            r'<title>[^<]*</title>',
            f'<title>{title}</title>',
            html
        )

        # Replace meta description
        html = re.sub(
            r'<meta name="description" content="[^"]*">',
            f'<meta name="description" content="{description}">',
            html
        )

        # Replace og:title
        html = re.sub(
            r'<meta property="og:title" content="[^"]*">',
            f'<meta property="og:title" content="{title}">',
            html
        )

        # Replace og:description
        html = re.sub(
            r'<meta property="og:description" content="[^"]*">',
            f'<meta property="og:description" content="{description}">',
            html
        )

        # Replace og:url
        html = re.sub(
            r'<meta property="og:url" content="[^"]*">',
            f'<meta property="og:url" content="{canonical_url}">',
            html
        )

        # Replace twitter:title
        html = re.sub(
            r'<meta name="twitter:title" content="[^"]*">',
            f'<meta name="twitter:title" content="{title}">',
            html
        )

        # Replace twitter:description
        html = re.sub(
            r'<meta name="twitter:description" content="[^"]*">',
            f'<meta name="twitter:description" content="{description}">',
            html
        )

        return html

    def write_page(path: Path, html: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)

    # Track sitemap entries
    sitemap_entries = []

    # ──────────────────────────────────────────
    # 1. Profile pages
    # ──────────────────────────────────────────
    profile_count = 0
    for p in profiles:
        slug = p["slug"]
        name = p["name"]
        canonical = f"{site_url}/profil/{slug}/"
        title = f"{name} - Radoskop {city_name}"
        desc = f"Profil: {name}. Sprawdz glosowania, frekwencje i aktywnosc w Radzie Miasta {city_gen}."

        html = make_page(canonical, title, desc)
        write_page(docs / "profil" / slug / "index.html", html)
        profile_count += 1

        sitemap_entries.append({
            "loc": canonical,
            "changefreq": "monthly",
            "priority": "0.7"
        })

    print(f"  Updated {profile_count} profile pages")

    # ──────────────────────────────────────────
    # 2. Budzet page
    # ──────────────────────────────────────────
    if config.get("has_budget") and (docs / "budzet").is_dir():
        canonical = f"{site_url}/budzet/"
        title = f"Budzet {city_gen} - Radoskop {city_name}"
        desc = f"Analiza budzetu miasta {city_gen}. Wydatki, dochody i inwestycje miejskie."

        html = make_page(canonical, title, desc)
        write_page(docs / "budzet" / "index.html", html)
        print(f"  Updated budzet page")

        sitemap_entries.append({
            "loc": canonical,
            "changefreq": "monthly",
            "priority": "0.8"
        })

    # ──────────────────────────────────────────
    # 3. Kadencja pages
    # ──────────────────────────────────────────
    # Extract KAD_SLUGS from JS in index.html (e.g. const KAD_SLUGS = {'2018-2023':'viii','2024-2029':'ix'};)
    KAD_SLUGS = {}
    kad_match = re.search(r"const\s+KAD_SLUGS\s*=\s*\{([^}]+)\}", main_html)
    if kad_match:
        # Parse JS object literal
        for m in re.finditer(r"'([^']+)'\s*:\s*'([^']+)'", kad_match.group(1)):
            KAD_SLUGS[m.group(1)] = m.group(2)
    if not KAD_SLUGS:
        # Fallback: use kadencja IDs as-is
        for k in kadencje:
            KAD_SLUGS[k["id"]] = k["id"]

    TAB_NAMES = {
        "ranking": "Ranking",
        "radni": "Radni",
        "sesje": "Sesje",
        "glosowania": "Glosowania",
        "podobienstwo": "Podobienstwo",
        "uchwaly": "Uchwaly",
        "interpelacje": "Interpelacje",
    }

    kad_count = 0
    for kid, kslug in KAD_SLUGS.items():
        # Main kadencja page
        kad_dir = docs / "kadencja" / kslug
        if not kad_dir.exists():
            kad_dir.mkdir(parents=True, exist_ok=True)

        canonical = f"{site_url}/kadencja/{kslug}/"
        title = f"Kadencja {kid} - Radoskop {city_name}"
        desc = f"Monitoring Rady Miasta {city_gen}, kadencja {kid}. Ranking, sesje, glosowania."

        html = make_page(canonical, title, desc)
        # kadencja/ level index.html (catch-all)
        write_page(kad_dir / "index.html", html)

        for tab_slug, tab_name in TAB_NAMES.items():
            tab_canonical = f"{site_url}/kadencja/{kslug}/{tab_slug}/"
            tab_title = f"{tab_name} - kadencja {kid} - Radoskop {city_name}"
            tab_desc = f"{tab_name} Rady Miasta {city_gen}, kadencja {kid}."

            tab_html = make_page(tab_canonical, tab_title, tab_desc)
            write_page(kad_dir / tab_slug / "index.html", tab_html)
            kad_count += 1

            sitemap_entries.append({
                "loc": tab_canonical,
                "changefreq": "weekly" if kid == kadencje[-1]["id"] else "monthly",
                "priority": "0.6" if kid != kadencje[-1]["id"] else "0.8"
            })

    print(f"  Updated {kad_count} kadencja tab pages")

    # ──────────────────────────────────────────
    # 4. Glosowanie catch-all page
    # ──────────────────────────────────────────
    glos_dir = docs / "glosowanie"
    if glos_dir.is_dir():
        canonical = f"{site_url}/"
        # Keep glosowanie index.html pointing to main page (it's a catch-all for SPA)
        html = make_page(f"{site_url}/",
                         f"Radoskop {city_name} - Monitoring Rady Miasta {city_gen}",
                         config.get("site_description", ""))
        write_page(glos_dir / "index.html", html)

    # ──────────────────────────────────────────
    # 5. Sesja catch-all page
    # ──────────────────────────────────────────
    sesja_dir = docs / "sesja"
    if sesja_dir.is_dir():
        html = make_page(f"{site_url}/",
                         f"Radoskop {city_name} - Monitoring Rady Miasta {city_gen}",
                         config.get("site_description", ""))
        write_page(sesja_dir / "index.html", html)

    # ──────────────────────────────────────────
    # 6. Update main index.html canonical (should already be correct)
    # ──────────────────────────────────────────
    main_canonical = f"{site_url}/"
    main_check = re.search(r'<link rel="canonical" href="([^"]*)">', main_html)
    if main_check and main_check.group(1) != main_canonical:
        main_html = re.sub(
            r'<link rel="canonical" href="[^"]*">',
            f'<link rel="canonical" href="{main_canonical}">',
            main_html
        )
        with open(main_html_path, "w", encoding="utf-8") as f:
            f.write(main_html)
        print(f"  Fixed main index.html canonical")

    # ──────────────────────────────────────────
    # 7. Regenerate sitemap.xml
    # ──────────────────────────────────────────
    sitemap_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f'  <url>\n    <loc>{site_url}/</loc>\n    <changefreq>weekly</changefreq>\n    <priority>1.0</priority>\n  </url>',
    ]

    for entry in sitemap_entries:
        sitemap_lines.append(
            f'  <url>\n    <loc>{entry["loc"]}</loc>\n'
            f'    <changefreq>{entry["changefreq"]}</changefreq>\n'
            f'    <priority>{entry["priority"]}</priority>\n  </url>'
        )

    sitemap_lines.append('</urlset>')

    with open(docs / "sitemap.xml", "w", encoding="utf-8") as f:
        f.write("\n".join(sitemap_lines) + "\n")

    entry_count = len(sitemap_entries) + 1  # +1 for homepage
    print(f"  Updated sitemap.xml ({entry_count} URLs)")


def main():
    base = Path("/sessions/stoic-epic-maxwell/mnt/gdansk-network")

    cities = [
        "radoskop-gdansk",
        "radoskop-warszawa",
        "radoskop-krakow",
        "radoskop-wroclaw",
        "radoskop-poznan",
        "radoskop-gdynia",
        "radoskop-sopot",
    ]

    for city in cities:
        city_dir = base / city
        if city_dir.exists():
            print(f"\n=== {city} ===")
            fix_city(city_dir)
        else:
            print(f"\nSkipping {city}: directory not found")


if __name__ == "__main__":
    main()
