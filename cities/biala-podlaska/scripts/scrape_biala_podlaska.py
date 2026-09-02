#!/usr/bin/env python3
"""
Radoskop Biała Podlaska — imienne głosowania z BIP lubelskie (eBOI 'documents' AJAX).

ŹRÓDŁO
======
bip.bialapodlaska.pl -> umbialapodlaska.bip.lubelskie.pl (platforma eBOI/lubelskie,
menu dokumentów z DataTables AJAX). Kategoria "Wyniki głosowań / Kadencja 2024-2029"
= menu id 1092:
    GET /index.php?id=1092&action=list-ajax&draw=1&start=0&length=200
      -> {aaData: [{id_dokumentu, tresc: "Wyniki głosowań z XXXI Sesji ... z dnia
          26.08.2026", data_utworzenia, ...}]}   (JSON, DataTables envelope)
    GET /index.php?id=1092&action=details&document_id={id}
      -> <a href=".../upload/pliki/raport_z_glosowan_*.docx">Plik źródłowy</a>
Raport DOCX = standard eSesja ("Głosowano w sprawie:" + "Wyniki imienne: ZA (N) ..."),
obsługiwany przez lib_voting_pdf_table.parse_voting_text(); sekcja "Uczestnictwo w
głosowaniach jawnych" na końcu jest ODCINANA (psuje parsing ostatniego głosowania).

bialapodlaska.esesja.pl = wildcard redirect na esesja.pl (brak BIP eSesja);
rada.bialapodlaska.pl / bialapodlaska.bip.net.pl — Brak.

Kluby: PENDING (kuratorować z BIP "Kluby radnych" gdy będą).
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))

import requests  # noqa: E402

from lib_bip_static import BipScraper  # noqa: E402
from lib_voting_pdf_table import (  # noqa: E402
    extract_docx_text,
    parse_voting_pdf,
    parse_voting_text,
)

KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}

BIP_BASE = "https://umbialapodlaska.bip.lubelskie.pl"
MENU_ID = "1092"  # Rada Miasta -> Wyniki głosowań -> Kadencja 2024-2029

DATE_RE = re.compile(r"z dnia\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")
ROMAN_RE = re.compile(r"z\s+([IVXLCDM]+)\s+Sesji", re.I)
FILE_RE = re.compile(
    r'href="(https://umbialapodlaska\.bip\.lubelskie\.pl/upload/pliki/[^"]+\.(?:docx|pdf))"',
    re.I,
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


class BialaPodlaskaScraper(BipScraper):
    """BIP lubelskie (eBOI) menu-dokumentów: AJAX lista + raport DOCX na sesję."""

    def _get(self, url: str, *, binary: bool = False, use_cache: bool = True):
        if self._session is None:
            self._init_session()
        cf = None
        if use_cache and self.cache_dir:
            cf = self._cache_path(url + (".bin" if binary else ""))
            if cf and cf.is_file():
                return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="replace")
        resp = self._session.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.content if binary else resp.text
        if cf:
            cf.parent.mkdir(parents=True, exist_ok=True)
            if binary:
                cf.write_bytes(data)
            else:
                cf.write_text(data, encoding="utf-8")
        return data

    # -- discovery ----------------------------------------------------------

    def discover_sessions(self) -> list[dict]:
        kad_start = self.kadencje[self.default_kadencja]["start"]
        url = (
            f"{BIP_BASE}/index.php?id={MENU_ID}&action=list-ajax"
            "&draw=1&start=0&length=300"
        )
        raw = self._get(url, use_cache=False)
        data = json.loads(raw)
        rows = data.get("aaData", [])
        print(f"  Lista BIP: {len(rows)} raportów głosowań")
        self._by_date: dict[str, list[dict]] = {}
        sessions: dict[str, dict] = {}
        for row in rows:
            title = row.get("tresc") or ""
            doc_id = row.get("id_dokumentu")
            if not doc_id:
                continue
            dm = DATE_RE.search(title)
            if dm:
                date = f"{dm.group(3)}-{dm.group(2).zfill(2)}-{dm.group(1).zfill(2)}"
            else:
                date = row.get("data_utworzenia") or ""
            if not date or date < kad_start:
                continue
            rm = ROMAN_RE.search(title)
            roman = rm.group(1).upper() if rm else ""
            detail_url = (
                f"{BIP_BASE}/index.php?id={MENU_ID}&action=details&document_id={doc_id}"
            )
            if date not in sessions:
                sessions[date] = {
                    "date": date,
                    "number": roman or date,
                    "url": detail_url,
                }
            self._by_date.setdefault(date, []).append(
                {"doc_id": doc_id, "detail_url": detail_url, "title": title}
            )
        out = sorted(sessions.values(), key=lambda s: s["date"])
        print(f"  Sesji IX kadencji: {len(out)}")
        return out

    # -- votes ---------------------------------------------------------------

    # -- votes ---------------------------------------------------------------

    PDF_VOTES = {"za": "za", "przeciw": "przeciw", "wstrzymuje się": "wstrzymal_sie",
                 "wstrzymało się": "wstrzymal_sie", "wstrzymuję się": "wstrzymal_sie",
                 "-": "brak_glosu"}
    TOTAL_LN = re.compile(r"^(za|przeciw|wstrzymuje się|wstrzymało się|wstrzymuję się):\s*(\d+)")
    HEADER_LN = re.compile(r"^Imię i nazwisko", re.I)

    @staticmethod
    def _norm_name(name: str) -> str:
        """'Wojciech BABICZ' -> 'Wojciech Babicz' (naziwsko uppercase -> title)."""
        out = []
        for w in name.split():
            if w.isupper():
                # diacritic-aware titlecase; keep internal hyphens
                out.append(w[:1] + w[1:].lower() if len(w) > 1 else w)
            else:
                out.append(w)
        return " ".join(out)

    def _parse_pdf_table(self, data: bytes, fname: str) -> dict:
        """Stary format PDF 'Wyniki głosowań na sesji' (2024–2025):
        topic -> tabela (BLOK 'Imię i nazwisko|Stanowisko|Głos', wiersze
        'NAZWA|rola|za|przeciw|-') -> sumy 'za: N', 'przeciw: 0|wstrzymuje się: 0'.
        Bloki NIE są w kolejności y — sortujemy (page, y)."""
        import pymupdf

        doc = pymupdf.open(stream=data, filetype="pdf")
        blocks: list[tuple[int, float, float, str]] = []
        for pno, page in enumerate(doc):
            for b in page.get_text("blocks"):
                x0, y0, _x1, _y1, text = b[0], b[1], b[2], b[3], b[4]
                blocks.append((pno, y0, x0, text.strip()))
        doc.close()
        blocks.sort(key=lambda b: (b[0], b[1], b[2]))

        date_m = None
        votes: list[dict] = []
        topic_parts: list[str] = []
        rows: list[tuple[str, str]] = []
        counts: dict[str, int] = {}

        def flush():
            nonlocal topic_parts, rows, counts
            if rows:
                nv = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [],
                      "nieobecni": []}
                for nm, gv in rows:
                    key = self.PDF_VOTES.get(gv.lower().strip())
                    if key:
                        nv[key].append(nm)
                topic = re.sub(r"\s+", " ", " ".join(topic_parts)).strip()
                votes.append({
                    "vote_index": len(votes),
                    "vote_type": "uchwala" if re.match(r"^uchwa", topic, re.I) else "wniosek",
                    "topic": topic[:500],
                    "counts": counts or {k: len(v) for k, v in nv.items()},
                    "named_votes": nv,
                })
            topic_parts, rows, counts = [], [], {}

        for _pno, _y, _x, text in blocks:
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if not lines:
                continue
            if lines[0].startswith("Wyniki głosowań"):
                if not date_m:
                    date_m = re.search(r"z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})", text)
                continue
            if self.HEADER_LN.match(lines[0]):
                if rows:
                    flush()
                continue
            # blok sum: 'za: 19' albo 'przeciw: 0\nwstrzymuje się: 0'
            low_first = lines[0].lower()
            if self.TOTAL_LN.match(low_first):
                for ln in lines:
                    tm = self.TOTAL_LN.match(ln.lower())
                    if tm:
                        key = {"za": "za", "przeciw": "przeciw"}.get(tm.group(1), "wstrzymal_sie")
                        counts[key] = int(tm.group(2))
                continue
            # wiersz tabeli: [NAZWA, rola, GŁOS] (1 blok = 1 wiersz)
            if len(lines) >= 3 and lines[-1].lower() in self.PDF_VOTES:
                rows.append((self._norm_name(lines[0]), lines[-1]))
                continue
            # linia tematowa następnego głosowania → domknij poprzednie
            if rows:
                flush()
            topic_parts.append(" ".join(lines))
        flush()

        iso = None
        if date_m:
            months = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
                      "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
                      "października": 10, "listopada": 11, "grudnia": 12}
            mo = months.get(date_m.group(2).lower())
            if mo:
                iso = f"{date_m.group(3)}-{mo:02d}-{int(date_m.group(1)):02d}"
        return {"source_file": fname, "date": iso, "number_roman": None,
                "number": None, "votes": votes, "vote_count": len(votes)}

    def _parse_report(self, data: bytes, fname: str) -> dict:
        low = fname.lower()
        if low.endswith(".pdf"):
            return self._parse_pdf_table(data, fname)
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        try:
            txt = extract_docx_text(tmp)
        finally:
            Path(tmp).unlink(missing_ok=True)
        # Odetnij "Uczestnictwo w głosowaniach jawnych" — lista frekwencji na końcu
        # dokumentu psuje parse'owanie NIEOBECNI ostatniego głosowania.
        cut = txt.find("Uczestnictwo w głosowaniach")
        if cut > 0:
            txt = txt[:cut]
        return parse_voting_text(txt, txt[:1500], source_name=fname)

    def parse_session_votes(self, session: dict) -> list[dict]:
        votes: list[dict] = []
        for rep in self._by_date.get(session["date"], []):
            try:
                html = self._get(rep["detail_url"], use_cache=True)
            except Exception as e:
                print(f"    Blad details {rep['detail_url']}: {e}")
                continue
            m = FILE_RE.search(html)
            if not m:
                print(f"    Brak pliku raportu: {rep['title'][:60]}")
                continue
            furl = m.group(1)
            fname = furl.split("/")[-1]
            try:
                data = self._get(furl, binary=True, use_cache=True)
            except Exception as e:
                print(f"    Blad pliku {fname}: {e}")
                continue
            try:
                parsed = self._parse_report(data, fname)
            except Exception as e:
                print(f"    Blad parse {fname}: {e}")
                continue
            for v in parsed.get("votes", []):
                nv = v.get("named_votes") or {}
                if not any(nv.values()):
                    continue
                topic = (v.get("topic") or rep["title"])[:500]
                votes.append(
                    {
                        "id": f"{session['date']}_{v.get('vote_index', len(votes))}_{rep['doc_id']}",
                        "topic": topic,
                        "resolution": v.get("resolution") or "",
                        "source_url": rep["detail_url"],
                        "named_votes": nv,
                    }
                )
        return votes


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="Radoskop Biała Podlaska")
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir")
    ap.add_argument("--max-sessions", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-full", action="store_true")
    args = ap.parse_args()

    sc = BialaPodlaskaScraper(
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
