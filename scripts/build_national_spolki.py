#!/usr/bin/env python3
"""
Krajowy rejestr spółek dla radoskop.pl.

Strona pojedynczej spółki jest KRAJOWA (jedna na cały kraj), bo ta sama spółka
pojawia się u wielu jednostek (np. Port Lotniczy Gdańsk u miasta Gdańsk i u
województwa pomorskiego). Subdomeny miast/sejmików mają tylko zakładkę "Spółki"
z listą swoich spółek, która linkuje TUTAJ.

Wejście: radoskop/cities/*/docs/spolki.json + radoskop/assemblies/*/docs/spolki.json
(budowane przez build_spolki.py). Dla każdego pliku czyta sąsiedni config.json
(nazwa jednostki + site_url subdomeny).

Wyjście (apex radoskop/docs/, deploy → radoskop.pl):
  docs/company/{krs}/index.html   — strona spółki (fakty z rejestru)
  docs/companies/index.html       — krajowa lista spółek
  docs/companies.json             — dane krajowe (dedup po KRS)
  docs/companies-sitemap.xml      — sitemap apexu dla /company/ (dodać do indeksu)

Wynik to NEUTRALNE fakty z oficjalnych rejestrów (KRS odpis pełny + MSiG). Bez
zestawiania z radnymi i bez ocen prawnych.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
from pathlib import Path

HUB = "https://radoskop.pl"
ORGANS = (("Zarząd", "zarzad"), ("Rada nadzorcza", "rada_nadzorcza"))


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _collect(base: Path) -> dict:
    """Zbierz spółki ze wszystkich jednostek, dedup po KRS."""
    by_krs: dict[str, dict] = {}
    sources = list((base / "cities").glob("*/docs/spolki.json"))
    sources += list((base / "assemblies").glob("*/docs/spolki.json"))
    for sp in sources:
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        cfg = {}
        cfg_path = sp.parent.parent / "config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                cfg = {}
        unit_name = cfg.get("city_name") or cfg.get("voivodeship_name") or sp.parent.parent.name
        unit_url = (cfg.get("site_url") or "").rstrip("/")
        for co in data.get("companies", []):
            krs = str(co.get("krs", "")).strip()
            if not krs:
                continue
            rec = by_krs.setdefault(krs, {
                "krs": krs, "name": co.get("name", ""),
                "owners": [], "zarzad": [], "rada_nadzorcza": [],
                "historia": [], "units": [],
            })
            if co.get("name") and len(co["name"]) > len(rec["name"]):
                rec["name"] = co["name"]
            for o in co.get("owners", []):
                if o not in rec["owners"]:
                    rec["owners"].append(o)
            for _, key in ORGANS:
                if co.get(key) and not rec[key]:
                    rec[key] = co[key]
            if co.get("historia") and not rec["historia"]:
                rec["historia"] = co["historia"]
            for fk in ("forma_prawna", "nip", "regon", "siedziba", "adres",
                       "kapital", "data_rejestracji", "pkd"):
                if co.get(fk) and not rec.get(fk):
                    rec[fk] = co[fk]
            if co.get("wspolnicy") and not rec.get("wspolnicy"):
                rec["wspolnicy"] = co["wspolnicy"]
            u = {"name": unit_name, "url": unit_url}
            if u not in rec["units"]:
                rec["units"].append(u)
    return by_krs


def _person_index(companies: list[dict]) -> list[dict]:
    """Osoby zasiadające w organach (zarząd/RN) więcej niż jednej spółki.

    FAKT z rejestru, bez ocen prawnych. Klucz = pełne imię i nazwisko; wpisy bez
    dociągniętego nazwiska (anonimizacja KRS) pomijamy, bo nie da się ich
    powiązać. Dla każdej osoby: lista miejsc (spółka, KRS, organ, rola, data)."""
    by_name: dict[str, dict] = {}
    for rec in companies:
        for label, key in ORGANS:
            for m in rec.get(key) or []:
                name = " ".join((m.get("name") or "").split())
                if not name:
                    continue
                p = by_name.setdefault(name, {"name": name, "seats": [], "_krs": set()})
                p["seats"].append({
                    "company": rec["name"], "krs": rec["krs"], "organ": label,
                    "rola": m.get("rola") or "", "od": m.get("od") or "",
                })
                p["_krs"].add(rec["krs"])
    multi = [{"name": p["name"], "company_count": len(p["_krs"]), "seats": p["seats"]}
             for p in by_name.values() if len(p["_krs"]) >= 2]
    multi.sort(key=lambda x: (-x["company_count"], x["name"].lower()))
    return multi


def _links_by_name(multi: list[dict]) -> dict:
    """name → lista {company, krs} (po jednej na spółkę) dla osób w ≥2 spółkach.
    Używane do adnotacji „także w" na stronie pojedynczej spółki."""
    out: dict[str, list[dict]] = {}
    for p in multi:
        seen, lst = set(), []
        for s in p["seats"]:
            if s["krs"] not in seen:
                seen.add(s["krs"])
                lst.append({"company": s["company"], "krs": s["krs"]})
        out[p["name"]] = lst
    return out


