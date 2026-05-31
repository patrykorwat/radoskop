#!/usr/bin/env python3
"""Build-time generator landingu (/) i listy radnych (/councillors/).

Renderuje szablony Jinja2 z template/landing/ (base + landing + councilors),
teksty UI pochodzą z katalogu landing_strings.STRINGS (klucze EN, źródło PL,
tłumaczenia per locale z fallbackiem locale->en->pl). Liczby/wyróżnienia liczy
z danych miasta (data.json + kadencja-{default}.json) i wstrzykuje do surowego
HTML (SEO). SPA z generate_site.py zachowuje jako app.html (worker fallback).

Wywołanie:
  python3 scripts/build_landing.py --config cities/wroclaw/config.json \
      --docs cities/wroclaw/docs --out cities/wroclaw/docs
"""
import argparse
import html
import json
import os
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape
from landing_strings import catalog
from generate_site import generate_ga_snippet  # emituje tag Umami (nazwa legacy)

KAD_SLUGS = {"2018-2024": "viii", "2024-2029": "ix"}
CONTESTED_DIFF = 10
DEFAULT_BADGE = {"bg": "#e5e7eb", "fg": "#374151"}
OG_LOCALE = {
    "pl": "pl_PL", "en": "en_US", "de": "de_DE", "cs": "cs_CZ", "nl": "nl_NL",
    "sk": "sk_SK", "fr": "fr_FR", "da": "da_DK", "hu": "hu_HU", "uk": "uk_UA",
    "lt": "lt_LT", "lv": "lv_LV", "et": "et_EE",
}


def nf(n):
    return re.sub(r"\B(?=(\d{3})+(?!\d))", " ", str(int(n)))


def fmt_date(s):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else (s or "")


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Obliczenia z danych ────────────────────────────────────────────────
def pick_kadencja(docs):
    data = load_json(os.path.join(docs, "data.json"))
    kid = data.get("default_kadencja")
    if not kid:
        kads = data.get("kadencje") or []
        kid = (kads[0].get("id") if kads else None)
    return kid, data


def compute_context(kad):
    councilors = kad.get("councilors") or []
    votes = kad.get("votes") or []
    sessions = kad.get("sessions") or []

    def diff(v):
        c = v.get("counts") or {}
        return abs((c.get("za") or 0) - (c.get("przeciw") or 0))

    contested = [v for v in votes if (v.get("counts") or {}).get("przeciw", 0) > 0
                 and diff(v) < CONTESTED_DIFF]

    active = max(councilors, key=lambda c: (c.get("aktywnosc") or 0, c.get("votes_total") or 0),
                 default=None)
    rebel = max(councilors, key=lambda c: c.get("rebellion_count") or 0, default=None)
    if rebel and not (rebel.get("rebellion_count") or 0) > 0:
        rebel = None

    def margin(v):
        c = v.get("counts") or {}
        return (c.get("za") or 0) - (c.get("przeciw") or 0)

    real = [v for v in votes if (v.get("counts") or {}).get("przeciw", 0) > 0]
    top_vote = min(real, key=lambda v: (abs(margin(v)),
                   -((v.get("counts") or {}).get("za", 0) + (v.get("counts") or {}).get("przeciw", 0))),
                   default=None)

    by_date = {}
    for v in votes:
        d = v.get("session_date") or ""
        by_date.setdefault(d, {"votes": 0, "contested": 0})
        by_date[d]["votes"] += 1
        if v in contested:
            by_date[d]["contested"] += 1
    ordered = [by_date[d] for d in sorted(by_date)]

    return {
        "total_votes": kad.get("total_votes", len(votes)),
        "total_councilors": kad.get("total_councilors", len(councilors)),
        "total_sessions": kad.get("total_sessions", len(sessions)),
        "contested": len(contested),
        "active": active, "rebel": rebel, "top_vote": top_vote,
        "activity": {"votes": [x["votes"] for x in ordered],
                     "contested": [x["contested"] for x in ordered]},
        "councilors": councilors, "votes": votes,
    }


