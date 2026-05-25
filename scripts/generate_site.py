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


# Treść Polityki prywatności i Regulaminu nie jest już inline'owana
# w SPA. Funkcje showPrivacy() / showTerms() w template/index.html
# robią redirect na apex radoskop.eu/privacy/ i /terms/, gdzie żyje
# bilingual content (PL + EN, toggle przez ?lang=en). Źródło treści:
# radoskop-premium/templates/legal/{regulamin,polityka-prywatnosci}{,-en}.html.
# AGPL repo (ten plik) nie zawiera business content (cen, klauzul SaaS).


def apply_english_paths(html: str) -> str:
    """Translate Polish URL slug paths to English. Działa dla WSZYSTKICH miast.

    Po migracji 2026-05 wszystkie miasta (PL i non-PL) używają angielskich
    slugów dla URL paths widzianych w przeglądarce. Cloudflare Worker
    (radoskop-premium/cloudflare/worker.js) ma synchroniczny PATH_REDIRECTS
    który robi 301 ze starych polskich URL-i na nowe angielskie, więc
    Google index zachowuje link equity.

    Affects URL paths visible in the browser only. API endpoints prefixed
    with API_BASE (np. API_BASE + '/kadencja/...') zostają bez zmian, bo
    backend FastAPI dalej używa polskich nazw kolekcji. Path map:

        /profil/        → /profile/        (councillor pages, indexed by Google)
        /aktualnosci/   → /news/           (RSS/news landing)
        /budzet/        → /budget/         (budget tab)
        /interpelacje/  → /interpellations/
        /kadencja/      → /term/           (URL only, NIE API)
        /glosowanie/    → /vote/
        /sesja/         → /session/
        /raporty/       → /reports/        + sub-paths radny/klub/miasto
                                             → councillor/club/city
        /moj-radny/     → /my-councillor/  (sitemap entry)

        TAB_SLUGS values: radni→councillors, sesje→sessions,
                         glosowania→votes, podobienstwo→similarity,
                         interpelacje→interpellations, budzet→budget.
        Klucze TAB_SLUGS zostają (są używane w `tab === 'interpelacje'`),
        zmiana tylko wartości czyli URL slugów.

    Reports checkout backwards compat: backend /api/checkout dalej
    przyjmuje legacy typy radny/klub/miasto, więc po stronie klienta
    mapujemy URL token → legacy type przed wysłaniem.

    Strict path-pattern replacements (nie surowe '/profil/'), żeby nie
    złapać przypadkiem fragmentu nazwy zmiennej albo CSS class.
    """
    # /profil/ — w URL contextach
    html = html.replace("path.startsWith('/profil/')", "path.startsWith('/profile/')")
    html = html.replace("path.replace('/profil/', '')", "path.replace('/profile/', '')")
    html = html.replace("'href=\"/profil/'", "'href=\"/profile/'")
    html = html.replace("navigateTo('/profil/'", "navigateTo('/profile/'")
    html = html.replace("{{SITE_URL}}/profil/", "{{SITE_URL}}/profile/")
    html = html.replace("src=\"{{SITE_URL}}/profil/", "src=\"{{SITE_URL}}/profile/")

    # /aktualnosci/
    html = html.replace(
        "path === '/aktualnosci' || path === '/aktualnosci/'",
        "path === '/news' || path === '/news/'",
    )
    html = html.replace("href=\"/aktualnosci/\"", "href=\"/news/\"")
    html = html.replace("// /aktualnosci/", "// /news/")

    # /budzet/
    html = html.replace(
        "path === '/budzet' || path === '/budzet/'",
        "path === '/budget' || path === '/budget/'",
    )
    html = html.replace("if (tab === 'budget') return '/budzet/';",
                        "if (tab === 'budget') return '/budget/';")

    # /interpelacje/ (URL path only — NIE klucz w TAB_SLUGS ani 'interpelacje' w
    # `tab === 'interpelacje'` checkach, bo to identyfikator JS, nie path)
    html = html.replace(
        "path === '/interpelacje' || path === '/interpelacje/'",
        "path === '/interpellations' || path === '/interpellations/'",
    )
    html = html.replace("if (tab === 'interpelacje') return '/interpelacje/';",
                        "if (tab === 'interpelacje') return '/interpellations/';")

    # /komisje/ (lista) i /komisja/{slug}/ (detal). PL "komisja" → EN "commission".
    # Identyfikator JS (klucz 'komisje' w activateTab, allTabs, TAB_SLUGS etc.)
    # zostaje bez zmian — to nazwa zmiennej, nie path. Zmieniamy WYŁĄCZNIE
    # URL paths user-facing. API endpointy (API_BASE + '/komisja/...') zostają
    # po polsku, jak inne API endpointy (/druk/, /kadencja/, /sesja/) — to
    # konwencja backendu, dotyka tylko data_api.py, nie SEO. Slug url_slug
    # (np. "komisja-zdrowia") zostaje jak w danych BIP bo to identyfikator
    # lokalny per kraj — w DE byłby "ausschuss-gesundheit" itd.
    html = html.replace(
        "path === '/komisje' || path === '/komisje/'",
        "path === '/commissions' || path === '/commissions/'",
    )
    html = html.replace("if (tab === 'komisje') return '/komisje/';",
                        "if (tab === 'komisje') return '/commissions/';")
    html = html.replace("path.startsWith('/komisja/')", "path.startsWith('/commission/')")
    html = html.replace("path.replace('/komisja/', '')", "path.replace('/commission/', '')")
    html = html.replace("navigateTo('/komisja/'", "navigateTo('/commission/'")
    # HTML linki w template literals (komisje grid cards): href="/komisja/${...}/"
    html = html.replace('href="/komisja/', 'href="/commission/')
    # OG meta canonical url: '{{SITE_URL}}/komisja/' + slug + '/'
    html = html.replace("{{SITE_URL}}/komisja/", "{{SITE_URL}}/commission/")

    # /kadencja/ — tylko URL paths, NIE API_BASE+'/kadencja/...'
    html = html.replace("path.startsWith('/kadencja/')", "path.startsWith('/term/')")
    html = html.replace("path.replace('/kadencja/', '')", "path.replace('/term/', '')")
    html = html.replace("return '/kadencja/' + kadSlug", "return '/term/' + kadSlug")
    # Komentarz w mainPath()
    html = html.replace("typu /kadencja/ix/undefined", "typu /term/ix/undefined")

    # /glosowanie/
    html = html.replace(
        "path === '/glosowanie' || path === '/glosowanie/'",
        "path === '/vote' || path === '/vote/'",
    )
    html = html.replace("path.startsWith('/glosowanie/')", "path.startsWith('/vote/')")
    html = html.replace("path.replace('/glosowanie/', '')", "path.replace('/vote/', '')")
    html = html.replace("navigateTo('/glosowanie/'", "navigateTo('/vote/'")
    html = html.replace("'href=\"/glosowanie/'", "'href=\"/vote/'")
    html = html.replace("{{SITE_URL}}/glosowanie/", "{{SITE_URL}}/vote/")
    html = html.replace("// /glosowanie/", "// /vote/")

    # /sesja/
    html = html.replace(
        "path === '/sesja' || path === '/sesja/'",
        "path === '/session' || path === '/session/'",
    )
    html = html.replace("path.startsWith('/sesja/')", "path.startsWith('/session/')")
    html = html.replace("path.replace('/sesja/', '')", "path.replace('/session/', '')")
    html = html.replace("navigateTo('/sesja/'", "navigateTo('/session/'")
    html = html.replace("'href=\"/sesja/'", "'href=\"/session/'")
    html = html.replace("{{SITE_URL}}/sesja/", "{{SITE_URL}}/session/")
    html = html.replace("// /sesja/", "// /session/")

    # /raporty/ + sub-paths
    html = html.replace(
        "path === '/raporty' || path === '/raporty/'",
        "path === '/reports' || path === '/reports/'",
    )
    html = html.replace("href=\"/raporty/\"", "href=\"/reports/\"")
    html = html.replace("'href=\"/raporty/radny/'", "'href=\"/reports/councillor/'")
    html = html.replace("'href=\"/raporty/klub/'", "'href=\"/reports/club/'")
    html = html.replace("'href=\"/raporty/miasto.pdf\"", "'href=\"/reports/city.pdf\"")
    html = html.replace("navigateTo('/raporty/')", "navigateTo('/reports/')")
    html = html.replace("siteUrl + '/raporty/'", "siteUrl + '/reports/'")
    html = html.replace("reportPath.replace('/raporty/', '')",
                        "reportPath.replace('/reports/', '')")
    html = html.replace(
        "// Paths: /raporty/radny/{slug}.pdf, /raporty/klub/{slug}.pdf, /raporty/miasto.pdf",
        "// Paths: /reports/councillor/{slug}.pdf, /reports/club/{slug}.pdf, /reports/city.pdf",
    )

    # Reports checkout: backend dalej oczekuje legacy typy radny/klub/miasto.
    # Wstrzykujemy mapowanie URL token → legacy type żeby nie ruszać API.
    html = html.replace(
        "var reportType = parts[0]; // radny, klub, miasto\n"
        "  var reportId = parts[1] || '';\n"
        "  var citySlug = location.hostname.split('.')[0]; // e.g. \"szczecin\"",
        "var urlType = parts[0]; // councillor, club, city\n"
        "  var reportId = parts[1] || '';\n"
        "  var citySlug = location.hostname.split('.')[0];\n\n"
        "  // Backend /api/checkout still expects legacy Polish type names\n"
        "  // (radny/klub/miasto). Map URL token to legacy type for backwards\n"
        "  // compatibility.\n"
        "  var typeMap = { councillor: 'radny', club: 'klub', city: 'miasto' };\n"
        "  var reportType = typeMap[urlType] || urlType;",
    )

    # TAB_SLUGS: zmiana tylko wartości (URL slugów), klucze obiektu zostają.
    # 2026-05: tab `profiles` usunięty (duplikat rankingu), `similarity` też
    # już dawniej usunięty. Aktualny shape: 6 tabsów (5 sekcji + komisje).
    html = html.replace(
        "{ranking:'ranking',sessions:'sesje',votes:'glosowania',komisje:'komisje',"
        "interpelacje:'interpelacje',budget:'budzet'}",
        "{ranking:'ranking',sessions:'sessions',votes:'votes',komisje:'commissions',"
        "interpelacje:'interpellations',budget:'budget'}",
    )

    # /druk/ — strona druku (treść + procedowanie w komisjach). URL-facing
    # path: /druk/ → /bill/. API endpoint (API_BASE + '/druk/...') zostaje
    # po polsku jak inne API endpointy — dotyka tylko backend, nie SEO.
    html = html.replace("path.startsWith('/druk/')", "path.startsWith('/bill/')")
    html = html.replace("path.replace('/druk/', '')", "path.replace('/bill/', '')")
    html = html.replace(
        "navigateTo('/druk/' + kadSlug + '/' + encodeURIComponent(drukId) + '/')",
        "navigateTo('/bill/' + kadSlug + '/' + encodeURIComponent(drukId) + '/')",
    )
    html = html.replace(
        'href="/druk/${KAD_SLUGS[currentKid]||currentKid}/${encodeURIComponent(dStr)}/"',
        'href="/bill/${KAD_SLUGS[currentKid]||currentKid}/${encodeURIComponent(dStr)}/"',
    )
    html = html.replace(
        'href="/druk/${KAD_SLUGS[voteKid]||voteKid}/${vote.druk}/"',
        'href="/bill/${KAD_SLUGS[voteKid]||voteKid}/${vote.druk}/"',
    )
    html = html.replace(
        'href="/druk/${KAD_SLUGS[currentKid]||currentKid}/${v.druk}/"',
        'href="/bill/${KAD_SLUGS[currentKid]||currentKid}/${v.druk}/"',
    )
    html = html.replace("{{SITE_URL}}/druk/", "{{SITE_URL}}/bill/")

    # /polityka-prywatnosci/ + /regulamin/ → /privacy/ + /terms/. Po migracji
    # 2026-05 path mapping jest jednolity. Treść strony żyje na apex
    # radoskop.eu/privacy/ i /terms/ (bilingual przez ?lang=en), SPA tylko
    # redirektuje. URL slug zawsze /privacy/ i /terms/ niezależnie od locale.
    html = html.replace(
        '<a href="/polityka-prywatnosci/"',
        '<a href="/privacy/"',
    )
    html = html.replace(
        '<a href="/regulamin/"',
        '<a href="/terms/"',
    )
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


