#!/usr/bin/env python3
"""Radoskop Jasło — scraper archiwum głosowań www2.um.jaslo.pl/sesje/.

Jasło nie ma eSesja BIP (jaslo.esesja.pl = wildcard). Rada Miejska publikuje
archiwum "ARCHIWUM GŁOSOWAŃ I ZAPISU AUDIO-VIDEO SESJI RADY MIEJSKIEJ, IX
Kadencja" na www2.um.jaslo.pl — per sesja:
  /sesje/9w/{n}/wyniki_glosowania.html  (UTF-16, bloki "Numer głosowania: N"
  z tabelami imiennymi Za / Przeciw / Wstrzymało się / Nie głosowało / Nieobecni)
  /sesje/9g/{n}/index.html              (lista głosowań + agregaty)
  video: jaslo.sesja.pl/portal/videos/...

Scraper jest podklasą lib_esesja.EsesjaScraper: nadpisuje scrape_session_list
+ scrape_votes_from_session, reszty (build_councilors/build_sessions/run/
save_split_output/build_profiles_json) używa z biblioteki bez zmian.

Dodane 2026-09-04 (cron do 500 miast). club_assignments PENDING (kuratorować).
"""

import re
import sys
import time
import html as htmllib
import urllib.request
import urllib.error
import ssl
from pathlib import Path
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper

BASE = "https://www2.um.jaslo.pl"
KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

_CTX = ssl.create_default_context()
# www2.um.jaslo.pl ma niekompletny łańcuch certyfikatu (brak intermediate) —
# weryfikacja zawodzi mimo poprawnego certu; fallback na kontekst bez weryfikacji.
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "RadoskopBot/1.0 (info@radoskop.eu)"}


