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

# Tracker Umami — te same wartości co generate_site.generate_ga_snippet
# (stats.radoskop.pl). Strony spółek to standalone HTML poza base.html/SPA,
# więc snippet trzeba wstrzyknąć tutaj osobno.
_UMAMI = ('<script async defer data-website-id="792c059f-c77e-4b4e-ad9c-31f4a7d5cfe4" '
          'src="https://stats.radoskop.pl/script.js"></script>')


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
    """Osoby zasiadające w organach (zarząd/RN) więcej niż jednej spółki —
    OBECNIE LUB W PRZESZŁOŚCI (z pełnej historii organów).

    FAKT z rejestru, bez ocen prawnych. Klucz = pełne imię i nazwisko; wpisy bez
    dociągniętego nazwiska (anonimizacja KRS) pomijamy, bo nie da się ich
    powiązać. Źródło: historia (aktualni + wykreśleni); gdy brak historii dla
    spółki — bieżący skład. Każde miejsce: spółka, KRS, organ, okres, czy
    obecnie."""
    by_name: dict[str, dict] = {}

    def _add(name, rec, organ, od, do, obecnie):
        name = " ".join((name or "").split())
        if not name:
            return
        p = by_name.setdefault(name, {"name": name, "seats": [], "_seen": set(),
                                      "_krs": set(), "_cur_krs": set()})
        sig = (rec["krs"], organ, od or "")
        if sig not in p["_seen"]:
            p["_seen"].add(sig)
            p["seats"].append({"company": rec["name"], "krs": rec["krs"],
                               "organ": organ, "od": od or "", "do": do or "",
                               "obecnie": bool(obecnie)})
        elif obecnie:
            for s in p["seats"]:
                if (s["krs"], s["organ"], s["od"]) == sig:
                    s["obecnie"] = True
        p["_krs"].add(rec["krs"])
        if obecnie:
            p["_cur_krs"].add(rec["krs"])

    for rec in companies:
        hist = rec.get("historia") or []
        if hist:
            for h in hist:
                organ = "Zarząd" if h.get("organ") == "zarzad" else "Rada nadzorcza"
                _add(h.get("name"), rec, organ, h.get("od"), h.get("do"), h.get("obecnie"))
        else:
            for label, key in ORGANS:
                for m in rec.get(key) or []:
                    _add(m.get("name"), rec, label, m.get("od"), None, True)

    multi = []
    for p in by_name.values():
        if len(p["_krs"]) >= 2:
            p["seats"].sort(key=lambda s: (s["od"] or ""), reverse=True)
            multi.append({"name": p["name"], "company_count": len(p["_krs"]),
                          "current_count": len(p["_cur_krs"]), "seats": p["seats"]})
    multi.sort(key=lambda x: (-x["company_count"], x["name"].lower()))
    return multi


def _links_by_name(multi: list[dict]) -> dict:
    """name → lista {company, krs, current} (po jednej na spółkę) dla osób w ≥2
    spółkach. current=True gdy osoba zasiada tam OBECNIE; inaczej relacja
    historyczna. Używane do adnotacji „także w organach"."""
    out: dict[str, list[dict]] = {}
    for p in multi:
        by_krs: dict[str, dict] = {}
        for s in p["seats"]:
            e = by_krs.setdefault(s["krs"], {"company": s["company"],
                                             "krs": s["krs"], "current": False})
            if s.get("obecnie"):
                e["current"] = True
        out[p["name"]] = list(by_krs.values())
    return out


def _also_html(name: str, rec_krs: str, links_by_name: dict) -> str:
    """„także w organach: …" — linki do innych spółek, w których osoba
    zasiada(ła). Relacje wyłącznie historyczne oznaczone „(dawniej)"."""
    name = " ".join((name or "").split())
    others = [c for c in links_by_name.get(name, []) if c["krs"] != rec_krs]
    if not others:
        return ""
    others.sort(key=lambda c: (not c.get("current"), c["company"].lower()))
    lk = ", ".join(
        f'<a href="{HUB}/company/{esc(c["krs"])}/">{esc(c["company"])}'
        f'{"" if c.get("current") else " (dawniej)"}</a>' for c in others)
    return f'<div class="tn">także w organach: {lk}</div>'