# ── Komponenty HTML ────────────────────────────────────────────────────
def club_styles(config):
    out = {}
    for name, c in (config.get("clubs") or {}).items():
        out[name] = {"bg": c.get("bg") or DEFAULT_BADGE["bg"],
                     "fg": c.get("color") or DEFAULT_BADGE["fg"]}
    return out


def badge_html(club, styles):
    if not club:
        return ""
    st = styles.get(club, DEFAULT_BADGE)
    return f'<span class="club-badge" style="background:{st["bg"]};color:{st["fg"]}">{esc(club)}</span>'


def hl_title_html(t, key="hero_title"):
    """Zamień [[x]] na <span class="hl">x</span> w tytule hero."""
    return re.sub(r"\[\[(.+?)\]\]", r'<span class="hl">\1</span>', esc(t[key]))


def sporne_rows(contested_votes, limit=5):
    rows = []
    for v in contested_votes[:limit]:
        c = v.get("counts") or {}
        za, prz, ws = c.get("za", 0), c.get("przeciw", 0), c.get("wstrzymal_sie", 0)
        tot = max(za + prz + ws, 1)
        bar = (f'<div style="display:flex;gap:1px;height:14px;">'
               f'<div style="width:{za/tot*100:.0f}%;background:var(--green);opacity:.85;border-radius:1px;"></div>'
               f'<div style="width:{prz/tot*100:.0f}%;background:var(--red);opacity:.85;border-radius:1px;"></div>'
               f'<div style="width:{ws/tot*100:.0f}%;background:var(--yellow);opacity:.85;border-radius:1px;"></div></div>')
        rows.append(
            '<tr>'
            f'<td style="font-weight:600;">{esc(v.get("topic"))}</td>'
            f'<td class="mu">{fmt_date(v.get("session_date"))}</td>'
            f'<td style="text-align:right;color:var(--green);font-weight:600;">{za}</td>'
            f'<td style="text-align:right;color:var(--red);font-weight:600;">{prz}</td>'
            f'<td style="text-align:right;" class="mu">{ws}</td>'
            f'<td style="padding-left:16px;">{bar}</td></tr>')
    return "\n          ".join(rows)


def metric_cell(v):
    if v is None:
        return '<div class="metric"><span class="mv" style="color:var(--muted)">—</span></div>'
    v = round(v)
    color = "var(--green)" if v >= 90 else ("var(--yellow)" if v >= 75 else "var(--red)")
    return (f'<div class="metric"><span class="mv">{v}%</span>'
            f'<span class="bar"><i style="width:{v}%;background:{color}"></i></span></div>')


def roster_payload(councilors):
    return [{"name": c.get("name"), "slug": c.get("slug"), "club": c.get("club") or "—",
             "attendance": c.get("frekwencja"), "votes": c.get("votes_total"),
             "agreement": c.get("zgodnosc_z_klubem")} for c in councilors]


def roster_rows(roster, styles, site_url):
    ordered = sorted(roster, key=lambda r: (r.get("agreement") is None, -(r.get("agreement") or 0)))
    rows = []
    for i, r in enumerate(ordered, 1):
        rows.append(
            '<tr>'
            f'<td class="rank">{i}</td>'
            f'<td class="name"><a href="{site_url}/profile/{esc(r["slug"])}/">{esc(r["name"])}</a></td>'
            f'<td class="hide-sm">{badge_html(r["club"], styles)}</td>'
            f'<td class="num">{metric_cell(r["attendance"])}</td>'
            f'<td class="num hide-sm">{r.get("votes") if r.get("votes") is not None else "—"}</td>'
            f'<td class="num">{metric_cell(r["agreement"])}</td></tr>')
    return "\n        ".join(rows)