def _fetch_raw(url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            raw = urllib.request.urlopen(req, timeout=25, context=_CTX).read()
            if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                return raw.decode("utf-16", errors="replace")
            # strony archiwum deklarują charset w meta; część plików jest
            # UTF-16 BEZ BOM (Dzień→DzieĔ przy cp1250), index bywa cp1250
            m = re.search(rb"charset=([\w-]+)", raw[:600], re.I)
            enc = (m.group(1).decode() if m else "cp1250").lower()
            if enc in ("utf-16", "utf-16le", "utf-16be", "unicode"):
                try:
                    t = raw.decode(enc if enc != "unicode" else "utf-16", errors="replace")
                    if "Numer" in t or "html" in t[:200]:
                        return t
                except Exception:  # noqa: BLE001
                    pass
            for alt in ("cp1250", "utf-8"):
                t = raw.decode(alt, errors="replace")
                if "Numer" in t or "sesji" in t:
                    return t
            return raw.decode("cp1250", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


# "Wyniki glosowania z sesji nr 202611 - XXXVII" / "sesji nr 202408 - Sesja II"
_SES_RE = re.compile(r"sesji nr\s+\d+\s*-\s*(?:Sesja\s+)?([IVXLLC]+)", re.I)
_DATE_RE = re.compile(r"Dzie\s*ń sesji\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")

# kategoria per label sekcji imiennej
_SEC_RES = [
    (re.compile(r"<b>\s*Za\s*:?\s*</b>", re.I), "za"),
    (re.compile(r"<b>\s*Przeciw\s*:?\s*</b>", re.I), "przeciw"),
    (re.compile(r"<b>\s*Wstrzym", re.I), "wstrzymal_sie"),
    (re.compile(r"<b>\s*Nie\s+g\s*losow", re.I), "brak_glosu"),
    (re.compile(r"<b>\s*Nieobecn", re.I), "nieobecni"),
]
_TABLE_RE = re.compile(r"<table[^>]*>.*?</table>", re.S | re.I)


def _names_in_table(table_html: str) -> list[str]:
    soup = BeautifulSoup(table_html, "html.parser")
    names = []
    for td in soup.find_all("td"):
        txt = htmllib.unescape(td.get_text(" ", strip=True))
        txt = re.sub(r"\s+", " ", txt).strip()
        if len(txt) > 3 and re.search(r"[A-Za-zŚśŁłŻżŹźÓóĄąĘęĆćŃń]", txt):
            names.append(txt)
    return names


class JasloScraper(EsesjaScraper):
    def scrape_session_list(self) -> list[dict]:
        print(f"  GET {BASE}/sesje/")
        txt = _fetch_raw(f"{BASE}/sesje/")
        nums = sorted({int(m.group(1)) for m in re.finditer(r"9w/(\d+)/wyniki_glosowania\.html", txt)})
        if not nums:
            print("  Brak linków 9w/*/wyniki_glosowania.html na stronie archiwum")
            return []
        print(f"  Archiwum IX kadencji: sesje {nums[0]}..{nums[-1]} ({len(nums)})")
        sessions = []
        for n in nums:
            url = f"{BASE}/sesje/9w/{n}/wyniki_glosowania.html"
            idx_url = f"{BASE}/sesje/9g/{n}/index.html"
            try:
                idx = _fetch_raw(idx_url)
            except Exception as e:  # noqa: BLE001
                print(f"    [skip] sesja {n}: {e}")
                continue
            dm = _DATE_RE.search(idx)
            if not dm:
                print(f"    [skip] sesja {n}: brak daty na index")
                continue
            iso = f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"
            sm = _SES_RE.search(idx)
            number = sm.group(1).upper() if sm else str(n)
            sessions.append({
                "id": f"sesja-{n}",
                "url": url,
                "date": iso,
                "number": number,
                "title": f"Sesja {number} Rady Miejskiej Jasła w dniu {iso}",
            })
            time.sleep(0.3)
        # pełna kadencja IX na archiwum — bez cięcia po dacie (Sesja I = 2024-05-06)
        sessions.sort(key=lambda s: s["date"])
        return sessions

    def scrape_votes_from_session(self, session: dict) -> list[dict]:
        page = _fetch_raw(session["url"])
        idx = page.find("Numer g")
        if idx < 0:
            print("    Brak bloków głosowań")
            return []
        body = page[idx:]
        parts = re.split(r"(?=<font[^>]*>\s*Numer g)", body)
        if len(parts) <= 1:
            parts = re.split(r"Numer g\s*łosowania\s*:", body)
            parts = ["Numer głosowania:" + p for p in parts[1:]]
        votes: list[dict] = []
        for block in parts:
            nm = re.search(r"Numer g\s*łosowania\s*:\s*(\d+)", block)
            if not nm:
                continue
            vote_no = int(nm.group(1))
            topic = self._topic(block)
            counts = self._counts(block)
            named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
            # sekcje po kolei: label <b>X:</b> po którym następuje <table>
            for sec in re.finditer(
                r"<b>\s*([^<]{2,40}?)\s*:?\s*</b>\s*(<table[^>]*>.*?</table>)",
                block, re.S | re.I,
            ):
                label, table = sec.group(1), sec.group(2)
                cat = None
                for rx, key in _SEC_RES:
                    if rx.search(f"<b>{label}</b>"):
                        cat = key
                        break
                if cat is None:
                    continue
                names = _names_in_table(table)
                if names:
                    named[cat].extend(names)
            total = sum(len(v) for v in named.values())
            if total == 0:
                continue
            # sanity: sum list == suma liczników (Za+Przeciw+Wstrz+Nie glos)
            agg = (counts.get("za", 0) + counts.get("przeciw", 0)
                   + counts.get("wstrzymal_sie", 0) + counts.get("brak_glosu", 0))
            if agg and agg != total:
                print(f"    [warn] sesja {session['number']} głos {vote_no}: listy {total} != agregat {agg}")
            votes.append({
                "id": f"{session['date']}_{session['number']}_{vote_no:03d}",
                "source_url": session["url"] + f"#glos-{vote_no}",
                "session_date": session["date"],
                "session_number": session["number"],
                "topic": topic[:500],
                "druk": None,
                "resolution": None,
                "counts": counts,
                "named_votes": named,
            })
        print(f"    Wyodrebniono {len(votes)} glosowan z imiennymi wynikami")
        return votes

    @staticmethod
    def _topic(block: str) -> str:
        m = re.search(r"Punkt numer\s*:\s*[^<]*?<br\s*/?>(.*?)<br", block, re.S | re.I)
        if not m:
            m = re.search(r"Dzie\s*ń sesji[^<]*", block)
        raw = m.group(1) if m else ""
        txt = re.sub(r"<[^>]+>", " ", raw)
        txt = htmllib.unescape(txt)
        return re.sub(r"\s+", " ", txt).strip()

    @staticmethod
    def _counts(block: str) -> dict:
        def grab(rx):
            m = re.search(rx, block)
            return int(m.group(1)) if m else 0
        return {
            "za": grab(r"Za\s*:\s*(\d+)"),
            "przeciw": grab(r"Przeciw\s*:\s*(\d+)"),
            "wstrzymal_sie": grab(r"Wstrzym\w*\s*(?:\s*się)?\s*:\s*(\d+)"),
            "brak_glosu": grab(r"Nie\s+g\s*losow\w*\s*:\s*(\d+)"),
        }


def _load_councilors() -> dict[str, str]:
    import json
    config_path = HERE.parent.parent / "config.json"
    if not config_path.is_file():
        return {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cfg.get("club_assignments", {}) or {}


if __name__ == "__main__":
    raise SystemExit(JasloScraper(
        base_url=BASE,
        kadencje=KADENCJE,
        councilors=_load_councilors(),
        # archiwum Jasła podaje "Imię Nazwisko" — NIE swapować (domyślny
        # swap eSesja jest dla formatu "Nazwisko Imię")
        name_order="as_is",
    ).run_cli(prog_name="Radoskop Jaslo (www2.um.jaslo.pl archiwum)"))