def _ktomaco_link(name: str | None, ktomaco_map: dict[str, str]) -> str:
    """HTML dla linku do ktomaco.pl — oświadczenia majątkowe."""
    if not name or not ktomaco_map:
        return ""
    name_norm = " ".join(name.strip().split()).lower()
    kt_slug = ktomaco_map.get(name_norm)
    if not kt_slug:
        return ""
    return (f' <a href="https://ktomaco.pl/osoba/{esc(kt_slug)}/" '
            f'target="_blank" rel="noopener" '
            f'style="color:var(--accent);font-size:11px;white-space:nowrap">'
            f'💰 oświadczenia</a>')


# ── HTML ────────────────────────────────────────────────────────────────

# Paleta i font 1:1 z głównym serwisem (template/partials/theme_vars.css +
# app/app.css), żeby apex wyglądał jak radoskop.eu. Motyw light/dark przez to
# samo cookie radoskop_theme (cross-subdomain).
_CSS = """
:root,[data-theme=light]{--bg:#f8f9fa;--surface:#fff;--border:#e2e5e9;--text:#1a1d27;--muted:#6b7280;--accent:#4f46e5;--green:#16a34a;--red:#dc2626}
[data-theme=dark]{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e4e4e7;--muted:#8b8d97;--accent:#6366f1;--green:#22c55e;--red:#ef4444}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.5}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.topbar{position:sticky;top:0;z-index:100;background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.topbar-logo{font-size:1.05rem;font-weight:700;color:var(--text)}.topbar-logo span{color:var(--accent)}
.topbar-nav{display:flex;gap:20px;flex-wrap:wrap}.topbar-nav a{font-size:.88rem;color:var(--muted)}.topbar-nav a:hover{color:var(--accent);text-decoration:none}
.topbar-actions{display:flex;gap:6px;align-items:center}
.auth-btn{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:.85rem;font-weight:500}.auth-btn:hover{opacity:.9}
.user-chip{display:inline-flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:4px 10px 4px 4px;font-size:.85rem;color:var(--text)}
.user-chip .user-avatar{width:24px;height:24px;border-radius:50%;background:var(--accent);color:#fff;display:inline-flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:600}
.user-chip .user-logout{background:none;border:none;color:var(--muted);cursor:pointer;padding:0 2px;font-size:1rem;line-height:1}.user-chip .user-logout:hover{color:var(--accent)}
.theme-toggle{background:none;border:1px solid var(--border);border-radius:8px;padding:6px 10px;cursor:pointer;color:var(--muted);font-size:.85rem;display:inline-flex;align-items:center;gap:4px}.theme-toggle:hover{border-color:var(--accent);color:var(--accent)}
.wrap{max-width:780px;margin:0 auto;padding:24px 18px 40px}
.crumb{font-size:13px;color:var(--muted);margin-bottom:14px}
h1{font-size:23px;margin:0 0 6px}.krs{color:var(--muted);font-size:13px;margin-bottom:12px}
.chip{display:inline-block;font-size:12px;padding:3px 10px;border-radius:999px;background:var(--bg);border:1px solid var(--border);color:var(--muted);margin:0 4px 4px 0}
.ot{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:16px 0 6px}
.mem{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;padding:8px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border);margin-bottom:6px}
.mem>span:first-child{flex:1;min-width:0;overflow-wrap:break-word}
.mem .tn{color:var(--muted);font-size:12px}
.mem>.tn{white-space:nowrap;flex-shrink:0;text-align:right}
.co{display:block;padding:12px 14px;border:1px solid var(--border);border-radius:12px;background:var(--surface);margin-bottom:10px;text-decoration:none;color:inherit}
.co:hover{border-color:var(--accent)}.co b{display:block}.co .s{color:var(--muted);font-size:12px}
.note{color:var(--muted);font-size:12px;margin-top:20px;border-top:1px solid var(--border);padding-top:12px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:4px 0 6px}
.cell{background:var(--bg);border-radius:8px;padding:8px 10px}
.cell .ck{font-size:12px;color:var(--muted);margin-bottom:2px}
.cell .cv{font-size:14px}
footer{text-align:center;padding:40px 18px;color:var(--muted);font-size:.8rem;border-top:1px solid var(--border);margin-top:40px}footer a{color:var(--accent)}
@media(max-width:768px){.topbar{padding:8px 12px;gap:8px;flex-wrap:wrap}.topbar-nav{gap:14px;order:3;flex-basis:100%;justify-content:center}}
"""