# ── HTML ────────────────────────────────────────────────────────────────

_CSS = """
:root{--bg:#f8f9fa;--card:#fff;--txt:#1a1d27;--muted:#5b6472;--line:#e2e8f0;--accent:#4f46e5;--surface:#f1f5f9}
@media (prefers-color-scheme:dark){:root{--bg:#0f1117;--card:#171a22;--txt:#e4e4e7;--muted:#9aa3b2;--line:#2a2f3a;--accent:#818cf8;--surface:#1c212b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:780px;margin:0 auto;padding:28px 18px 60px}a{color:var(--accent)}
.crumb{font-size:13px;color:var(--muted);margin-bottom:14px}
h1{font-size:23px;margin:0 0 6px}.krs{color:var(--muted);font-size:13px;margin-bottom:12px}
.chip{display:inline-block;font-size:12px;padding:3px 10px;border-radius:999px;background:var(--surface);border:1px solid var(--line);color:var(--muted);margin:0 4px 4px 0}
.ot{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:16px 0 6px}
.mem{display:flex;justify-content:space-between;gap:10px;padding:8px 10px;border-radius:8px;background:var(--card);border:1px solid var(--line);margin-bottom:6px}
.mem .tn{color:var(--muted);font-size:12px;white-space:nowrap}
.co{display:block;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--card);margin-bottom:10px;text-decoration:none;color:inherit}
.co b{display:block}.co .s{color:var(--muted);font-size:12px}
.note{color:var(--muted);font-size:12px;margin-top:20px;border-top:1px solid var(--line);padding-top:12px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:4px 0 6px}
.cell{background:var(--surface);border-radius:8px;padding:8px 10px}
.cell .ck{font-size:12px;color:var(--muted);margin-bottom:2px}
.cell .cv{font-size:14px}
"""


def _page(title: str, desc: str, canonical: str, body: str, jsonld: dict | None) -> str:
    ld = ""
    if jsonld:
        payload = json.dumps(jsonld, ensure_ascii=False).replace("<", "\\u003c")
        ld = f'<script type="application/ld+json">{payload}</script>'
    return f"""<!DOCTYPE html>
<html lang="pl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(canonical)}">
<meta name="robots" content="index, follow">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{esc(canonical)}">
<meta property="og:type" content="website">
<style>{_CSS}</style>{ld}
</head><body><div class="wrap">{body}
<p class="note">Dane z oficjalnych rejestrów: KRS (odpis pełny) i Monitor Sądowy i Gospodarczy.
Skład organów to fakty z rejestru. <a href="{HUB}/">radoskop.pl</a></p>
</div></body></html>"""


