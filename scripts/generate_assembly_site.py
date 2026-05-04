#!/usr/bin/env python3
"""
Generator strony Radoskop dla pojedynczego sejmiku województwa.

Reuse pełnego template miasta (`radoskop/template_assembly/index.html`,
będącego kopią `radoskop/template/index.html` z podmienionymi frazami
"Rada Miasta" → "Sejmik Województwa"). Wstrzykuje config sejmika w te
same placeholdery co miasta (CITY_NAME, CITY_GENITIVE, CLUB_CSS,
CLUB_JS itd.), więc strona dostaje pełen routing SPA z podstronami
/profil/{slug}, /sesja/{n}, /glosowanie/{id}, /interpelacje, /budzet,
/kadencja/{id}.

Mapowanie config sejmika → placeholdery miasta:
  CITY_NAME       = voivodeship_name z .capitalize()  (np. "Mazowieckie")
  CITY_GENITIVE   = voivodeship_genitive              (np. "Mazowieckiego")
  SITE_TITLE      = site_title
  SITE_URL        = site_url
  SITE_DESCRIPTION = site_description
  BIP_URL/NAME    = bip_url / bip_name
  CLUB_CSS / JS   = generowane z clubs (jak w miastach)

Plus zapisuje sitemap.xml, robots.txt, CNAME.

Użycie:
    python3 generate_assembly_site.py \\
        --config radoskop/assemblies/mazowieckie/config.json \\
        --output radoskop/assemblies/mazowieckie/docs/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Pomocnicze: identyczne jak w generate_site.py (kluby, GA, AdSense)
# ---------------------------------------------------------------------------

def generate_club_css(clubs: dict[str, Any]) -> str:
    lines = []
    for name, cfg in clubs.items():
        bg = cfg.get("bg", "")
        color = cfg.get("color", "")
        lines.append(f".club-{name} {{ background:{bg}; color:{color}; }}")
    return "\n".join(lines)


def generate_club_js(clubs: dict[str, Any]) -> str:
    names = list(clubs.keys())
    if not clubs:
        return (
            "function clubColor(club) {\n  return 'var(--muted)';\n}\n"
            "function clubBg(club) {\n  return '#374151';\n}\n"
            "function clubClass(club) {\n  return 'club-unknown';"
        )

    chain = " : ".join(
        f"club === '{n}' ? '{c.get('color_var', c.get('color', 'var(--muted)'))}'"
        for n, c in clubs.items()
    )
    club_color = f"function clubColor(club) {{\n  return {chain} : 'var(--muted)';\n}}"

    chain_bg = " : ".join(
        f"club === '{n}' ? '{c.get('avatar_bg', c.get('color', '#374151'))}'"
        for n, c in clubs.items()
    )
    club_bg = f"function clubBg(club) {{\n  return {chain_bg} : '#374151';\n}}"

    names_js = "[" + ",".join(f"'{n}'" for n in names) + "]"
    club_class = (
        f"function clubClass(club) {{\n"
        f"  return {names_js}.includes(club) ? `club-${{club}}` : 'club-unknown';"
    )
    return f"{club_color}\n{club_bg}\n{club_class}"


def generate_adsense_snippet(pub_id: str) -> str:
    if not pub_id:
        return "<!-- No AdSense configured -->"
    return (
        f'<script async src="https://pagead2.googlesyndication.com/pagead/js/'
        f'adsbygoogle.js?client={pub_id}" crossorigin="anonymous"></script>'
    )


def generate_ga_snippet(ga_id: str) -> str:
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


def generate_sitemap(config: dict[str, Any]) -> str:
    site_url = config.get("site_url", "").rstrip("/")
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url>\n'
        f'    <loc>{site_url}/</loc>\n'
        f'    <lastmod>{today}</lastmod>\n'
        f'    <changefreq>daily</changefreq>\n'
        f'    <priority>1.0</priority>\n'
        f'  </url>\n'
        f'</urlset>\n'
    )


def generate_robots(config: dict[str, Any]) -> str:
    site_url = config.get("site_url", "").rstrip("/")
    return f'User-agent: *\nAllow: /\n\nSitemap: {site_url}/sitemap.xml\n'


def _generate_clubs_data_js(clubs: dict[str, Any], assignments: dict[str, str]) -> str:
    """Wstrzyknij definicje klubów i przypisania radny -> klub jako JS,
    oraz mapę globalną dostępną dla template miasta (clubAssignments).
    """
    return (
        "// Sejmik: definicje klubów i mapowanie radny -> klub.\n"
        "const SEJMIK_CLUBS = " + json.dumps(clubs, ensure_ascii=False) + ";\n"
        "const SEJMIK_CLUB_OF = " + json.dumps(assignments, ensure_ascii=False) + ";\n"
        "// Globalna mapa nazwisko -> klub (template miasta używa, jeśli\n"
        "// councilors[].club nie jest ustawione w data.json).\n"
        "window.clubAssignments = SEJMIK_CLUB_OF;"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Radoskop sejmik site")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--template", default=None,
        help="Domyślnie radoskop/template_assembly/index.html (kopia template miasta z podmienionymi frazami).",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    cfg_path = Path(args.config).resolve()
    if not cfg_path.is_file():
        print(f"ERROR: config nie istnieje: {cfg_path}", file=sys.stderr)
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    if cfg.get("samorzad_type") != "wojewodztwo":
        print(
            f"ERROR: samorzad_type='{cfg.get('samorzad_type')}', "
            "oczekuję 'wojewodztwo'. Dla miast użyj generate_site.py.",
            file=sys.stderr,
        )
        return 1

    repo_root = Path(__file__).resolve().parent.parent
    template_path = (
        Path(args.template).resolve()
        if args.template
        else repo_root / "template_assembly" / "index.html"
    )
    if not template_path.is_file():
        print(f"ERROR: brak templatu: {template_path}", file=sys.stderr)
        return 1
    template = template_path.read_text(encoding="utf-8")

    voiv_name = (cfg.get("voivodeship_name") or cfg.get("voivodeship_slug") or "").strip()
    voiv_gen = cfg.get("voivodeship_genitive", "").strip()

    # Template miasta oczekuje CITY_NAME i CITY_GENITIVE w formie nazwy
    # własnej (capitalized: "Gdańsk", "Gdańska"). W naszym configu sejmika
    # voivodeship_name/genitive są lowercase ("mazowieckie", "mazowieckiego"),
    # bo to przymiotniki. Capitalizujemy żeby fraza "Sejmik Województwa
    # Mazowieckiego" wyszła poprawnie.
    city_name = voiv_name.capitalize() if voiv_name else ""
    city_gen = voiv_gen.capitalize() if voiv_gen else ""

    replacements = {
        "{{CITY_NAME}}": city_name,
        "{{CITY_GENITIVE}}": city_gen,
        "{{SITE_TITLE}}": cfg.get("site_title", ""),
        "{{SITE_URL}}": cfg.get("site_url", "").rstrip("/"),
        "{{SITE_DESCRIPTION}}": cfg.get("site_description", ""),
        "{{BIP_URL}}": cfg.get("bip_url", ""),
        "{{BIP_NAME}}": cfg.get("bip_name", ""),
        "{{GITHUB_URL}}": cfg.get("github_url", "https://github.com/radoskoppl/radoskop"),
        "{{AUTHOR}}": cfg.get("author", ""),
        "{{GA_ID}}": cfg.get("ga_id", ""),
        "{{GA_SNIPPET}}": generate_ga_snippet(cfg.get("ga_id", "")),
        "{{ADSENSE_SNIPPET}}": generate_adsense_snippet(cfg.get("adsense_pub_id", "")),
        "{{CLUB_CSS}}": generate_club_css(cfg.get("clubs", {})),
        "{{CLUB_JS}}": generate_club_js(cfg.get("clubs", {})),
        "{{BUDGET_NOTE}}": cfg.get("budget_note", ""),
    }

    html = template
    for k, v in replacements.items():
        html = html.replace(k, v)

    # Dorzuć JS-ową mapę klubowości (template miasta nie ma placeholdera
    # na to, więc wstrzykujemy przed </body>).
    clubs_js = _generate_clubs_data_js(
        cfg.get("clubs", {}), cfg.get("club_assignments", {})
    )
    inject = f"<script>\n{clubs_js}\n</script>\n</body>"
    if "</body>" in html:
        html = html.replace("</body>", inject, 1)

    # Sanity check: pozostałe placeholdery
    remaining = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", html)))
    if remaining:
        print(f"WARNING: nierozwiązane placeholdery: {remaining}", file=sys.stderr)

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "index.html").write_text(html, encoding="utf-8")
    (output_dir / "sitemap.xml").write_text(generate_sitemap(cfg), encoding="utf-8")
    (output_dir / "robots.txt").write_text(generate_robots(cfg), encoding="utf-8")
    if cfg.get("cname"):
        (output_dir / "CNAME").write_text(cfg["cname"] + "\n", encoding="utf-8")

    # 404.html SPA fallback (z radoskop/404.html jeśli istnieje)
    spa_404 = repo_root / "404.html"
    if spa_404.is_file():
        import shutil as _sh
        _sh.copy2(spa_404, output_dir / "404.html")

    print(f"Wygenerowano stronę sejmiku: {output_dir}/index.html ({len(html)} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
