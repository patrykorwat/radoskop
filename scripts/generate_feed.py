#!/usr/bin/env python3
"""
Generate RSS/Atom feeds and /aktualnosci/ HTML page for Radoskop cities.

Feed items (sorted by date, newest first):
  1. Votes (all, with result and topic)
  2. Interpelacje (all, with author and subject)
  3. Sessions (with stats summary)

Output:
  - docs/feed.xml                 (Atom feed, last 100 items)
  - docs/aktualnosci/index.html   (browsable news page, all items)
  - docs/aktualnosci.json         (structured data for SPA)

Usage:
    python generate_feed.py --base /path/to/gdansk-network
    python generate_feed.py --base /path/to/gdansk-network --city radoskop-gdansk
"""

import argparse
import html
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from lib_session_summary import session_votes, summarize_session, best_session_fact


def esc(text):
    return html.escape(str(text), quote=True)


def clean_topic(topic):
    return (topic or "").replace(";", "").strip()


def get_slug(name, profiles_by_name):
    p = profiles_by_name.get(name)
    if p:
        return p["slug"]
    return name.lower().replace(" ", "-")


# ── Item generators ────────────────────────────────────

def generate_vote_items(kad_data, city_name, site_url, vote_slug="vote"):
    """Generate a feed item for every vote."""
    items = []
    for v in kad_data.get("votes", []):
        vid = v.get("id", "")
        if not vid:
            continue

        topic = clean_topic(v.get("topic", ""))
        c = v.get("counts", {})
        za = c.get("za", 0)
        przeciw = c.get("przeciw", 0)
        wstrzym = c.get("wstrzymal_sie", 0)
        sdate = v.get("session_date", "")
        snum = v.get("session_number", "")

        if za > przeciw:
            result = "przyjete"
        elif przeciw > za:
            result = "odrzucone"
        else:
            result = "remis"

        title = topic[:100] if topic else f"Glosowanie {vid}"

        items.append({
            "type": "vote",
            "date": sdate,
            "title": title,
            "summary": f"Wynik: {result} (za {za}, przeciw {przeciw}, wstrzym. {wstrzym}). Sesja {snum}.",
            "url": f"{site_url}/{vote_slug}/{vid}/",
        })

    return items