def _is_councilorless(config: dict) -> bool:
    """True dla miast bez głosów per radny (à main levée / tylko per frakcja).

    Trigger: config["voting_mode"]=="show_of_hands" albo config["voting_display"]
    =="faction". Takie miasta nie mają rankingu radnych — strona główna prowadzi
    zakładką "Głosowania", a taby radnych są ukryte.
    """
    return (
        str(config.get("voting_mode", "")).lower() == "show_of_hands"
        or str(config.get("voting_display", "")).lower() == "faction"
    )


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


def generate_aktualnosci_button(output_dir: Path, locale: str = "pl") -> str:
    """Render the news tab link only when the city has data.

    Without scraped activity, generate_feed.py produces nothing under
    /news/, so the link would 404. Hide the button instead.

    Po migracji 2026-05 URL slug to zawsze /news/, niezależnie od locale.
    Label przycisku zostaje zlokalizowany ("Aktualności" w PL, lokalny
    odpowiednik w innych) — apply_locale go podmienia.
    """
    if not has_activity_data(output_dir):
        return ""
    # tab-secondary klasa: na mobile/tablet ukryte do czasu kliknięcia
    # "Więcej". Na desktop pokazane normalnie obok pozostałych tabów.
    return '        <a href="/news/" class="tab tab-secondary" style="text-decoration:none">Aktualności</a>'


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


