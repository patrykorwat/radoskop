#!/usr/bin/env python3
"""
Generate RSS/Atom feeds and /aktualnosci/ HTML page for Radoskop cities.

Auto-generates news items from voting data:
  1. New sessions (with summary stats)
  2. Controversial votes (za < 65% of voting)
  3. Rebellions (councillor voted against their club on non-procedural topics)
  4. Unanimous rejections (all voted against)

Output:
  - docs/feed.xml        (Atom feed)
  - docs/aktualnosci/index.html  (browsable news page)
  - docs/aktualnosci.json        (structured data for SPA)

Usage:
    python generate_feed.py --base /path/to/gdansk-network
    python generate_feed.py --base /path/to/gdansk-network --city radoskop-gdansk
"""

import argparse
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape


def esc(text):
    return html.escape(str(text), quote=True)


# ── News item types ────────────────────────────────────

PROCEDURAL_RE = re.compile(
    r'protokoł|porządk.*obrad|ślubowani|zamknięci.*obrad|otwarci.*sesji|'
    r'wniosek formalny|włącz?enie druku|odesłanie do komisji|'
    r'tekst.*jednolity|stanowisko nr',
    re.IGNORECASE
)


def is_procedural(topic):
    return bool(PROCEDURAL_RE.search(topic or ''))


def generate_items(kad_data, city_name, site_url, profiles_by_name):
    """Generate news items from kadencja data. Returns list of dicts sorted by date desc."""
    items = []
    kid = kad_data.get("id", "")
    councilors = {c["name"]: c for c in kad_data.get("councilors", [])}

    # Index votes by session
    votes_by_session = {}
    for v in kad_data.get("votes", []):
        snum = v.get("session_number", "")
        votes_by_session.setdefault(snum, []).append(v)

    # 1. Session summaries
    for s in kad_data.get("sessions", []):
        snum = s.get("number", "")
        sdate = s.get("date", "")
        vote_count = s.get("vote_count", 0)
        attendee_count = s.get("attendee_count", 0)

        if not sdate or vote_count == 0:
            continue

        session_votes = votes_by_session.get(snum, [])
        rejected = sum(
            1 for v in session_votes
            if v.get("counts", {}).get("przeciw", 0) > v.get("counts", {}).get("za", 0)
        )
        controversial = sum(
            1 for v in session_votes
            if is_controversial(v)
        )

        summary_parts = [f"{vote_count} glosowan"]
        if rejected:
            summary_parts.append(f"{rejected} odrzuconych")
        if controversial:
            summary_parts.append(f"{controversial} kontrowersyjnych")
        summary_parts.append(f"{attendee_count} obecnych radnych")

        items.append({
            "type": "session",
            "date": sdate,
            "title": f"Sesja {snum} Rady Miasta {city_name}",
            "summary": ". ".join(summary_parts) + ".",
            "url": f"{site_url}/sesja/{snum}/",
            "priority": 3,
        })

    # 2. Controversial votes (za < 65% of those who voted za+przeciw+wstrzym)
    for v in kad_data.get("votes", []):
        if not is_controversial(v):
            continue
        if is_procedural(v.get("topic", "")):
            continue

        c = v.get("counts", {})
        za = c.get("za", 0)
        przeciw = c.get("przeciw", 0)
        wstrzym = c.get("wstrzymal_sie", 0)
        passed = za > przeciw

        topic = clean_topic(v.get("topic", ""))
        vid = v.get("id", "")
        sdate = v.get("session_date", "")

        result_text = "przyjete" if passed else "odrzucone"
        items.append({
            "type": "controversial_vote",
            "date": sdate,
            "title": f"Kontrowersyjne glosowanie: {topic[:80]}",
            "summary": f"Wynik: {result_text} (za {za}, przeciw {przeciw}, wstrzym. {wstrzym}). Sesja {v.get('session_number', '')}.",
            "url": f"{site_url}/glosowanie/{vid}/",
            "priority": 1,
        })

    # 3. Unanimous rejections
    for v in kad_data.get("votes", []):
        c = v.get("counts", {})
        za = c.get("za", 0)
        przeciw = c.get("przeciw", 0)
        if za == 0 and przeciw > 10:
            topic = clean_topic(v.get("topic", ""))
            vid = v.get("id", "")
            items.append({
                "type": "unanimous_reject",
                "date": v.get("session_date", ""),
                "title": f"Jednoglosnie odrzucone: {topic[:80]}",
                "summary": f"Wszyscy glosujacy (przeciw {przeciw}) odrzucili uchwale.",
                "url": f"{site_url}/glosowanie/{vid}/",
                "priority": 2,
            })

    # 4. Notable rebellions (only non-procedural, group by session)
    rebellion_items = {}
    for cname, cdata in councilors.items():
        club = cdata.get("club", "")
        for reb in cdata.get("rebellions", []):
            topic = reb.get("topic", "")
            if is_procedural(topic):
                continue
            sdate = reb.get("session", "")
            key = f"{sdate}_{cname}"
            # Only include if this councillor has few rebellions (notable)
            # or if it's a recent one
            rebellion_items[key] = {
                "type": "rebellion",
                "date": sdate,
                "title": f"{cname} ({club}) zaglosowal/a wbrew klubowi",
                "summary": f"{reb.get('their_vote', '?')} zamiast {reb.get('club_majority', '?')} w sprawie: {clean_topic(topic)[:100]}",
                "url": f"{site_url}/profil/{get_slug(cname, profiles_by_name)}/",
                "priority": 4,
            }

    # Group rebellions by date, limit to max 5 per session to avoid spam
    reb_by_date = {}
    for item in rebellion_items.values():
        reb_by_date.setdefault(item["date"], []).append(item)

    for date, rebs in reb_by_date.items():
        if len(rebs) <= 5:
            items.extend(rebs)
        else:
            # Too many, create a summary item instead
            names = [r["title"].split(" (")[0] for r in rebs[:8]]
            items.append({
                "type": "rebellion_summary",
                "date": date,
                "title": f"{len(rebs)} radnych glosowalo wbrew klubowi",
                "summary": ", ".join(names) + ("..." if len(rebs) > 8 else "") + f". Sesja z {date}.",
                "url": f"{site_url}/",
                "priority": 4,
            })

    # Sort by date desc, then priority asc
    items.sort(key=lambda x: (x["date"], -x["priority"]), reverse=True)

    return items


