#!/usr/bin/env python3
"""
Scraper sesji plenarnych Berlin Abgeordnetenhaus (Plenarprotokolle).

Workflow:
1. Pobierz PARDOK XML metadata kadencji (np. pardok-wp19.xml, ~50 MB).
2. Wyciągnij linki Plenarprotokoll PDF (DokArt=PlPr, LokURL).
3. Dla każdej sesji:
   a. Pobierz PDF (cache na dysku w .cache/plpr/).
   b. Konwertuj pdftotext (bez --layout, naturalna kolejność czytania).
   c. Parsuj stenogram regex'em "Imię Nazwisko (FRAKCJA):" + sekcja mowy
      do następnego mówcy. Liczy słowa per turn.
   d. Wyciągnij datę sesji z pierwszych ~2000 znaków (format niemiecki:
      "Donnerstag, 12. Februar 2026").
   e. Wyciągnij listę usprawiedliwionych nieobecnych (entschuldigt sind ...).
4. Per sesja: speakers[] z (name, fraktion, turns, words).
5. Agregacja per radny: aktywność (% sesji z wypowiedzią), słowa łącznie.
6. Output: docs/kadencja-{kadencja_id}.json z `votes: []` empty,
   `councilors[]` wypełnione metrykami aktywności.

Schema kompatybilna z polish/czech miastami; `votes_*` wszystkie zero
(Berlin nie ma imiennych głosowań w default).

Użycie:
    python3 scrape_sessions.py
    python3 scrape_sessions.py --max-sessions 3
    python3 scrape_sessions.py --no-cache  # zignoruj cache, pobierz świeże
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
CITY_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = CITY_DIR / "config.json"
DEFAULT_DOCS = CITY_DIR / "docs"
DEFAULT_CACHE = CITY_DIR / ".cache" / "plpr"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 60
SLEEP_BETWEEN = 0.3

# Niemieckie miesiące → numer
GERMAN_MONTHS = {
    "Januar": 1, "Februar": 2, "März": 3, "April": 4,
    "Mai": 5, "Juni": 6, "Juli": 7, "August": 8,
    "September": 9, "Oktober": 10, "November": 11, "Dezember": 12,
}

# Mapowanie skrótów fraktion z PDF → klucz config.clubs
FRAKTION_MAP = {
    "CDU": "CDU",
    "SPD": "SPD",
    "GRÜNE": "GRUENE",
    "GRUENE": "GRUENE",
    "LINKE": "LINKE",
    "AFD": "AFD",
    "AfD": "AFD",
    "FDP": "FDP",
    "fraktionslos": "NZ",
}


def http_download(url: str, dest: Path, timeout: int = DEFAULT_TIMEOUT) -> bool:
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except (HTTPError, URLError) as exc:
        print(f"  download fail {url}: {exc}", file=sys.stderr)
        return False


def pdftotext(pdf: Path, txt: Path) -> bool:
    """Konwertuj PDF → TXT.

    Próbujemy w kolejności:
    1. pdftotext (system, część poppler-utils) — najlepsza jakość rozpoznania
       kolumn, fallbackowo używamy gdy zainstalowany.
    2. pymupdf (Python fitz) — domyślnie dostępny w obrazie NAS, działa bez
       zewnętrznych zależności systemowych.
    """
    # Pierwsza próba: pdftotext system
    try:
        subprocess.run(
            ["pdftotext", str(pdf), str(txt)],
            check=True, timeout=120, capture_output=True,
        )
        if txt.exists() and txt.stat().st_size > 0:
            return True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # Fallback: pymupdf
    try:
        import fitz  # type: ignore
    except ImportError:
        print(f"  pdftotext + pymupdf both unavailable", file=sys.stderr)
        return False
    try:
        doc = fitz.open(str(pdf))
        chunks = []
        for page in doc:
            chunks.append(page.get_text("text"))
        doc.close()
        text = "\n".join(chunks)
        txt.write_text(text, encoding="utf-8")
        return txt.exists() and txt.stat().st_size > 0
    except Exception as exc:
        print(f"  pymupdf fail: {exc}", file=sys.stderr)
        return False


# Kanoniczny slugifier wspólny dla całego projektu — patrz
# radoskop/scripts/lib_slug.py (musi dawać identyczne slugi co
# scrape_abgeordnete.py, inaczej rozjadą się profile i głosy).
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lib_slug import make_slug as _lib_make_slug  # noqa: E402
from lib_stenogram import build_transcript, write_transcript  # noqa: E402


def slugify(name: str) -> str:
    return _lib_make_slug(name) or "abgeordnet"


def normalize_name(raw: str) -> str:
    """Plenarprotokoll używa "Klaus Wolff" jako name w "Klaus Wolff (CDU):".
    Czasami z tytułem: "Dr. Klaus Wolff", "Prof. Dr. Klaus Wolff".
    Tytuły zachowujemy (są w club_assignments z config: "Dr. Timur Husein").
    """
    return re.sub(r"\s+", " ", raw).strip()


def parse_xml_for_plpr_links(xml_path: Path) -> list[tuple[str, str, str]]:
    """Zwróć listę (DokNr, DokDat, LokURL) dla wszystkich Plenarprotokoll
    w XML PARDOK.

    XML zawiera deletes (VFunktion=delete) i upserts. Trzymamy tylko
    najnowszy <Dokument> per DokNr (XML jest append-only, ostatnie wpisy
    zastępują wcześniejsze).
    """
    plpr: dict[str, tuple[str, str]] = {}
    buffer = ""
    with xml_path.open("r", encoding="utf-8", errors="replace") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            buffer = (buffer + chunk)[-2_000_000:]
            for m in re.finditer(r"<Dokument>(.*?)</Dokument>", buffer, re.DOTALL):
                block = m.group(1)
                if "<DokArt>PlPr</DokArt>" not in block:
                    continue
                num = re.search(r"<DokNr>([^<]+)</DokNr>", block)
                date = re.search(r"<DokDat>([^<]+)</DokDat>", block)
                url = re.search(r"<LokURL>([^<]+)</LokURL>", block)
                if num and url:
                    plpr[num.group(1)] = (
                        date.group(1) if date else "",
                        url.group(1),
                    )

    def sort_key(k: str) -> tuple[int, int]:
        m = re.match(r"(\d+)/(\d+)", k)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    return [(k, d, u) for k, (d, u) in sorted(plpr.items(), key=lambda x: sort_key(x[0]))]


def parse_german_date(text: str) -> str | None:
    """Z fragmentu PDF wyciągnij ISO date.
    Format: "Donnerstag, 12. Februar 2026" lub "12. Februar 2026"
    """
    m = re.search(r"(\d{1,2})\.\s*([A-ZÄÖÜa-zäöü]+)\s+(\d{4})", text)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2)
    year = int(m.group(3))
    month = GERMAN_MONTHS.get(month_name)
    if not month:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


SPEAKER_RE = re.compile(
    # Linia z "Imię Nazwisko (FRAKCJA):" — może mieć tytuły (Dr., Prof.),
    # może mieć imię z podwójnym członem ("Marc-Andre"), może mieć Senator/in
    # (np. "Senatorin Ute Bonde (Senatsverwaltung für Mobilität...):").
    # Nas interesują tylko Abgeordnete, nie senatorzy z administracji.
    r"^"
    r"((?:Dr\.\s|Prof\.\s|Dipl\.[-\.A-Za-z]*\s|Mag\.\s|Ing\.\s)*"
    r"[A-ZÄÖÜ][\wäöüß\.\-]+\s+"
    r"(?:[A-ZÄÖÜ][\wäöüß\.\-]+\s+)?"  # opcjonalne drugie imię/nazwisko
    r"[A-ZÄÖÜ][\wäöüß\.\-]+)"
    r"\s*\((CDU|SPD|GRÜNE|GRUENE|LINKE|AFD|AfD|FDP|fraktionslos)\)\s*:",
    re.MULTILINE,
)


def parse_plenarprotokoll(text: str, names_db: dict[str, str]) -> dict[str, Any]:
    """Z tekstu Plenarprotokoll wyciągnij speakers, datę i metadata.

    names_db: dict {full_name: club} z config.club_assignments. Używamy
    do walidacji speaker name (czasem pdftotext tnie nazwy między
    kolumnami i regex łapie śmieci).
    """
    out: dict[str, Any] = {
        "date": parse_german_date(text[:3000]),
        "speakers": {},  # name → {turns, words, fraktion}
        "turns": [],     # uporządkowane wypowiedzi z pełną treścią (stenogram)
        "total_words": 0,
        "drucksachen": [],  # (Drucksache 19/XXXX)
    }

    matches = list(SPEAKER_RE.finditer(text))
    for i, m in enumerate(matches):
        name = normalize_name(m.group(1))
        fraktion = FRAKTION_MAP.get(m.group(2), m.group(2))

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end]

        # Strip stage directions [Beifall...], [Zuruf...]
        chunk_clean = re.sub(r"\[[^\]]*?\]", " ", chunk)
        # Strip wtrącenia presidenta np. "Vizepräsident Mathias Schulz: Bitte!"
        chunk_clean = re.sub(
            r"^(?:Vize)?[Pp]räsident(?:in)?[^:]*:[^\n]*\n",
            "",
            chunk_clean,
            flags=re.MULTILINE,
        )

        word_count = len(re.findall(r"\b\w+\b", chunk_clean))
        # Tekst wypowiedzi do stenogramu: pdftotext zostawia łamania kolumn,
        # zbijamy białe znaki w pojedyncze spacje.
        speech = re.sub(r"\s+", " ", chunk_clean).strip()
        out["turns"].append({
            "name": name,
            "fraktion": fraktion,
            "text": speech,
            "words": word_count,
        })

        if name not in out["speakers"]:
            out["speakers"][name] = {
                "turns": 0,
                "words": 0,
                "fraktion": fraktion,
            }
        out["speakers"][name]["turns"] += 1
        out["speakers"][name]["words"] += word_count
        out["total_words"] += word_count

    # Drucksachen — wnioski, projekty
    for m in re.finditer(r"Drucksache\s+(\d+/\d+)", text):
        ds = m.group(1)
        if ds not in out["drucksachen"]:
            out["drucksachen"].append(ds)

    return out


def build_kadencja(
    config: dict[str, Any],
    kadencja_id: str,
    cache_dir: Path,
    max_sessions: int | None,
    no_cache: bool,
) -> dict[str, Any]:
    kad_meta = config.get("kadencje", {}).get(kadencja_id, {})
    wp = kad_meta.get("wp")
    if not wp:
        raise ValueError(f"config.kadencje[{kadencja_id}].wp missing")

    xml_url = config.get("berlin_pardok_xml_url", "").format(wp=wp)
    if not xml_url:
        raise ValueError("config.berlin_pardok_xml_url missing")

    print(f"[plpr] kadencja {kadencja_id} (wp={wp})", file=sys.stderr)
    xml_path = cache_dir / f"pardok-wp{wp}.xml"
    if no_cache or not xml_path.exists():
        print(f"  download XML metadata", file=sys.stderr)
        if not http_download(xml_url, xml_path, timeout=120):
            raise RuntimeError("XML download failed")

    plpr_list = parse_xml_for_plpr_links(xml_path)
    print(f"  found {len(plpr_list)} Plenarprotokolle in XML", file=sys.stderr)

    if max_sessions:
        plpr_list = plpr_list[-max_sessions:]
        print(f"  limited to last {len(plpr_list)} sesji (--max-sessions)", file=sys.stderr)

    names_db = config.get("club_assignments", {}) or {}

    sessions_out: list[dict[str, Any]] = []
    speakers_total: dict[str, dict[str, Any]] = {}

    for i, (num, date_de, url) in enumerate(plpr_list):
        # Pobierz PDF do cache
        nnn = num.split("/")[-1].zfill(3)
        pdf_path = cache_dir / f"p{wp}-{nnn}.pdf"
        txt_path = cache_dir / f"p{wp}-{nnn}.txt"

        if no_cache or not pdf_path.exists():
            print(f"  [{i+1}/{len(plpr_list)}] download {num} ({date_de})", file=sys.stderr)
            if not http_download(url, pdf_path):
                continue
            time.sleep(SLEEP_BETWEEN)

        if no_cache or not txt_path.exists():
            if not pdftotext(pdf_path, txt_path):
                continue

        text = txt_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_plenarprotokoll(text, names_db)

        # Validate speaker names against config.club_assignments —
        # mówcy nie pasujący do listy abgeordnetów (np. senatorzy admin)
        # są pomijani z agregatu per-radny, ale zapisujemy w session.
        valid_speakers = {
            name: data for name, data in parsed["speakers"].items()
            if name in names_db
        }

        attendees = sorted(valid_speakers.keys())
        # Schema speakers[]: SPA czyta sp.statements (linijka 2315 template),
        # plus s.statements (suma per sesja, linijka 1908 sets speaker_count
        # oraz wykres aktywności linijka 1600 czyta s.statements).
        speakers_arr = [
            {"name": n, "club": d["fraktion"], "statements": d["turns"], "words": d["words"]}
            for n, d in sorted(valid_speakers.items(), key=lambda x: -x[1]["words"])
        ]
        # Tury do stenogramu — tylko mówcy z listy abgeordnetów (jak speakers_arr),
        # żeby odsiać śmieci z pdftotext. _turns jest zdejmowane w main po zapisie.
        valid_names = set(valid_speakers.keys())
        session_turns = [
            {"name": t["name"], "club": t.get("fraktion"), "text": t["text"], "words": t["words"]}
            for t in parsed["turns"] if t["name"] in valid_names
        ]
        sessions_out.append({
            "date": parsed["date"],
            "number": num.split("/")[-1],
            "session": num,
            "vote_count": 0,
            "attendee_count": len(attendees),
            "attendees": attendees,
            "source_url": url,
            "speakers": speakers_arr,
            "drucksachen": parsed["drucksachen"],
            "statements": sum(sp["statements"] for sp in speakers_arr),
            "total_words": parsed["total_words"],
            "_turns": session_turns,
        })

        # Aggregate per speaker (kadencja-level)
        for name, data in valid_speakers.items():
            if name not in speakers_total:
                speakers_total[name] = {
                    "fraktion": data["fraktion"],
                    "sessions_with_speech": 0,
                    "total_turns": 0,
                    "total_words": 0,
                }
            speakers_total[name]["sessions_with_speech"] += 1
            speakers_total[name]["total_turns"] += data["turns"]
            speakers_total[name]["total_words"] += data["words"]

    # Build councilor_index (lista wszystkich abgeordnetów z config)
    councilor_index: list[str] = sorted(names_db.keys())

    # Per-radny statystyki w schemacie Radoskop. votes_* zero (brak imiennych).
    n_sessions = len(sessions_out)
    councilors: list[dict[str, Any]] = []
    for name in councilor_index:
        s = speakers_total.get(name, {
            "fraktion": names_db[name],
            "sessions_with_speech": 0,
            "total_turns": 0,
            "total_words": 0,
        })
        sessions_active = s["sessions_with_speech"]
        # Frekwencja: brak listy obecności w PDF, więc używamy sessions_with_speech
        # jako proxy. To DOLNE oszacowanie: deputowany może być na sesji bez wystąpienia.
        aktywnosc = round(100 * sessions_active / n_sessions, 1) if n_sessions else 0.0
        councilors.append({
            "name": name,
            "slug": slugify(name),
            "club": names_db[name],
            "frekwencja": aktywnosc,  # lower bound
            "aktywnosc": aktywnosc,
            "zgodnosc_z_klubem": 0.0,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0, "votes_total": 0,
            "rebellion_count": 0,
            "rebellions": [],
            "speaker_turns": s["total_turns"],
            "speaker_words": s["total_words"],
            "speaker_sessions": sessions_active,
        })

    return {
        "id": kadencja_id,
        "label": kad_meta.get("label", kadencja_id),
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessions": sessions_out,
        "total_sessions": len(sessions_out),
        "total_votes": 0,
        "total_councilors": len(councilor_index),
        "councilors": councilors,
        "votes": [],
        "similarity_top": [],
        "similarity_bottom": [],
        "councilor_index": councilor_index,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--kadencja-id", default=None)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=None)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    kadencja_id = args.kadencja_id or config.get("kadencja_active")
    if not kadencja_id:
        print("[plpr] brak kadencja_active i nie podano --kadencja-id", file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir)
    out = build_kadencja(config, kadencja_id, cache_dir, args.max_sessions, args.no_cache)

    output_path = Path(args.output) if args.output else DEFAULT_DOCS / f"kadencja-{kadencja_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Stenogramy: zapis pełnych tur per sesja do transcripts/{kid}/{num}.json,
    # zdjęcie _turns z sesji i ustawienie has_transcript przed dumpem kadencji.
    _club_lookup = {c["name"]: c.get("club") for c in out["councilors"]}
    _written = 0
    for sd in out["sessions"]:
        turns = sd.pop("_turns", None)
        if not turns:
            continue
        meta = {"city": "berlin", "city_name": "Berlin", "kadencja": kadencja_id,
                "session_number": sd["number"], "date": sd.get("date"),
                "source_url": sd.get("source_url")}
        tr = build_transcript(meta, turns, _club_lookup)
        write_transcript(output_path.parent, kadencja_id, sd["number"], tr)
        sd["has_transcript"] = True
        sd["transcript_word_count"] = tr["stats"]["total_words"]
        _written += 1
    print(f"[plpr] stenogramy zapisane: {_written}", file=sys.stderr)

    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # Zamiast wywoływać build_assembly_metrics (które robi votes-based metryki),
    # piszemy bezpośrednio data.json i profiles.json. Berlin ma już wszystko
    # policzone w councilors[].
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    docs = output_path.parent
    data_payload = {
        "scraped_at": now,
        "generated": True,
        "default_kadencja": kadencja_id,
        "kadencje": [{
            "id": kadencja_id,
            "label": out["label"],
            "sessions": out["sessions"],
            "total_sessions": out["total_sessions"],
            "total_votes": 0,
            "total_councilors": out["total_councilors"],
            "councilors": out["councilors"],
        }],
    }
    (docs / "data.json").write_text(
        json.dumps(data_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Per-radny seria sesji (do wykresu aktywności na profilu): dla każdego
    # sesji w której zabrał głos zapisujemy {date, session, statements, words}.
    sessions_by_speaker: dict[str, list[dict[str, Any]]] = {}
    for sess in sessions_out:
        for sp in sess.get("speakers", []):
            sessions_by_speaker.setdefault(sp["name"], []).append({
                "date": sess["date"],
                # Numer zgodny z trasą /session/{number}/ i kluczem stenogramu
                # (sess["number"], np. "45"), a nie pełny "19/45" — inaczej
                # deep-link "Pokaż wypowiedzi" w profilu trafiał w 404.
                "session": sess.get("number") or sess.get("session"),
                "statements": sp["statements"],
                "words": sp["words"],
            })

    clubs_meta = config.get("clubs", {}) or {}
    profiles: list[dict[str, Any]] = []
    for c in out["councilors"]:
        club_full = clubs_meta.get(c["club"], {}).get("name") or c["club"]
        spk_sessions = sessions_by_speaker.get(c["name"], [])
        sessions_spoke = c["speaker_sessions"]
        total_statements = c["speaker_turns"]
        total_words = c["speaker_words"]
        # Profile-level activity (linijka 1280 template — preview kafelka).
        activity_summary = {
            "total_statements": total_statements,
            "total_words": total_words,
            "sessions_spoke": sessions_spoke,
        }
        # Per-kadencja activity (linijka 1412+ template — pełen panel).
        avg_st = round(total_statements / sessions_spoke, 1) if sessions_spoke else 0
        avg_wd = round(total_words / sessions_spoke) if sessions_spoke else 0
        kadencja_activity = {
            "sessions_spoke": sessions_spoke,
            "total_statements": total_statements,
            "total_words": total_words,
            "avg_statements_per_session": avg_st,
            "avg_words_per_session": avg_wd,
            "sessions": spk_sessions,
        }
        profiles.append({
            "name": c["name"],
            "slug": c["slug"],
            "club": c["club"],
            "frekwencja": c["frekwencja"],
            "former": False,
            "roles": [],
            "has_activity_data": sessions_spoke > 0,
            "activity": activity_summary if sessions_spoke > 0 else None,
            "kadencje": {
                kadencja_id: {
                    "club": c["club"],
                    "club_full": club_full,
                    "okręg": None,
                    "okręg_dzielnice": None,
                    "roles": [],
                    "komisje": [],
                    "notes": "",
                    "mid_term": False,
                    "former": False,
                    "frekwencja": c["frekwencja"],
                    "aktywnosc": c["aktywnosc"],
                    "zgodnosc_z_klubem": c["zgodnosc_z_klubem"],
                    "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                    "votes_brak": 0, "votes_nieobecny": 0,
                    "votes_total": 0,
                    "rebellion_count": 0,
                    "rebellions": [],
                    "has_voting_data": False,
                    "has_activity_data": sessions_spoke > 0,
                    "activity": kadencja_activity if sessions_spoke > 0 else None,
                },
            },
        })
    profiles.sort(key=lambda p: p["name"].lower())
    (docs / "profiles.json").write_text(
        json.dumps({"scraped_at": now, "profiles": profiles, "total": len(profiles)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n[plpr] zapisano:", file=sys.stderr)
    print(f"  {output_path}", file=sys.stderr)
    print(f"  {docs / 'data.json'}", file=sys.stderr)
    print(f"  {docs / 'profiles.json'}", file=sys.stderr)
    print(f"  sesje:        {out['total_sessions']}", file=sys.stderr)
    print(f"  abgeordnete:  {out['total_councilors']}", file=sys.stderr)
    print(f"  total_words:  {sum(c['speaker_words'] for c in out['councilors']):,}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