def generate_sitemap(config: dict, output_dir: Path | None = None) -> str:
    """Generate full sitemap.xml.

    Po migracji 2026-05 slug paths są zawsze angielskie, niezależnie od
    locale miasta. Worker (radoskop-premium/cloudflare/worker.js) ma
    PATH_REDIRECTS robiący 301 ze starych polskich URL-i.

    Zawiera:
      - / (homepage)
      - /my-councillor/, /news/, /reports/, /privacy/, /terms/
      - /budget/ jeśli has_budget
      - /term/{kid}/ i /term/{kid}/{tab}/ dla każdej kadencji
      - /profile/{slug}/ dla każdego radnego z profiles.json

    generate_seo_pages.py uruchamia się PO i nadpisuje ten sitemap pełniejszą
    wersją (z głosowaniami i sesjami) gdy działa. Ta funkcja jest fallbackiem
    żeby zawsze były wszystkie strony statyczne, nawet gdy SEO step pominięty.
    """
    url = config["site_url"].rstrip("/")
    entries = []

    def add(loc: str, changefreq: str, priority: str) -> None:
        entries.append(
            f'  <url>\n    <loc>{loc}</loc>\n'
            f'    <changefreq>{changefreq}</changefreq>\n'
            f'    <priority>{priority}</priority>\n  </url>'
        )

    # Homepage
    add(f"{url}/", "weekly", "1.0")

    # Standardowe podstrony static
    add(f"{url}/my-councillor/", "weekly", "0.9")
    add(f"{url}/news/", "daily", "0.8")
    add(f"{url}/reports/", "weekly", "0.6")
    add(f"{url}/privacy/", "yearly", "0.3")
    add(f"{url}/terms/", "yearly", "0.3")

    if config.get("has_budget"):
        add(f"{url}/budget/", "monthly", "0.8")

    # Term / kadencje (per kadencja + per tab)
    # Tab slugi muszą zgadzać się z generate_seo_pages.py SLUG dict i z
    # apply_english_paths() niżej w tym pliku.
    tab_slugs = [
        "councillors",
        "sessions",
        "votes",
        "similarity",
        "interpellations",
        "ranking",
    ]
    # Kadencje source: nowsze configi (praha, berlin) mają config.kadencje
    # jako dict {id: meta}. Starsze (polskie miasta) trzymają listę kadencji
    # w docs/data.json -> kadencje [{id: ...}, ...].
    kadencje_ids: list[str] = []
    config_kadencje = config.get("kadencje")
    if isinstance(config_kadencje, dict) and config_kadencje:
        kadencje_ids = list(config_kadencje.keys())
    elif output_dir is not None:
        data_path = output_dir / "data.json"
        if data_path.exists():
            try:
                data = json.loads(data_path.read_text(encoding="utf-8"))
                for k in data.get("kadencje", []):
                    if isinstance(k, dict):
                        kid = k.get("id")
                    else:
                        kid = k
                    if kid:
                        kadencje_ids.append(kid)
            except (json.JSONDecodeError, OSError):
                pass

    for kid in kadencje_ids:
        add(f"{url}/term/{kid}/", "weekly", "0.8")
        for tab in tab_slugs:
            add(f"{url}/term/{kid}/{tab}/", "weekly", "0.6")

    # Catch-all directory pages (generate_seo_pages.py też je dodaje)
    add(f"{url}/profile/", "monthly", "0.9")
    add(f"{url}/term/", "monthly", "0.9")

    # Per-radny profile pages z profiles.json. Plik jest generowany przez
    # scrape przed generate_site.py, więc powinien istnieć.
    if output_dir is not None:
        profiles_path = output_dir / "profiles.json"
        if profiles_path.exists():
            try:
                profiles_data = json.loads(profiles_path.read_text(encoding="utf-8"))
                for p in profiles_data.get("profiles", []):
                    slug = p.get("slug") or p.get("id")
                    if slug:
                        add(f"{url}/profile/{slug}/", "weekly", "0.7")
            except (json.JSONDecodeError, OSError):
                pass

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries) + "\n"
        '</urlset>\n'
    )