def is_controversial(vote):
    c = vote.get("counts", {})
    za = c.get("za", 0)
    przeciw = c.get("przeciw", 0)
    wstrzym = c.get("wstrzymal_sie", 0)
    total = za + przeciw + wstrzym
    if total < 5:
        return False
    return przeciw >= 3 and za / total < 0.65


def clean_topic(topic):
    return (topic or "").replace(";", "").strip()


def get_slug(name, profiles_by_name):
    p = profiles_by_name.get(name)
    if p:
        return p["slug"]
    return name.lower().replace(" ", "-")


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
    <id>{url}</id>
    <published>{date}T12:00:00+01:00</published>
    <updated>{date}T12:00:00+01:00</updated>
    <summary type="html">{summary}</summary>
    <category term="{type}"/>
  </entry>
"""

ATOM_FOOTER = "</feed>\n"


def generate_atom(items, city_name, city_gen, site_url, max_items=50):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = ATOM_HEADER.format(
        title=f"Radoskop {city_name}",
        subtitle=f"Aktualnosci z Rady Miasta {city_gen}. Glosowania, sesje, rebelie.",
        site_url=site_url,
        updated=now,
    )
    for item in items[:max_items]:
        out += ATOM_ENTRY.format(
            title=xml_escape(item["title"]),
            url=xml_escape(item["url"]),
            date=item["date"],
            summary=xml_escape(item["summary"]),
            type=item["type"],
        )
    out += ATOM_FOOTER
    return out


# ── HTML news page ─────────────────────────────────────

TYPE_LABELS = {
    "session": "Sesja",
    "controversial_vote": "Kontrowersyjne",
    "unanimous_reject": "Jednoglosnie odrzucone",
    "rebellion": "Wbrew klubowi",
    "rebellion_summary": "Wbrew klubowi",
}

TYPE_COLORS = {
    "session": "#2563eb",
    "controversial_vote": "#dc2626",
    "unanimous_reject": "#ca8a04",
    "rebellion": "#7c3aed",
    "rebellion_summary": "#7c3aed",
}


def generate_html_page(items, main_html, city_name, city_gen, site_url, max_items=100):
    """Generate /aktualnosci/index.html with embedded news content."""
    canonical = f"{site_url}/aktualnosci/"
    title = f"Aktualnosci z Rady Miasta {city_gen}"
    desc = f"Najnowsze glosowania, sesje i rebelie w Radzie Miasta {city_gen}. Automatycznie generowane z danych BIP."

    # Build body content
    body_parts = [
        f'<h1>Aktualnosci z Rady Miasta {esc(city_gen)}</h1>',
        f'<p style="color:#6b7280;margin-bottom:24px">Automatycznie generowane z danych BIP. '
        f'<a href="{site_url}/feed.xml" style="color:#4f46e5">Subskrybuj RSS/Atom</a></p>',
    ]

    current_month = ""
    for item in items[:max_items]:
        month = item["date"][:7]  # YYYY-MM
        if month != current_month:
            current_month = month
            try:
                dt = datetime.strptime(month, "%Y-%m")
                month_label = dt.strftime("%B %Y").capitalize()
                # Polish month names
                month_label = polish_month(dt)
            except ValueError:
                month_label = month
            body_parts.append(f'<h2 style="margin-top:32px;font-size:1.1rem;color:#6b7280;border-bottom:1px solid #e2e5e9;padding-bottom:8px">{month_label}</h2>')

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

    # Reuse make_page pattern from generate_seo_pages
    h = main_html

    h = re.sub(r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{canonical}">', h)
    h = re.sub(r'<title>[^<]*</title>', f'<title>{esc(title)} &mdash; Radoskop {esc(city_name)}</title>', h)
    h = re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{esc(desc)}">', h)
    h = re.sub(r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{esc(title)}">', h)
    h = re.sub(r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{esc(desc)}">', h)
    h = re.sub(r'<meta property="og:url" content="[^"]*">', f'<meta property="og:url" content="{canonical}">', h)
    h = re.sub(r'<meta name="twitter:title" content="[^"]*">', f'<meta name="twitter:title" content="{esc(title)}">', h)
    h = re.sub(r'<meta name="twitter:description" content="[^"]*">', f'<meta name="twitter:description" content="{esc(desc)}">', h)

    # Add RSS autodiscovery link
    rss_link = f'<link rel="alternate" type="application/atom+xml" title="Radoskop {esc(city_name)}" href="{site_url}/feed.xml">'
    h = h.replace('</head>', f'{rss_link}\n</head>')

    # Inject body content
    seo_block = f'\n<div id="seo-content" style="padding:20px;max-width:800px;margin:0 auto">\n{body}\n</div>\n'
    hide_script = '<script>var sc=document.getElementById("seo-content");if(sc)sc.style.display="none";</script>\n'
    h = h.replace('<div id="loading">', seo_block + hide_script + '<div id="loading">')

    return h


POLISH_MONTHS = {
    1: "Styczen", 2: "Luty", 3: "Marzec", 4: "Kwiecien",
    5: "Maj", 6: "Czerwiec", 7: "Lipiec", 8: "Sierpien",
    9: "Wrzesien", 10: "Pazdziernik", 11: "Listopad", 12: "Grudzien",
}


def polish_month(dt):
    return f"{POLISH_MONTHS.get(dt.month, '')} {dt.year}"


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

    # Generate items from all kadencje
    all_items = []
    for k in kadencje:
        kid = k.get("id", "")
        kad_file = docs / f"kadencja-{kid}.json"
        if not kad_file.exists():
            continue
        with open(kad_file, "r", encoding="utf-8") as f:
            kad_data = json.load(f)
        all_items.extend(generate_items(kad_data, city_name, site_url, profiles_by_name))

    # Deduplicate by URL
    seen = set()
    unique_items = []
    for item in all_items:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique_items.append(item)

    # Sort by date desc
    unique_items.sort(key=lambda x: x["date"], reverse=True)

    print(f"  {len(unique_items)} news items ({sum(1 for i in unique_items if i['type']=='session')} sessions, "
          f"{sum(1 for i in unique_items if i['type']=='controversial_vote')} controversial, "
          f"{sum(1 for i in unique_items if i['type'] in ('rebellion','rebellion_summary'))} rebellions)")

    # Write Atom feed
    atom = generate_atom(unique_items, city_name, city_gen, site_url)
    with open(docs / "feed.xml", "w", encoding="utf-8") as f:
        f.write(atom)
    print(f"  feed.xml written")

    # Write JSON (for potential SPA use)
    with open(docs / "aktualnosci.json", "w", encoding="utf-8") as f:
        json.dump({"items": unique_items[:200]}, f, ensure_ascii=False, indent=None)
    print(f"  aktualnosci.json written")

    # Write HTML page
    main_html_path = docs / "index.html"
    with open(main_html_path, "r", encoding="utf-8") as f:
        main_html = f.read()

    page_html = generate_html_page(unique_items, main_html, city_name, city_gen, site_url)
    aktualnosci_dir = docs / "aktualnosci"
    aktualnosci_dir.mkdir(parents=True, exist_ok=True)
    with open(aktualnosci_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(page_html)
    print(f"  aktualnosci/index.html written")

    # Add RSS autodiscovery to main index.html if missing
    if 'application/atom+xml' not in main_html:
        rss_link = f'<link rel="alternate" type="application/atom+xml" title="Radoskop {esc(city_name)}" href="{site_url}/feed.xml">'
        main_html = main_html.replace('</head>', f'{rss_link}\n</head>')
        with open(main_html_path, "w", encoding="utf-8") as f:
            f.write(main_html)
        print(f"  Added RSS autodiscovery to index.html")


def main():
    parser = argparse.ArgumentParser(description="Generate RSS feeds for Radoskop")
    parser.add_argument("--base", required=True, help="Base directory")
    parser.add_argument("--city", default=None, help="Single city (e.g. radoskop-gdansk)")
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


if __name__ == "__main__":
    main()
