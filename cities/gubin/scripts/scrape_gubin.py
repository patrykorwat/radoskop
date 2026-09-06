#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Gubin — głosowania imienne (BIP bip.gubin.pl, rejestr aktów 1394).

BIP = platforma BIP (SYSTEMDOBIP-style, serwery-render). Rejestr 'uchwały Rady
Miejskiej od 2014r.' = /akty/1394/typ/1/ (paginacja /akty/1394/{N}/typ/1/,
10 aktów/strona). Każdy akt ma załączniki: 'uchwała nr RNNN.NNN.RRRR.pdf' ORAZ
'wynik głosowania ...pdf' = jednostronicowy PDF z WARSTWĄ TEKSTU:
  GŁOSOWAŁO: n / głosowało ZA: n / głosowało PRZECIW: n / WSTRZYMAŁO się: n
  LP 1..15 | Nazwisko i Imię | jak głosował  (ZA / PRZECIW / WSTRZYMAŁ SIĘ /
  nie głosował / nieobecny) — pary linia-nazwisko, linia-głos
  'Gubin, dn.: 27 sierpnia 2026r.' + nagłówek punktu 'Podjęcie uchwały w sprawie ...'
Format nazwisk w PDF: 'Nazwisko Imię' (ten sam szyk co roster XML BIP:
/xml/695/1597/Sklad_Rady_Miejskiej_w_Gubinie/wersja/).
Rzymski w numerze uchwały (XXX.216.2026) = numer sesji → sesje grupowane po nim.
eSesja wildcard (korporacyjna), brak AlfaTV/Nefeni. Walidacja: pary nazwisk ==
licznik GŁOSOWAŁO + kategorie per głos.