def _esc_js_string(s: str) -> str:
    """Escape string żeby działał wewnątrz JS literala '...'.

    Treść trafia do template w postaci:
        el.innerHTML = '{{IMPRESSUM_HTML}}';
    czyli musimy uciec apostrofy i nowe linie, oraz zamienić < na bezpieczne.
    Najprostszy sposób: zwracamy JSON-encoded string bez otaczających
    cudzysłowów (escape działa też dla apostrofów bo nie używamy ich
    w JSON), potem trim cudzysłowów. Dodatkowo nowe linie zamieniane są
    na puste żeby HTML się nie łamał w środku JS.
    """
    return (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", " ")
        .replace("\r", "")
    )


def build_impressum_html(config: dict, country: str) -> tuple[str, str]:
    """Zwróć krotkę (innerHTML dla Impressum, link w stopce).

    PL miasta: pusty innerHTML, pusty link (Regulamin pokrywa identyfikację).
    DE/CS miasta: pełny Impressum w odpowiednim języku, link w stopce.

    config.impressum może zawierać:
        - "name": "Patryk Orwat"
        - "address_lines": ["ul. Przykładowa 1", "00-001 Warszawa, Polska"]
        - "email": "patrykorwat@gmail.com"
        - "phone": "+48 ..." (opcjonalne)
        - "vat_id": "PL..." (opcjonalne)
    Jeśli brakuje, dla DE/CS wstawiamy placeholder z ostrzeżeniem żeby
    uzupełnić przed deployem.
    """
    if country == "pl":
        return "", ""

    imp = config.get("impressum") or {}
    name = imp.get("name") or "Patryk Orwat"
    address_lines = imp.get("address_lines") or [
        "[Adresse zur Vervollständigung in config.impressum.address_lines]"
    ]
    email = imp.get("email") or "patrykorwat@gmail.com"
    phone = imp.get("phone") or ""
    vat_id = imp.get("vat_id") or ""

    addr_html = "<br>".join(address_lines)
    phone_line = f"<br>Telefon: {phone}" if phone else ""
    vat_line = f"<p>USt-IdNr.: {vat_id}</p>" if vat_id else ""

    if country == "de":
        body = (
            '<div style="max-width:800px;margin:0 auto;padding:20px 0">'
            '<button class="profile-back" onclick="showMain()">← Startseite</button>'
            '<h1 style="font-size:1.5rem;margin:20px 0 10px">Impressum</h1>'
            '<p style="color:var(--muted);margin-bottom:20px">Anbieterkennzeichnung gemäß §5 TMG und §18 Abs. 2 Medienstaatsvertrag (MStV).</p>'

            '<h2 style="font-size:1.1rem;margin:24px 0 8px">Anbieter</h2>'
            f'<p>{name}<br>{addr_html}</p>'

            '<h2 style="font-size:1.1rem;margin:24px 0 8px">Kontakt</h2>'
            f'<p>E-Mail: <a href="mailto:{email}">{email}</a>{phone_line}</p>'

            '<h2 style="font-size:1.1rem;margin:24px 0 8px">Verantwortlich für den Inhalt nach §18 Abs. 2 MStV</h2>'
            f'<p>{name}<br>{addr_html}</p>'

            f'{vat_line}'

            '<h2 style="font-size:1.1rem;margin:24px 0 8px">Streitschlichtung</h2>'
            '<p>Die Europäische Kommission stellt eine Plattform zur Online-Streitbeilegung bereit: '
            '<a href="https://ec.europa.eu/consumers/odr/" target="_blank" rel="noopener">https://ec.europa.eu/consumers/odr/</a>. '
            'Wir sind nicht bereit oder verpflichtet, an einem Streitbeilegungsverfahren vor einer Verbraucherschlichtungsstelle teilzunehmen.</p>'

            '<h2 style="font-size:1.1rem;margin:24px 0 8px">Haftung für Inhalte</h2>'
            '<p>Als Anbieter sind wir gemäß §7 Abs. 1 TMG für eigene Inhalte auf diesen Seiten nach den allgemeinen Gesetzen verantwortlich. '
            'Nach §§8 bis 10 TMG sind wir als Anbieter jedoch nicht verpflichtet, übermittelte oder gespeicherte fremde Informationen zu überwachen oder nach Umständen zu forschen, die auf eine rechtswidrige Tätigkeit hinweisen. '
            'Die dargestellten Daten zu Mandatsträger:innen stammen ausschließlich aus offiziellen öffentlichen Registern (Plenarprotokolle des Berliner Abgeordnetenhauses, PARDOK, Open Data). '
            'Bei Hinweisen auf Unrichtigkeiten korrigieren wir Daten nach Verifikation gegen die Quelle innerhalb von 7 Werktagen.</p>'

            '<h2 style="font-size:1.1rem;margin:24px 0 8px">Datenschutz</h2>'
            '<p>Informationen zur Verarbeitung personenbezogener Daten finden Sie in der '
            '<a href="/privacy/" onclick="event.preventDefault();showPrivacy()">Datenschutzerklärung</a>. '
            'Informationen zu Mandatsträger:innen werden auf Grundlage von Art. 6 Abs. 1 lit. e und f DSGVO sowie Art. 85 DSGVO in Verbindung mit dem Berliner Pressegesetz und §57 BDSG verarbeitet.</p>'

            '<h2 style="font-size:1.1rem;margin:24px 0 8px">Datenrichtigstellung für Mandatsträger:innen</h2>'
            f'<p>Anträge auf Datenrichtigstellung bitte an <a href="mailto:{email}">{email}</a> mit Angabe von Name, Stadt, beanstandeter Daten und Quellverweis. Bearbeitung innerhalb von 7 Werktagen.</p>'

            '<h2 style="font-size:1.1rem;margin:24px 0 8px">Redaktionelle Leitlinien und Pressekodex</h2>'
            '<p>Radoskop folgt den <a href="https://www.presserat.de/pressekodex.html" target="_blank" rel="noopener">Publizistischen Grundsätzen (Pressekodex)</a> des Deutschen Presserats als anerkanntem Standard journalistischer Sorgfalt. '
            'Für die Tätigkeit von Radoskop relevant sind insbesondere folgende Ziffern:</p>'
            '<ul style="margin:8px 0 8px 24px">'
            '<li><strong>Ziffer 1</strong> (Wahrhaftigkeit und Achtung der Menschenwürde): Alle dargestellten Daten stammen aus überprüfbaren offiziellen Quellen.</li>'
            '<li><strong>Ziffer 2</strong> (Sorgfalt): Die Methodik der Datenverarbeitung ist als Open Source auf <a href="https://github.com/radoskoppl/radoskop" target="_blank" rel="noopener">GitHub</a> dokumentiert und nachvollziehbar.</li>'
            '<li><strong>Ziffer 3</strong> (Richtigstellung): Korrekturen werden innerhalb von 7 Werktagen nach Verifikation gegen die Quelle vorgenommen.</li>'
            '<li><strong>Ziffer 7</strong> (Trennung von Werbung und Redaktion): Radoskop zeigt keine Werbung und nimmt keine Werbeeinnahmen an.</li>'
            '<li><strong>Ziffer 8</strong> (Schutz der Persönlichkeit): Es werden ausschließlich Daten aus dem öffentlichen Mandat verarbeitet.</li>'
            '<li><strong>Ziffer 12</strong> (Diskriminierungen): Es werden keine wertenden Etiketten verwendet, ausschließlich objektive Metriken aus offiziellen Protokollen.</li>'
            '</ul>'

            '<h2 style="font-size:1.1rem;margin:24px 0 8px">Beschwerdeverfahren beim Deutschen Presserat</h2>'
            '<p>Wer der Auffassung ist, dass eine Veröffentlichung auf Radoskop gegen den Pressekodex verstößt, kann eine Beschwerde beim Deutschen Presserat einreichen. Das Verfahren ist kostenlos und kann auf <a href="https://www.presserat.de/beschwerde.html" target="_blank" rel="noopener">presserat.de/beschwerde.html</a> eingereicht werden. '
            f'Parallel oder alternativ kann jeder Mandatsträger und jede Mandatsträgerin direkt eine Datenrichtigstellung über <a href="mailto:{email}">{email}</a> beantragen, die in der Regel schneller bearbeitet wird.</p>'

            '</div>'
        )
        link = ' · <a href="/impressum/" onclick="event.preventDefault();showImpressum()">Impressum</a>'
        return _esc_js_string(body), link

    # Czech (cs) lub inne kraje na .eu — wersja angielska z czeskim
    # nagłówkiem i czeskimi referencjami prawnymi.
    body = (
        '<div style="max-width:800px;margin:0 auto;padding:20px 0">'
        '<button class="profile-back" onclick="showMain()">← Domů</button>'
        '<h1 style="font-size:1.5rem;margin:20px 0 10px">Provozovatel / Imprint</h1>'
        '<p style="color:var(--muted);margin-bottom:20px">Identifikace provozovatele dle §6 zákona č. 480/2004 Sb. o některých službách informační společnosti.</p>'

        '<h2 style="font-size:1.1rem;margin:24px 0 8px">Provozovatel</h2>'
        f'<p>{name}<br>{addr_html}</p>'

        '<h2 style="font-size:1.1rem;margin:24px 0 8px">Kontakt</h2>'
        f'<p>E-mail: <a href="mailto:{email}">{email}</a>{phone_line}</p>'

        f'{vat_line}'

        '<h2 style="font-size:1.1rem;margin:24px 0 8px">Odpovědnost za obsah</h2>'
        '<p>Data o zastupitelích pochází výhradně z oficiálních veřejných zdrojů (opendata Praha, hlasování Magistrátu hl. m. Prahy). '
        'Žádosti o opravu údajů vyřizujeme do 7 pracovních dnů od ověření proti zdrojovému dokumentu.</p>'

        '<h2 style="font-size:1.1rem;margin:24px 0 8px">Ochrana osobních údajů</h2>'
        '<p>Informace o zpracování osobních údajů naleznete v '
        '<a href="/privacy/" onclick="event.preventDefault();showPrivacy()">Zásadách ochrany osobních údajů</a>.</p>'

        '</div>'
    )
    link = ' · <a href="/impressum/" onclick="event.preventDefault();showImpressum()">Provozovatel</a>'
    return _esc_js_string(body), link