def generate_session_items(kad_data, city_name, site_url, session_slug="session"):
    """Generate a feed item for every session.

    UWAGA: title musi zostać w formacie "Sesja {snum} Rady Miasta {city}" —
    dedup bota X/Bluesky ma tytuł w kluczu (url+type+title); zmiana tytułu
    wszystkich historycznych sesji spowodowałaby ich ponowne opostowanie.
    Wzbogacamy wyłącznie summary (podsumowanie posesyjne 2026-06-10).
    """
    items = []
    all_votes = kad_data.get("votes", []) or []
    councilors = kad_data.get("councilors", []) or []

    # Pre-pass: policz podsumowania + rekordy kadencji raz, do hooków social.
    # term_closest_margin = najciaśniejsze sporne głosowanie w kadencji,
    # term_max_vote_count = najwięcej głosowań na jednej sesji. Dzięki temu
    # bot może powiedzieć "najciaśniej w kadencji" / "najwięcej głosowań".
    _sessions = []
    _closest_margin = None
    _max_votes = 0
    for s in kad_data.get("sessions", []):
        if not s.get("date"):
            continue
        sv = session_votes(s, all_votes)
        summ = summarize_session(s, sv, councilors)
        _sessions.append((s, sv, summ))
        if summ["vote_count"] > _max_votes:
            _max_votes = summ["vote_count"]
        for cv in summ.get("contested") or []:
            cc = cv.get("counts") or {}
            margin = abs((cc.get("za", 0) or 0) - (cc.get("przeciw", 0) or 0))
            if _closest_margin is None or margin < _closest_margin:
                _closest_margin = margin
    _ctx = {"term_closest_margin": _closest_margin, "term_max_vote_count": _max_votes}

    for s, sess_votes, summary in _sessions:
        snum = s.get("number", "")
        sdate = s.get("date", "")

        vote_count = summary["vote_count"] or s.get("vote_count", 0)
        attendee_count = summary["attendee_count"] or s.get("attendee_count", 0)

        parts = [f"{vote_count} glosowan"]
        if sess_votes:
            parts.append(f"{summary['passed']} przyjetych")
            if summary["contested_count"]:
                parts.append(f"{summary['contested_count']} spornych")
        if attendee_count:
            parts.append(f"{attendee_count} obecnych")

        summary_text = ", ".join(parts) + "."
        # Najbardziej sporne głosowanie jako zajawka — to jest haczyk
        # newsowy dla czytelnika feedu i posta bota.
        if summary["contested"]:
            _top = summary["contested"][0]
            _topic = clean_topic(_top.get("topic", ""))
            _c = _top.get("counts", {}) or {}
            if _topic:
                summary_text += (
                    f" Najbardziej sporne: {_topic[:120]}"
                    f" (za {_c.get('za', 0)}, przeciw {_c.get('przeciw', 0)})."
                )

        item = {
            "type": "session",
            "date": sdate,
            "title": f"Sesja {snum} Rady Miasta {city_name}",
            "summary": summary_text,
            "url": f"{site_url}/{session_slug}/{snum}/",
        }
        # Hook social: najmocniejszy fakt sesji (knife-edge/odrzucenie/frekwencja),
        # deterministyczny z liczb. Bot X/Bluesky renderuje z niego chwytliwy
        # nagłówek zamiast changelogu. None => bot zostaje przy zwykłym tytule.
        hook = best_session_fact(s, summary, _ctx)
        if hook:
            item["hook"] = hook
        items.append(item)

    return items


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}$")


def effective_interp_date(ip, today):
    """Sensowna data interpelacji do feedu, chroniąca przed datą w przyszłości.

    BIP bywa z literówką roku (np. 2029 zamiast 2026 — rok końca kadencji w etykiecie),
    co wrzucało wpis na sam szczyt /news/, RSS i bota (sort malejąco po dacie).
    Poprawnego roku NIE zgadujemy (dane 1:1). Kolejność: data_wplywu jeśli ISO i nie
    późniejsza niż dziś, inaczej data_odpowiedzi jeśli sensowna, inaczej dziś (data
    generowania). Zwraca (date, raw_future) — raw_future to surowa przyszła wartość
    (do logu) albo None.
    """
    raw = ip.get("data_wplywu", "") or ""
    if not _ISO_DATE.match(raw):
        return raw, None  # pusta lub nie-ISO — zostaw jak jest (feed pomija puste)
    if raw <= today:
        return raw, None
    odp = ip.get("data_odpowiedzi", "") or ""
    if _ISO_DATE.match(odp) and odp <= today:
        return odp, raw
    return today, raw