def _company_body(rec: dict, links_by_name: dict | None = None) -> str:
    links_by_name = links_by_name or {}
    parts = [f'<div class="crumb"><a href="{HUB}/companies/">Spółki</a> ›</div>',
             f"<h1>{esc(rec['name'])}</h1>",
             f'<div class="krs">KRS {esc(rec["krs"])}</div>']
    info_cells = []
    if rec.get("forma_prawna"):
        info_cells.append(("Forma prawna", _forma(rec["forma_prawna"])))
    if rec.get("kapital"):
        info_cells.append(("Kapitał zakładowy", _fmt_kapital(rec["kapital"])))
    if rec.get("data_rejestracji"):
        info_cells.append(("Data rejestracji", _fmt(rec["data_rejestracji"])))
    if rec.get("nip"):
        info_cells.append(("NIP", rec["nip"]))
    if rec.get("regon"):
        info_cells.append(("REGON", _regon9(rec["regon"])))
    if rec.get("adres") or rec.get("siedziba"):
        info_cells.append(("Siedziba", rec.get("adres") or rec.get("siedziba")))
    if info_cells:
        parts.append('<div class="ot">Dane podstawowe</div><div class="kv">'
                     + "".join(f'<div class="cell"><div class="ck">{esc(k)}</div>'
                               f'<div class="cv">{esc(v)}</div></div>' for k, v in info_cells)
                     + "</div>")

    wsp = rec.get("wspolnicy") or []
    if wsp:
        parts.append('<div class="ot">Struktura właścicielska</div>')
        for w in wsp:
            ud = esc(w.get("udzialy") or "")
            ud_html = f'<div class="tn" style="white-space:normal">{ud}</div>' if ud else ""
            parts.append(f'<div class="mem"><span>{esc(w.get("nazwa") or "—")}{ud_html}</span></div>')
    elif rec["owners"]:
        parts.append('<div class="ot">Właściciele</div>')
        parts.append("".join(f'<span class="chip">{esc(o)}</span>' for o in rec["owners"]))

    for label, key in ORGANS:
        mem = rec.get(key) or []
        if not mem:
            continue
        parts.append(f'<div class="ot">{esc(label)}</div>')
        for m in mem:
            name = " ".join((m.get("name") or "").split())
            who = esc(name or m.get("rola") or "—")
            note = f' <span class="tn">({esc(m["note"])})</span>' if m.get("note") else ""
            # Powiązanie osobowe: ta sama osoba w organach innych spółek (fakt).
            also = ""
            others = [c for c in links_by_name.get(name, []) if c["krs"] != rec["krs"]]
            if others:
                lk = ", ".join(f'<a href="{HUB}/company/{esc(c["krs"])}/">{esc(c["company"])}</a>'
                               for c in others)
                also = f'<div class="tn">także w organach: {lk}</div>'
            od = m.get("od")
            when = ""
            if od:
                when = "od " + _fmt(od) + (" · " + _years(od) if _years(od) else "")
            parts.append(f'<div class="mem"><span>{who}{note}{also}</span>'
                         f'<span class="tn">{esc(when)}</span></div>')

    # Pełna historia organów (odpis pełny KRS): aktualni + wykreśleni, z datami.
    hist = rec.get("historia") or []
    if hist:
        parts.append('<div class="ot">Pełna historia organów</div>')
        for okey, olabel in (("zarzad", "Zarząd"), ("rada_nadzorcza", "Rada nadzorcza")):
            rows = [h for h in hist if h.get("organ") == okey]
            if not rows:
                continue
            rows.sort(key=lambda h: (h.get("od") or ""), reverse=True)
            n_anon = sum(1 for h in rows if not h.get("name"))
            sub = f"{olabel} — {len(rows)} w historii"
            if n_anon:
                sub += f", {n_anon} bez pełnego nazwiska"
            parts.append(f'<div class="tn" style="margin:10px 0 4px">{esc(sub)}</div>')
            for h in rows:
                who = esc(h.get("name") or h.get("inicjaly") or "—")
                anon = "" if h.get("name") else ' <span class="tn">(inicjały — KRS anonimizuje JSON)</span>'
                od = _fmt(h["od"]) if h.get("od") else "?"
                end = "obecnie" if h.get("obecnie") else (_fmt(h["do"]) if h.get("do") else "—")
                lata = f' · {h["lata"]} lat' if h.get("lata") is not None else ""
                parts.append(f'<div class="mem"><span>{who}{anon}</span>'
                             f'<span class="tn">{esc(od)} → {esc(end)}{esc(lata)}</span></div>')

    if rec.get("pkd"):
        parts.append('<div class="ot">Przedmiot działalności (PKD)</div>'
                     f'<p style="margin:0 0 4px;font-size:14px">{esc(rec["pkd"])}</p>')

    if rec["units"]:
        links = ", ".join(
            f'<a href="{esc(u["url"])}/companies/">{esc(u["name"])}</a>' if u["url"] else esc(u["name"])
            for u in rec["units"])
        parts.append(f'<div class="ot">Występuje w jednostkach</div><p>{links}</p>')
    return "".join(parts)