Użycie: python scrape_gubin.py [city_dir]
"""
import html as htmllib
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://bip.gubin.pl"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
IX_START = "2024-05-07"
ROSTER_XML = "/xml/695/1597/Sklad_Rady_Miejskiej_w_Gubinie/wersja/"
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "wrzesnia": 9, "września": 9,
          "pazdziernika": 10, "października": 10, "listopada": 11, "grudnia": 12,
          # mianownik (2025-r. raporty: '18 grudzień 2025r.')
          "styczen": 1, "styczeń": 1, "luty": 2, "marzec": 3,
          "kwiecien": 4, "kwiecień": 4, "maj": 5, "czerwiec": 6, "lipiec": 7,
          "sierpien": 8, "sierpień": 8, "wrzesien": 9, "wrzesień": 9,
          "pazdziernik": 10, "październik": 10, "listopad": 11,
          "grudzien": 12, "grudzień": 12}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                return r.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + attempt * 2)


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z-]", "", s)


def vote_key(tok: str):
    t = norm_name(tok)
    if t == "za":
        return "za"
    if t.startswith("przeciw"):
        return "przeciw"
    if t.startswith("wstrzym"):
        return "wstrzymal_sie"
    if "nieobecny" in t or "nieobecna" in t:
        return "nieobecni"
    if "nie" in t and "glos" in t:
        return "brak_glosu"
    return None


def parse_wynik(text: str):
    """Return (counts dict, per-category name lists, date, topic) or None."""
    t = text.replace("\u00a0", " ")
    m = re.search(r"G.{0,3}OSOWA.{0,2}O:\s*(\d+)", t, re.I)
    if not m:
        return None
    n_glos = int(m.group(1))
    za = re.search(r"ZA:\s*(\d+)", t)
    p_ = re.search(r"PRZECIW:\s*(\d+)", t)
    w = re.search(r"WSTRZYMA.{0,3}O si.{0,2}:\s*(\d+)", t)
    dm = re.search(r"Gubin, dn\.?:?\s*(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", t)
    if not dm or dm.group(2) not in MONTHS:
        return None
    date = f"{dm.group(3)}-{MONTHS[dm.group(2)]:02d}-{dm.group(1).zfill(2)}"
    tm = re.search(r"(Podj.{0,3}cie|Odczytanie|Przyj.{0,3}cie) uchwa.{0,2}y w sprawie", t, re.I)
    topic = ""
    if tm:
        tail = t[tm.start():]
        cut = re.search(r"Gubin, dn|^[\s\u2013-]*\d{2,4}[\s\u2013-]*$", tail, re.M)
        topic = tail[:cut.start()] if cut else tail[:400]
        topic = re.sub(r"\s+", " ", topic).strip(" \u2013-")
        topic = re.sub(r"[\s\u2013]+\d{1,4}$", "", topic).strip(" \u2013-")
    # body between LP numbers block and 'Gubin, dn'
    lines = [re.sub(r"\s+", " ", l).strip() for l in t.splitlines()]
    lines = [l for l in lines if l]
    try:
        gi = next(i for i, l in enumerate(lines) if re.fullmatch(r"\d+", l))
    except StopIteration:
        return None
    # skip the LP numeric run
    i = gi
    while i < len(lines) and re.fullmatch(r"\d+", lines[i]):
        i += 1
    per = {}
    n_pairs = 0
    while i + 1 < len(lines):
        nm = lines[i]
        vk = vote_key(lines[i + 1])
        if vk and re.match(r"^[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+ [A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+$", nm):
            per.setdefault(vk, []).append(nm)
            n_pairs += 1
            i += 2
            continue
        i += 1
    if not per:
        return None
    counts = {"glosowalo": n_glos,
              "za": int(za.group(1)) if za else len(per.get("za", [])),
              "przeciw": int(p_.group(1)) if p_ else len(per.get("przeciw", [])),
              "wstrzymalo": int(w.group(1)) if w else len(per.get("wstrzymal_sie", []))}
    voted = sum(len(per.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie"))
    if (counts["za"] != len(per.get("za", []))
            or counts["przeciw"] != len(per.get("przeciw", []))
            or counts["wstrzymalo"] != len(per.get("wstrzymal_sie", []))):
        return None
    if voted != n_glos and n_pairs != n_glos:
        return None
    return counts, per, date, topic


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)

    # 1) roster z XML BIP
    roster = []
    try:
        raw = _get(BASE + ROSTER_XML).decode("utf-8")
        for cell in re.findall(r"<div>([^<]+)</div>", raw):
            c = cell.strip()
            if re.match(r"^[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+ [A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+$", c):
                roster.append(c)
        roster = list(dict.fromkeys(roster))
    except Exception as e:
        print(f"  [warn] roster: {e}")
    print(f"[gubin] roster: {len(roster)}")

    # 2) rejestry aktów (od najnowszych), zbieraj strony aktów IX kad.
    act_urls, stop = [], False
    for page in range(1, 40):
        u = f"{BASE}/akty/1394/typ/1/" if page == 1 else f"{BASE}/akty/1394/{page}/typ/1/"
        try:
            t = _get(u).decode("utf-8", "replace")
        except Exception:
            break
        urls = re.findall(r'href="(https://bip\.gubin\.pl/akty/1394/\d+/(?!typ)[^"]+)"', t)
        urls = list(dict.fromkeys(urls))
        if not urls:
            break
        years = [int(y) for y in re.findall(r"uchwala[^/]*?\.(20\d\d)\.html|\.(\d{4})", "")]  # noqa
        ys = [int(m.group(1)) for m in re.finditer(r"nr [IVXLCDM]+\.\d+\.(20\d\d)", t)]
        act_urls.extend(urls)
        print(f"  rejestr str {page}: {len(urls)} aktów, lata {min(ys) if ys else '?'}–{max(ys) if ys else '?'}")
        if ys and max(ys) < 2024:
            stop = True
            break
        time.sleep(0.2)

    # 3) per akt: link 'wynik głosowania'
    tasks = []
    for au in act_urls:
        try:
            t = _get(htmllib.unescape(au)).decode("utf-8", "replace")
        except Exception:
            continue
        links = re.findall(r'<a[^>]*href="(https://bip\.gubin\.pl/system/pobierz\.php\?[^"]+)"[^>]*>(.*?)</a>', t, re.S)
        wl, un = None, None
        for href, lab in links:
            lab = re.sub(r"<[^>]+>", " ", lab)
            lab = re.sub(r"\s+", " ", lab).strip()
            ll = htmllib.unescape(lab).lower().replace("ł", "l")
            if "wynik" in ll and "glos" in ll:
                wl = htmllib.unescape(href)
            um = re.search(r"uchwa[łl]a\s+nr\s+([IVXLCDM]+)\.(\d+)\.(20\d\d)", ll, re.I)
            if um and not un:
                un = um
        if wl and un:
            tasks.append({"url": wl, "title": f"uchwała nr {un.group(1)}.{un.group(2)}.{un.group(3)}",
                          "roman": un.group(1).upper(), "rok": int(un.group(3))})
        time.sleep(0.15)
    tasks = [x for x in tasks if x["rok"] >= 2024]
    print(f"[gubin] aktów z wynikiem glosowania (2024+): {len(tasks)}")

    # 4) pobierz PDF-y, parsuj
    all_names = roster[:]
    idxs = {nm: n for n, nm in enumerate(all_names)}
    votes_out, session_dates = [], {}

    def add_name(nm):
        if nm not in idxs:
            kn = {norm_name(x): x for x in all_names}
            if norm_name(nm) in kn:
                nm = kn[norm_name(nm)]
            else:
                all_names.append(nm)
            idxs[nm] = len(all_names) - 1
        return idxs[nm]

    for tk in tasks:
        if tk["rok"] == 2024:
            pass
        try:
            import pymupdf
            pdf = _get(tk["url"])
            doc = pymupdf.open(stream=pdf, filetype="pdf")
            text = "".join(pg.get_text() for pg in doc)
            if len(text.strip()) < 40:
                continue
            r = parse_wynik(text)
        except Exception as e:
            print(f"  [warn] {tk['title'][:50]}: {e}")
            time.sleep(0.2)
            continue
        if not r:
            print(f"  [skip] {tk['title'][:60]} — parser nie zwalidował")
            continue
        counts, per, date, topic = r
        if date < IX_START:
            continue
        nv = {k: [] for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
        for k, names in per.items():
            nv[k] = sorted({add_name(nm) for nm in names})
        uprawn = sum(len(v) for v in nv.values())
        votes_out.append({
            "id": f"{date}_{len(votes_out):03d}", "source_url": tk["url"],
            "session_date": date, "session_number": tk["roman"],
            "topic": (topic or tk["title"])[:250], "druk": "None",
            "resolution": "przyjete" if counts["za"] > counts["przeciw"] else "odrzucone",
            "counts": {"uprawnieni": uprawn, "za": counts["za"],
                       "przeciw": counts["przeciw"], "wstrzymal_sie": counts["wstrzymalo"]},
            "named_votes": nv})
        if tk["roman"]:
            session_dates[tk["roman"]] = max(session_dates.get(tk["roman"], ""), date)
        time.sleep(0.2)

    votes_out.sort(key=lambda v: v["session_date"])
    sessions = [{"date": d, "number": rn, "label": f"Sesja {rn} ({d})",
                 "vote_count": sum(1 for v in votes_out if v["session_number"] == rn),
                 "attendee_count": None, "attendees": [], "speakers": []}
                for rn, d in sorted(session_dates.items(), key=lambda kv: kv[1])]
    sessions.sort(key=lambda s: s["date"], reverse=True)

    councilors = []
    for i, nm in enumerate(all_names):
        z = p_ = w = b_ = nb = tot = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]: z += 1; tot += 1
            elif i in nv["przeciw"]: p_ += 1; tot += 1
            elif i in nv["wstrzymal_sie"]: w += 1; tot += 1
            elif i in nv["brak_glosu"]: b_ += 1; tot += 1
            elif i in nv["nieobecni"]: nb += 1
        uprawn = tot + nb + b_
        councilors.append({"name": nm, "slug": slugify(nm), "club": "", "role": "",
                           "za": z, "przeciw": p_, "wstrzymal_sie": w,
                           "brak_glosu": b_, "nieobecny": nb, "glosowal": tot,
                           "frekwencja": round(100 * tot / uprawn, 1) if uprawn else 0,
                           "aktywnosc": round(100 * tot / uprawn, 1) if uprawn else 0,
                           "zgodnosc_z_klubem": None, "rebellion_count": 0})
    now = datetime.now(timezone.utc).isoformat()
    kad = {"id": "2024-2029", "label": "IX kadencja (2024–2029)",
           "sessions": sessions, "votes": votes_out,
           "councilor_index": all_names, "councilors": councilors,
           "total_councilors": len(all_names), "total_votes": len(votes_out),
           "similarity_top": [], "similarity_bottom": []}
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {"scraped_at": now, "total": len(councilors), "profiles": [
        {"name": c["name"], "slug": c["slug"], "club": "", "role": "",
         "photo_url": "", "bio": "", "email": "", "social_links": {},
         "voting": {"za": c["za"], "przeciw": c["przeciw"], "wstrzymal_sie": c["wstrzymal_sie"]},
         "kadencje": {"2024-2029": {
             "club": "", "has_voting_data": True, "role": "",
             "frekwencja": c["frekwencja"], "aktywnosc": c["aktywnosc"],
             "zgodnosc_z_klubem": None, "rebellion_count": 0,
             "votes_total": c["glosowal"]}}} for c in councilors]}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"scraped_at": now, "city": "Gubin", "bip": "https://bip.gubin.pl",
            "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
            "stats": {"sessions": len(sessions), "votes": len(votes_out),
                      "councilors": len(all_names)}}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[gubin] DONE: {len(sessions)} sesji / {len(votes_out)} głosów / {len(all_names)} radnych")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("."))
