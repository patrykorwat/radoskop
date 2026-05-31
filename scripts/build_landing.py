#!/usr/bin/env python3
"""Build-time generator landingu (/) i listy radnych (/councillors/).

Czyta gotowe dane miasta (data.json + kadencja-{default}.json + profiles.json),
liczy kontekst landingu (statystyki, wyroznienia, aktywnosc) i rendereuje
template/landing.html oraz template/councillors.html podstawiajac tokeny {{...}}.

Dane wstrzykiwane sa do SUROWEGO HTML (SEO), nie tylko do JS. Brak zaleznosci od
API w runtime. Wywolanie:

  python3 scripts/build_landing.py --config cities/wroclaw/config.json \
      --docs cities/wroclaw/docs --out cities/wroclaw/docs
"""
import argparse
import html
import json
import os
import re

# Mapa kadencja-id -> rzymski slug URL (spojne z template/index.html KAD_SLUGS).
KAD_SLUGS = {"2018-2024": "viii", "2024-2029": "ix"}
CONTESTED_DIFF = 10  # "sporne": |za - przeciw| < 10
DEFAULT_BADGE = {"bg": "#e5e7eb", "fg": "#374151"}


def nf(n):
    """1234567 -> '1 234 567' (waska spacja jako separator tysiecy)."""
    return re.sub(r"\B(?=(\d{3})+(?!\d))", " ", str(int(n)))


def fmt_date(s):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else (s or "")


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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

    total_votes = kad.get("total_votes", len(votes))
    total_councilors = kad.get("total_councilors", len(councilors))
    total_sessions = kad.get("total_sessions", len(sessions))

    # Sporne: realne glosowanie (przeciw>0) o malej roznicy.
    def diff(v):
        c = v.get("counts") or {}
        return abs((c.get("za") or 0) - (c.get("przeciw") or 0))

    contested = [v for v in votes if (v.get("counts") or {}).get("przeciw", 0) > 0
                 and diff(v) < CONTESTED_DIFF]

    # Najaktywniejszy.
    active = max(councilors, key=lambda c: (c.get("aktywnosc") or 0,
                 c.get("votes_total") or 0), default=None)
    # Najczesciej przeciw klubowi (tylko jesli ktokolwiek > 0).
    rebel = max(councilors, key=lambda c: c.get("rebellion_count") or 0, default=None)
    if rebel and not (rebel.get("rebellion_count") or 0) > 0:
        rebel = None

    # Najbardziej kontrowersyjne: najmniejsza dodatnia roznica za-przeciw.
    def margin(v):
        c = v.get("counts") or {}
        return (c.get("za") or 0) - (c.get("przeciw") or 0)

    real = [v for v in votes if (v.get("counts") or {}).get("przeciw", 0) > 0]
    top_vote = min(real, key=lambda v: (abs(margin(v)), -((v.get("counts") or {}).get("za", 0)
                   + (v.get("counts") or {}).get("przeciw", 0))), default=None)

    # Aktywnosc per sesja (sparkbars).
    by_date = {}
    for v in votes:
        d = v.get("session_date") or ""
        by_date.setdefault(d, {"votes": 0, "contested": 0})
        by_date[d]["votes"] += 1
        if v in contested:
            by_date[d]["contested"] += 1
    ordered = [by_date[d] for d in sorted(by_date)]
    activity = {
        "votes": [x["votes"] for x in ordered],
        "contested": [x["contested"] for x in ordered],
    }

    return {
        "total_votes": total_votes,
        "total_councilors": total_councilors,
        "total_sessions": total_sessions,
        "contested": len(contested),
        "active": active,
        "rebel": rebel,
        "top_vote": top_vote,
        "activity": activity,
        "councilors": councilors,
    }


def club_styles(config):
    out = {}
    for name, c in (config.get("clubs") or {}).items():
        out[name] = {"bg": c.get("bg") or DEFAULT_BADGE["bg"],
                     "fg": c.get("color") or DEFAULT_BADGE["fg"]}
    return out


def badge_html(club, styles):
    st = styles.get(club, DEFAULT_BADGE)
    return (f'<span class="club-badge" style="background:{st["bg"]};color:{st["fg"]}">'
            f'{esc(club)}</span>')


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
            f'<td style="padding-left:16px;">{bar}</td>'
            '</tr>')
    return "\n          ".join(rows)


def roster_payload(councilors):
    out = []
    for c in councilors:
        out.append({
            "name": c.get("name"),
            "slug": c.get("slug"),
            "club": c.get("club") or "Niezrzeszeni",
            "attendance": c.get("frekwencja"),
            "votes": c.get("votes_total"),
            "agreement": c.get("zgodnosc_z_klubem"),
        })
    return out