# Anti-FOUC: ustaw motyw z cookie radoskop_theme PRZED renderem (jak head.html).
_THEME_JS = r"""try{var m=document.cookie.match(/(?:^|;\s*)radoskop_theme=([^;]+)/);var t=m?decodeURIComponent(m[1]):null;if(t!=='dark'&&t!=='light'){try{var s=localStorage.getItem('radoskop_theme');if(s==='dark'||s==='light')t=s;}catch(e){}}if(!t)t=(window.matchMedia&&matchMedia('(prefers-color-scheme:dark)').matches)?'dark':'light';document.documentElement.setAttribute('data-theme',t);}catch(e){}"""

# Wspólny chrome (motyw + pasek logowania) z KANONICZNEGO template/app/chrome.js
# — jedno źródło z głównym serwisem, zamiast własnej kopii. Brak pliku → pusty
# string (strona degraduje łagodnie, treść i tak jest prerenderem).
try:
    _CHROME_JS = (Path(__file__).resolve().parent.parent
                  / "template" / "app" / "chrome.js").read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _CHROME_JS = ""

# Nawigacja klienta (pjax): klik w link spółki podmienia tylko treść #app bez
# przeładowania. Progresywne — bez JS zwykłe linki działają, a każda strona to
# pełny prerender (SEO bez zmian). Toggle motywu/auth są w _CHROME_JS.
_APP_JS = r"""
(function(){var app=document.getElementById('app');if(!app)return;
function internal(a){try{var u=new URL(a.href);return u.origin===location.origin&&/^\/(company|companies)(\/|$)/.test(u.pathname);}catch(e){return false;}}
async function load(url,push){try{var r=await fetch(url);if(!r.ok){location.href=url;return;}var t=await r.text();var doc=new DOMParser().parseFromString(t,'text/html');var na=doc.getElementById('app');if(!na){location.href=url;return;}app.innerHTML=na.innerHTML;if(doc.title)document.title=doc.title;if(push)history.pushState({},'',url);window.scrollTo(0,0);}catch(e){location.href=url;}}
document.addEventListener('click',function(e){var a=e.target.closest?e.target.closest('a'):null;if(!a||a.target||e.metaKey||e.ctrlKey||e.shiftKey||e.button)return;if(internal(a)){e.preventDefault();load(a.href,true);}});
window.addEventListener('popstate',function(){load(location.href,false);});})();
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
<script>{_THEME_JS}</script>
<style>{_CSS}</style>{ld}{_UMAMI}
</head><body>
<nav class="topbar" aria-label="Główna nawigacja">
<a class="topbar-logo" href="https://radoskop.eu/">Rado<span>skop</span></a>
<div class="topbar-nav"><a href="https://radoskop.eu/">Strona główna</a><a href="{HUB}/companies/">Spółki</a><a href="https://radoskop.eu/planning/">Planowanie</a><a href="https://radoskop.eu/reports/">Raporty</a><a href="https://radoskop.eu/pro/" class="nav-sales">Pro</a><a href="https://radoskop.eu/pricing/" class="nav-sales">Cennik</a></div>
<div class="topbar-actions">
<button class="auth-btn" id="auth-login-btn" onclick="_bridgeLogin()" style="display:none">Zaloguj się</button>
<div class="user-chip" id="user-chip" style="display:none"><button id="user-chip-link" onclick="goToProfile()" aria-label="Moje konto" title="Moje konto" style="background:none;border:none;padding:0;display:inline-flex;align-items:center;gap:8px;cursor:pointer;color:inherit;font:inherit"><span class="user-avatar" id="user-avatar"></span><span id="user-name" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span></button><button class="user-logout" onclick="doLogout()" aria-label="Wyloguj" title="Wyloguj">×</button></div>
<button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()" aria-label="Przełącz motyw jasny/ciemny">◐</button>
</div></nav>
<main id="app" class="wrap">{body}
<p class="note">Dane z oficjalnych rejestrów: KRS (odpis pełny) i Monitor Sądowy i Gospodarczy.
Skład organów to fakty z rejestru.</p>
</main>
<footer>
<div style="margin-bottom:12px"><a href="https://radoskop.eu/">Radoskop</a> · <a href="https://radoskop.eu/#cities">wszystkie miasta</a> · <a href="https://x.com/radoskop" target="_blank" rel="noopener me">X (Twitter)</a> · <a href="https://bsky.app/profile/radoskop.bsky.social" target="_blank" rel="noopener me">Bluesky</a></div>
<div style="margin-bottom:12px"><a href="https://radoskop.eu/planning/">Planowanie</a> · <a href="https://radoskop.eu/reports/">Raporty</a> · <a href="https://radoskop.eu/pricing/">Cennik</a> · <a href="https://radoskop.eu/pro/">Pro</a> · <a href="https://radoskop.eu/privacy/">Polityka prywatności</a> · <a href="https://radoskop.eu/terms/">Regulamin</a></div>
<div style="font-size:.75rem">Dane źródłowe: KRS i Monitor Sądowy i Gospodarczy · Kod otwarty (AGPL-3.0)</div>
</footer>
<script>{_CHROME_JS}</script>
<script>{_APP_JS}</script>
</body></html>"""