def generate_interpelacje_items(interpelacje, city_name, site_url, profiles_by_name,
                                 profile_slug="profile", interpelacje_slug="interpellations"):
    """Generate a feed item for every interpelacja."""
    items = []
    today = date.today().isoformat()
    future_flagged = []
    for ip in interpelacje:
        date_str, raw_future = effective_interp_date(ip, today)
        if not date_str:
            continue
        if raw_future:
            future_flagged.append((raw_future, date_str, ip.get("radny", ""),
                                   ip.get("przedmiot", "")))
        radny = ip.get("radny", "")
        przedmiot = ip.get("przedmiot", "")
        typ = ip.get("typ", "interpelacja")

        typ_label = "Interpelacja" if typ == "interpelacja" else "Zapytanie"

        slug = get_slug(radny, profiles_by_name) if radny else ""
        # Link wprost do taba "Aktywność" profilu radnego (interpelacje i
        # zapytania), żeby czytelnik z posta na X/Bluesky trafił od razu na
        # listę jego interpelacji, a nie na zakładkę główną. ?tab=activity
        # zamiast osobnego segmentu ścieżki: trafia w prerenderowaną stronę
        # /profile/{slug}/ (canonical zostaje czysty), bez ryzyka soft-404 w GSC.
        # Bez profilu (nazwisko nierozpoznane) — ogólny tab interpelacji miasta.
        if slug and radny in profiles_by_name:
            url = f"{site_url}/{profile_slug}/{slug}/?tab=activity"
        else:
            url = f"{site_url}/{interpelacje_slug}/"

        items.append({
            "type": "interpelacja",
            # Podtyp rozróżnia interpelację od zapytania (art. 24 ustawy o
            # samorządzie gminnym to dwa odrębne wnioski radnego). type zostaje
            # "interpelacja" dla kompatybilności konsumentów feedu; subtype niesie
            # właściwy rodzaj, żeby bot mógł je policzyć i nazwać osobno.
            "subtype": typ,
            "date": date_str,
            "title": f"{typ_label}: {przedmiot[:100]}" if przedmiot else f"{typ_label} ({radny})",
            "summary": f"{radny}. {przedmiot[:150]}" if przedmiot else radny,
            # radny + przedmiot wprost (poza title/summary), żeby bot mógł
            # wyróżnić jeden wniosek tematem zamiast samego changelogu liczby.
            # przedmiot to słowa radnego (faktyczne), więc bezpieczne, bez LLM.
            "radny": radny,
            "przedmiot": przedmiot,
            "url": url,
        })

    if future_flagged:
        print(f"  UWAGA: {len(future_flagged)} interpelacji z datą wpływu w "
              f"przyszłości (BIP), użyto fallback:")
        for raw_future, used, radny, przedmiot in future_flagged[:10]:
            print(f"    {raw_future} -> {used} [{radny}: {(przedmiot or '')[:50]}]")

    return items


# ── Atom feed ──────────────────────────────────────────

ATOM_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>{title}</title>
  <subtitle>{subtitle}</subtitle>
  <link href="{site_url}/feed.xml" rel="self" type="application/atom+xml"/>
  <link href="{site_url}/" rel="alternate" type="text/html"/>
  <id>{site_url}/</id>
  <updated>{updated}</updated>
  <author>
    <name>Radoskop</name>
    <uri>https://radoskop.pl</uri>
  </author>
"""

ATOM_ENTRY = """  <entry>
    <title>{title}</title>
    <link href="{url}" rel="alternate" type="text/html"/>
    <id>{id}</id>
    <published>{date}T12:00:00+01:00</published>
    <updated>{date}T12:00:00+01:00</updated>
    <summary type="html">{summary}</summary>
    <category term="{type}"/>
  </entry>