def metric_cell(v):
    if v is None:
        return '<div class="metric"><span class="mv" style="color:var(--muted)">—</span></div>'
    v = round(v)
    color = "var(--green)" if v >= 90 else ("var(--yellow)" if v >= 75 else "var(--red)")
    return (f'<div class="metric"><span class="mv">{v}%</span>'
            f'<span class="bar"><i style="width:{v}%;background:{color}"></i></span></div>')


def roster_rows(roster, styles, site_url):
    rows = []
    ordered = sorted(roster, key=lambda r: (r.get("agreement") is None,
                     -(r.get("agreement") or 0)))
    for i, r in enumerate(ordered, 1):
        rows.append(
            '<tr>'
            f'<td class="rank">{i}</td>'
            f'<td class="name"><a href="{site_url}/profile/{esc(r["slug"])}/">{esc(r["name"])}</a></td>'
            f'<td class="hide-sm">{badge_html(r["club"], styles)}</td>'
            f'<td class="num">{metric_cell(r["attendance"])}</td>'
            f'<td class="num hide-sm">{r.get("votes") if r.get("votes") is not None else "—"}</td>'
            f'<td class="num">{metric_cell(r["agreement"])}</td>'
            '</tr>')
    return "\n        ".join(rows)


def club_options(roster):
    seen = []
    for r in roster:
        if r["club"] not in seen:
            seen.append(r["club"])
    return "\n      ".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in seen)


def strip_between(text, start, end):
    return re.sub(re.escape(start) + r".*?" + re.escape(end), "", text, flags=re.S)


def base_tokens(config, kid):
    city = config.get("city_name") or config.get("voivodeship_name") or config.get("capital") or ""
    gen = config.get("city_genitive") or config.get("voivodeship_genitive") or ""
    label = (kid or "").replace("-", " / ")
    return {
        "{{SITE_TITLE}}": config.get("site_title", f"Radoskop {city}"),
        "{{SITE_DESCRIPTION}}": config.get("site_description_short")
            or config.get("site_description", ""),
        "{{CITY_NAME}}": city,
        "{{CITY_GENITIVE}}": gen,
        "{{SITE_URL}}": config["site_url"].rstrip("/"),
        "{{AUTHOR}}": config.get("author", "Radoskop"),
        "{{BIP_URL}}": config.get("bip_url", ""),
        "{{BIP_NAME}}": config.get("bip_name", "BIP"),
        "{{GITHUB_URL}}": config.get("github_url", ""),
        "{{ROOT_URL}}": "https://radoskop.pl",
        "{{GA_SNIPPET}}": "",
        "{{DEFAULT_KAD_SLUG}}": KAD_SLUGS.get(kid, kid or ""),
        "{{DEFAULT_KAD_LABEL}}": label,
        "{{KAD_LABEL_SUFFIX}}": f" · kadencja {label}" if label else "",
    }


def render(template, tokens):
    out = template
    for k, v in tokens.items():
        out = out.replace(k, str(v))
    return out


