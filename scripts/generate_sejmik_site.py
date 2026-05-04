#!/usr/bin/env python3
"""
Generator strony Radoskop dla pojedynczego sejmiku województwa.

Analogiczny do `generate_site.py` (dla miast), ale używa templatu
`radoskop/template_sejmik/index.html` i mapuje config sejmiku
(samorzad_type=wojewodztwo) na placeholdery RADA_*, VOIVODESHIP_*.

Użycie:
    python3 generate_sejmik_site.py \
        --config radoskop/sejmiki/mazowieckie/config.json \
        --output radoskop/sejmiki/mazowieckie/docs/

W pipeline NAS uruchamiane po scrape_glosowania.py i scrape_interpelacje.py
(czyli po updacie kadencja-{id}.json i interpelacje.json).

Pisze do output/:
  - index.html (wygenerowany z templatu)
  - sitemap.xml
  - robots.txt
  - CNAME (jeśli config ma 'cname')
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _generate_clubs_data_js(clubs: dict[str, Any], assignments: dict[str, str]) -> str:
    """Wstrzyknij definicje klubów i przypisania radny -> klub jako JS."""
    return (
        "const CLUBS = " + json.dumps(clubs, ensure_ascii=False) + ";\n"
        "const CLUB_OF = " + json.dumps(assignments, ensure_ascii=False) + ";"
    )


def generate_ga_snippet(ga_id: str) -> str:
    if not ga_id:
        return "<!-- No analytics configured -->"
    return f'''<script>
window.dataLayer=window.dataLayer||[];
function gtag(){{dataLayer.push(arguments);}}
(function(){{
  var c=document.cookie.match(/(?:^|;\\s*)cookie_consent=([^;]*)/);
  if(c&&c[1]==="rejected"){{window["ga-disable-{ga_id}"]=true;return;}}
  if(!c||c[1]==="accepted"){{
    var s=document.createElement("script");
    s.async=true;s.src="https://www.googletagmanager.com/gtag/js?id={ga_id}";
    document.head.appendChild(s);
    gtag("js",new Date());gtag("config","{ga_id}");
  }}
}})();
</script>'''


def generate_sitemap(config: dict[str, Any]) -> str:
    site_url = config.get("site_url", "").rstrip("/")
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{site_url}/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
'''


def generate_robots(config: dict[str, Any]) -> str:
    site_url = config.get("site_url", "").rstrip("/")
    return f'''User-agent: *
Allow: /

Sitemap: {site_url}/sitemap.xml
'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Radoskop sejmik site from template + config")
    parser.add_argument("--config", required=True, help="Ścieżka do config.json sejmiku")
    parser.add_argument("--template", default=None,
                        help="Ścieżka do template_sejmik/index.html (domyślnie: auto-detect)")
    parser.add_argument("--output", required=True, help="Katalog docs/ wyjściowy")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        print(f"ERROR: Config not found: {config_path}", file=sys.stderr)
        return 1
    with config_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    if cfg.get("samorzad_type") != "wojewodztwo":
        print(
            f"ERROR: Config.samorzad_type='{cfg.get('samorzad_type')}', "
            "oczekuję 'wojewodztwo'. Użyj generate_site.py dla miast.",
            file=sys.stderr,
        )
        return 1

    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    template_path = (
        Path(args.template).resolve()
        if args.template
        else repo_root / "template_sejmik" / "index.html"
    )
    if not template_path.is_file():
        print(f"ERROR: Template not found: {template_path}", file=sys.stderr)
        return 1
    template = template_path.read_text(encoding="utf-8")

    # Mapowanie pól config -> placeholdery
    replacements = {
        "{{RADA_NAME}}": cfg.get("rada_name", ""),
        "{{RADA_GENITIVE}}": cfg.get("rada_name_genitive", ""),
        "{{VOIVODESHIP_NAME}}": cfg.get("voivodeship_name", cfg.get("voivodeship_slug", "")),
        "{{VOIVODESHIP_GENITIVE}}": cfg.get("voivodeship_genitive", ""),
        "{{CAPITAL}}": cfg.get("capital", ""),
        "{{COUNCILOR_COUNT}}": str(cfg.get("councilor_count", "")),
        "{{KADENCJA_ACTIVE}}": cfg.get("kadencja_active", ""),
        "{{SITE_TITLE}}": cfg.get("site_title", ""),
        "{{SITE_URL}}": cfg.get("site_url", "").rstrip("/"),
        "{{SITE_DESCRIPTION}}": cfg.get("site_description", ""),
        "{{SITE_DESCRIPTION_SHORT}}": cfg.get("site_description_short", cfg.get("site_description", "")),
        "{{BIP_URL}}": cfg.get("bip_url", ""),
        "{{BIP_NAME}}": cfg.get("bip_name", ""),
        "{{GITHUB_URL}}": cfg.get("github_url", "https://github.com/radoskoppl/radoskop"),
        "{{AUTHOR}}": cfg.get("author", ""),
        "{{GA_SNIPPET}}": generate_ga_snippet(cfg.get("ga_id", "")),
        "{{CLUBS_DATA_JS}}": _generate_clubs_data_js(
            cfg.get("clubs", {}), cfg.get("club_assignments", {})
        ),
    }

    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    # Sanity check: czy nic nie zostało nierozwiązane
    import re
    remaining = re.findall(r"\{\{[A-Z_]+\}\}", html)
    if remaining:
        print(f"WARNING: nierozwiązane placeholdery: {sorted(set(remaining))}", file=sys.stderr)

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "index.html").write_text(html, encoding="utf-8")
    (output_dir / "sitemap.xml").write_text(generate_sitemap(cfg), encoding="utf-8")
    (output_dir / "robots.txt").write_text(generate_robots(cfg), encoding="utf-8")

    cname = cfg.get("cname")
    if cname:
        (output_dir / "CNAME").write_text(cname + "\n", encoding="utf-8")

    print(f"Wygenerowano stronę sejmiku: {output_dir}/index.html ({len(html)} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
