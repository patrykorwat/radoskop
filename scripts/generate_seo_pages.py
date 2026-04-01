#!/usr/bin/env python3
"""
Generate SEO-optimized static pages for Radoskop city instances.

Creates content-rich HTML pages for search engines to index:
  - /profil/{slug}/index.html   (councillor profiles)
  - /glosowanie/{id}/index.html (individual votes)
  - /sesja/{number}/index.html  (sessions)
  - /kadencja/{slug}/index.html (kadencja tabs)
  - /budzet/index.html          (budget page)
  - sitemap.xml                 (full sitemap)

Each page:
  1. Has unique <title>, <meta description>, <link canonical>, OG tags
  2. Has og:image pointing to generated OG image (if available)
  3. Contains visible text content for Google to index
  4. Loads the full SPA JS so the page becomes interactive after hydration

Usage:
    python generate_seo_pages.py --base /path/to/gdansk-network
    python generate_seo_pages.py --base /path/to/gdansk-network --city radoskop-gdansk
"""

import argparse
import html
import json
import re
from pathlib import Path


def esc(text):
    """HTML-escape text for safe embedding."""
    return html.escape(str(text), quote=True)


def make_page(main_html, canonical_url, title, description, og_image=None, extra_body=""):
    """Create a page variant with unique SEO tags and optional body content."""
    h = main_html

    # Replace canonical
    h = re.sub(
        r'<link rel="canonical" href="[^"]*">',
        f'<link rel="canonical" href="{canonical_url}">',
        h
    )

    # Replace <title>
    h = re.sub(r'<title>[^<]*</title>', f'<title>{esc(title)}</title>', h)

    # Replace meta description
    h = re.sub(
        r'<meta name="description" content="[^"]*">',
        f'<meta name="description" content="{esc(description)}">',
        h
    )

    # Replace og:title
    h = re.sub(
        r'<meta property="og:title" content="[^"]*">',
        f'<meta property="og:title" content="{esc(title)}">',
        h
    )

    # Replace og:description
    h = re.sub(
        r'<meta property="og:description" content="[^"]*">',
        f'<meta property="og:description" content="{esc(description)}">',
        h
    )

    # Replace og:url
    h = re.sub(
        r'<meta property="og:url" content="[^"]*">',
        f'<meta property="og:url" content="{canonical_url}">',
        h
    )

    # Replace twitter:title
    h = re.sub(
        r'<meta name="twitter:title" content="[^"]*">',
        f'<meta name="twitter:title" content="{esc(title)}">',
        h
    )

    # Replace twitter:description
    h = re.sub(
        r'<meta name="twitter:description" content="[^"]*">',
        f'<meta name="twitter:description" content="{esc(description)}">',
        h
    )

    # Add og:image if provided (insert after og:url)
    if og_image:
        og_image_tag = f'<meta property="og:image" content="{og_image}">'
        og_image_tw = '<meta name="twitter:card" content="summary_large_image">'
        # Remove existing og:image if any
        h = re.sub(r'<meta property="og:image" content="[^"]*">\n?', '', h)
        # Change twitter card to summary_large_image
        h = re.sub(r'<meta name="twitter:card" content="[^"]*">', og_image_tw, h)
        # Insert og:image after og:url
        h = h.replace(
            f'<meta property="og:url" content="{canonical_url}">',
            f'<meta property="og:url" content="{canonical_url}">\n{og_image_tag}'
        )

    # Inject SEO body content (visible text for crawlers) before the loading div
    if extra_body:
        # Insert as a noscript-visible section right after <div id="loading">
        seo_block = f'\n<div id="seo-content" style="padding:20px;max-width:800px;margin:0 auto">\n{extra_body}\n</div>\n'
        # Hide seo-content once JS loads (the SPA will take over)
        hide_script = '<script>var sc=document.getElementById("seo-content");if(sc)sc.style.display="none";</script>\n'
        h = h.replace(
            '<div id="loading">',
            seo_block + hide_script + '<div id="loading">'
        )

    return h


