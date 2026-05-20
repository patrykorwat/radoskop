#!/usr/bin/env python3
"""
Generator strony Radoskop dla pojedynczego sejmiku województwa.

Reuse kanonicznego template miasta (`radoskop/template/index.html`) plus
in-place transformacja "Rada Miasta" → "Sejmik Województwa" przez
`transform_template_for_assembly()`. Wstrzykuje config sejmika w te same
placeholdery co miasta (CITY_NAME, CITY_GENITIVE, CLUB_CSS, CLUB_JS itd.),
więc strona dostaje pełen routing SPA z podstronami /profile/{slug},
/session/{n}, /vote/{id}, /interpellations, /budget, /term/{id} (po migracji
2026-05 wszystkie URL slugi są angielskie).

Stary `radoskop/template_assembly/` był osobną kopią template które drifowała
sprzed aktualizacji miast (brak anti-FOUC theme switch, brak topbar nav,
stary kadencja-bar). Usunięty 2026-05-18, teraz jeden źródłowy template
dla wszystkich poziomów samorządu.

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

# Reużywamy apply_english_paths z generate_site.py — sejmiki używają tego
# samego kanonicznego template/index.html co miasta i muszą po migracji
# 2026-05 mieć angielskie URL slugi tak samo jak miasta.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_site import apply_english_paths  # noqa: E402


# ---------------------------------------------------------------------------
# Transformacja template miasta → template sejmiku
# ---------------------------------------------------------------------------

def transform_template_for_assembly(html: str, kind: str = "wojewodztwo") -> str:
    """Zamień frazy "Rada Miasta" → "Sejmik Województwa" lub "Landtag".

    Generuje template sejmiku/landu in-place z kanonicznego radoskop/template/
    index.html, eliminuje potrzebę utrzymywania osobnej kopii
    radoskop/template_assembly/ (która drifowała sprzed aktualizacji
    miast — anti-FOUC, topbar, kadencja-bar pill style, etc.).

    Zachowuje wszystkie placeholdery {{CITY_NAME}}, {{CITY_GENITIVE}},
    {{CLUB_CSS}} itd. — tylko substytuuje statyczne frazy.

    kind='wojewodztwo' (default): polski sejmik wojewódzki, odmiany przez
        przypadki: Rada Miasta → Sejmik Województwa.
    kind='land': niemiecki Landtag (jednolicie "Landtag" we wszystkich
        przypadkach po polsku, bo w niemieckim brak odmiany do uzbrojenia).
    """
    if kind == "land":
        # Wszystkie polskie odmiany "Rada Miasta" idą na "Landtag", bez
        # odmieniania (Landtag w niemieckim się nie odmienia). Plus odmiana
        # CITY_NAME na CITY_GENITIVE jest robiona przez placeholdery niżej.
        # Dodatkowe frazy bez "Rada" prefix: "radni Miasta {{CITY_GENITIVE}}"
        # w copy/og description, "rada miasta" w meta keywords.
        replacements = [
            # Z prefixem "Rada"
            ("Rada Miasta", "Landtag"),
            ("rada miasta", "landtag"),
            ("Rady Miasta", "Landtagu"),
            ("rady miasta", "landtagu"),
            ("Radzie Miasta", "Landtagowi"),
            ("Radę Miasta", "Landtag"),
            ("Radą Miasta", "Landtagiem"),
            # "radni Miasta {{CITY_GENITIVE}}" → "posłowie Landtagu {{CITY_GENITIVE}}"
            ("radni Miasta", "posłowie Landtagu"),
            ("radnych Miasta", "posłów Landtagu"),
            ("radnymi Miasta", "posłami Landtagu"),
            # Schema.org GovernmentOrganization keywords
            (", rada miasta, ", ", landtag, "),
        ]
    else:
        replacements = [
            # Mianownik
            ("Rada Miasta", "Sejmik Województwa"),
            ("rada miasta", "sejmik województwa"),
            # Dopełniacz (najczęstszy — meta description, og:description)
            ("Rady Miasta", "Sejmiku Województwa"),
            ("rady miasta", "sejmiku województwa"),
            # Celownik
            ("Radzie Miasta", "Sejmikowi Województwa"),
            # Biernik
            ("Radę Miasta", "Sejmik Województwa"),
            # Narzędnik
            ("Radą Miasta", "Sejmikiem Województwa"),
            # Miejscownik (w Radzie Miasta — pokryte przez Celownik wzorzec)
            # Plus warianty z innymi przyimkami
        ]
    for old, new in replacements:
        html = html.replace(old, new)
    return html


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


# Self hosted Umami at stats.radoskop.pl. Same website ID as the city sites;
# assembly pages report into the same entry.
UMAMI_WEBSITE_ID = "792c059f-c77e-4b4e-ad9c-31f4a7d5cfe4"
UMAMI_SCRIPT_URL = "https://stats.radoskop.pl/script.js"


def has_activity_data(output_dir: Path) -> bool:
    """Return True iff the assembly has any scraped activity (votes/sessions/interpelacje).

    Mirror of the city version; assemblies use the same docs/ layout.
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
    """Render the news tab link only when the assembly has data.

    Po migracji 2026-05 URL slug to /news/, niezależnie od locale (sejmik
    jest PL ale wszystkie miasta używają już angielskich slugów).
    """
    if not has_activity_data(output_dir):
        return ""
    return '        <a href="/news/" class="tab" style="text-decoration:none">Aktualności</a>'


