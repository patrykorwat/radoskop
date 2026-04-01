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


def generate_adsense_snippet(pub_id: str) -> str:
    """Generate static Google AdSense tag (must be in HTML for crawler verification)."""
    if not pub_id:
        return "<!-- No AdSense configured -->"
    return (
        f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={pub_id}" crossorigin="anonymous"></script>'
    )


def generate_ga_snippet(ga_id: str) -> str:
    """Generate Google Analytics snippet with cookie consent check."""
    if not ga_id:
        return "<!-- No analytics configured -->"
    return (
        f'<script>\n'
        f'window.dataLayer=window.dataLayer||[];\n'
        f'function gtag(){{dataLayer.push(arguments);}}\n'
        f'(function(){{\n'
        f'  var c=document.cookie.match(/(?:^|;\\s*)cookie_consent=([^;]*)/);\n'
        f'  if(c&&c[1]==="rejected"){{window["ga-disable-{ga_id}"]=true;return;}}\n'
        f'  if(!c||c[1]==="accepted"){{\n'
        f'    var s=document.createElement("script");\n'
        f'    s.async=true;s.src="https://www.googletagmanager.com/gtag/js?id={ga_id}";\n'
        f'    document.head.appendChild(s);\n'
        f'    gtag("js",new Date());gtag("config","{ga_id}");\n'
        f'  }}\n'
        f'}})();\n'
        f'</script>'
    )


def generate_sitemap(config: dict) -> str:
    """Generate sitemap.xml."""
    url = config["site_url"]
    entries = [
        f'  <url>\n    <loc>{url}/</loc>\n    <changefreq>weekly</changefreq>\n    <priority>1.0</priority>\n  </url>',
    ]
    if config.get("has_budget"):
        entries.append(f'  <url>\n    <loc>{url}/budzet</loc>\n    <changefreq>monthly</changefreq>\n    <priority>0.8</priority>\n  </url>')

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
        "{{GA_ID}}": config.get("ga_id", ""),
        "{{GA_SNIPPET}}": generate_ga_snippet(config.get("ga_id", "")),
        "{{ADSENSE_SNIPPET}}": generate_adsense_snippet(config.get("adsense_pub_id", "")),
        "{{CLUB_CSS}}": generate_club_css(config.get("clubs", {})),
        "{{CLUB_JS}}": generate_club_js(config.get("clubs", {})),
        "{{BUDGET_NOTE}}": config.get("budget_note", ""),
    }

    # Apply replacements
    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

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