def _links_body(multi: list[dict]) -> str:
    parts = [f'<div class="crumb"><a href="{HUB}/companies/">Spółki</a> › Powiązania</div>',
             "<h1>Osoby w organach wielu spółek</h1>",
             f'<p class="krs">{len(multi)} '
             f'{"osoba zasiada" if len(multi) == 1 else "osób zasiada"} w zarządzie lub '
             f'radzie nadzorczej więcej niż jednej spółki z udziałem samorządu. '
             f'Zestawienie faktów z rejestru (KRS, MSiG) — bez ocen prawnych.</p>']
    for p in multi:
        parts.append(f'<div class="ot">{esc(p["name"])} · w {p["company_count"]} spółkach</div>')
        for s in p["seats"]:
            role = s["rola"] or s["organ"]
            od = (" · od " + _fmt(s["od"])) if s["od"] else ""
            parts.append(
                f'<div class="mem"><span><a href="{HUB}/company/{esc(s["krs"])}/">'
                f'{esc(s["company"])}</a> — {esc(role)}</span>'
                f'<span class="tn">{esc(s["organ"])}{esc(od)}</span></div>')
    if not multi:
        parts.append('<p class="krs">Brak osób zasiadających w organach więcej niż '
                     'jednej spółki w bieżących danych.</p>')
    return "".join(parts)


def _fmt(iso: str) -> str:
    p = str(iso).split("-")
    return f"{p[2]}.{p[1]}.{p[0]}" if len(p) == 3 else str(iso)


def _years(iso: str) -> str:
    try:
        d = _dt.date.fromisoformat(iso)
    except Exception:  # noqa: BLE001
        return ""
    days = (_dt.date.today() - d).days
    return f"{days/365.25:.1f} lat" if days >= 0 else ""


_FORMA = {
    "SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ": "spółka z o.o.",
    "SPÓŁKA AKCYJNA": "spółka akcyjna",
    "PROSTA SPÓŁKA AKCYJNA": "prosta spółka akcyjna",
}


def _forma(s: str) -> str:
    return _FORMA.get((s or "").strip().upper(), (s or "").lower())