def generate_ga_snippet(_legacy_ga_id: str = "") -> str:
    """Emit the Umami tracker tag (function name preserved for backwards compat)."""
    return (
        f'<script async defer '
        f'data-website-id="{UMAMI_WEBSITE_ID}" '
        f'src="{UMAMI_SCRIPT_URL}"></script>'
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
        help="Domyślnie radoskop/template/index.html (kanoniczny template miast). "
             "Frazy 'Rada Miasta' są dynamicznie transformowane na 'Sejmik Województwa' "
             "w transform_template_for_assembly(). Stary radoskop/template_assembly/ "
             "był usunięty 2026-05-18 bo nie nadążał za zmianami w template/index.html.",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    cfg_path = Path(args.config).resolve()
    if not cfg_path.is_file():
        print(f"ERROR: config nie istnieje: {cfg_path}", file=sys.stderr)
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    # Sejmik wojewódzki (PL) plus Landtag niemiecki (DE) używają tego samego
    # renderera SPA. Land to też assembly poziomu regionalnego, schemat
    # kadencja/sesje/głosowania jest identyczny. Dla miast użyj generate_site.py.
    accepted = {"wojewodztwo", "land"}
    if cfg.get("samorzad_type") not in accepted:
        print(
            f"ERROR: samorzad_type='{cfg.get('samorzad_type')}', "
            f"oczekuję jednego z {sorted(accepted)}. Dla miast użyj generate_site.py.",
            file=sys.stderr,
        )
        return 1

    repo_root = Path(__file__).resolve().parent.parent
    template_path = (
        Path(args.template).resolve()
        if args.template
        else repo_root / "template" / "index.html"
    )
    if not template_path.is_file():
        print(f"ERROR: brak templatu: {template_path}", file=sys.stderr)
        return 1
    template = template_path.read_text(encoding="utf-8")
    samorzad_kind = cfg.get("samorzad_type", "wojewodztwo")
    # Transform "Rada Miasta" → "Sejmik Województwa" (PL) lub "Landtag" (DE)
    # w wszystkich miejscach (meta tagi, copy strony, schema.org
    # GovernmentOrganization). Eliminuje potrzebę utrzymywania osobnej kopii
    # template_assembly/.
    template = transform_template_for_assembly(template, kind=samorzad_kind)

    voiv_name = (cfg.get("voivodeship_name") or cfg.get("voivodeship_slug") or "").strip()
    voiv_gen = cfg.get("voivodeship_genitive", "").strip()

    if samorzad_kind == "land":
        # Niemiecki land: voivodeship_name jest już własną nazwą
        # ("Mecklenburg-Vorpommern"), nie polskim przymiotnikiem. Nie
        # capitalize() bo zepsułby dash-łączone słowa (Mecklenburg-Vorpommern
        # → Mecklenburg-vorpommern). Genitive z DE config już wyrażony
        # poprawnie (Mecklenburg-Vorpommerns).
        city_name = voiv_name
        city_gen = voiv_gen or voiv_name
    else:
        # Template miasta oczekuje CITY_NAME i CITY_GENITIVE w formie nazwy
        # własnej (capitalized: "Gdańsk", "Gdańska"). W naszym configu sejmika
        # voivodeship_name/genitive są lowercase ("mazowieckie",
        # "mazowieckiego"), bo to przymiotniki. Capitalizujemy żeby fraza
        # "Sejmik Województwa Mazowieckiego" wyszła poprawnie.
        city_name = voiv_name.capitalize() if voiv_name else ""
        city_gen = voiv_gen.capitalize() if voiv_gen else ""

    # Apex domain zależy od country: PL sejmiki → radoskop.pl, niemieckie
    # landy → radoskop.eu (sister apex dla zagranicznych jednostek).
    is_foreign = (cfg.get("country") or "pl").lower() != "pl"
    if is_foreign:
        root_host = "radoskop.eu"
        root_url = "https://radoskop.eu"
        default_sub_apex = "radoskop.eu"
    else:
        root_host = "radoskop.pl"
        root_url = "https://radoskop.pl"
        default_sub_apex = "radoskop.pl"

    replacements = {
        # Sejmiki/landy nie używają disclaimera per-radny — wszystkie polskie
        # sejmiki mają imienne głosowania, Landtag MV ma per-Abgeordneten.
        # Placeholder pusty żeby template nie miał {{...}} leftover.
        "{{VOTE_DATA_DISCLAIMER}}": "",
        "{{CITY_NAME}}": city_name,
        "{{CITY_GENITIVE}}": city_gen,
        "{{SITE_TITLE}}": cfg.get("site_title", ""),
        "{{SITE_URL}}": cfg.get("site_url", "").rstrip("/"),
        "{{SITE_DESCRIPTION}}": cfg.get("site_description", ""),
        "{{BIP_URL}}": cfg.get("bip_url", ""),
        "{{BIP_NAME}}": cfg.get("bip_name", ""),
        "{{GITHUB_URL}}": cfg.get("github_url", "https://github.com/radoskoppl/radoskop"),
        "{{AUTHOR}}": cfg.get("author", ""),
        "{{GA_SNIPPET}}": generate_ga_snippet(),
        "{{CLUB_CSS}}": generate_club_css(cfg.get("clubs", {})),
        "{{CLUB_JS}}": generate_club_js(cfg.get("clubs", {})),
        "{{BUDGET_NOTE}}": cfg.get("budget_note", ""),
        "{{AKTUALNOSCI_BUTTON}}": generate_aktualnosci_button(Path(args.output)),
        # Apex domain — PL na .pl, DE na .eu (sister TLD)
        "{{ROOT_HOST}}": root_host,
        "{{ROOT_URL}}": root_url,
        "{{EXAMPLE_SUBDOMAIN}}": cfg.get("cname") or f"{cfg.get('voivodeship_slug','')}.{default_sub_apex}",
        # Capability flags — sejmik zawsze ma imienne głosowania
        "{{HAS_VOTING_DATA}}": "true" if cfg.get("has_voting_data", True) else "false",
        "{{HAS_SPEAKER_ACTIVITY}}": "true" if cfg.get("has_speaker_activity", False) else "false",
        # Impressum/Pressekodex — DE-only, dla PL puste. Tu też puste, ale
        # docelowo dla landtagu MV potrzebne (Telemediengesetz §5).
        "{{IMPRESSUM_HTML}}": "",
        "{{IMPRESSUM_FOOTER_LINK}}": "",
        "{{PRESSEKODEX_NOTICE}}": "",
    }

    html = template
    for k, v in replacements.items():
        html = html.replace(k, v)

    # Migracja 2026-05: angielskie URL slugi dla wszystkich subsites,
    # włączając sejmiki. Worker robi 301 z polskich URL-i. Mapping
    # spójny z miastami przez wspólny apply_english_paths.
    html = apply_english_paths(html)

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
