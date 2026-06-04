#!/usr/bin/env python3
"""
Radoskop Wałbrzych — scraper głosowań z BIP (rejestr uchwał), NIE z portalu eSesja.

DLACZEGO BIP, NIE PORTAL eSESJA
================================
Portal https://walbrzych.esesja.pl jest praktycznie MARTWY: archiwum kadencji
2024-2029 zawiera 8 posiedzeń, wszystkie z kwietnia 2025 (1 sesja plenarna
"Sesja nr XIV" + 7 komisji). Miasto używało publicznego portalu ~1 miesiąc.
Stąd Radoskop miał 1 sesję i freshness 400+ dni.

Prawdziwe źródło: BIP, moduł "Uchwały Rady i Głosowania Radnych"
    https://bip.um.walbrzych.pl/uchwaly/2970   (rejestr IX kadencji 2024-2029)
Każda uchwała ma stronę szczegółów z załącznikiem "głosowanie" — PDF
WYGENEROWANY PRZEZ eSesja.pl Z WARSTWĄ TEKSTOWĄ, format identyczny jak
protokoły Gdyni:
    Wyniki głosowania
    Głosowano w sprawie: ...
    ZA: 18, PRZECIW: 0, WSTRZYMUJĘ SIĘ: 0, BRAK GŁOSU: 5, NIEOBECNI: 2
    Wyniki imienne:
    ZA (18) Nazwisko1 , Nazwisko2 , ...
    ...
    Głosowanie zakończono w dniu: 25 kwietnia 2024, o godz. 12:16

Pipeline:
1. Crawl stron rejestru ?page=N (listing server-side, bez JS; ?year= NIE
   filtruje — formularz jest POST + recaptcha, więc filtrujemy po dacie).
2. Per uchwała: strona szczegółów → numer (RZYMSKI/nr/rok), "z dnia",
   "w sprawie", załącznik "głosowanie".
3. Grupowanie po (data, numer rzymski) = sesje.
4. Per uchwała: pobierz PDF głosowania, pdftotext -layout, parsuj kategorie.
5. Reszta (kluby, profile, data.json + kadencja-*.json, incremental cache)
   dziedziczona z lib_esesja.EsesjaScraper — nadpisujemy tylko
   scrape_session_list() i scrape_votes_from_session().

Ograniczenie: rejestr ma tylko głosowania NAD UCHWAŁAMI (bez wniosków
formalnych/porządku obrad). To i tak komplet merytoryczny.

Wymaga: pdftotext (poppler-utils).

Council members + club assignments z config.json (sekcja club_assignments).
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

from lib_esesja import EsesjaScraper  # noqa: E402

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

BIP_BASE = "https://bip.um.walbrzych.pl"
REGISTER_URL = f"{BIP_BASE}/uchwaly/2970"  # rejestr uchwał IX kadencji
MAX_LISTING_PAGES = 80

UCHWALA_LINK_RE = re.compile(
    r'href="(https://bip\.um\.walbrzych\.pl/uchwala/\d+/[^"]+)"'
)
NR_RE = re.compile(r"([IVXLCDM]+)\s*/\s*(\d+)\s*/\s*(\d{2})")
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

# Kategorie w PDF "Wyniki imienne". Kolejność = kolejność w dokumencie.
PDF_CATEGORIES = [
    ("ZA", "za"),
    ("PRZECIW", "przeciw"),
    ("WSTRZYMUJĘ SIĘ", "wstrzymal_sie"),
    ("BRAK GŁOSU", "brak_glosu"),
    ("NIEOBECNI", "nieobecni"),
]
PDF_HEADER_RE = re.compile(
    r"(?m)^\s*(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\s*\((\d+)\)\s*$"
)
PDF_COUNTS_RE = re.compile(
    r"ZA:\s*(\d+)\s*,\s*PRZECIW:\s*(\d+)\s*,\s*WSTRZYMUJĘ SIĘ:\s*(\d+)\s*,"
    r"\s*BRAK GŁOSU:\s*(\d+)\s*,\s*NIEOBECNI:\s*(\d+)"
)


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


def parse_glosowanie_pdf_text(text: str) -> dict | None:
    """Parsuje tekst PDF 'głosowanie' (format eSesja) → {counts, named_votes, topic}."""
    if "Wyniki imienne" not in text:
        return None

    topic = ""
    tm = re.search(r"Głosowano w sprawie:\s*(.+?)\s*(?:ZA:|Wyniki imienne)", text, re.S)
    if tm:
        topic = re.sub(r"\s+", " ", tm.group(1)).strip().rstrip(".")

    named: dict[str, list[str]] = {key: [] for _label, key in PDF_CATEGORIES}

    imienne_part = text.split("Wyniki imienne", 1)[1]
    # Utnij stopkę.
    imienne_part = re.split(r"Głosowanie zakończono", imienne_part)[0]

    headers = list(PDF_HEADER_RE.finditer(imienne_part))
    label_to_key = {label: key for label, key in PDF_CATEGORIES}
    for i, h in enumerate(headers):
        key = label_to_key[h.group(1)]
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(imienne_part)
        blob = re.sub(r"\s+", " ", imienne_part[start:end]).strip()
        if not blob:
            continue
        for raw in blob.split(","):
            name = re.sub(r"\s+", " ", raw).strip().strip(".,;")
            if name and len(name) > 3 and " " in name:
                named[key].append(name)

    if sum(len(v) for v in named.values()) == 0:
        return None

    counts = {key: len(named[key]) for _label, key in PDF_CATEGORIES}
    cm = PDF_COUNTS_RE.search(text)
    if cm:
        for (_label, key), val in zip(PDF_CATEGORIES, cm.groups()):
            counts[key] = int(val)

    return {"topic": topic, "counts": counts, "named_votes": named}


class WalbrzychBipScraper(EsesjaScraper):
    """eSesja-zgodny scraper czytający rejestr uchwał BIP zamiast portalu."""

    # -- discovery --------------------------------------------------------

    def _fetch_html(self, url: str, use_cache: bool) -> str:
        """Surowy HTML (fetch() lib zwraca soup; tu potrzebujemy regex po href)."""
        if self._session is None:
            self._init_session()
        cache_file = self._cache_path(url) if use_cache else None
        if cache_file and cache_file.is_file():
            return cache_file.read_text(encoding="utf-8")
        resp = self._session.get(url, timeout=60)
        resp.raise_for_status()
        html = resp.text
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(html, encoding="utf-8")
        return html

    def _drop_cache(self, url: str) -> None:
        cf = self._cache_path(url)
        if cf and cf.is_file():
            try:
                cf.unlink()
            except OSError:
                pass

    def scrape_session_list(self) -> list[dict]:
        kadencja_start = self.kadencje[self.default_kadencja]["start"]

        # 1) Listing: zawsze fresh (wykrywanie nowych uchwał).
        uchwala_urls: list[str] = []
        seen: set[str] = set()
        page = 1
        while page <= MAX_LISTING_PAGES:
            url = REGISTER_URL if page == 1 else f"{REGISTER_URL}?page={page}"
            try:
                html = self._fetch_html(url, use_cache=False)
            except Exception as e:
                print(f"  Blad listing page {page}: {e}")
                break
            new = 0
            for m in UCHWALA_LINK_RE.finditer(html):
                u = m.group(1)
                if u in seen:
                    continue
                seen.add(u)
                # Tani filtr cross-referencji do starych kadencji: slug kończy
                # się dwucyfrowym rokiem; bierzemy >= 24 (resztę odfiltruje data).
                ym = re.search(r"-(\d{2})$", u)
                if ym and int(ym.group(1)) < 24:
                    continue
                uchwala_urls.append(u)
                new += 1
            if new == 0:
                break
            page += 1
        print(f"  Rejestr BIP: {len(uchwala_urls)} uchwał do sprawdzenia "
              f"({page - 1} stron)")

        # 2) Szczegóły uchwał → (nr, data, tytuł, załącznik głosowanie).
        sessions_map: dict[tuple[str, str], dict] = {}
        self._uchwaly_by_session: dict[tuple[str, str], list[dict]] = {}

        for i, u in enumerate(uchwala_urls, 1):
            try:
                html = self._fetch_html(u, use_cache=True)
            except Exception as e:
                print(f"    Blad uchwala {u}: {e}")
                continue

            nr_m = NR_RE.search(html)
            if not nr_m:
                continue
            # Data: <th>z dnia</th><td><time datetime="2026-05-28T00:00:00">…
            date_iso = ""
            tm_dt = re.search(
                r'z dnia</th>.*?<time\s+datetime="(\d{4}-\d{2}-\d{2})', html, re.S
            )
            if tm_dt:
                date_iso = tm_dt.group(1)
            else:
                near = html[html.find("z dnia"):html.find("z dnia") + 400]
                dm = DATE_RE.search(near)
                if dm:
                    date_iso = (
                        f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"
                    )
            if not date_iso:
                continue
            if date_iso < kadencja_start:
                continue
            roman = nr_m.group(1)
            uchwala_nr = f"{nr_m.group(1)}/{nr_m.group(2)}/{nr_m.group(3)}"

            tm = re.search(
                r"w sprawie\s*</th>\s*<td[^>]*>\s*(.*?)\s*</td>", html, re.S
            )
            title = re.sub(r"<[^>]+>", "", tm.group(1)) if tm else ""
            title = re.sub(r"\s+", " ", title).strip()

            gm = re.search(
                r'href="(https://bip\.um\.walbrzych\.pl/attachments/download/\d+)"'
                r'[^>]*>\s*głosowanie', html, re.I
            )
            if not gm:
                # Brak załącznika głosowania (jeszcze?) — nie cache'uj strony,
                # żeby kolejny run zobaczył dopublikowany załącznik.
                self._drop_cache(u)
                continue

            key = (date_iso, roman)
            sessions_map.setdefault(key, {
                "id": f"{date_iso}_{roman}",
                "date": date_iso,
                "number": roman,
                "url": REGISTER_URL,
                "title": f"Sesja nr {roman} w dniu {date_iso}",
            })
            self._uchwaly_by_session.setdefault(key, []).append({
                "nr": uchwala_nr,
                "seq": int(nr_m.group(2)),
                "title": title,
                "url": u,
                "pdf_url": gm.group(1),
            })
            if i % 25 == 0:
                print(f"    ... {i}/{len(uchwala_urls)} uchwał przejrzanych")

        sessions = sorted(sessions_map.values(), key=lambda s: s["date"])
        print(f"  Znaleziono {len(sessions)} sesji (z rejestru uchwał), "
              f"{sum(len(v) for v in self._uchwaly_by_session.values())} uchwał z głosowaniem")
        return sessions

    # -- votes ------------------------------------------------------------

    def _pdf_text(self, pdf_url: str) -> str:
        """Pobierz PDF głosowania (cache po id załącznika) i zwróć tekst."""
        if self._session is None:
            self._init_session()
        att_id = pdf_url.rstrip("/").rsplit("/", 1)[-1]
        txt_cache = None
        if self._cache_dir:
            txt_cache = Path(self._cache_dir) / f"glosowanie_{att_id}.txt"
            if txt_cache.is_file():
                return txt_cache.read_text(encoding="utf-8")
        resp = self._session.get(pdf_url, timeout=90)
        resp.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(resp.content)
            tmp = f.name
        try:
            out = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", tmp, "-"],
                capture_output=True, text=True, timeout=60,
            )
            text = out.stdout if out.returncode == 0 else ""
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
        if txt_cache and text:
            txt_cache.parent.mkdir(parents=True, exist_ok=True)
            txt_cache.write_text(text, encoding="utf-8")
        return text

    def scrape_votes_from_session(self, session: dict) -> list[dict]:
        key = (session["date"], session["number"])
        uchwaly = sorted(
            getattr(self, "_uchwaly_by_session", {}).get(key, []),
            key=lambda x: x["seq"],
        )
        votes: list[dict] = []
        for idx, uch in enumerate(uchwaly):
            try:
                text = self._pdf_text(uch["pdf_url"])
            except Exception as e:
                print(f"    Blad PDF {uch['pdf_url']}: {e}")
                continue
            parsed = parse_glosowanie_pdf_text(text) if text else None
            if not parsed:
                continue
            topic = parsed["topic"] or uch["title"] or f"Uchwała {uch['nr']}"
            votes.append({
                "id": f"{session['date']}_{session['number']}_{idx:03d}_000",
                "source_url": uch["url"],
                "session_date": session["date"],
                "session_number": session["number"],
                "topic": topic[:500],
                "druk": uch["nr"],
                "resolution": None,
                "counts": parsed["counts"],
                "named_votes": parsed["named_votes"],
            })
        print(f"    Wyodrebniono {len(votes)} glosowan (uchwały z PDF)")
        return votes


if __name__ == "__main__":
    raise SystemExit(WalbrzychBipScraper(
        base_url="https://walbrzych.esesja.pl",
        kadencje=KADENCJE,
        councilors=COUNCILORS,
    ).run_cli(prog_name="Radoskop Wałbrzych (BIP rejestr uchwał + eSesja PDF)"))
