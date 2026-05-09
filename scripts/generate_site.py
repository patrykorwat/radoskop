#!/usr/bin/env python3
"""
Generate a Radoskop site instance from template + city config.

Usage:
    python generate_site.py --config ../radoskop-gdansk/config.json --output ../radoskop-gdansk/docs/
    python generate_site.py --config ../radoskop-warszawa/config.json --output ../radoskop-warszawa/docs/
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# Lokalny import modułu i18n (ten sam katalog scripts/)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n import apply_locale  # noqa: E402


# Angielskie wersje Polityki prywatności i Regulaminu — używane dla
# wszystkich miast spoza Polski (locale != "pl"). PL miasta zachowują
# polski tekst z template. Treść po angielsku, nie po niemiecku/czesku,
# bo: (a) realnie obsługujemy max 3-4 miasta zagraniczne i mnożenie
# tłumaczeń legalnych jest kosztowne; (b) angielski jest lingua franca
# dla użytkowników zagranicznych, którym i tak DE/CS to drugi język.
PRIVACY_HTML_EN = (
    "el.innerHTML = '<div style=\"max-width:800px;margin:0 auto;padding:20px 0\">'\n"
    "    + '<button class=\"profile-back\" onclick=\"showMain()\">← Home</button>'\n"
    "    + '<h1 style=\"font-size:1.5rem;margin:20px 0 10px\">Privacy policy</h1>'\n"
    "    + '<p style=\"color:var(--muted);margin-bottom:20px\">Last update: 1 April 2026</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">1. Data controller</h2>'\n"
    "    + '<p>The controller of personal data is {{AUTHOR}} (contact: patrykorwat@gmail.com). '\n"
    "    + 'Radoskop runs on the domains radoskop.pl (Polish cities) and radoskop.eu (cities outside Poland, including Praha and Berlin) and on city subdomains (e.g. {{EXAMPLE_SUBDOMAIN}}).</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">2. Data we collect</h2>'\n"
    "    + '<p>The site does not require registration or login. For traffic analytics we use Umami, a self hosted analytics tool running at stats.radoskop.pl. We collect:</p>'\n"
    "    + '<ul style=\"margin:8px 0 8px 24px\"><li>Approximate location (country, city)</li>'\n"
    "    + '<li>Device type, browser, operating system</li>'\n"
    "    + '<li>Pages visited and time on page</li>'\n"
    "    + '<li>Traffic source (e.g. search engine, referral)</li></ul>'\n"
    "    + '<p>Umami does not use cookies or persistent identifiers. The visitor identifier is a hash of IP address and User Agent header, rotated daily and not reversible. The full IP address is not stored.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">3. Cookies</h2>'\n"
    "    + '<p>The site sets only one functional cookie:</p>'\n"
    "    + '<ul style=\"margin:8px 0 8px 24px\">'\n"
    "    + '<li><strong>radoskop_theme</strong> (1 year) — remembers your light or dark mode preference</li></ul>'\n"
    "    + '<p>We do not use advertising or analytics cookies, so we do not show a consent banner. You can clear cookies in your browser settings at any time.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">4. Advertising</h2>'\n"
    "    + '<p>Radoskop does not display ads and does not use ad networks.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">5. Purpose and legal basis</h2>'\n"
    "    + '<p>Analytics data is used to assess page popularity, prioritise development and measure publication impact. '\n"
    "    + 'The legal basis is GDPR Article 6(1)(f) (legitimate interest in traffic analysis) and GDPR Article 6(1)(a) (consent for functional cookies, expressed by using the site).</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">6. Sharing data</h2>'\n"
    "    + '<p>Analytics data stays on the controller infrastructure and is not shared with third parties. We do not sell user personal data.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">7. Public data on councillors</h2>'\n"
    "    + '<p>The site presents publicly available data from official records (e.g. Polish BIP, Czech opendata, German Plenarprotokolle) about councillor activity: voting results, attendance, written enquiries. This is public information made available under freedom of information law.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">8. Your rights</h2>'\n"
    "    + '<p>Under GDPR you have the right to: access your data, correct it, erase it, restrict processing, '\n"
    "    + 'data portability, object to processing and withdraw consent at any time (by clearing cookies or contacting the controller). '\n"
    "    + 'You also have the right to lodge a complaint with the Polish data protection authority (PUODO) or your local supervisory authority.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">9. Contact</h2>'\n"
    "    + '<p>For privacy and personal data matters: <a href=\"mailto:patrykorwat@gmail.com\">patrykorwat@gmail.com</a></p>'\n"
    "\n"
    "    + '</div>';"
)


TERMS_HTML_EN = (
    "el.innerHTML = '<div style=\"max-width:800px;margin:0 auto;padding:20px 0\">'\n"
    "    + '<button class=\"profile-back\" onclick=\"showMain()\">← Home</button>'\n"
    "    + '<h1 style=\"font-size:1.5rem;margin:20px 0 10px\">Radoskop terms of service</h1>'\n"
    "    + '<p style=\"color:var(--muted);margin-bottom:20px\">Last update: 1 April 2026</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">1. General</h2>'\n"
    "    + '<p>Radoskop (the Service) is operated by Patryk Orwat (the Operator). '\n"
    "    + 'The Service runs on the domains radoskop.pl (Polish cities) and radoskop.eu (cities outside Poland, including Praha and Berlin) and on city subdomains (e.g. {{EXAMPLE_SUBDOMAIN}}). '\n"
    "    + 'Using the Service means you accept these terms.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">2. Nature of the service</h2>'\n"
    "    + '<p>Radoskop is a tool for monitoring the work of city councils. '\n"
    "    + 'It presents publicly available data from official records: roll call vote results, councillor attendance, written enquiries and other data on local government activity. '\n"
    "    + 'The Service is informational and educational. It is not an official service of any city government.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">3. Data sources</h2>'\n"
    "    + '<p>All data presented in the Service comes from publicly available sources, primarily the official records of each city. '\n"
    "    + 'Data is fetched automatically and processed algorithmically. The Operator makes reasonable efforts to keep data current and correct '\n"
    "    + 'but does not guarantee full accuracy. In case of discrepancy the original source data is authoritative.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">4. Indicators and statistics</h2>'\n"
    "    + '<p>The indicators shown (attendance, activity, club discipline, rebellion count) are computed from official source data using transparent algorithms. '\n"
    "    + 'They are informational and do not constitute a value judgement on a councillor. '\n"
    "    + 'The computation methodology is available in the Service source code on GitHub.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">5. Advertising</h2>'\n"
    "    + '<p>Radoskop does not display ads and does not use ad networks. The Service is funded by the Operator and by paid reports.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">6. Licence and source code</h2>'\n"
    "    + '<p>The Service source code is published under AGPL-3.0 on GitHub. '\n"
    "    + 'Data presented in the Service, as public information, can be reused freely with attribution.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">7. Liability</h2>'\n"
    "    + '<p>The Operator is not liable for: temporary unavailability of the Service, errors in data resulting from errors in the source records, '\n"
    "    + 'decisions taken on the basis of information from the Service, third party services that the Service links to.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">8. Privacy</h2>'\n"
    "    + '<p>Personal data processing rules are described in the <a href=\"/privacy/\" onclick=\"event.preventDefault();showPrivacy()\">Privacy policy</a>.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">9. Changes</h2>'\n"
    "    + '<p>The Operator reserves the right to amend these terms. The current version is always available at /terms/ on every Service instance.</p>'\n"
    "\n"
    "    + '<h2 style=\"font-size:1.1rem;margin:24px 0 8px\">10. Contact</h2>'\n"
    "    + '<p>Questions and comments about the Service: <a href=\"mailto:patrykorwat@gmail.com\">patrykorwat@gmail.com</a></p>'\n"
    "\n"
    "    + '</div>';"
)


PRIVACY_TAIL_EN = (
    "navigateTo('/privacy/');\n"
    "  setTitle('Privacy policy');\n"
    "  setOgMeta({\n"
    "    title: 'Privacy policy — Radoskop {{CITY_NAME}}',\n"
    "    description: 'Privacy policy and cookie information for Radoskop.',\n"
    "    url: '{{SITE_URL}}/privacy/'\n"
    "  });"
)


TERMS_TAIL_EN = (
    "navigateTo('/terms/');\n"
    "  setTitle('Terms of service');\n"
    "  setOgMeta({\n"
    "    title: 'Terms of service — Radoskop {{CITY_NAME}}',\n"
    "    description: 'Radoskop terms of service. Information on data sources, methodology and usage rules.',\n"
    "    url: '{{SITE_URL}}/terms/'\n"
    "  });"
)


def apply_english_legal(html: str) -> str:
    """Podmień Politykę prywatności i Regulamin na angielską wersję.

    Markery /* PRIVACY_HTML_BEGIN */ ... /* PRIVACY_HTML_END */ obejmują
    cały blok: innerHTML + navigateTo + setTitle + setOgMeta. Funkcja
    uruchamia się PO apply_locale, więc cokolwiek apply_locale wstrzyknął
    do wnętrza markerów (np. "Datenschutz" w setTitle), zostaje
    nadpisane angielską wersją. Markery to neutralne komentarze JS,
    nie ruszane przez apply_locale.

    Footer link href + router patterns: nie pod markerami, ale to URL-e
    bez polskich słów (apply_locale ich nie tłumaczy), więc bezpieczne
    do podmiany w dowolnej kolejności. Footer label ("Polityka
    prywatności" → "Datenschutz" / "Zásady...") zachowujemy zlokalizowany,
    bo treść strony jest po angielsku ale label w UI dopasowuje się do
    reszty interfejsu.
    """
    privacy_re = re.compile(
        r"/\* PRIVACY_HTML_BEGIN \*/[\s\S]*?/\* PRIVACY_HTML_END \*/"
    )
    terms_re = re.compile(
        r"/\* TERMS_HTML_BEGIN \*/[\s\S]*?/\* TERMS_HTML_END \*/"
    )
    html = privacy_re.sub(
        lambda _m: (
            "/* PRIVACY_HTML_BEGIN */\n"
            f"  {PRIVACY_HTML_EN}\n"
            f"  {PRIVACY_TAIL_EN}\n"
            "  /* PRIVACY_HTML_END */"
        ),
        html,
    )
    html = terms_re.sub(
        lambda _m: (
            "/* TERMS_HTML_BEGIN */\n"
            f"  {TERMS_HTML_EN}\n"
            f"  {TERMS_TAIL_EN}\n"
            "  /* TERMS_HTML_END */"
        ),
        html,
    )

    # Footer link href: zachowujemy zlokalizowany label, podmieniamy URL
    # i route w window.history. Regex bo label po apply_locale może być
    # już "Datenschutz", "AGB", "Zásady ochrany osobních údajů", "Podmínky".
    html = re.sub(
        r'<a href="/polityka-prywatnosci/"',
        '<a href="/privacy/"',
        html,
    )
    html = re.sub(
        r'<a href="/regulamin/"',
        '<a href="/terms/"',
        html,
    )

    # Router (init + popstate): /polityka-prywatnosci/ → /privacy/,
    # /regulamin/ → /terms/. To URL-e, niezależne od locale.
    html = html.replace(
        "path === '/polityka-prywatnosci' || path === '/polityka-prywatnosci/'",
        "path === '/privacy' || path === '/privacy/'",
    )
    html = html.replace(
        "path === '/regulamin' || path === '/regulamin/'",
        "path === '/terms' || path === '/terms/'",
    )

    return html


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

    # Domena root zależy od kraju: PL miasta na radoskop.pl, reszta na
    # radoskop.eu. Cname miasta (config.cname) używamy jako example
    # subdomeny w tekstach prawnych.
    country = (config.get("country") or "pl").lower()
    root_host = "radoskop.pl" if country == "pl" else "radoskop.eu"
    root_url = f"https://{root_host}"
    example_subdomain = config.get("cname") or f"gdansk.{root_host}"

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
        "{{ROOT_HOST}}": root_host,
        "{{ROOT_URL}}": root_url,
        "{{EXAMPLE_SUBDOMAIN}}": example_subdomain,
        # Capability flags do JS template literali — JS boolean
        "{{HAS_VOTING_DATA}}": "true" if config.get("has_voting_data", True) else "false",
        "{{HAS_SPEAKER_ACTIVITY}}": "true" if config.get("has_speaker_activity", False) else "false",
    }

    locale = config.get("locale", "pl")

    # Lokalizacja UI dla miast spoza Polski (config.locale == "de"/"cs"/"en").
    # WAŻNE: apply_locale uruchamiamy ZANIM podstawimy placeholdery
    # (CITY_NAME, CITY_GENITIVE), bo część fraz w słownikach matchuje
    # na frazach z placeholderem, np. "Jak głosują radni Miasta
    # {{CITY_GENITIVE}}? Dane z protokołów BIP." → DE/CS odpowiednik.
    # Po apply_locale w niemieckiej/czeskiej wersji placeholdery dalej
    # są w {{...}} i zostają podmienione w pętli replacements.
    # Polskie miasta nie mają pola locale → no-op.
    html = apply_locale(template, locale)

    # Po apply_locale podmieniamy Politykę prywatności i Regulamin na
    # wersję angielską (dla miast spoza PL). Markery są neutralne dla
    # apply_locale, więc cokolwiek wstrzyknął w setTitle/setOgMeta
    # (np. 'Datenschutz') zostaje nadpisane EN wersją. Apply_locale
    # nie tknie EN treści bo już go nie uruchamiamy ponownie.
    if locale.lower() != "pl":
        html = apply_english_legal(html)

    # Apply replacements
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

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
