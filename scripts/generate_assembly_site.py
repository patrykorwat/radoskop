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
from generate_site import apply_english_paths, assemble_template, build_seo_content  # noqa: E402
from landing_strings import catalog as landing_catalog  # noqa: E402


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
    kind='powiat': polska rada powiatu. "Rada" zostaje, zmienia się tylko
        człon "Miasta" → "Powiatu" (dopełniacz w każdej frazie), więc
        odmiany przypadków przechodzą naturalnie (Radzie Miasta → Radzie
        Powiatu). CITY_GENITIVE = przymiotnik powiatu w dopełniaczu
        ("Tatrzańskiego"), fraza "Rada Powiatu {{CITY_GENITIVE}}" składa
        się poprawnie.
    """
    if kind == "powiat":
        replacements = [
            ("Rada Miasta", "Rada Powiatu"),
            ("rada miasta", "rada powiatu"),
            ("Rady Miasta", "Rady Powiatu"),
            ("rady miasta", "rady powiatu"),
            ("Radzie Miasta", "Radzie Powiatu"),
            ("Radę Miasta", "Radę Powiatu"),
            ("Radą Miasta", "Radą Powiatu"),
            # "radni Miasta {{CITY_GENITIVE}}" → "radni Powiatu Tatrzańskiego"
            ("radni Miasta", "radni Powiatu"),
            ("radnych Miasta", "radnych Powiatu"),
            ("radnymi Miasta", "radnymi Powiatu"),
        ]
        for old, new in replacements:
            html = html.replace(old, new)
        return html
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
        # clubClass domknięte — szablon nie dokleja już `}` po {{CLUB_JS}}
        # (zmiana przy splicie assetów; spójne z generate_site.py).
        return (
            "function clubColor(club) {\n  return 'var(--muted)';\n}\n"
            "function clubBg(club) {\n  return '#374151';\n}\n"
            "function clubClass(club) {\n  return 'club-unknown';\n}"
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
        f"  return {names_js}.includes(club) ? `club-${{club}}` : 'club-unknown';\n}}"
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


def generate_sitemap(config: dict[str, Any], has_oversight: bool = False) -> str:
    site_url = config.get("site_url", "").rstrip("/")
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    extra = ""
    if has_oversight:
        extra = (
            f'  <url>\n'
            f'    <loc>{site_url}/division/</loc>\n'
            f'    <lastmod>{today}</lastmod>\n'
            f'    <changefreq>monthly</changefreq>\n'
            f'    <priority>0.5</priority>\n'
            f'  </url>\n'
        )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url>\n'
        f'    <loc>{site_url}/</loc>\n'
        f'    <lastmod>{today}</lastmod>\n'
        f'    <changefreq>daily</changefreq>\n'
        f'    <priority>1.0</priority>\n'
        f'  </url>\n'
        f'{extra}'
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

def _osc(s: Any) -> str:
    s = str(s if s is not None else "")
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def generate_oversight_button(cfg: dict[str, Any], tree: dict[str, Any]) -> str:
    """Link nawigacji do podstrony Podział administracyjny.

    Tylko dla polskich sejmików, których kod TERYT województwa jest w drzewie i
    ma powiaty. Land DE (MV) nie ma drzewa powiatów, więc nic nie zwraca.
    Uwaga nazewnictwa: NIE "nadzór" — sejmik nie sprawuje nadzoru nad JST (to
    wojewoda i RIO). Strona pokazuje podział terytorialny regionu.
    """
    code = cfg.get("teryt")
    if not code or code not in tree or not tree[code].get("powiaty"):
        return ""
    return ('        <a href="/division/" class="tab" '
            'style="text-decoration:none">Podział administracyjny</a>')


def build_oversight_html(woj: dict[str, Any], coverage: dict[str, Any],
                         cfg: dict[str, Any]) -> str:
    """Standalone strona: drzewo powiatów i gmin województwa (teryt_tree),
    miasta na prawach powiatu wyróżnione, jednostki monitorowane podlinkowane."""
    cov_local = coverage.get("local", {})
    cov_district = coverage.get("district", {})
    site_url = cfg.get("site_url", "").rstrip("/")
    rada = cfg.get("rada_name", "Sejmik")
    wname = woj["name"]

    def gmina_li(g: dict) -> str:
        ent = cov_local.get(g["teryt"])
        nm = (f'<a href="{_osc(ent["url"])}">{_osc(g["name"])}</a> '
              '<span class="badge rad">Radoskop</span>') if ent else _osc(g["name"])
        return (f'<li><span class="gmina">{nm}</span> '
                f'<span class="rodz">{_osc(g.get("rodzaj", ""))}</span></li>')

    def powiat_block(p: dict) -> str:
        grodzki = p.get("grodzki")
        nm = p["name"].replace("powiat ", "") if grodzki else p["name"]
        typ = "miasto na prawach powiatu" if grodzki else "powiat"
        ents = cov_district.get(p["teryt"]) or []
        cov_link = ""
        if ents:
            links = ", ".join(f'<a href="{_osc(e["url"])}">{_osc(e["name"])}</a>'
                              for e in ents)
            cov_link = f' <span class="badge rad">Radoskop: {links}</span>'
        gminy = sorted(p["gminy"].values(), key=lambda g: g["name"])
        gm = "".join(gmina_li(g) for g in gminy)
        cls = "powiat grodzki" if grodzki else "powiat"
        return (f'<details class="{cls}"><summary>'
                f'<span class="pnm">{_osc(nm)}</span>'
                f'<span class="ptyp">{_osc(typ)}</span>'
                f'<span class="cnt">{len(gminy)} gmin</span>{cov_link}</summary>'
                f'<ul class="gminy">{gm}</ul></details>')

    pw = list(woj["powiaty"].values())
    grodzkie = sorted((p for p in pw if p.get("grodzki")), key=lambda p: p["name"])
    ziemskie = sorted((p for p in pw if not p.get("grodzki")), key=lambda p: p["name"])
    total_gmin = sum(len(p["gminy"]) for p in pw)

    return f"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Podział administracyjny — {_osc(rada)}</title>
<meta name="description" content="Podział administracyjny województwa {_osc(wname)}: powiaty i gminy. Radoskop.">
<link rel="canonical" href="{_osc(site_url)}/division/">
<style>
  :root{{--accent:#4f46e5;--amber:#b45309;--muted:#6b7280;--border:#e5e7eb;--text:#111;--bg:#fff;}}
  *{{box-sizing:border-box;}}
  body{{margin:0;font:15px/1.5 system-ui,-apple-system,sans-serif;color:var(--text);background:var(--bg);}}
  .top{{border-bottom:1px solid var(--border);padding:10px 18px;}}
  .top a{{color:var(--accent);text-decoration:none;font-size:.86rem;}}
  .wrap{{max-width:860px;margin:0 auto;padding:22px 18px 60px;}}
  h1{{font-size:1.5rem;margin:.2rem 0 .4rem;}}
  .lead{{color:var(--muted);margin:0 0 18px;}}
  .stats{{display:flex;gap:18px;flex-wrap:wrap;font-size:.88rem;margin:0 0 22px;}}
  .stats b{{color:var(--text);}} .stats span{{color:var(--muted);}}
  h2{{font-size:1.05rem;margin:26px 0 10px;border-bottom:1px solid var(--border);padding-bottom:6px;}}
  details.powiat{{border:1px solid var(--border);border-radius:9px;margin:7px 0;padding:2px 12px;}}
  details.grodzki{{border-color:rgba(180,83,9,.45);background:rgba(245,158,11,.05);}}
  summary{{cursor:pointer;display:flex;align-items:center;gap:10px;padding:9px 2px;list-style:none;}}
  summary::-webkit-details-marker{{display:none;}}
  .pnm{{font-weight:600;}}
  .ptyp{{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.03em;}}
  details.grodzki .ptyp{{color:var(--amber);font-weight:700;}}
  .cnt{{font-size:.78rem;color:var(--muted);margin-left:auto;}}
  .badge{{font-size:.7rem;padding:2px 7px;border-radius:999px;}}
  .badge.rad{{background:rgba(79,70,229,.12);color:var(--accent);font-weight:600;}}
  ul.gminy{{margin:0 0 10px;padding:4px 0 4px 4px;list-style:none;columns:2;column-gap:26px;}}
  ul.gminy li{{break-inside:avoid;padding:2px 0;font-size:.9rem;border-bottom:1px dotted var(--border);}}
  .gmina a{{color:var(--accent);text-decoration:none;}} .gmina a:hover{{text-decoration:underline;}}
  .rodz{{font-size:.72rem;color:var(--muted);}}
  @media(max-width:560px){{ul.gminy{{columns:1;}}}}
</style></head><body>
<div class="top"><a href="/">← {_osc(rada)}</a></div>
<div class="wrap">
<h1>Podział administracyjny</h1>
<p class="lead">Powiaty i gminy w województwie {_osc(wname)}. Jednostki monitorowane przez
Radoskop są podlinkowane. Nadzór nad nimi sprawują wojewoda i regionalna izba
obrachunkowa, nie sejmik.</p>
<div class="stats">
  <span><b>{len(ziemskie)}</b> powiatów ziemskich</span>
  <span><b>{len(grodzkie)}</b> miast na prawach powiatu</span>
  <span><b>{total_gmin}</b> gmin</span>
</div>
<h2>Miasta na prawach powiatu ({len(grodzkie)})</h2>
{''.join(powiat_block(p) for p in grodzkie)}
<h2>Powiaty ziemskie ({len(ziemskie)})</h2>
{''.join(powiat_block(p) for p in ziemskie)}
</div></body></html>"""


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
    accepted = {"wojewodztwo", "land", "powiat"}
    if cfg.get("samorzad_type") not in accepted:
        print(
            f"ERROR: samorzad_type='{cfg.get('samorzad_type')}', "
            f"oczekuję jednego z {sorted(accepted)}. Dla miast użyj generate_site.py.",
            file=sys.stderr,
        )
        return 1

    repo_root = Path(__file__).resolve().parent.parent
    # Drzewo + pokrycie jednostek do podstrony Nadzór administracyjny (opcjonalne).
    _units = repo_root / "docs" / "units"
    try:
        _tree = json.loads((_units / "teryt_tree.json").read_text(encoding="utf-8"))
        _cov = json.loads((_units / "coverage.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _tree, _cov = {}, {}
    template_path = (
        Path(args.template).resolve()
        if args.template
        else repo_root / "template" / "index.html"
    )
    if not template_path.is_file():
        print(f"ERROR: brak templatu: {template_path}", file=sys.stderr)
        return 1
    template = template_path.read_text(encoding="utf-8")
    # Sklej partiale (CSS/head/auth-modal) PRZED transform/locale — transform
    # podmienia "Rada Miasta"→"Sejmik" m.in. w head (JSON-LD, meta), więc musi
    # widzieć pełny HTML. Reconstrukcja bajt-w-bajt.
    template = assemble_template(template, template_path.parent)
    samorzad_kind = cfg.get("samorzad_type", "wojewodztwo")
    # Transform "Rada Miasta" → "Sejmik Województwa" (PL) lub "Landtag" (DE)
    # w wszystkich miejscach (meta tagi, copy strony, schema.org
    # GovernmentOrganization). Eliminuje potrzebę utrzymywania osobnej kopii
    # template_assembly/.
    template = transform_template_for_assembly(template, kind=samorzad_kind)

    # Powiat trzyma nazwę w district_name/district_genitive (przymiotnik
    # lowercase: "tatrzański"/"tatrzańskiego"), sejmik w voivodeship_*.
    # Dalej wspólna ścieżka: capitalize w gałęzi else niżej.
    if samorzad_kind == "powiat":
        voiv_name = (cfg.get("district_name") or cfg.get("slug") or "").strip()
        voiv_gen = cfg.get("district_genitive", "").strip()
    else:
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
    elif samorzad_kind == "powiat":
        # Przymiotniki powiatów bywają wieloczłonowe ("warszawski zachodni",
        # "bieruńsko-lędziński"). W nazwie organu każdy człon jest
        # kapitalizowany ("Rada Powiatu Warszawskiego Zachodniego",
        # "Bieruńsko-Lędzińskiego"), więc kapitalizacja per słowo i per
        # człon dywizowy, nie .capitalize() całości.
        def _cap_powiat(s: str) -> str:
            return " ".join("-".join(p[:1].upper() + p[1:] for p in w.split("-"))
                            for w in s.split())
        city_name = _cap_powiat(voiv_name)
        city_gen = _cap_powiat(voiv_gen)
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

    # Reguły kategoryzacji per locale. Polskie sejmiki → pl, Landtag MV → de.
    from vote_categories import generate_cat_rules_js, generate_vote_cats_labels_js
    _cat_rules_js = generate_cat_rules_js(cfg.get("locale", "pl"))
    _vote_cats_labels_js = generate_vote_cats_labels_js(cfg.get("locale", "pl"))

    # Assembly slug = katalog assemblies/{slug}/. Mappuje na {{CITY_SLUG}}
    # tak jak w generate_site.py (template wspólny). JS subscribe-na-alerty
    # wysyła ten slug do backendu — bez substytucji DB dostaje literał
    # "{{CITY_SLUG}}" i alerty nie znajdują match'u.
    # Powiat: własny slug PRZED voivodeship_slug, bo voivodeship_slug w jego
    # configu to rodzic w hierarchii (np. malopolskie), nie tożsamość.
    if samorzad_kind == "powiat":
        assembly_slug = cfg.get("slug") or cfg_path.parent.name
    else:
        assembly_slug = cfg.get("voivodeship_slug") or cfg.get("slug") or cfg_path.parent.name

    _lcat_asm = landing_catalog((cfg.get("locale") or "pl").lower(),
                                samorzad_type=samorzad_kind)
    replacements = {
        "{{CAT_RULES_JS}}": _cat_rules_js,
        "{{VOTE_CATS_LABELS_JS}}": _vote_cats_labels_js,
        # Sejmiki/landy nie używają disclaimera per-radny — wszystkie polskie
        # sejmiki mają imienne głosowania, Landtag MV ma per-Abgeordneten.
        # Placeholder pusty żeby template nie miał {{...}} leftover.
        "{{VOTE_DATA_DISCLAIMER}}": "",
        "{{CITY_SLUG}}": assembly_slug,
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
        "{{AKTUALNOSCI_BUTTON}}": (generate_aktualnosci_button(Path(args.output))
                                   + generate_oversight_button(cfg, _tree)),
        # Apex domain — PL na .pl, DE na .eu (sister TLD)
        "{{ROOT_HOST}}": root_host,
        "{{ROOT_URL}}": root_url,
        "{{EXAMPLE_SUBDOMAIN}}": cfg.get("cname") or f"{cfg.get('voivodeship_slug','')}.{default_sub_apex}",
        # Capability flags — sejmik zawsze ma imienne głosowania, są radni.
        "{{HAS_VOTING_DATA}}": "true" if cfg.get("has_voting_data", True) else "false",
        "{{HAS_SPEAKER_ACTIVITY}}": "true" if cfg.get("has_speaker_activity", False) else "false",
        # Sejmiki nigdy nie mają trybu faction/show_of_hands bez per-radny,
        # więc COUNCILOR_ROSTER_MODE zawsze false (sejmiki z imiennymi głosami
        # mają normalny ranking radnych, nie roster z profiles.json).
        "{{COUNCILOR_ROSTER_MODE}}": "false",
        "{{LANDING_I18N}}": json.dumps(_lcat_asm, ensure_ascii=False),
        # SEO fallback dla "/" — eyebrow = rada_name (sejmik/Landtag, nie rada
        # miasta). htitle bierze z katalogu, który dla assembly ma już wariant
        # sejmikowy (catalog(..., assembly=True) nakłada hero_title_assembly).
        "{{SEO_CONTENT}}": build_seo_content(
            _lcat_asm, city_gen, eyebrow_override=cfg.get("rada_name", "")),
        # Sejmiki/landy mają radnych (Abgeordnete też), więc HAS_COUNCILORS=true.
        # Bez tego template ma {{...}} leftover w renderze.
        "{{HAS_COUNCILORS}}": "false" if cfg.get("has_councilorless") else "true",
        # Kind/vote category JS — dla sejmików defaults empty bo nie ma
        # bespoke kategoryzacji item_kind jak np. Paryż.
        "{{KIND_CATS_JS}}": "{}",
        "{{VOTE_CATS_EXTRA_JS}}": "{}",
        # Impressum/Pressekodex — DE-only, dla PL puste. Tu też puste, ale
        # docelowo dla landtagu MV potrzebne (Telemediengesetz §5).
        "{{IMPRESSUM_HTML}}": "",
        "{{IMPRESSUM_FOOTER_LINK}}": "",
        "{{PRESSEKODEX_NOTICE}}": "",
        # Linki sprzedażowe (Pro/Cennik) — non-PL na angielską wersję
        # stron apexu (?lang=en), spójnie z generate_site.py.
        "{{SALES_QS}}": "" if cfg.get("locale", "pl").lower() == "pl" else "?lang=en",
    }

    # Lokalizacja UI dla landów spoza Polski (Landtag MV → de).
    # WAŻNE: apply_locale ZANIM podstawimy placeholdery, bo część fraz
    # ma {{CITY_NAME}}/{{CITY_GENITIVE}} w wartościach polskich. Po
    # tłumaczeniu placeholdery są w wartościach niemieckich.
    # Dla polskich sejmików locale="pl" (lub brak) → apply_locale no-op.
    from i18n import apply_locale  # noqa: E402
    locale = cfg.get("locale", "pl")
    template = apply_locale(template, locale)

    html = template
    for k, v in replacements.items():
        html = html.replace(k, v)

    # Migracja 2026-05: angielskie URL slugi dla wszystkich subsites,
    # włączając sejmiki. Worker robi 301 z polskich URL-i. Mapping
    # spójny z miastami przez wspólny apply_english_paths.
    html = apply_english_paths(html)

    # Dorzuć JS-ową mapę klubowości (template miasta nie ma placeholdera
    # na to, więc wstrzykujemy przed </body>).
    # club_assignments: docs/club_assignments.json (S3-only, pisane przez
    # scraper) z overlayem ręcznego seedu z config (config wygrywa).
    _live_ca: dict = {}
    _ca_path = Path(args.output) / "club_assignments.json"
    if _ca_path.exists():
        try:
            _live_ca = json.loads(_ca_path.read_text(encoding="utf-8")) or {}
        except Exception:
            _live_ca = {}
    _club_assignments = {**_live_ca, **(cfg.get("club_assignments") or {})}
    clubs_js = _generate_clubs_data_js(
        cfg.get("clubs", {}), _club_assignments
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
    # Podstrona Nadzór administracyjny (osobny dokument, jak /news/). Tylko PL
    # sejmik z kodem TERYT obecnym w drzewie. Land DE pomijamy.
    _woj_code = cfg.get("teryt")
    _has_oversight = bool(_woj_code and _woj_code in _tree
                          and _tree[_woj_code].get("powiaty"))
    if _has_oversight:
        _odir = output_dir / "division"
        _odir.mkdir(parents=True, exist_ok=True)
        (_odir / "index.html").write_text(
            build_oversight_html(_tree[_woj_code], _cov, cfg), encoding="utf-8")
        print(f"  napisano division/index.html ({_tree[_woj_code]['name']})")
    (output_dir / "sitemap.xml").write_text(
        generate_sitemap(cfg, has_oversight=_has_oversight), encoding="utf-8")
    (output_dir / "robots.txt").write_text(generate_robots(cfg), encoding="utf-8")
    if cfg.get("cname"):
        (output_dir / "CNAME").write_text(cfg["cname"] + "\n", encoding="utf-8")

    # 404.html SPA fallback (z radoskop/404.html jeśli istnieje)
    spa_404 = repo_root / "404.html"
    if spa_404.is_file():
        import shutil as _sh
        _sh.copy2(spa_404, output_dir / "404.html")

    # Spółki: statyczny plik dla zakładki "Spółki" (jeśli zbudowany). Frontend
    # pokazuje zakładkę tylko gdy ten plik istnieje.
    _spolki_src = cfg_path.parent / "docs" / "spolki.json"
    if _spolki_src.is_file():
        import shutil as _sh2
        _sh2.copy2(_spolki_src, output_dir / "spolki.json")

    _kind_label = {"wojewodztwo": "sejmiku", "land": "landtagu",
                   "powiat": "rady powiatu"}.get(samorzad_kind, samorzad_kind)
    print(f"Wygenerowano stronę {_kind_label}: {output_dir}/index.html ({len(html)} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
