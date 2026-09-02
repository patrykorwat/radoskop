#!/usr/bin/env python3
"""
Radoskop Pabianice — imienne głosowania z BIP (rejestr uchwał), NIE z eSesja.

ŹRÓDŁO
======
Portal eSesja: pabianice.esesja.pl = pętla 302 redirect (subdomena nieaktywna).
BIP: https://bip.um.pabianice.pl (platforma Nexa 'artykuly'), rejestr uchwał:
    https://bip.um.pabianice.pl/uchwaly/25          (strona 1)
    https://bip.um.pabianice.pl/uchwaly/25/{N}/10   (kolejne strony, 10 na stronę)
Każda uchwała ma stronę /uchwala/{id}/{slug} z:
  - <time datetime="YYYY-MM-DD"> (data uchwały = data sesji),
  - numerem 'Uchwała Nr XXIX/298/26',
  - tematem 'w sprawie ...',
  - załącznikiem 'Wyniki głosowania jawnego imiennego' = PDF dwukolumnowy
    (Lp | Nazwisko i imię | Głos; wartości ZA/NIEOBECNY/WSTRZYMUJĘ SIĘ/...),
    format = per-page, obsługiwany przez lib_voting_pdf_table.
    parse_voting_pdf_per_page().
Grupowanie po dacie datetime = sesje.

Ograniczenie: głosowania NAD UCHWAŁAMI (rejestr); wnioski formalne publikowane
tylko w protokołach sesji — pominięte (komplet merytoryczny zachowany).

Rada: /artykul/89/20969/radni-rady-miejskiej-ix-kadencji (23 radnych, nazwisko
pierwsze — ta sama kolejność co w PDF głosowań).
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_bip_static import BipScraper  # noqa: E402
from lib_voting_pdf_table import (  # noqa: E402
    _parse_per_page_vote,
    parse_voting_pdf_per_page,
)


def parse_glosowanie_pdf(data: bytes, name: str) -> list[dict]:
    """PDF 'Wyniki głosowania jawnego imiennego' (1 strona = 1 głosowanie,
    dwukolumnowa tabela Lp|Nazwisko|Głos). Najszybciej: warstwa tekstu
    pymupdf; fallback: pełny parser lib (OCR dla skanów)."""
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = [p.get_text() for p in doc]
    doc.close()
    if sum(len(t.strip()) for t in pages) >= 50 * max(1, len(pages)):
        votes = []
        for idx, pt in enumerate(pages):
            v = _parse_per_page_vote(pt, len(votes))
            if v:
                votes.append(v)
        return votes
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        parsed = parse_voting_pdf_per_page(tmp)
        return parsed.get("votes", [])
    finally:
        Path(tmp).unlink(missing_ok=True)

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

BIP_BASE = "https://bip.um.pabianice.pl"
REGISTER_URL = f"{BIP_BASE}/uchwaly/25"
MAX_LISTING_PAGES = 60

UCHWALA_LINK_RE = re.compile(r'href="(https://bip\.um\.pabianice\.pl/uchwala/\d+/[^"]+)"')
NR_RE = re.compile(r"Uchwała Nr\s+([IVXLCDM]+(?:/\d+/\d{2})?)")
DATETIME_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})')
TOPIC_RE = re.compile(r"w sprawie (.+?) status uchwa", re.S)
ATT_RE = re.compile(
    r'<a[^>]+href="(https://bip\.um\.pabianice\.pl/attachments/download/\d+)"[^>]*>\s*([^<]{0,80})',
    re.S,
)
VOTE_ATT_LABEL = re.compile(r"glosowan|głosowan", re.I)


def _load_councilors() -> dict[str, str]:
    config_path = HERE.parent.parent / "config.json"
    if not config_path.is_file():
        return {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cfg.get("club_assignments", {}) or {}


COUNCILORS = _load_councilors()


class PabianiceScraper(BipScraper):
    def _fetch_text_raw(self, url: str, use_cache: bool = True) -> str:
        if self._session is None:
            self._init_session()
        cf = self._cache_path(url) if (use_cache and self.cache_dir) else None
        if cf and cf.is_file():
            return cf.read_text(encoding="utf-8", errors="replace")
        resp = self._session.get(url, timeout=60)
        resp.raise_for_status()
        html = resp.text
        if cf:
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_text(html, encoding="utf-8")
        return html

    def _fetch_pdf(self, url: str) -> bytes:
        if self._session is None:
            self._init_session()
        cf = self._cache_path(url + ".pdf") if self.cache_dir else None
        if cf and cf.is_file():
            return cf.read_bytes()
        resp = self._session.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.content
        if cf:
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_bytes(data)
        return data

    # -- discovery ----------------------------------------------------------

    def discover_sessions(self) -> list[dict]:
        kad_start = self.kadencje[self.default_kadencja]["start"]
        urls: list[str] = []
        seen: set[str] = set()
        page = 1
        while page <= MAX_LISTING_PAGES:
            url = REGISTER_URL if page == 1 else f"{REGISTER_URL}/{page}/10"
            try:
                html = self._fetch_text_raw(url, use_cache=False)
            except Exception as e:
                print(f"  Blad listing {url}: {e}")
                break
            new = 0
            for m in UCHWALA_LINK_RE.finditer(html):
                u = m.group(1)
                if u in seen:
                    continue
                seen.add(u)
                urls.append(u)
                new += 1
            if new == 0:
                break
            page += 1
        print(f"  Rejestr BIP: {len(urls)} uchwał")

        sessions_map: dict[str, dict] = {}
        self._uchwaly: dict[str, list[dict]] = {}
        for i, u in enumerate(urls, 1):
            try:
                html = self._fetch_text_raw(u, use_cache=True)
            except Exception as e:
                print(f"    Blad {u}: {e}")
                continue
            dm = DATETIME_RE.search(html)
            if not dm:
                continue
            date = dm.group(1)
            if date < kad_start:
                continue
            nr_m = NR_RE.search(re.sub(r"<[^>]+>", " ", html))
            plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
            tm = TOPIC_RE.search(plain)
            topic = tm.group(1).strip() if tm else ""
            # załącznik z wynikami głosowania
            vote_url = ""
            for m in ATT_RE.finditer(html):
                if VOTE_ATT_LABEL.search(m.group(2)):
                    vote_url = m.group(1)
                    break
            if not vote_url:
                continue
            num = nr_m.group(1) if nr_m else date
            session_key = date
            if session_key not in sessions_map:
                sessions_map[session_key] = {
                    "date": date,
                    "number": num.split("/")[0],
                    "url": u,
                }
            self._uchwaly.setdefault(session_key, []).append(
                {"url": u, "vote_url": vote_url, "topic": topic, "nr": num}
            )
            if i % 25 == 0:
                print(f"  ... {i}/{len(urls)} przetworzonych")

        out = sorted(sessions_map.values(), key=lambda s: s["date"])
        print(f"  Sesji z głosowaniami: {len(out)}")
        return out

    # -- votes ---------------------------------------------------------------

    def parse_session_votes(self, session: dict) -> list[dict]:
        votes: list[dict] = []

        def _first_last(nm: str) -> str:
            """PDF BIP kolumna 'Nazwisko i imię' -> 'Imię Nazwisko' (kolejność PL)."""
            parts = nm.split()
            if len(parts) >= 2:
                return " ".join([parts[-1]] + parts[:-1])
            return nm

        for uw in self._uchwaly.get(session["date"], []):
            try:
                data = self._fetch_pdf(uw["vote_url"])
            except Exception as e:
                print(f"    Blad PDF {uw['vote_url']}: {e}")
                continue
            for pv in parse_glosowanie_pdf(data, uw["vote_url"]):
                nv = pv.get("named_votes") or {}
                nv = {k: [_first_last(x) for x in v] for k, v in nv.items()}
                if not any(nv.values()):
                    continue
                topic = uw["topic"] or re.sub(
                    r"^\s*\d+[\s.]*", "", (pv.get("topic") or "")
                )[:300]
                votes.append(
                    {
                        "id": f"{session['date']}_{uw['nr']}",
                        "topic": f"Uchwała {uw['nr']}: {topic}"[:500],
                        "resolution": uw["nr"],
                        "source_url": uw["url"],
                        "named_votes": nv,
                    }
                )
        return votes


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="Radoskop Pabianice")
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir")
    ap.add_argument("--max-sessions", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-full", action="store_true")
    args = ap.parse_args()

    sc = PabianiceScraper(
        base_url=BIP_BASE,
        kadencje=KADENCJE,
        councilors=COUNCILORS,
        delay=0.7,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
    )
    raise SystemExit(
        sc.run(
            output_path=args.output,
            profiles_path=args.profiles,
            max_sessions=args.max_sessions,
            dry_run=args.dry_run,
            force_full=args.force_full,
        )
    )
