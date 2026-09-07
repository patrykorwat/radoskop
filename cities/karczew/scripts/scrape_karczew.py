#!/usr/bin/env python3
"""Radoskop Karczew — scraper platformy posiedzenia.pl (GK PRO, iframe portal).

BIP Miasta Karczew (bip.karczew.pl) kieruje wykazy imiennych głosowań od
2024-07-15 na portal https://karczew.posiedzenia.pl . Portal renderuje dane
przez XHR do /admin/zawartosc.php (sesja PHP ważna tylko w originie portalu,
curl bywa odrzucany "Nie jesteś poprawnie zalogowany") — dlatego scraper używa
headless Chromium (playwright) i wywołuje te endpointy z kontekstu strony.

Struktura API (odkryta 2026-09-07):
  GET /admin/start.php?ScreenSize=...            -> inicjalizacja sesji, Podmiot
  GET /admin/zawartosc.php?action=O3&podmiot=N   -> JSON: list.sessions[].points[]
  GET /admin/zawartosc.php?action=O7&parametr1=<pointId>&podmiot=N
        -> HTML z <chartScores src='<base64 JSON>'>: per-person głosy.
        votes [1]=ZA [2]=PRZECIW [3]=WSTRZYMUJĘ SIĘ; options{notVoting,absent}.

Dane Radoskop: named_votes per radny + głosowania imienne IX kadencji.
"""

from __future__ import annotations

import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper  # noqa: E402  (build/aggregation reuse)

import json
import re
import base64

PORTAL = "https://karczew.posiedzenia.pl"
CADENCY_START = "2024-05-07"

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": CADENCY_START},
}

RE_ROMAN = re.compile(r"\b([IVXLCDM]+)\s+Sesja", re.I)


class PosiedzeniaPlScraper(EsesjaScraper):
    """EsesjaScraper z podmienionymi krokami 1-2 na API posiedzenia.pl."""

    def __init__(self, *args, portal: str = PORTAL, **kwargs):
        kwargs.setdefault("name_order", "keep")  # API zwraca Imię Nazwisko
        super().__init__(base_url=portal, *args, **kwargs)
        self.portal = portal.rstrip("/")
        self._pw = None
        self._frame = None
        self.podmiot = None

    # -- playwright context --------------------------------------------------

    def _ensure_frame(self):
        if self._frame is not None:
            return self._frame
        self._pw = sync_playwright().start()
        browser = self._pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36"
        )
        page = ctx.new_page()
        page.goto(f"{self.portal}/?action=glosowania", wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(6000)
        frame = next(f for f in page.frames if "portal-posiedzenia.pl" in f.url)
        self._browser = browser
        self._ctx = ctx
        self._page = page
        self._frame = frame
        self.podmiot = frame.evaluate("Podmiot")
        if not self.podmiot:
            raise RuntimeError("Nie udalo sie pobrac Podmiot z portalu")
        return frame

    def _fetch(self, action: str, param_str: str) -> str:
        frame = self._ensure_frame()
        url = (
            f"https://portal-posiedzenia.pl/admin/zawartosc.php?action={action}"
            f"&{param_str}&podmiot={self.podmiot}&osoba=0&time=1&tabId=tab-radoskop"
        )
        return frame.evaluate(
            "async (u) => { const r = await fetch(u); return await r.text(); }", url
        )

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # -- Step 1: session list -------------------------------------------------

    def scrape_session_list(self) -> list[dict]:
        print("  Pobieranie listy sesji (action=O3)...")
        raw = self._fetch("O3", "parametr=null")
        data = json.loads(raw)
        sessions: list[dict] = []
        for s in data["list"]["sessions"]:
            date = (s.get("date") or "")[:10]
            if date < CADENCY_START:
                continue
            m = RE_ROMAN.search(s.get("name", ""))
            sessions.append({
                "id": str(s["id"]),
                "date": date,
                "number": m.group(1).upper() if m else "",
                "url": f"{self.portal}/?action=glosowania",
                "title": s.get("name", ""),
                "points": s.get("points", []),
            })
        print(f"  Znaleziono {len(sessions)} sesji IX kadencji")
        return sorted(sessions, key=lambda x: x["date"])

    # -- Step 2: votes per session ---------------------------------------------

    def scrape_votes_from_session(self, session: dict) -> list[dict]:
        votes: list[dict] = []
        for idx, pt in enumerate(session.get("points", []) or []):
            try:
                html = self._fetch("O7", f"parametr1={pt['id']}&parametr2=")
            except Exception as e:
                print(f"      Blad pobierania punktu {pt['id']}: {e}")
                continue
            m = re.search(r"src='([A-Za-z0-9+/=]{40,})'", html)
            if not m:
                continue
            try:
                d = json.loads(base64.b64decode(m.group(1)))
            except Exception:
                continue
            if not d.get("resultExists") or not d.get("persons"):
                continue
            named = {
                "za": [], "przeciw": [], "wstrzymal_sie": [],
                "brak_glosu": [], "nieobecni": [],
            }
            for p in d["persons"]:
                name = f"{p.get('firstName','')} {p.get('lastName','')}".strip()
                if not name or len(name) < 3:
                    continue
                if not p.get("present"):
                    named["nieobecni"].append(name)
                elif not p.get("voted"):
                    named["brak_glosu"].append(name)
                else:
                    v = (p.get("votes") or [None])[0]
                    if v == 1:
                        named["za"].append(name)
                    elif v == 2:
                        named["przeciw"].append(name)
                    elif v == 3:
                        named["wstrzymal_sie"].append(name)
                    else:
                        named["brak_glosu"].append(name)
            if sum(len(x) for x in named.values()) == 0:
                continue
            topic = (d.get("pointName") or pt.get("name") or f"Glosowanie {idx+1}").strip()
            desc = (d.get("description") or "").strip()
            if desc:
                topic = f"{topic} {desc}"
            session_num = session.get("number", "") or ""
            num_part = f"_{session_num}" if session_num else ""
            votes.append({
                "id": f"{session['date']}{num_part}_{idx:03d}_000",
                "source_url": f"{self.portal}/?action=glosowania",
                "session_date": session["date"],
                "session_number": session_num,
                "topic": topic[:500],
                "druk": None,
                "resolution": "przyjete" if d.get("votingResult") else "odrzucone",
                "counts": {k: len(v) for k, v in named.items()},
                "named_votes": named,
            })
        print(f"    Wyodrebniono {len(votes)} glosowan z imiennymi wynikami")
        return votes


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
    sc = PosiedzeniaPlScraper(
        kadencje=KADENCJE,
        councilors=_load_councilors(),
    )
    try:
        rc = sc.run_cli(prog_name="Radoskop Karczew (posiedzenia.pl)")
    finally:
        sc.close()
    raise SystemExit(rc)