def is_lightweight(config, ctx):
    if config.get("vote_mode") == "faction":
        return True
    if config.get("councilor_roster_mode"):
        return True
    if config.get("has_voting_data") is False:
        return True
    return ctx["total_votes"] == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--docs", required=True, help="katalog z data.json/kadencja-*.json/profiles.json")
    ap.add_argument("--template-dir", default=None)
    ap.add_argument("--out", required=True, help="katalog wyjsciowy (index.html + councillors/index.html)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    tpl_dir = args.template_dir or os.path.join(os.path.dirname(here), "template")
    config = load_json(args.config)

    kid, _ = pick_kadencja(args.docs)
    kad = load_json(os.path.join(args.docs, f"kadencja-{kid}.json"))
    ctx = compute_context(kad)
    styles = club_styles(config)
    roster = roster_payload(ctx["councilors"])
    tokens = base_tokens(config, kid)
    site_url = tokens["{{SITE_URL}}"]
    light = is_lightweight(config, ctx)

    # ---- Landing ----
    landing = open(os.path.join(tpl_dir, "landing.html"), encoding="utf-8").read()

    if light:
        landing = strip_between(landing, "<!--METRICS_START-->", "<!--METRICS_END-->")
        insight = (f"Rada {tokens['{{CITY_GENITIVE}}']} liczy "
                   f"<strong>{ctx['total_councilors']}</strong> radnych. "
                   f"Sprawdź ich pełną listę i kluby.")
        ltokens = {
            "{{HERO_INSIGHT}}": insight,
            "{{STAT_GLOSOWANIA}}": nf(ctx["total_votes"]),
            "{{STAT_RADNYCH}}": ctx["total_councilors"],
            "{{LANDING_DATA_SCRIPT}}": "",
        }
    else:
        a, r, tv = ctx["active"], ctx["rebel"], ctx["top_vote"]
        insight = (f"W tej kadencji radni {tokens['{{CITY_GENITIVE}}']} oddali już "
                   f"<strong>{nf(ctx['total_votes'])} głosów</strong>. "
                   f"Spornych było <strong>{nf(ctx['contested'])}</strong>.")
        if tv:
            c = tv.get("counts") or {}
            vmargin = abs((c.get("za") or 0) - (c.get("przeciw") or 0))
            vote_sub = f"{fmt_date(tv.get('session_date'))} · różnica {vmargin}"
            vote_num = f"{c.get('za', 0)}:{c.get('przeciw', 0)}"
            vote_title = tv.get("topic") or ""
        else:
            vote_sub, vote_num, vote_title = "", "—", "brak danych"
        data_script = ("<script>window.RADOSKOP_ACTIVITY=" +
                       json.dumps(ctx["activity"], ensure_ascii=False) + ";</script>")
        ltokens = {
            "{{HERO_INSIGHT}}": insight,
            "{{STAT_GLOSOWANIA}}": nf(ctx["total_votes"]),
            "{{STAT_RADNYCH}}": ctx["total_councilors"],
            "{{STAT_SPORNE}}": nf(ctx["contested"]),
            "{{STAT_SESJE}}": ctx["total_sessions"],
            "{{HL_ACTIVE_NAME}}": esc(a.get("name")) if a else "—",
            "{{HL_ACTIVE_CLUB}}": esc(a.get("club")) if a else "",
            "{{HL_ACTIVE_CLUBCLASS}}": "",
            "{{HL_ACTIVE_NUM}}": round(a.get("aktywnosc") or 0) if a else 0,
            "{{HL_REBEL_NAME}}": esc(r.get("name")) if r else "—",
            "{{HL_REBEL_CLUB}}": esc(r.get("club")) if r else "",
            "{{HL_REBEL_CLUBCLASS}}": "",
            "{{HL_REBEL_NUM}}": (r.get("rebellion_count") or 0) if r else 0,
            "{{HL_VOTE_TITLE}}": esc(vote_title),
            "{{HL_VOTE_SUB}}": vote_sub,
            "{{HL_VOTE_NUM}}": vote_num,
            "{{SPORNE_ROWS}}": sporne_rows(
                sorted([v for v in (kad.get("votes") or [])
                        if (v.get("counts") or {}).get("przeciw", 0) > 0],
                       key=lambda v: v.get("session_date") or "", reverse=True)),
            "{{LANDING_DATA_SCRIPT}}": data_script,
        }
        # Badge highlightow: inline style z konfiguracji klubow.
        for who, person in (("ACTIVE", a), ("REBEL", r)):
            club = (person or {}).get("club") if person else None
            st = styles.get(club, DEFAULT_BADGE) if club else DEFAULT_BADGE
            landing = landing.replace(
                f'<span class="club-badge {{{{HL_{who}_CLUBCLASS}}}}" id="hl-{who.lower()}-club">{{{{HL_{who}_CLUB}}}}</span>',
                badge_html(club, styles) if club else "")

    landing = render(landing, {**tokens, **ltokens})

    os.makedirs(args.out, exist_ok=True)
    out_index = os.path.join(args.out, "index.html")
    out_app = os.path.join(args.out, "app.html")
    # Zachowaj SPA jako app.html — worker serwuje go fallbackiem dla tras
    # /profile/, /vote/, /session/, /term/... Idempotentnie: gdy index.html to
    # juz landing (rerun bez ponownego generate_site), nie nadpisuj app.html.
    if os.path.exists(out_index):
        cur = open(out_index, encoding="utf-8").read()
        looks_like_landing = 'id="hero-insight"' in cur
        if not looks_like_landing:
            with open(out_app, "w", encoding="utf-8") as f:
                f.write(cur)
    with open(out_index, "w", encoding="utf-8") as f:
        f.write(landing)

    # ---- Councillors ----
    coun = open(os.path.join(tpl_dir, "councillors.html"), encoding="utf-8").read() \
        if os.path.exists(os.path.join(tpl_dir, "councillors.html")) \
        else open(os.path.join(tpl_dir, "councilors.html"), encoding="utf-8").read()
    ctokens = {
        "{{ROSTER_COUNT}}": len(roster),
        "{{CLUB_OPTIONS}}": club_options(roster),
        "{{ROSTER_ROWS}}": roster_rows(roster, styles, site_url),
        "{{ROSTER_JSON}}": json.dumps(roster, ensure_ascii=False),
        "{{CLUB_STYLE_JSON}}": json.dumps(styles, ensure_ascii=False),
    }
    coun = render(coun, {**tokens, **ctokens})
    coun_dir = os.path.join(args.out, "councillors")
    os.makedirs(coun_dir, exist_ok=True)
    with open(os.path.join(coun_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(coun)

    print(f"OK kid={kid} light={light} votes={ctx['total_votes']} "
          f"councilors={ctx['total_councilors']} contested={ctx['contested']} "
          f"roster={len(roster)}")


if __name__ == "__main__":
    main()