def _build_kind_cats_js(config: dict) -> str:
    """JS obiekt mapujący item_kind → klucz VOTE_CATS.

    Używany przez miasta gdzie głosowania mają item_kind zamiast / oprócz
    tematu tekstowego (np. Paryż: voeu/amendement/projet_deliberation).
    Konfigurowane przez config.item_kind_cats lub autowykrywane dla locale fr.
    """
    import json as _json
    mapping = config.get("item_kind_cats") or {}
    if not mapping and config.get("locale", "pl") == "fr":
        mapping = {
            "voeu": "voeu",
            "amendement": "amendement",
            "projet_deliberation": "deliberation",
        }
    return _json.dumps(mapping, ensure_ascii=False)


def _build_vote_cats_extra_js(config: dict) -> str:
    """JS obiekt z dodatkowymi wpisami do VOTE_CATS dla specyficznych miast.

    Dla Paryża dodaje voeu/amendement/deliberation jako osobne kategorie.
    Konfigurowane przez config.vote_cats_extra lub autowykrywane dla locale fr.
    """
    import json as _json
    extra = config.get("vote_cats_extra") or {}
    if not extra and config.get("locale", "pl") == "fr":
        extra = {
            "deliberation": {"label": "Délibération", "order": 1},
            "voeu": {"label": "Vœu", "order": 2},
            "amendement": {"label": "Amendement", "order": 3},
        }
    return _json.dumps(extra, ensure_ascii=False)


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

    # Impressum (Anbieterkennzeichnung): obowiązkowe dla DE (§5 TMG +
    # §18 ust. 2 MStV), zalecane dla CZ (§6 zákona č. 480/2004 Sb.).
    # Dla PL nie ma osobnego wymogu, identyfikacja administratora
    # jest w Regulaminie. Pełny tekst budujemy z config.impressum
    # jeśli istnieje, w przeciwnym razie placeholder że uzupełniony
    # zostanie przed wdrożeniem.
    impressum_html, impressum_footer_link = build_impressum_html(config, country)

    # Pressekodex notice: tylko DE. Dobrowolny ale wzmacnia argument
    # "anerkannter Standard journalistischer Sorgfalt" przed niemieckim
    # sądem oraz daje miękką ścieżkę reklamacyjną przez Deutscher Presserat.
    if country == "de":
        pressekodex_notice = (
            '<div style="margin-top:8px;color:var(--muted);font-size:0.75rem">'
            'Wir folgen dem '
            '<a href="https://www.presserat.de/pressekodex.html" target="_blank" rel="noopener">'
            'Pressekodex</a> des Deutschen Presserats. '
            '<a href="https://www.presserat.de/beschwerde.html" target="_blank" rel="noopener">'
            'Beschwerdeverfahren</a>.'
            '</div>'
        )
    else:
        pressekodex_notice = ""

    # Disclaimer dla miast, których źródło nie publikuje per-radny attribution.
    # Pole `vote_data_disclaimer` w config (string albo dict {locale: text}).
    # Renderowane jako żółty banner pod nagłówkiem hero. Pusty placeholder
    # jeśli config nie ma tego pola (większość miast PL go nie potrzebuje).
    _disclaimer = config.get("vote_data_disclaimer", "")
    if isinstance(_disclaimer, dict):
        _disclaimer = _disclaimer.get(config.get("locale", "pl"), _disclaimer.get("pl", ""))
    if _disclaimer:
        vote_disclaimer_html = (
            '<div style="margin:14px auto 0;max-width:720px;padding:10px 14px;'
            'background:rgba(250,204,21,0.12);border:1px solid rgba(202,138,4,0.5);'
            'border-radius:8px;font-size:0.9rem;color:var(--text);line-height:1.5">'
            f'{_disclaimer}'
            '</div>'
        )
    else:
        vote_disclaimer_html = ""

    # Reguły kategoryzacji per locale (litewskie dla Wilna, słowackie dla
    # Bratysławy itd.). Fallback PL gdy locale nieobsługiwany.
    from vote_categories import generate_cat_rules_js
    _cat_rules_js = generate_cat_rules_js(config.get("locale", "pl"))

    # Build replacements
    replacements = {
        "{{CAT_RULES_JS}}": _cat_rules_js,
        "{{KIND_CATS_JS}}": _build_kind_cats_js(config),
        "{{VOTE_CATS_EXTRA_JS}}": _build_vote_cats_extra_js(config),
        "{{VOTE_DATA_DISCLAIMER}}": vote_disclaimer_html,
        "{{CITY_NAME}}": config.get("city_name") or config.get("voivodeship_name", ""),
        "{{CITY_GENITIVE}}": config.get("city_genitive") or config.get("voivodeship_genitive", ""),
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
        "{{AKTUALNOSCI_BUTTON}}": generate_aktualnosci_button(
            Path(args.output), config.get("locale", "pl").lower()
        ),
        "{{ROOT_HOST}}": root_host,
        "{{ROOT_URL}}": root_url,
        "{{EXAMPLE_SUBDOMAIN}}": example_subdomain,
        # Capability flags do JS template literali — JS boolean
        "{{HAS_VOTING_DATA}}": "true" if config.get("has_voting_data", True) else "false",
        "{{HAS_SPEAKER_ACTIVITY}}": "true" if config.get("has_speaker_activity", False) else "false",
        # Miasta bez radnych per osoba (głosowanie à main levée / per frakcja).
        # Strona główna prowadzi wtedy zakładką "Głosowania".
        "{{HAS_COUNCILORS}}": "false" if (_is_councilorless(config) and not config.get("has_named_votes")) else "true",
        # Impressum / Anbieterkennzeichnung
        "{{IMPRESSUM_HTML}}": impressum_html,
        "{{IMPRESSUM_FOOTER_LINK}}": impressum_footer_link,
        "{{PRESSEKODEX_NOTICE}}": pressekodex_notice,
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

    # Migracja 2026-05: wszystkie miasta (PL i non-PL) używają angielskich
    # URL slugów. Worker (radoskop-premium/cloudflare/worker.js) ma
    # PATH_REDIRECTS który robi 301 ze starych polskich URL-i, więc
    # link equity z Google index zostaje zachowane. apply_english_paths()
    # leci zawsze, niezależnie od locale.
    html = apply_english_paths(html)

    # Treść Polityki prywatności i Regulaminu nie żyje już w SPA. Funkcje
    # showPrivacy() i showTerms() w template robią redirect na apex
    # radoskop.eu/privacy/ i /terms/, gdzie obsługiwany jest bilingual
    # toggle (?lang=en). Locale jest czytany w runtime z <html lang>,
    # więc dla miast non-PL musimy zaktualizować <html lang> i og:locale,
    # czego apply_locale nie robi (operuje tylko na tekście widocznym dla
    # użytkownika, nie na atrybutach HTML).
    locale_lower = locale.lower()
    if locale_lower != "pl":
        og_locale_map = {
            "de": "de_DE", "cs": "cs_CZ", "en": "en_US", "fr": "fr_FR",
            "lt": "lt_LT", "et": "et_EE", "lv": "lv_LV", "nl": "nl_NL",
            "sk": "sk_SK", "hu": "hu_HU",
        }
        og_locale = og_locale_map.get(locale_lower, locale_lower)
        html = html.replace('<html lang="pl">', f'<html lang="{locale_lower}">', 1)
        html = html.replace(
            '<meta property="og:locale" content="pl_PL">',
            f'<meta property="og:locale" content="{og_locale}">',
            1,
        )

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
    if _is_councilorless(config):
        # Miasta bez radnych per osoba (Paryż à main levée): brak rankingu,
        # frekwencji, profili, sesji imiennych. Strona główna = "Głosowania".
        # Ukrywamy taby radnych/sesji/komisji/interpelacji; flip aktywnej
        # zakładki i widocznej sekcji na votes robimy niżej (na HTML).
        has_named = config.get("has_named_votes")
        # Ranking ukryty zawsze (brak per-radny statystyk).
        # Sessions i councillors odkrywamy gdy miasto ma profiles (has_named_votes).
        always_hide = ["ranking", "komisje", "interpelacje"]
        if not has_named:
            always_hide += ["sessions", "councillors"]
        for t in always_hide:
            hide_css.append(f'[data-tab="{t}"]{{display:none!important}}')
        # Statyczny stan początkowy (zanim JS przejmie): aktywny tab + widoczna
        # sekcja = votes. Match po data-tab/id (apply_locale nie tłumaczy ich).
        html = html.replace('class="tab active" data-tab="ranking"', 'class="tab" data-tab="ranking"')
        html = html.replace('class="tab" data-tab="votes"', 'class="tab active" data-tab="votes"')
        html = html.replace('<div id="tab-ranking" class="section">', '<div id="tab-ranking" class="section" style="display:none">')
        html = html.replace('<div id="tab-votes" class="section" style="display:none">', '<div id="tab-votes" class="section">')
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
        f.write(generate_sitemap(config, output_dir))

    # robots.txt
    with open(output_dir / "robots.txt", "w", encoding="utf-8") as f:
        f.write(generate_robots(config))

    # CNAME
    if config.get("cname"):
        with open(output_dir / "CNAME", "w") as f:
            f.write(config["cname"] + "\n")

    print(f"Generated site for {config.get('city_name') or config.get('voivodeship_name', '?')}:")
    print(f"  index.html  → {output_dir / 'index.html'}")
    print(f"  404.html    → {output_dir / '404.html'}")
    print(f"  sitemap.xml → {output_dir / 'sitemap.xml'}")
    print(f"  robots.txt  → {output_dir / 'robots.txt'}")
    if config.get("cname"):
        print(f"  CNAME       → {output_dir / 'CNAME'}")


if __name__ == "__main__":
    main()
