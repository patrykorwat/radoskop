#!/usr/bin/env python3
"""Radoskop Kudowa-Zdrój — scraper BIP (bip.kudowa.pl, platforma 2clickportal).

Rada Miejska Kudowy-Zdroju publikuje w BIP rejestr "Głosowania RM"
(https://bip.kudowa.pl/rejestr-glosowania-rm.html, paginacja ?page=N) z
per-sesyjnymi stronami "WYKAZ GŁOSOWAŃ <roman> sesja ... z dnia <data>",
których załącznik PDF (files/file_add/download/*.pdf) ma PEŁNE WYNIKI IMIENNE
w formacie tekstowym (nie skan):

    N. <tytuł punktu>
    głosowanie <topic>
    jednostka ...
    wynik Głosowanie zakończone wynikiem: przyjęto|odrzucono
    ...
    Podsumowanie
    ZA 12 100 % pula głosów 15 -
    PRZECIW 0 0 % oddanych głosów 12 80 %
    WSTRZYMAŁO SIĘ 0 0 % nieoddanych głosów 3 20 %
    Wyniki imienne
    lp nazwisko imię głos
    1 Archacki Daniel ZA
    2 Chilarska Elżbieta nieobecna
    15 Ziółkowska Sylwia WSTRZYMAŁA SIĘ

Scraper: subclass EsesjaScraper (build/aggregation reuse), podmienione kroki
1-2: scrape_session_list z rejestru BIP, scrape_votes_from_session z PDF.
Kolejność nazwisk w PDF to "Nazwisko Imię" → name_order="swap_surname_first".
Walidacja: per głosowanie porównanie liczników z Podsumowanie vs wyniki imienne.

Dodane 2026-09-07 (cron do 500 miast). club_assignments pending.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper  # noqa: E402

BASE = "https://bip.kudowa.pl"
REJESTR = f"{BASE}/rejestr-glosowania-rm.html"
CADENCY_START = "2024-05-01"  # I sesja IX kadencji: 6 maja 2024

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "wrzesnia": 9, "września": 9,
    "pazdziernika": 10, "października": 10, "listopada": 11, "grudnia": 12,
}

RE_LINK = re.compile(
    r"wykaz-glosowan-([ivxlcdm]+)-sesja[^\"']*?-z-dnia-(\d{1,2})-([a-z]+)-(\d{4})", re.I
)
RE_ATT = re.compile(r'href="([^"]*files/file_add/download/[^"]+\.pdf)"', re.I)
RE_VOTE_HEAD = re.compile(r"^głosowanie\s+(.+)$", re.I)
RE_RESULT = re.compile(r"^wynik\b.*wynikiem:\s*(przyjęto|odrzucono|stwierdzono)", re.I)
RE_ROLLCALL_LINE = re.compile(
    r"^(\d{1,3})\s+(.+?)\s+"
    r"(ZA|PRZECIW(?:\s+\d+)?|WSTRZ(?:YMAŁO|YMAŁA|YMYŚ?CIE|YMUJE\s+SIĘ|YMAŁO\s+SIĘ)[^%]*?"
    r"|NIEOBECN[AY]\w*|BRAK\s+GŁOSU(?:\s+\d+)?|GŁOSU\s+BRAK)\s*$",
    re.I,
)
RE_SUM = re.compile(r"^(ZA|PRZECIW|WSTRZYMAŁO SIĘ)\s+(\d+)\b")


def _classify_vote(tok: str) -> str | None:
    t = tok.strip().lower()
    if t == "za":
        return "za"
    if t.startswith("przeciw"):
        return "przeciw"
    if "strzym" in t:
        return "wstrzymal_sie"
    if t.startswith("nieob"):
        return "nieobecni"
    if "brak" in t or "glosu" in t.replace("ł", "l"):
        return "brak_glosu"
    return None


def parse_wykaz_text(full_text: str) -> list[dict]:
    """Parser strumienia linii WYKAZU GŁOSOWAŃ (cały PDF sklejony)."""
    votes: list[dict] = []
    cur: dict | None = None
    in_rollcall = False
    summary: dict[str, int] = {}
    for raw in full_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        vh = RE_VOTE_HEAD.match(line)
        if vh:
            if cur is not None:
                votes.append(cur)
            cur = {
                "topic": vh.group(1).strip(),
                "resolution": None,
                "named_votes": {
                    "za": [], "przeciw": [], "wstrzymal_sie": [],
                    "brak_glosu": [], "nieobecni": [],
                },
                "summary": {},
            }
            in_rollcall = False
            summary = {}
            continue
        if cur is None:
            continue
        rm = RE_RESULT.match(line)
        if rm:
            r = rm.group(1).lower()
            cur["resolution"] = "odrzucone" if r == "odrzucono" else "przyjete"
            continue
        if line.lower().startswith("podsumowanie"):
            in_rollcall = False
            continue
        sm = RE_SUM.match(line)
        if sm and not in_rollcall:
            key = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMAŁO SIĘ": "wstrzymal_sie"}[sm.group(1)]
            cur["summary"][key] = int(sm.group(2))
            continue
        if line.lower().startswith("wyniki imienne"):
            in_rollcall = True
            continue
        if re.match(r"^lp\s+nazwisko", line, re.I):
            continue
        if in_rollcall:
            lm = RE_ROLLCALL_LINE.match(line)
            if lm:
                name_raw, tok = lm.group(2).strip(), lm.group(3).strip()
                cat = _classify_vote(tok)
                if cat is None:
                    in_rollcall = False  # nie-liścia linia — koniec listy
                    continue
                if len(name_raw) >= 3:
                    cur["named_votes"][cat].append(name_raw)
                continue
            # każda inna linia kończy sekcję imienną
            in_rollcall = False
    if cur is not None:
        votes.append(cur)
    return votes


class KudowaBipScraper(EsesjaScraper):
    """EsesjaScraper z krokiem 1-2 na rejestrze głosowań BIP Kudowy-Zdroju."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("name_order", "swap_surname_first")
        super().__init__(base_url=BASE, *args, **kwargs)
        self._pdf_cache: dict[str, str] = {}

    # -- HTTP -----------------------------------------------------------------

    def _get_text(self, url: str) -> str:
        soup = self.fetch(url, use_cache=True)
        return str(soup)

    # -- Step 1: session list ---------------------------------------------------

    def scrape_session_list(self) -> list[dict]:
        print("  Pobieranie rejestru głosowań BIP (rejestr-glosowania-rm)...")
        seen_urls: set[str] = set()
        sessions: list[dict] = []
        page = 1
        while page <= 10:
            url = REJESTR if page == 1 else f"{REJESTR}?page={page}"
            try:
                html = self._get_text(url)
            except Exception as e:
                print(f"  Blad pobierania {url}: {e}")
                break
            new = 0
            for m in RE_LINK.finditer(html):
                roman, day, mon, year = m.groups()
                # pełny href: od 'href="' przed matchem do zamykającego cudzysłowu
                start = html.rfind('href="', max(0, m.start() - 200), m.start())
                if start == -1:
                    continue
                q_end = html.find('"', m.start())
                if q_end == -1:
                    continue
                href = html[start + 6:q_end].split("?")[0]
                if not href.startswith("http"):
                    href = f"{BASE}/{href.lstrip('/')}"
                mm = int(MONTHS.get(mon.lower(), 0))
                if not mm:
                    continue
                date = f"{year}-{mm:02d}-{int(day):02d}"
                if href in seen_urls:
                    continue
                if date < CADENCY_START:
                    continue
                seen_urls.add(href)
                new += 1
                sessions.append({
                    "id": f"{roman.upper()}_{date}",
                    "date": date,
                    "number": roman.upper(),
                    "url": href,
                    "title": f"Sesja {roman.upper()} w dniu {date}",
                })
            if new == 0:
                break
            page += 1
        print(f"  Znaleziono {len(sessions)} sesji IX kadencji")
        return sorted(sessions, key=lambda x: x["date"])

    # -- Step 2: votes per session ---------------------------------------------

    def scrape_votes_from_session(self, session: dict) -> list[dict]:
        try:
            html = self._get_text(session["url"])
        except Exception as e:
            print(f"    Blad pobierania strony sesji: {e}")
            return []
        am = RE_ATT.search(html)
        if not am:
            print("    Brak załącznika PDF z wykazem głosowań")
            return []
        pdf_url = am.group(1)
        if not pdf_url.startswith("http"):
            pdf_url = f"{BASE}/{pdf_url.lstrip('/')}"
        try:
            resp = self._http().get(pdf_url, timeout=60)
            resp.raise_for_status()
            raw_pdf = resp.content
        except Exception as e:
            print(f"    Blad pobierania PDF: {e}")
            return []
        try:
            import pdfplumber
            import io
            with pdfplumber.open(io.BytesIO(raw_pdf)) as pdf:
                full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception as e:
            print(f"    Blad parsowania PDF: {e}")
            return []
        if len(full_text) < 200:
            print("    PDF bez warstwy tekstowej — pomijam")
            return []
        parsed = parse_wykaz_text(full_text)
        votes: list[dict] = []
        for idx, v in enumerate(parsed):
            nv = v["named_votes"]
            total = sum(len(x) for x in nv.values())
            if total == 0:
                continue
            # walidacja liczników z Podsumowanie (ZA/PRZECIW/WSTRZ)
            ok = True
            for k, cnt in v["summary"].items():
                if len(nv[k]) != cnt:
                    print(f"    MISMATCH '{v['topic'][:40]}': sum={len(nv[k])} vs podsumowanie={cnt} — pomijam")
                    ok = False
                    break
            if not ok:
                continue
            votes.append({
                "id": f"{session['date']}_{session['number']}_{idx:03d}_000",
                "source_url": session["url"],
                "session_date": session["date"],
                "session_number": session["number"],
                "topic": v["topic"][:500],
                "druk": None,
                "resolution": v["resolution"] or "przyjete",
                "counts": {k: len(x) for k, x in nv.items()},
                "named_votes": nv,
            })
        print(f"    Wyodrebniono {len(votes)} glosowan z imiennymi wynikami")
        return votes

    def _http(self):
        if getattr(self, "_requests_session", None) is None:
            import requests
            s = requests.Session()
            s.headers.update({"User-Agent": self.UA})
            self._requests_session = s
        return self._requests_session

    # PDF download cache przez disk cache biblioteki pominiete — sesje sa ~40,
    # incremental window i tak ogranicza re-pobrania.


def _load_councilors() -> dict[str, str]:
    config_path = HERE.parent.parent / "config.json"
    if not config_path.is_file():
        return {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cfg.get("club_assignments", {}) or {}


if __name__ == "__main__":
    sc = KudowaBipScraper(kadencje=KADENCJE, councilors=_load_councilors())
    raise SystemExit(sc.run_cli(prog_name="Radoskop Kudowa-Zdrój (BIP rejestr głosowań)"))