def write_page(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def process_city(city_dir: Path):
    """Generate all SEO pages for one city."""
    docs = city_dir / "docs"
    config_path = city_dir / "config.json"

    if not docs.exists() or not config_path.exists():
        print(f"  Skipping {city_dir.name}: missing docs/ or config.json")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    site_url = config["site_url"].rstrip("/")
    city_name = config["city_name"]
    city_gen = config["city_genitive"]

    # Read main index.html
    main_html_path = docs / "index.html"
    with open(main_html_path, "r", encoding="utf-8") as f:
        main_html = f.read()

    # Load profiles
    profiles = []
    profiles_path = docs / "profiles.json"
    if profiles_path.exists():
        with open(profiles_path, "r", encoding="utf-8") as f:
            profiles = json.load(f).get("profiles", [])

    # Load data.json for kadencje index
    kadencje = []
    data_path = docs / "data.json"
    if data_path.exists():
        with open(data_path, "r", encoding="utf-8") as f:
            kadencje = json.load(f).get("kadencje", [])

    # Extract KAD_SLUGS from JS
    KAD_SLUGS = {}
    kad_match = re.search(r"const\s+KAD_SLUGS\s*=\s*\{([^}]+)\}", main_html)
    if kad_match:
        for m in re.finditer(r"'([^']+)'\s*:\s*'([^']+)'", kad_match.group(1)):
            KAD_SLUGS[m.group(1)] = m.group(2)
    if not KAD_SLUGS:
        for k in kadencje:
            KAD_SLUGS[k["id"]] = k["id"]

    sitemap_entries = []

    # ════════════════════════════════════════════
    # 1. Profile pages
    # ════════════════════════════════════════════
    profile_count = 0
    for p in profiles:
        slug = p["slug"]
        name = p["name"]

        # Get stats from most recent kadencja
        kad_keys = sorted(p.get("kadencje", {}).keys(), reverse=True)
        kad = p["kadencje"][kad_keys[0]] if kad_keys else {}

        club = kad.get("club", "")
        club_full = kad.get("club_full", club)
        frekwencja = kad.get("frekwencja", 0)
        aktywnosc = kad.get("aktywnosc", 0)
        zgodnosc = kad.get("zgodnosc_z_klubem", 0)
        votes_za = kad.get("votes_za", 0)
        votes_przeciw = kad.get("votes_przeciw", 0)
        votes_wstrzymal = kad.get("votes_wstrzymal", 0)

        canonical = f"{site_url}/profil/{slug}/"
        title = f"{name}, {club} \u2013 Radoskop {city_name}"
        desc = (
            f"{name}, klub {club_full}. "
            f"Frekwencja {frekwencja:.0f}%, aktywnosc {aktywnosc:.0f}%, "
            f"zgodnosc z klubem {zgodnosc:.0f}%. "
            f"Rada Miasta {city_gen}."
        )

        og_img = f"{site_url}/profil/{slug}/og.png"
        og_img_path = docs / "profil" / slug / "og.png"
        if not og_img_path.exists():
            og_img = None

        body = (
            f"<h1>{esc(name)}</h1>\n"
            f"<p>Klub: {esc(club_full)}</p>\n"
            f"<p>Frekwencja: {frekwencja:.0f}% · "
            f"Aktywnosc: {aktywnosc:.0f}% · "
            f"Zgodnosc z klubem: {zgodnosc:.0f}%</p>\n"
            f"<p>Za: {votes_za} · Przeciw: {votes_przeciw} · Wstrzymal sie: {votes_wstrzymal}</p>\n"
            f"<p><a href=\"{site_url}/\">Radoskop {esc(city_name)}</a></p>\n"
        )

        page = make_page(main_html, canonical, title, desc, og_image=og_img, extra_body=body)
        write_page(docs / "profil" / slug / "index.html", page)
        profile_count += 1

        sitemap_entries.append({"loc": canonical, "changefreq": "weekly", "priority": "0.7"})

    print(f"  {profile_count} profile pages")

    # ════════════════════════════════════════════
    # 2. Vote pages (per kadencja)
    # ════════════════════════════════════════════
    vote_count = 0
    for k in kadencje:
        kid = k.get("id", "")
        kad_file = docs / f"kadencja-{kid}.json"
        if not kad_file.exists():
            continue

        with open(kad_file, "r", encoding="utf-8") as f:
            kad_data = json.load(f)

        for vote in kad_data.get("votes", []):
            vid = vote.get("id", "")
            if not vid:
                continue

            topic = vote.get("topic", "").replace(";", "").strip()
            counts = vote.get("counts", {})
            za = counts.get("za", 0)
            przeciw = counts.get("przeciw", 0)
            wstrzymal = counts.get("wstrzymal_sie", 0)
            session_date = vote.get("session_date", "")
            session_number = vote.get("session_number", "")

            if za > przeciw:
                result = "przyjete"
            elif przeciw > za:
                result = "odrzucone"
            else:
                result = "remis"

            canonical = f"{site_url}/glosowanie/{vid}/"
            title_text = topic[:80] if topic else f"Glosowanie {vid}"
            title = f"{title_text} \u2013 Radoskop {city_name}"
            desc = (
                f"Glosowanie: {topic[:120]}. "
                f"Wynik: za {za}, przeciw {przeciw}, wstrzymal sie {wstrzymal}. "
                f"Sesja {session_number}, {session_date}."
            )

            og_img = f"{site_url}/glosowanie/{vid}/og.png"
            og_img_path = docs / "glosowanie" / vid / "og.png"
            if not og_img_path.exists():
                og_img = None

            body = (
                f"<h1>{esc(topic or f'Glosowanie {vid}')}</h1>\n"
                f"<p>Sesja {esc(session_number)}, {esc(session_date)}</p>\n"
                f"<p>Wynik: <strong>{result}</strong></p>\n"
                f"<p>Za: {za} · Przeciw: {przeciw} · Wstrzymal sie: {wstrzymal}</p>\n"
                f"<p><a href=\"{site_url}/\">Radoskop {esc(city_name)}</a></p>\n"
            )

            page = make_page(main_html, canonical, title, desc, og_image=og_img, extra_body=body)
            write_page(docs / "glosowanie" / vid / "index.html", page)
            vote_count += 1

            sitemap_entries.append({"loc": canonical, "changefreq": "monthly", "priority": "0.5"})

    print(f"  {vote_count} vote pages")

    # ════════════════════════════════════════════
    # 3. Session pages
    # ════════════════════════════════════════════
    session_count = 0
    for k in kadencje:
        kid = k.get("id", "")
        kad_file = docs / f"kadencja-{kid}.json"
        if not kad_file.exists():
            continue

        with open(kad_file, "r", encoding="utf-8") as f:
            kad_data = json.load(f)

        for s in kad_data.get("sessions", []):
            snum = s.get("number", "")
            if not snum:
                continue

            sdate = s.get("date", "")
            vote_cnt = s.get("vote_count", 0)
            attendee_cnt = s.get("attendee_count", 0)

            canonical = f"{site_url}/sesja/{snum}/"
            title = f"Sesja {snum} ({sdate}) \u2013 Radoskop {city_name}"
            desc = (
                f"Sesja {snum} Rady Miasta {city_gen}, {sdate}. "
                f"{vote_cnt} glosowan, {attendee_cnt} obecnych radnych."
            )

            body = (
                f"<h1>Sesja {esc(snum)}</h1>\n"
                f"<p>Data: {esc(sdate)}</p>\n"
                f"<p>Glosowan: {vote_cnt} · Obecnych: {attendee_cnt}</p>\n"
                f"<p><a href=\"{site_url}/\">Radoskop {esc(city_name)}</a></p>\n"
            )

            page = make_page(main_html, canonical, title, desc, extra_body=body)
            write_page(docs / "sesja" / snum / "index.html", page)
            session_count += 1

            sitemap_entries.append({"loc": canonical, "changefreq": "monthly", "priority": "0.5"})

    print(f"  {session_count} session pages")

    # ════════════════════════════════════════════
    # 4. Kadencja tab pages
    # ════════════════════════════════════════════
    TAB_NAMES = {
        "ranking": "Ranking radnych",
        "radni": "Profile radnych",
        "sesje": "Sesje",
        "glosowania": "Glosowania",
        "podobienstwo": "Podobienstwo glosowan",
        "interpelacje": "Interpelacje",
    }

    kad_count = 0
    for kid, kslug in KAD_SLUGS.items():
        canonical = f"{site_url}/kadencja/{kslug}/"
        title = f"Kadencja {kid} \u2013 Radoskop {city_name}"
        desc = f"Monitoring Rady Miasta {city_gen}, kadencja {kid}. Ranking, sesje, glosowania i aktywnosc radnych."

        page = make_page(main_html, canonical, title, desc)
        write_page(docs / "kadencja" / kslug / "index.html", page)

        sitemap_entries.append({"loc": canonical, "changefreq": "weekly", "priority": "0.8"})

        for tab_slug, tab_name in TAB_NAMES.items():
            tab_canonical = f"{site_url}/kadencja/{kslug}/{tab_slug}/"
            tab_title = f"{tab_name}, kadencja {kid} \u2013 Radoskop {city_name}"
            tab_desc = f"{tab_name} Rady Miasta {city_gen}, kadencja {kid}."

            tab_page = make_page(main_html, tab_canonical, tab_title, tab_desc)
            write_page(docs / "kadencja" / kslug / tab_slug / "index.html", tab_page)
            kad_count += 1

            sitemap_entries.append({"loc": tab_canonical, "changefreq": "weekly", "priority": "0.6"})

    print(f"  {kad_count} kadencja tab pages")

    # ════════════════════════════════════════════
    # 5. Budget page
    # ════════════════════════════════════════════
    if config.get("has_budget"):
        canonical = f"{site_url}/budzet/"
        title = f"Budzet {city_gen} \u2013 Radoskop {city_name}"
        desc = f"Analiza budzetu miasta {city_gen}. Wydatki, dochody i inwestycje miejskie."

        page = make_page(main_html, canonical, title, desc)
        write_page(docs / "budzet" / "index.html", page)
        sitemap_entries.append({"loc": canonical, "changefreq": "monthly", "priority": "0.8"})
        print(f"  1 budget page")

    # ════════════════════════════════════════════
    # 6. Catch-all directory pages
    # ════════════════════════════════════════════
    for dirname, title_part, desc_part, prio in [
        ("profil", f"Radni {city_gen}", f"Profile radnych {city_gen}. Frekwencja, glosowania i aktywnosc.", "0.9"),
        ("kadencja", f"Kadencje Rady Miasta {city_gen}", f"Kadencje Rady Miasta {city_gen}. Ranking, sesje i glosowania.", "0.9"),
    ]:
        d = docs / dirname
        if d.is_dir() or profiles:  # create even if not existing yet
            canonical = f"{site_url}/{dirname}/"
            title = f"{title_part} \u2013 Radoskop {city_name}"
            page = make_page(main_html, canonical, title, desc_part)
            write_page(d / "index.html", page)
            sitemap_entries.append({"loc": canonical, "changefreq": "monthly", "priority": prio})

    # ════════════════════════════════════════════
    # 6b. Privacy policy page
    # ════════════════════════════════════════════
    privacy_canonical = f"{site_url}/polityka-prywatnosci/"
    privacy_title = f"Polityka prywatności \u2013 Radoskop {city_name}"
    privacy_desc = f"Polityka prywatności i informacje o plikach cookies serwisu Radoskop {city_name}."
    privacy_page = make_page(main_html, privacy_canonical, privacy_title, privacy_desc)
    write_page(docs / "polityka-prywatnosci" / "index.html", privacy_page)
    sitemap_entries.append({"loc": privacy_canonical, "changefreq": "yearly", "priority": "0.3"})

    terms_canonical = f"{site_url}/regulamin/"
    terms_title = f"Regulamin \u2013 Radoskop {city_name}"
    terms_desc = f"Regulamin serwisu Radoskop {city_name}. Źródła danych, metodologia i zasady korzystania."
    terms_page = make_page(main_html, terms_canonical, terms_title, terms_desc)
    write_page(docs / "regulamin" / "index.html", terms_page)
    sitemap_entries.append({"loc": terms_canonical, "changefreq": "yearly", "priority": "0.3"})

    # ════════════════════════════════════════════
    # 7. Fix main index.html canonical
    # ════════════════════════════════════════════
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
        print(f"  Fixed main canonical")

    # ════════════════════════════════════════════
    # 8. Generate sitemap.xml
    # ════════════════════════════════════════════
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

    total_urls = len(sitemap_entries) + 1
    print(f"  sitemap.xml: {total_urls} URLs")


def main():
    parser = argparse.ArgumentParser(description="Generate SEO pages for Radoskop")
    parser.add_argument("--base", required=True, help="Base directory containing radoskop-* city dirs")
    parser.add_argument("--city", default=None, help="Process only this city (e.g. radoskop-gdansk)")
    args = parser.parse_args()

    base = Path(args.base)

    if args.city:
        cities = [args.city]
    else:
        cities = sorted([
            d.name for d in base.iterdir()
            if d.is_dir() and d.name.startswith("radoskop-") and d.name != "radoskop"
        ])

    for city in cities:
        city_dir = base / city
        if city_dir.exists():
            print(f"\n=== {city} ===")
            process_city(city_dir)
        else:
            print(f"  Skipping {city}: not found")


if __name__ == "__main__":
    main()