def _company_body(rec: dict, links_by_name: dict | None = None,
                  ktomaco_map: dict[str, str] | None = None) -> str:
    links_by_name = links_by_name or {}
    ktomaco_map = ktomaco_map or {}
    KTOMCO_BASE = "https://ktomaco.pl/osoba"
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
        info_cells.append(("Siedziba", _pl_title(rec.get("adres") or rec.get("siedziba"))))
    if info_cells:
        parts.append('<div class="ot">Dane podstawowe</div><div class="kv">'
                     + "".join(f'<div class="cell"><div class="ck">{esc(k)}</div>'
                               f'<div class="cv">{esc(v)}</div></div>' for k, v in info_cells)
                     + "</div>")

    wsp = rec.get("wspolnicy") or []
    if wsp:
        parts.append('<div class="ot">Struktura właścicielska</div>')
        for w in wsp:
            ud = esc(_delower(w.get("udzialy") or ""))
            ud_html = f'<div class="tn" style="white-space:normal">{ud}</div>' if ud else ""
            parts.append(f'<div class="mem"><span>{esc(_pl_title(w.get("nazwa") or "—"))}{ud_html}</span></div>')
    elif rec["owners"]:
        parts.append('<div class="ot">Właściciele</div>')
        parts.append("".join(f'<span class="chip">{esc(_pl_title(o))}</span>' for o in rec["owners"]))

    for label, key in ORGANS:
        mem = rec.get(key) or []
        if not mem:
            continue
        parts.append(f'<div class="ot">{esc(label)}</div>')
        for m in mem:
            name = " ".join((m.get("name") or "").split())
            who = esc(name or m.get("rola") or "—")
            note = f' <span class="tn">({esc(m["note"])})</span>' if m.get("note") else ""
            # Powiązanie osobowe: ta sama osoba w organach innych spółek
            # (obecnie lub historycznie) — fakt z rejestru.
            also = _also_html(name, rec["krs"], links_by_name)
            _kt = _ktomaco_link(name, ktomaco_map)
            od = m.get("od")
            when = ""
            if od:
                when = "od " + _fmt(od) + (" · " + _years(od) if _years(od) else "")
            parts.append(f'<div class="mem"><span>{who}{note}{also}{_kt}</span>'
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
                has_name = bool(h.get("name"))
                who = esc(h.get("name") or h.get("funkcja") or h.get("inicjaly") or "—")
                if not has_name:
                    why = "nazwisko zanonimizowane w rejestrze" if h.get("funkcja") \
                          else "inicjały — KRS anonimizuje JSON"
                    anon = f' <span class="tn">({why})</span>'
                else:
                    anon = ""
                also = _also_html(h.get("name"), rec["krs"], links_by_name)
                _kt_hist = _ktomaco_link(h.get("name"), ktomaco_map)
                od = _fmt(h["od"]) if h.get("od") else "?"
                end = "obecnie" if h.get("obecnie") else (_fmt(h["do"]) if h.get("do") else "—")
                lata = (' · ' + _lata(h["lata"])) if h.get("lata") is not None else ""
                parts.append(f'<div class="mem"><span>{who}{anon}{also}{_kt_hist}</span>'
                             f'<span class="tn">{esc(od)} → {esc(end)}{esc(lata)}</span></div>')

    if rec.get("pkd"):
        pkd = rec["pkd"]
        if " — " in pkd:
            kod, opis = pkd.split(" — ", 1)
            if opis.isupper():
                opis = opis.capitalize()
            pkd = f"{kod} — {opis}"
        parts.append('<div class="ot">Przedmiot działalności (PKD)</div>'
                     f'<p style="margin:0 0 4px;font-size:14px">{esc(pkd)}</p>')

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
            od = _fmt(s["od"]) if s.get("od") else "?"
            end = "obecnie" if s.get("obecnie") else (_fmt(s["do"]) if s.get("do") else "—")
            parts.append(
                f'<div class="mem"><span><a href="{HUB}/company/{esc(s["krs"])}/">'
                f'{esc(s["company"])}</a> — {esc(s["organ"])}</span>'
                f'<span class="tn">{esc(od)} → {esc(end)}</span></div>')
    if not multi:
        parts.append('<p class="krs">Brak osób zasiadających w organach więcej niż '
                     'jednej spółki w bieżących danych.</p>')
    return "".join(parts)


def _fmt(iso: str) -> str:
    p = str(iso).split("-")
    return f"{p[2]}.{p[1]}.{p[0]}" if len(p) == 3 else str(iso)


def _lata(val) -> str:
    """'X,Y lat' z przecinkiem dziesiętnym (PL)."""
    try:
        return f"{float(val):.1f}".replace(".", ",") + " lat"
    except (TypeError, ValueError):
        return ""


def _years(iso: str) -> str:
    try:
        d = _dt.date.fromisoformat(iso)
    except Exception:  # noqa: BLE001
        return ""
    days = (_dt.date.today() - d).days
    return _lata(days / 365.25) if days >= 0 else ""


_PL_LOWER = {"z", "i", "w", "o", "u", "od", "do", "na", "po", "za", "oraz",
             "sp.", "o.o.", "z.o.o."}


def _pl_title(s) -> str:
    """ALL-CAPS z KRS (nazwa podmiotu, adres) → Forma Tytułowa, z drobnymi
    wyjątkami PL i form prawnych (sp. z o.o., S.A.). Stringi już w mieszanej
    wielkości liter zostawiamy bez zmian."""
    s = str(s or "")
    if not s or not s.isupper():
        return s
    out = []
    for i, w in enumerate(s.title().split()):
        lw = w.lower()
        if lw in ("s.a.", "sa"):
            out.append("S.A.")
        elif i > 0 and lw in _PL_LOWER:
            out.append(lw)
        else:
            out.append(w)
    return " ".join(out)


def _delower(s) -> str:
    """ALL-CAPS opis (np. udziały) → małe litery."""
    s = str(s or "")
    return s.lower() if s.isupper() else s


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

    # Krajowa mapa ktomaco.pl — agreguj z wszystkich miast
    ktomaco_map: dict[str, str] = {}
    for kt_path in sorted(args.base.glob("cities/*/docs/ktomaco.json")):
        try:
            _m = json.loads(kt_path.read_text(encoding="utf-8"))
            for _radoskop_slug, _kt_slug in _m.items():
                # Znajdź nazwisko radnego z profiles.json
                _prof_path = kt_path.parent / "profiles.json"
                if _prof_path.exists():
                    try:
                        _pd = json.loads(_prof_path.read_text(encoding="utf-8"))
                        for _p in _pd.get("profiles", []):
                            if _p.get("slug") == _radoskop_slug:
                                _name = " ".join((_p.get("name") or "").split()).lower()
                                if _name:
                                    ktomaco_map[_name] = _kt_slug
                                break
                    except Exception:
                        pass
        except Exception:
            pass

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
        page = _page(title, desc, cu, _company_body(rec, links_by_name, ktomaco_map), ld)
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
