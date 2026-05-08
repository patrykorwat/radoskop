#!/usr/bin/env python3
"""
Generate a Radoskop site instance from template + city config.

Usage:
    python generate_site.py --config ../radoskop-gdansk/config.json --output ../radoskop-gdansk/docs/
    python generate_site.py --config ../radoskop-warszawa/config.json --output ../radoskop-warszawa/docs/
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

# Lokalny import modułu i18n (ten sam katalog scripts/)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n import apply_locale  # noqa: E402


def generate_club_css(clubs: dict) -> str:
    """Generate CSS classes for clubs."""
    lines = []
    for name, cfg in clubs.items():
        lines.append(f".club-{name} {{ background:{cfg['bg']}; color:{cfg['color']}; }}")
    return "\n".join(lines)


def generate_club_js(clubs: dict) -> str:
    """Generate clubColor, clubBg, clubClass JS functions."""
    names = list(clubs.keys())

    if not clubs:
        # No clubs defined: return safe fallback functions
        # NOTE: clubClass is left without closing } — the template provides it
        return (
            "function clubColor(club) {\n  return 'var(--muted)';\n}\n"
            "function clubBg(club) {\n  return '#374151';\n}\n"
            "function clubClass(club) {\n  return 'club-unknown';"
        )

    # clubColor — uses color_var if available, falls back to color
    chain = " : ".join(
        f"club === '{n}' ? '{c.get('color_var', c['color'])}'"
        for n, c in clubs.items()
    )
    club_color = f"function clubColor(club) {{\n  return {chain} : 'var(--muted)';\n}}"

    # clubBg
    chain_bg = " : ".join(f"club === '{n}' ? '{c['avatar_bg']}'" for n, c in clubs.items())
    club_bg = f"function clubBg(club) {{\n  return {chain_bg} : '#374151';\n}}"

    # clubClass
    names_js = "[" + ",".join(f"'{n}'" for n in names) + "]"
    club_class = f"function clubClass(club) {{\n  return {names_js}.includes(club) ? `club-${{club}}` : 'club-unknown';"

    return f"{club_color}\n{club_bg}\n{club_class}"


# Self hosted Umami at stats.radoskop.pl. One website ID for the whole project,
# all subdomains (gdansk.radoskop.pl, bytom.radoskop.pl, ...) report into the same
# website entry. Cookieless, GDPR clean, no consent banner needed for the tracker.
UMAMI_WEBSITE_ID = "792c059f-c77e-4b4e-ad9c-31f4a7d5cfe4"
UMAMI_SCRIPT_URL = "https://stats.radoskop.pl/script.js"


def has_activity_data(output_dir: Path) -> bool:
    """Return True iff the city has any scraped activity worth listing in /aktualnosci/.

    Checks for actual votes/sessions/interpelacje, not just the skeleton data.json
    that scrape pipeline writes when a city has no scraped data yet.
    """
    data_path = output_dir / "data.json"
    if not data_path.exists():
        return False
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if data.get("_status") == "no_data":
        return False

    # Any kadencja-*.json with votes or sessions counts as activity.
    for k in data.get("kadencje", []):
        kid = k.get("id") if isinstance(k, dict) else k
        if not kid:
            continue
        kad_file = output_dir / f"kadencja-{kid}.json"
        if not kad_file.exists():
            continue
        try:
            kd = json.loads(kad_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if kd.get("votes") or kd.get("sessions"):
            return True

    # Or any scraped interpelacje.
    interp_path = output_dir / "interpelacje.json"
    if interp_path.exists():
        try:
            interp = json.loads(interp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            interp = None
        if isinstance(interp, list) and interp:
            return True
        if isinstance(interp, dict) and (interp.get("interpelacje") or interp.get("items")):
            return True

    return False


def generate_aktualnosci_button(output_dir: Path) -> str:
    """Render the Aktualności tab link only when the city has data.

    Without scraped activity, generate_feed.py produces nothing under
    /aktualnosci/, so the link would 404. Hide the button instead.
    """
    if not has_activity_data(output_dir):
        return ""
    return '        <a href="/aktualnosci/" class="tab" style="text-decoration:none">Aktualności</a>'


def generate_ga_snippet(_legacy_ga_id: str = "") -> str:
    """Emit the Umami tracker tag.

    Function name kept for now so existing build callers and the {{GA_SNIPPET}}
    template placeholder do not need to change. The argument is ignored;
    kept so callers passing a GA id do not raise.
    """
    return (
        f'<script async defer '
        f'data-website-id="{UMAMI_WEBSITE_ID}" '
        f'src="{UMAMI_SCRIPT_URL}"></script>'
    )


def generate_sitemap(config: dict) -> str:
    """Generate sitemap.xml."""
    url = config["site_url"]
    entries = [
        f'  <url>\n    <loc>{url}/</loc>\n    <changefreq>weekly</changefreq>\n    <priority>1.0</priority>\n  </url>',
    ]
    if config.get("has_budget"):
        entries.append(f'  <url>\n    <loc>{url}/budzet</loc>\n    <changefreq>monthly</changefreq>\n    <priority>0.8</priority>\n  </url>')

    # Mój radny page
    entries.append(f'  <url>\n    <loc>{url}/moj-radny/</loc>\n    <changefreq>weekly</changefreq>\n    <priority>0.9</priority>\n  </url>')

    # Aktualności page
    entries.append(f'  <url>\n    <loc>{url}/aktualnosci/</loc>\n    <changefreq>daily</changefreq>\n    <priority>0.8</priority>\n  </url>')

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries) + "\n"
        '</urlset>\n'
    )


def generate_robots(config: dict) -> str:
    """Generate robots.txt."""
    return (
        f"User-agent: *\n"
        f"Allow: /\n"
        f"Disallow: /*.json$\n"
        f"Disallow: /*?p=\n"
        f"\n"
        f"Sitemap: {config['site_url']}/sitemap.xml\n"
    )


def main():
    parser = argparse.ArgumentParser(description="Generate Radoskop site from template + config")
    parser.add_argument("--config", required=True, help="Path to city config.json")
    parser.add_argument("--template", default=None, help="Path to template/index.html (default: auto-detect)")
    parser.add_argument("--output", required=True, help="Output docs/ directory")
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Find template
    script_dir = Path(__file__).parent
    template_dir = script_dir.parent / "template"
    template_path = Path(args.template) if args.template else template_dir / "index.html"

    if not template_path.exists():
        print(f"ERROR: Template not found: {template_path}")
        sys.exit(1)

    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    # Build replacements
    replacements = {
        "{{CITY_NAME}}": config["city_name"],
        "{{CITY_GENITIVE}}": config["city_genitive"],
        "{{SITE_TITLE}}": config["site_title"],
        "{{SITE_URL}}": config["site_url"],
        "{{SITE_DESCRIPTION}}": config["site_description"],
        "{{BIP_URL}}": config["bip_url"],
        "{{BIP_NAME}}": config["bip_name"],
        "{{GITHUB_URL}}": config["github_url"],
        "{{AUTHOR}}": config["author"],
        "{{GA_SNIPPET}}": generate_ga_snippet(),
        "{{CLUB_CSS}}": generate_club_css(config.get("clubs", {})),
        "{{CLUB_JS}}": generate_club_js(config.get("clubs", {})),
        "{{BUDGET_NOTE}}": config.get("budget_note", ""),
        "{{AKTUALNOSCI_BUTTON}}": generate_aktualnosci_button(Path(args.output)),
    }

    # Apply replacements
    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    # Lokalizacja UI dla miast spoza Polski (config.locale == "en").
    # Polskie miasta nie mają tego pola → no-op.
    locale = config.get("locale", "pl")
    html = apply_locale(html, locale)

    # Currency: polskie miasta używają zł (default w template), Praga
    # używa Kč. Zamieniamy konkretnie na końcówce template literal w
    # tabeli budżetu, żeby nie ruszyć cen raportów (te zostają w zł, bo
    # płatności są w zł niezależnie od miasta).
    currency = config.get("currency", "zł")
    if currency != "zł":
        html = html.replace("${fmtM(c.amount)} zł", f"${{fmtM(c.amount)}} {currency}")

    # Ukryj taby których miasto nie ma. Domyślnie wszystkie taby są
    # widoczne w template, więc miasto bez interpelacji (np. Praga,
    # Czechy nie mają tego mechanizmu prawnego) musi je explicit ukryć.
    # Robimy to przez wstrzyknięcie style w <head>, dodawanie display:none
    # przez data-tab atrybut i id przedziału.
    hide_css = []
    if not config.get("has_interpelacje", True):
        hide_css.append('[data-tab="interpelacje"]{display:none!important}')
    if not config.get("has_budget", True):
        hide_css.append('[data-tab="budget"]{display:none!important}')
    if not config.get("has_voting_data", True):
        # DACH miasta (Berlin, Wien, Hamburg) nie mają imiennych głosowań,
        # ukrywamy tab "Głosowania/Abstimmungen". Tab "Sesje" zostaje widoczny
        # bo pokazuje frekwencję i aktywność mówczą.
        hide_css.append('[data-tab="votes"]{display:none!important}')
    if hide_css:
        injected = "<style>" + "".join(hide_css) + "</style>"
        # Wstrzykuje przed </head>. Jeśli z jakiegoś powodu nie ma
        # </head> (template był obcięty), wstaw przed pierwszą sekcją.
        if "</head>" in html:
            html = html.replace("</head>", injected + "</head>", 1)
        else:
            html = injected + html

    # Check for remaining placeholders
    import re
    remaining = re.findall(r'\{\{[A-Z_]+\}\}', html)
    if remaining:
        print(f"WARNING: Unresolved placeholders: {set(remaining)}")

    # Write output
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # index.html
    with open(output_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(html)

    # 404.html
    spa_404 = script_dir.parent / "404.html"
    if spa_404.exists():
        shutil.copy2(spa_404, output_dir / "404.html")

    # sitemap.xml
    with open(output_dir / "sitemap.xml", "w", encoding="utf-8") as f:
        f.write(generate_sitemap(config))

    # robots.txt
    with open(output_dir / "robots.txt", "w", encoding="utf-8") as f:
        f.write(generate_robots(config))

    # CNAME
    if config.get("cname"):
        with open(output_dir / "CNAME", "w") as f:
            f.write(config["cname"] + "\n")

    print(f"Generated site for {config['city_name']}:")
    print(f"  index.html  → {output_dir / 'index.html'}")
    print(f"  404.html    → {output_dir / '404.html'}")
    print(f"  sitemap.xml → {output_dir / 'sitemap.xml'}")
    print(f"  robots.txt  → {output_dir / 'robots.txt'}")
    if config.get("cname"):
        print(f"  CNAME       → {output_dir / 'CNAME'}")


if __name__ == "__main__":
    main()