def club_options(roster):
    seen = []
    for r in roster:
        if r["club"] not in seen:
            seen.append(r["club"])
    return "\n      ".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in seen)


# ── Konfiguracja / locale ──────────────────────────────────────────────
def city_name(config):
    return config.get("city_name") or config.get("voivodeship_name") or config.get("capital") or ""


def city_genitive(config):
    return config.get("city_genitive") or config.get("voivodeship_genitive") or city_name(config)


def is_assembly(config):
    return bool(config.get("voivodeship_name") or config.get("samorzad_type") == "wojewodztwo")


def is_councilorless(config):
    """Spójne z generate_site._is_councilorless: głosy tylko per frakcja / à main levée."""
    return (str(config.get("voting_mode", "")).lower() == "show_of_hands"
            or str(config.get("voting_display", "")).lower() == "faction")


def is_lightweight(config, ctx):
    # Wariant lekki: brak imiennych głosów per radny => sekcje metryczne bez sensu.
    if is_councilorless(config):
        return True
    if config.get("has_voting_data") is False or config.get("has_named_votes") is False:
        return True
    return ctx["total_votes"] == 0 or not ctx["councilors"]


# ── Główny render ──────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--template-dir", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    tpl_dir = args.template_dir or os.path.join(os.path.dirname(here), "template", "landing")
    config = load_json(args.config)

    kid, _ = pick_kadencja(args.docs)
    kad = load_json(os.path.join(args.docs, f"kadencja-{kid}.json"))
    ctx = compute_context(kad)

    locale = (config.get("locale") or "pl").lower()
    t = catalog(locale)
    styles = club_styles(config)
    roster = roster_payload(ctx["councilors"])
    light = is_lightweight(config, ctx)

    site_url = config["site_url"].rstrip("/")
    name = city_name(config)
    gen = city_genitive(config)
    kad_slug = KAD_SLUGS.get(kid, kid or "")
    kad_label = (kid or "").replace("-", " / ")

    assembly = is_assembly(config)
    eyebrow = (config.get("rada_name_genitive") or config.get("rada_name")) if assembly \
        else t["hero_eyebrow"].format(name=gen)
    # Sejmik: poprawne frazy ("radni województwa mazowieckiego", hero "sejmik województwa").
    insight_name = f"województwa {config.get('voivodeship_genitive', gen)}" if assembly else gen
    hero_key = "hero_title_assembly" if assembly else "hero_title"

    common = {
        "lang": locale, "og_locale": OG_LOCALE.get(locale, "pl_PL"),
        "t": t, "site_url": site_url, "city_name": name, "city_genitive": gen,
        "author": config.get("author", "Radoskop"),
        "bip_url": config.get("bip_url", ""), "bip_name": config.get("bip_name", "BIP"),
        "github_url": config.get("github_url", ""), "root_url": "https://radoskop.pl",
        "umami_snippet": generate_ga_snippet(),
        "site_title": config.get("site_title", f"Radoskop {name}"),
        "site_description": config.get("site_description_short") or config.get("site_description", ""),
        "url_councillors": f"{site_url}/councillors/",
        "url_votes": f"{site_url}/term/{kad_slug}/votes/" if kad_slug else f"{site_url}/councillors/",
        "url_sessions": f"{site_url}/term/{kad_slug}/sessions/" if kad_slug else f"{site_url}/councillors/",
        "url_interpellations": f"{site_url}/interpellations/",
        "url_budget": f"{site_url}/budget/",
        "url_committees": f"{site_url}/commissions/",
    }

    # Loader przeszukuje: template/landing (landing.html, councilors.html),
    # template/partials (base.html), oraz template/ root — żeby include'y
    # "partials/theme_vars.css" i "app/landing.css" w base.html się rozwiązały.
    template_root = os.path.dirname(tpl_dir)
    partials_dir = os.path.join(template_root, "partials")
    env = Environment(loader=FileSystemLoader([tpl_dir, partials_dir, template_root]),
                      autoescape=select_autoescape(default=False))

    # ── Landing context ──
    if light:
        insight = t["insight_light"].format(name=insight_name, n=ctx["total_councilors"])
    else:
        insight = t["insight_full"].format(name=insight_name, votes=nf(ctx["total_votes"]),
                                            contested=nf(ctx["contested"]))

    a, r, tv = ctx["active"], ctx["rebel"], ctx["top_vote"]
    if tv:
        c = tv.get("counts") or {}
        m = abs((c.get("za") or 0) - (c.get("przeciw") or 0))
        vote = {"title": esc(tv.get("topic")),
                "sub": f"{fmt_date(tv.get('session_date'))} · {t['vote_margin'].format(n=m)}",
                "num": f"{c.get('za', 0)}:{c.get('przeciw', 0)}"}
    else:
        vote = {"title": "—", "sub": "", "num": "—"}

    landing_ctx = dict(common,
        eyebrow=eyebrow, kad_label=kad_label,
        hero_title_html=hl_title_html(t, hero_key),
        insight_html=insight, lightweight=light,
        stat_votes=nf(ctx["total_votes"]), stat_councilors=ctx["total_councilors"],
        stat_contested=nf(ctx["contested"]), stat_sessions=ctx["total_sessions"],
        hl={
            "active": {"name": esc(a.get("name")) if a else "—",
                       "badge": badge_html(a.get("club") if a else None, styles),
                       "num": round(a.get("aktywnosc") or 0) if a else 0},
            "rebel": {"name": esc(r.get("name")) if r else "—",
                      "badge": badge_html(r.get("club") if r else None, styles),
                      "num": (r.get("rebellion_count") or 0) if r else 0},
            "vote": vote,
        },
        sporne_rows=sporne_rows(sorted(
            [v for v in (kad.get("votes") or []) if (v.get("counts") or {}).get("przeciw", 0) > 0],
            key=lambda v: v.get("session_date") or "", reverse=True)),
        activity_json=json.dumps(ctx["activity"], ensure_ascii=False),
        proof_note=t["proof_note"].format(bip=esc(config.get("bip_name", "BIP"))),
    )
    landing_html = env.get_template("landing.html").render(**landing_ctx)

    # ── Councillors context ──
    coun_ctx = dict(common,
        councilors_title=t["councilors_title"].format(name=gen),
        councilors_meta_desc=t["sec_highlights_sub"],
        councilors_sub=t["councilors_sub"].format(n=len(roster)),
        club_options=club_options(roster),
        roster_rows=roster_rows(roster, styles, site_url),
        roster_json=json.dumps(roster, ensure_ascii=False),
        club_style_json=json.dumps(styles, ensure_ascii=False),
        i18n_shown_json=json.dumps(t["shown_count"], ensure_ascii=False),
    )
    coun_html = env.get_template("councilors.html").render(**coun_ctx)

    # ── Zapis: SPA -> app.html (idempotentnie), landing -> index.html ──
    os.makedirs(args.out, exist_ok=True)
    out_index = os.path.join(args.out, "index.html")
    out_app = os.path.join(args.out, "app.html")
    if os.path.exists(out_index):
        cur = open(out_index, encoding="utf-8").read()
        if 'class="hero"' not in cur:  # to SPA, nie landing
            with open(out_app, "w", encoding="utf-8") as f:
                f.write(cur)
    with open(out_index, "w", encoding="utf-8") as f:
        f.write(landing_html)

    coun_dir = os.path.join(args.out, "councillors")
    os.makedirs(coun_dir, exist_ok=True)
    with open(os.path.join(coun_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(coun_html)

    print(f"OK kid={kid} locale={locale} light={light} votes={ctx['total_votes']} "
          f"councilors={ctx['total_councilors']} contested={ctx['contested']} roster={len(roster)}")


if __name__ == "__main__":
    main()