"""

ATOM_FOOTER = "</feed>\n"


def generate_atom(items, city_name, city_gen, site_url, max_items=100):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = ATOM_HEADER.format(
        title=f"Radoskop {city_name}",
        subtitle=f"Glosowania, interpelacje i sesje Rady Miasta {city_gen}.",
        site_url=site_url,
        updated=now,
    )
    seen_ids = set()
    count = 0
    for item in items:
        if count >= max_items:
            break
        # Unique ID per entry (Atom requires unique <id>)
        entry_id = item["url"] + "#" + item["type"]
        if entry_id in seen_ids:
            entry_id = item["url"] + "#" + item["type"] + "_" + item["date"]
        if entry_id in seen_ids:
            # Wciaz kolizja: np. dwie interpelacje tego samego radnego z tego
            # samego dnia (wspolny URL profilu). Rozrozniamy hashem tytulu.
            import hashlib as _hl
            tslug = _hl.md5(item.get("title", "").encode("utf-8")).hexdigest()[:8]
            entry_id = item["url"] + "#" + item["type"] + "_" + item["date"] + "_" + tslug
        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)

        out += ATOM_ENTRY.format(
            title=xml_escape(item["title"]),
            url=xml_escape(item["url"]),
            id=xml_escape(entry_id),
            date=item["date"],
            summary=xml_escape(item["summary"]),
            type=item["type"],
        )
        count += 1
    out += ATOM_FOOTER
    return out


# ── HTML news page ─────────────────────────────────────

TYPE_LABELS = {
    "vote": "Glosowanie",
    "session": "Sesja",
    "interpelacja": "Interpelacja",
}

TYPE_COLORS = {
    "vote": "#4f46e5",
    "session": "#2563eb",
    "interpelacja": "#16a34a",
}

POLISH_MONTHS = {
    1: "Styczen", 2: "Luty", 3: "Marzec", 4: "Kwiecien",
    5: "Maj", 6: "Czerwiec", 7: "Lipiec", 8: "Sierpien",
    9: "Wrzesien", 10: "Pazdziernik", 11: "Listopad", 12: "Grudzien",
}


def polish_month(dt):
    return f"{POLISH_MONTHS.get(dt.month, '')} {dt.year}"


def generate_html_page(items, main_html, city_name, city_gen, site_url, max_items=200,
                        news_slug="news"):
    """Generate /{news_slug}/index.html with embedded news content."""
    canonical = f"{site_url}/{news_slug}/"
    title = f"Aktualnosci z Rady Miasta {city_gen}"
    desc = f"Glosowania, interpelacje i sesje Rady Miasta {city_gen}. Automatycznie generowane z danych BIP."

    body_parts = [
        f'<h1>Aktualnosci z Rady Miasta {esc(city_gen)}</h1>',
        f'<p style="color:#6b7280;margin-bottom:24px">Automatycznie generowane z danych BIP. '
        f'<a href="{site_url}/feed.xml" style="color:#4f46e5">Subskrybuj RSS/Atom</a></p>',
    ]

    current_month = ""
    for item in items[:max_items]:
        month = item["date"][:7]
        if month != current_month:
            current_month = month
            try:
                dt = datetime.strptime(month, "%Y-%m")
                month_label = polish_month(dt)
            except ValueError:
                month_label = month
            body_parts.append(
                f'<h2 style="margin-top:32px;font-size:1.1rem;color:#6b7280;'
                f'border-bottom:1px solid #e2e5e9;padding-bottom:8px">{month_label}</h2>'
            )

        color = TYPE_COLORS.get(item["type"], "#6b7280")
        label = TYPE_LABELS.get(item["type"], item["type"])

        body_parts.append(
            f'<div style="padding:12px 0;border-bottom:1px solid #f3f4f6">'
            f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:0.75rem;font-weight:600;'
            f'background:{color}15;color:{color};margin-right:8px">{esc(label)}</span>'
            f'<span style="color:#6b7280;font-size:0.8rem">{esc(item["date"])}</span>'
            f'<div style="margin-top:4px"><a href="{esc(item["url"])}" style="color:#1a1d27;text-decoration:none;font-weight:600">{esc(item["title"])}</a></div>'
            f'<div style="color:#6b7280;font-size:0.85rem;margin-top:2px">{esc(item["summary"])}</div>'
            f'</div>'
        )

    body = "\n".join(body_parts)

    h = main_html
    # Wytnij homepage'owy blok SEO ({{SEO_CONTENT}} = <section id="seo-content">…)
    # zaszyty w index.html dla "/". Na /news/ jego treść (landing miasta) nie ma
    # sensu, a id="seo-content" kolidowałoby z naszym blokiem. Mirror logiki z
    # generate_seo_pages.make_page. Strona główna zachowuje swój blok (nie idzie tędy).
    h = re.sub(
        r'<section id="seo-content">.*?</section>\s*'
        r'<script>var _sc=document\.getElementById\("seo-content"\);'
        r'if\(_sc\)_sc\.style\.display="none";</script>\s*',
        '',
        h,
        flags=re.S,
    )
    h = re.sub(r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{canonical}">', h)
    h = re.sub(r'<title>[^<]*</title>', f'<title>{esc(title)} &mdash; Radoskop {esc(city_name)}</title>', h)
    h = re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{esc(desc)}">', h)
    h = re.sub(r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{esc(title)}">', h)
    h = re.sub(r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{esc(desc)}">', h)
    h = re.sub(r'<meta property="og:url" content="[^"]*">', f'<meta property="og:url" content="{canonical}">', h)
    h = re.sub(r'<meta name="twitter:title" content="[^"]*">', f'<meta name="twitter:title" content="{esc(title)}">', h)
    h = re.sub(r'<meta name="twitter:description" content="[^"]*">', f'<meta name="twitter:description" content="{esc(desc)}">', h)

    rss_link = f'<link rel="alternate" type="application/atom+xml" title="Radoskop {esc(city_name)}" href="{site_url}/feed.xml">'
    if 'application/atom+xml' not in h:
        h = h.replace('</head>', f'{rss_link}\n</head>')

    # WAŻNE: osobny id="news-content", NIE "seo-content". init() w template/index.html
    # robi querySelectorAll('#seo-content').forEach(remove) zanim branch /news/ pokaże
    # treść — gdyby news był pod seo-content, zostałby skasowany i strona byłaby pusta.
    seo_block = f'\n<div id="news-content" style="padding:20px;max-width:800px;margin:0 auto">\n{body}\n</div>\n'
    hide_script = '<script>var sc=document.getElementById("news-content");if(sc)sc.style.display="none";</script>\n'
    h = h.replace('<div id="loading">', seo_block + hide_script + '<div id="loading">')

    return h


# ── City processing ────────────────────────────────────

def process_city(city_dir: Path):
    docs = city_dir / "docs"
    config_path = city_dir / "config.json"

    if not docs.exists() or not config_path.exists():
        print(f"  Skipping {city_dir.name}")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    city_name = config["city_name"]
    city_gen = config["city_genitive"]
    site_url = config["site_url"].rstrip("/")

    # Po migracji 2026-05 wszystkie miasta używają angielskich URL slugów.
    # Worker robi 301 ze starych polskich URL-i. Slug map musi zgadzać się
    # z generate_site.py apply_english_paths i generate_seo_pages.py SLUG.
    slugs = {
        "vote": "vote",
        "session": "session",
        "profile": "profile",
        "interpelacje": "interpellations",
        "news": "news",
    }

    # Load profiles
    profiles_by_name = {}
    profiles_path = docs / "profiles.json"
    if profiles_path.exists():
        with open(profiles_path, "r", encoding="utf-8") as f:
            for p in json.load(f).get("profiles", []):
                profiles_by_name[p["name"]] = p

    # Load kadencja data
    data_path = docs / "data.json"
    if not data_path.exists():
        print(f"  No data.json")
        return

    with open(data_path, "r", encoding="utf-8") as f:
        kadencje = json.load(f).get("kadencje", [])

    # Generate vote and session items from all kadencje
    all_items = []
    for k in kadencje:
        kid = k.get("id", "")
        kad_file = docs / f"kadencja-{kid}.json"
        if not kad_file.exists():
            continue
        with open(kad_file, "r", encoding="utf-8") as f:
            kad_data = json.load(f)
        all_items.extend(generate_vote_items(kad_data, city_name, site_url, slugs["vote"]))
        all_items.extend(generate_session_items(kad_data, city_name, site_url, slugs["session"]))

    # Load and generate interpelacje items
    interp_path = docs / "interpelacje.json"
    if interp_path.exists():
        with open(interp_path, "r", encoding="utf-8") as f:
            interp_raw = json.load(f)
        # Handle both list and dict formats
        if isinstance(interp_raw, list):
            interpelacje = interp_raw
        else:
            interpelacje = interp_raw.get("interpelacje", interp_raw.get("items", []))
        all_items.extend(generate_interpelacje_items(
            interpelacje, city_name, site_url, profiles_by_name,
            slugs["profile"], slugs["interpelacje"],
        ))

    # Deduplicate by URL + type + title. Title musi byc w kluczu: interpelacje
    # tego samego radnego z tego samego dnia linkuja do JEDNEGO profilu, wiec
    # klucz url+type sklejal je w jedna pozycje (Sopot 2026-06-05: bot policzyl
    # 2 z 3 interpelacji). Prawdziwe duplikaty (ta sama pozycja z dwoch plikow
    # kadencji) maja identyczny tytul, wiec nadal odpadaja.
    seen = set()
    unique_items = []
    for item in all_items:
        key = item["url"] + "|" + item["type"] + "|" + item.get("title", "")
        if key not in seen:
            seen.add(key)
            unique_items.append(item)

    # Sort by date desc; w obrębie tej samej daty podsumowanie sesji nad
    # pojedynczymi głosowaniami, żeby w aktualnościach i feedzie news sesji
    # był nad szumem (True > False, reverse podnosi sesję na górę).
    unique_items.sort(key=lambda x: (x["date"], x["type"] == "session"), reverse=True)

    vote_count = sum(1 for i in unique_items if i["type"] == "vote")
    session_count = sum(1 for i in unique_items if i["type"] == "session")
    interp_count = sum(1 for i in unique_items if i["type"] == "interpelacja")
    print(f"  {len(unique_items)} items: {vote_count} votes, {interp_count} interpelacje, {session_count} sessions")

    # Write Atom feed
    atom = generate_atom(unique_items, city_name, city_gen, site_url)
    with open(docs / "feed.xml", "w", encoding="utf-8") as f:
        f.write(atom)

    # Write JSON
    aktualnosci_payload = {
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": unique_items[:500],
    }
    with open(docs / "aktualnosci.json", "w", encoding="utf-8") as f:
        json.dump(aktualnosci_payload, f, ensure_ascii=False, indent=None)

    # Write HTML page
    main_html_path = docs / "index.html"
    with open(main_html_path, "r", encoding="utf-8") as f:
        main_html = f.read()

    page_html = generate_html_page(
        unique_items, main_html, city_name, city_gen, site_url,
        news_slug=slugs["news"],
    )
    aktualnosci_dir = docs / slugs["news"]
    aktualnosci_dir.mkdir(parents=True, exist_ok=True)
    with open(aktualnosci_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(page_html)

    # Add RSS autodiscovery to main index.html if missing
    if 'application/atom+xml' not in main_html:
        rss_link = f'<link rel="alternate" type="application/atom+xml" title="Radoskop {esc(city_name)}" href="{site_url}/feed.xml">'
        main_html = main_html.replace('</head>', f'{rss_link}\n</head>')
        with open(main_html_path, "w", encoding="utf-8") as f:
            f.write(main_html)

    print(f"  feed.xml + aktualnosci/index.html written")


def main():
    parser = argparse.ArgumentParser(description="Generate RSS feeds for Radoskop")
    parser.add_argument("--base", required=True, help="Base directory")
    parser.add_argument("--city", default=None, help="Single city (e.g. radoskop-gdansk)")
    args = parser.parse_args()

    base = Path(args.base)
    if args.city:
        slug = args.city[len("radoskop-"):] if args.city.startswith("radoskop-") else args.city
        cities = [slug]
    else:
        cities = sorted([
            d.name for d in base.iterdir()
            if d.is_dir()
            and (d / "config.json").exists()
            and d.name not in {"radoskop", "_main"}
        ])
        cities = [c[len("radoskop-"):] if c.startswith("radoskop-") else c for c in cities]

    for city in cities:
        city_dir = base / city
        if not city_dir.exists():
            city_dir = base / f"radoskop-{city}"
        if city_dir.exists():
            print(f"\n=== {city} ===")
            process_city(city_dir)


if __name__ == "__main__":
    main()