def _fmt_kapital(k: dict) -> str:
    raw = str(k.get("wartosc") or "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        s = f"{int(round(float(raw))):,}".replace(",", " ")
    except Exception:  # noqa: BLE001
        s = str(k.get("wartosc") or "")
    cur = (k.get("waluta") or "PLN").upper()
    return f"{s} {'zł' if cur == 'PLN' else cur}".strip()


def _regon9(r) -> str:
    r = str(r or "")
    return r[:9] if len(r) == 14 and r.endswith("00000") else r


def main() -> int:
    ap = argparse.ArgumentParser(description="Krajowy rejestr spółek (apex radoskop.pl)")
    ap.add_argument("--base", type=Path, default=Path("radoskop"),
                    help="katalog z cities/ i assemblies/ (domyślnie radoskop)")
    ap.add_argument("--out", type=Path, default=None,
                    help="apex docs (domyślnie {base}/docs)")
    args = ap.parse_args()
    out = args.out or (args.base / "docs")

    by_krs = _collect(args.base)
    companies = sorted(by_krs.values(), key=lambda r: r["name"].lower())
    today = _dt.date.today().isoformat()

    # Powiązania osobowe: osoby w organach >1 spółki (fakt z rejestru).
    multi = _person_index(companies)
    links_by_name = _links_by_name(multi)

    # companies.json
    (out).mkdir(parents=True, exist_ok=True)
    (out / "companies.json").write_text(
        json.dumps({"generated_at": today, "companies": companies}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out / "companies-links.json").write_text(
        json.dumps({"generated_at": today, "people": multi}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # per-company pages
    sitemap = [f"{HUB}/companies/", f"{HUB}/companies/powiazania/"]
    for rec in companies:
        cu = f"{HUB}/company/{rec['krs']}/"
        title = f"{rec['name']} – organy spółki – Radoskop"
        desc = (f"{rec['name']} (KRS {rec['krs']}): właściciele oraz skład zarządu "
                f"i rady nadzorczej. Dane z KRS i Monitora Sądowego i Gospodarczego.")
        ld = {"@context": "https://schema.org", "@type": "Organization",
              "name": rec["name"], "identifier": f"KRS {rec['krs']}", "url": cu}
        page = _page(title, desc, cu, _company_body(rec, links_by_name), ld)
        d = out / "company" / rec["krs"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(page, encoding="utf-8")
        sitemap.append(cu)

    # strona powiązań osobowych
    links_page = _page(
        "Osoby w organach wielu spółek – Radoskop",
        "Osoby zasiadające w zarządach i radach nadzorczych więcej niż jednej spółki "
        "z udziałem samorządu. Fakty z KRS i Monitora Sądowego i Gospodarczego.",
        f"{HUB}/companies/powiazania/", _links_body(multi), None)
    (out / "companies" / "powiazania").mkdir(parents=True, exist_ok=True)
    (out / "companies" / "powiazania" / "index.html").write_text(links_page, encoding="utf-8")

    # national index
    items = []
    for rec in companies:
        units = ", ".join(u["name"] for u in rec["units"])
        items.append(f'<a class="co" href="{HUB}/company/{rec["krs"]}/"><b>{esc(rec["name"])}</b>'
                     f'<span class="s">KRS {esc(rec["krs"])}{(" · " + esc(units)) if units else ""}</span></a>')
    teaser = ""
    if multi:
        teaser = (f'<p><a href="{HUB}/companies/powiazania/"><b>Osoby w organach wielu spółek</b></a> — '
                  f'{len(multi)} {"osoba zasiada" if len(multi) == 1 else "osób zasiada"} '
                  f'w zarządzie lub radzie nadzorczej więcej niż jednej spółki.</p>')
    idx_body = (f"<h1>Spółki z udziałem samorządów</h1>"
                f'<p class="krs">{len(companies)} spółek. Dane z KRS i MSiG, aktualizacja {today}.</p>'
                + teaser + "".join(items))
    idx = _page("Spółki samorządowe – Radoskop", "Krajowy rejestr spółek z udziałem samorządów: "
                "zarządy i rady nadzorcze. Dane z KRS i Monitora Sądowego i Gospodarczego.",
                f"{HUB}/companies/", idx_body, None)
    (out / "companies").mkdir(parents=True, exist_ok=True)
    (out / "companies" / "index.html").write_text(idx, encoding="utf-8")

    # sitemap (urlset) dla apexu — dodać do sitemap-index radoskop.pl
    urls = "".join(f"<url><loc>{esc(u)}</loc><changefreq>monthly</changefreq></url>" for u in sitemap)
    (out / "companies-sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + "</urlset>",
        encoding="utf-8")

    print(f"Krajowy rejestr: {len(companies)} spółek → {out}/company/, {out}/companies/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
