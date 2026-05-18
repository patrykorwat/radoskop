#!/usr/bin/env python3
"""
Generic BIP scraper scaffold for Radoskop.

Purpose: cities NOT on eSesja (Toruń, Rzeszów, Olsztyn, Bielsko-Biała, Gliwice,
Zabrze, etc.) each have their own BIP layout but share the same downstream
shape (sessions → votes → councilors → output JSON). lib_esesja.py covers
eSesja layout generically; this module covers everything else by giving each
city a thin subclass that only implements two methods:

    discover_sessions(self) -> list[dict]
        Each dict needs at minimum {"date": "YYYY-MM-DD", "url": "..."}.
        "number" optional — falls back to date for URL slug.

    parse_session_votes(self, session: dict) -> list[dict]
        Each vote dict: {
            "id": str (any unique id),
            "topic": str,
            "named_votes": {"za": [...], "przeciw": [...], ...},
        }
        Library fills in counts, source_url, session_date, session_number.

Everything else (HTTP fetch with caching, councilor stats, similarity,
output structure, profiles, save_split_output) is identical to eSesja.

Per-city scraper template (~120 lines including parsing logic):

    class TorunScraper(BipScraper):
        def discover_sessions(self):
            soup = self.fetch("https://bip.torun.pl/.../sesje")
            return [
                {"date": ..., "number": ..., "url": ...}
                for link in soup.select(...)
            ]

        def parse_session_votes(self, session):
            soup = self.fetch(session["url"])
            return [self._parse_one_vote(...) for ...]

    if __name__ == "__main__":
        TorunScraper(
            base_url="https://bip.torun.pl",
            kadencje={"2024-2029": {"label": "...", "start": "2024-05-07"}},
            councilors={...},
        ).run_cli(prog_name="Radoskop Toruń")
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Zainstaluj: pip install beautifulsoup4 lxml", file=sys.stderr)
    raise

try:
    import requests
except ImportError:
    print("Zainstaluj: pip install requests", file=sys.stderr)
    raise

# Reuse stateless helpers and output writers from lib_esesja so both scrapers
# emit byte-for-byte identical JSON shapes.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from lib_esesja import (  # noqa: E402
    parse_polish_date,
    make_slug,
    build_name_lookup,
    compact_named_votes,
    save_split_output,
    load_profiles,
    build_profiles_json,
    load_previous_votes_by_date,
)


class BipScraper(ABC):
    """Abstract base for non-eSesja per-city scrapers.

    Subclass and implement `discover_sessions` + `parse_session_votes`. The
    base class handles HTTP, output assembly, councilor aggregation and the
    standard CLI shape (--output, --profiles, --max-sessions, --dry-run).
    """

    UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        base_url: str,
        kadencje: dict,
        councilors: dict | None = None,
        delay: float = 1.0,
        cache_dir: Path | None = None,
        default_kadencja: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.kadencje = kadencje
        self.councilors = councilors or {}
        self.club_lookup = build_name_lookup(self.councilors)
        self.delay = delay
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if default_kadencja:
            self.default_kadencja = default_kadencja
        else:
            self.default_kadencja = next(iter(self.kadencje.keys()))
        self._session: requests.Session | None = None

    # -- HTTP layer with optional disk cache -------------------------------

    def _init_session(self) -> None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": self.UA,
            "Accept-Language": "pl-PL,pl;q=0.9",
        })
        self._session = s

    def fetch(self, url: str, *, encoding: str | None = None) -> BeautifulSoup:
        """HTML fetch returning BeautifulSoup. Use fetch_bytes for PDFs."""
        text = self.fetch_text(url, encoding=encoding)
        return BeautifulSoup(text, "lxml")

    def fetch_text(self, url: str, *, encoding: str | None = None) -> str:
        if self._session is None:
            self._init_session()
        time.sleep(self.delay)
        cached = self._cache_read_text(url)
        if cached is not None:
            print(f"  CACHE {url}")
            return cached
        print(f"  GET {url}")
        resp = self._session.get(url, timeout=30)  # type: ignore[union-attr]
        resp.raise_for_status()
        if encoding:
            resp.encoding = encoding
        self._cache_write_text(url, resp.text)
        return resp.text

    def fetch_bytes(self, url: str) -> bytes:
        """Binary fetch (e.g. PDFs) with optional disk cache."""
        if self._session is None:
            self._init_session()
        time.sleep(self.delay)
        cached = self._cache_read_bytes(url)
        if cached is not None:
            print(f"  CACHE {url}")
            return cached
        print(f"  GET {url}")
        resp = self._session.get(url, timeout=60)  # type: ignore[union-attr]
        resp.raise_for_status()
        self._cache_write_bytes(url, resp.content)
        return resp.content

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        # URL → file name. Stable hash for collisions, readable suffix.
        import hashlib
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        suffix = ".bin"
        if url.endswith(".pdf"):
            suffix = ".pdf"
        elif url.endswith((".html", "/")):
            suffix = ".html"
        return self.cache_dir / f"{h}{suffix}"

    def _cache_read_text(self, url: str) -> str | None:
        p = self._cache_path(url)
        if p and p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        return None

    def _cache_read_bytes(self, url: str) -> bytes | None:
        p = self._cache_path(url)
        if p and p.exists():
            return p.read_bytes()
        return None

    def _cache_write_text(self, url: str, text: str) -> None:
        p = self._cache_path(url)
        if not p:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def _cache_write_bytes(self, url: str, data: bytes) -> None:
        p = self._cache_path(url)
        if not p:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    # -- Club resolution ---------------------------------------------------

    def resolve_club(self, name: str) -> str:
        if name in self.club_lookup:
            return self.club_lookup[name]
        parts = name.split()
        if parts:
            last = parts[0]
            for key, club in self.club_lookup.items():
                if key.split()[0] == last or key.split()[-1] == last:
                    return club
        return ""

    # -- Hooks the subclass must implement ---------------------------------

    @abstractmethod
    def discover_sessions(self) -> list[dict]:
        """Return list of {date: 'YYYY-MM-DD', url: '...', number?: 'XV', ...}."""
        raise NotImplementedError

    @abstractmethod
    def parse_session_votes(self, session: dict) -> list[dict]:
        """Return list of vote dicts for one session.

        Each vote needs at minimum:
            {"id": "...", "topic": "...", "named_votes": {"za": [...], ...}}

        Library will populate counts, session_date, session_number, source_url.
        """
        raise NotImplementedError

    # -- Aggregations (shared with lib_esesja) -----------------------------

    def _normalize_vote(self, vote: dict, session: dict, vote_idx: int) -> dict:
        named = vote.get("named_votes", {}) or {}
        # Ensure all canonical keys exist.
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
            named.setdefault(cat, [])
        counts = {cat: len(named[cat]) for cat in named}
        # Include session number in fallback vote_id so two sessions on the
        # same date do not produce identical IDs (Radom XXI vs XXII both on
        # 2025-03-31 collapsed to 2025-03-31_001_000 for both, hiding
        # duplicates from manual JSON inspection).
        session_num = session.get("number", "") or ""
        num_part = f"_{session_num}" if session_num else ""
        return {
            "id": vote.get("id") or f"{session['date']}{num_part}_{vote_idx:03d}_000",
            "source_url": vote.get("source_url") or session.get("url", ""),
            "session_date": session["date"],
            "session_number": session.get("number", "") or session["date"],
            "topic": (vote.get("topic") or f"Glosowanie {vote_idx + 1}")[:500],
            "druk": vote.get("druk"),
            "resolution": vote.get("resolution"),
            "counts": counts,
            "named_votes": named,
        }

    def build_councilors(
        self,
        all_votes: list[dict],
        sessions: list[dict],
        existing_profiles: dict,
    ) -> list[dict]:
        stats: dict[str, dict] = defaultdict(lambda: {
            "name": "", "club": "", "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0, "votes_total": 0,
            "frekwencja": 0, "aktywnosc": 0, "zgodnosc_z_klubem": 0,
            "votes_with_club_majority": 0, "votes_against_club_majority": 0,
            "rebellion_count": 0, "rebellions": [],
            "has_voting_data": True, "has_activity_data": False,
        })

        for vote in all_votes:
            for cat, names in vote["named_votes"].items():
                for name in names:
                    stats[name]["name"] = name
                    stats[name]["club"] = self.resolve_club(name)
                    stats[name]["votes_total"] += 1
                    if cat == "za":
                        stats[name]["votes_za"] += 1
                    elif cat == "przeciw":
                        stats[name]["votes_przeciw"] += 1
                    elif cat == "wstrzymal_sie":
                        stats[name]["votes_wstrzymal"] += 1
                    elif cat == "brak_glosu":
                        stats[name]["votes_brak"] += 1

        # Zgodność z klubem: per głosowanie ustal majority per klub (max z
        # za/przeciw/wstrzymal_sie), potem porównaj głos każdego radnego.
        # Klub "?" (niezrzeszeni, nieznani) pomijamy. Liczymy tylko aktywne
        # głosy (za/przeciw/wstrzymal_sie), nie absencje.
        ACTIVE_CATS = ("za", "przeciw", "wstrzymal_sie")
        for vote in all_votes:
            # Mapa: name → cat (tylko aktywne)
            councilor_vote: dict[str, str] = {}
            for cat in ACTIVE_CATS:
                for name in vote["named_votes"].get(cat, []):
                    councilor_vote[name] = cat

            # Per klub policz głosy w 3 kategoriach.
            club_tally: dict[str, dict[str, int]] = defaultdict(
                lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0}
            )
            for name, cat in councilor_vote.items():
                club = stats[name].get("club") or self.resolve_club(name)
                if not club or club == "?":
                    continue
                club_tally[club][cat] += 1

            # Wyłoń majority per klub. Tie → bierzemy max() default order
            # (za > przeciw > wstrzymal_sie). Klub z 0 głosów aktywnych
            # pomijamy.
            club_majority: dict[str, str] = {}
            for club, tally in club_tally.items():
                if sum(tally.values()) == 0:
                    continue
                club_majority[club] = max(tally, key=tally.get)

            # Per radny: zgodność z majority swojego klubu.
            for name, their_cat in councilor_vote.items():
                club = stats[name].get("club") or self.resolve_club(name)
                if not club or club == "?":
                    continue
                majority = club_majority.get(club)
                if not majority:
                    continue
                if their_cat == majority:
                    stats[name]["votes_with_club_majority"] += 1
                else:
                    stats[name]["votes_against_club_majority"] += 1
                    stats[name]["rebellions"].append({
                        "session": vote.get("session_date", ""),
                        "topic": (vote.get("topic") or "")[:100],
                        "their_vote": their_cat,
                        "club_majority": majority,
                    })

        for s in stats.values():
            if s["votes_total"] > 0:
                s["frekwencja"] = round(
                    (s["votes_total"] - s["votes_brak"]) / s["votes_total"] * 100, 1
                )
                s["aktywnosc"] = round(
                    (s["votes_za"] + s["votes_przeciw"] + s["votes_wstrzymal"])
                    / s["votes_total"] * 100, 1
                )
            alignment_total = s["votes_with_club_majority"] + s["votes_against_club_majority"]
            if alignment_total > 0:
                s["zgodnosc_z_klubem"] = round(
                    s["votes_with_club_majority"] / alignment_total * 100, 1
                )
            s["rebellion_count"] = len(s["rebellions"])

        result = []
        for name, s in sorted(stats.items()):
            if name in existing_profiles:
                s.update({k: v for k, v in existing_profiles[name].items() if k not in s or not s[k]})
            result.append(s)
        return result

    @staticmethod
    def compute_similarity(all_votes: list[dict], councilors: list[dict]) -> tuple[list, list]:
        name_to_club = {c["name"]: c.get("club", "?") for c in councilors}
        vectors: dict[str, dict] = defaultdict(dict)
        for v in all_votes:
            for cat in ["za", "przeciw", "wstrzymal_sie"]:
                for name in v["named_votes"].get(cat, []):
                    vectors[name][v["id"]] = cat

        names = sorted(vectors.keys())
        pairs = []
        for a, b in combinations(names, 2):
            common = set(vectors[a].keys()) & set(vectors[b].keys())
            if len(common) < 10:
                continue
            same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
            score = round(same / len(common) * 100, 1)
            pairs.append({
                "a": a, "b": b,
                "club_a": name_to_club.get(a, "?"),
                "club_b": name_to_club.get(b, "?"),
                "score": score,
                "common_votes": len(common),
            })
        pairs.sort(key=lambda x: x["score"], reverse=True)
        return pairs[:20], pairs[-20:][::-1]

    @staticmethod
    def build_sessions(sessions_raw: list[dict], all_votes: list[dict]) -> list[dict]:
        votes_by_date: dict[str, list[dict]] = defaultdict(list)
        for v in all_votes:
            votes_by_date[v["session_date"]].append(v)
        result = []
        for s in sessions_raw:
            date = s["date"]
            session_votes = votes_by_date.get(date, [])
            attendees: set[str] = set()
            for v in session_votes:
                for cat in ["za", "przeciw", "wstrzymal_sie", "brak_glosu"]:
                    attendees.update(v["named_votes"].get(cat, []))
            number = s.get("number", "") or date
            result.append({
                "date": date,
                "number": number,
                "vote_count": len(session_votes),
                "attendee_count": len(attendees),
                "attendees": sorted(attendees),
                "speakers": [],
            })
        return sorted(result, key=lambda x: x["date"])

    # -- Top-level run -----------------------------------------------------

    def run(
        self,
        output_path: str | Path,
        profiles_path: str | Path,
        max_sessions: int = 0,
        dry_run: bool = False,
        incremental_window_days: int = 30,
        force_full: bool = False,
    ) -> int:
        self._init_session()
        slug = self.base_url.split("//", 1)[-1].split("/", 1)[0].split(".", 1)[0]
        print(f"\n=== Radoskop {slug} — BIP scraper ===\n")

        print("[1/4] Pobieranie listy sesji...")
        sessions = list(self.discover_sessions())
        # Filter to current kadencja.
        kadencja_start = self.kadencje[self.default_kadencja]["start"]
        sessions = [s for s in sessions if s.get("date", "") >= kadencja_start]
        if not sessions:
            print("BŁĄD: Nie znaleziono sesji w bieżącej kadencji.")
            return 1
        if max_sessions > 0:
            sessions = sessions[:max_sessions]
        sessions.sort(key=lambda s: s["date"])
        print(f"  Znaleziono {len(sessions)} sesji w kadencji {self.default_kadencja}\n")

        if dry_run:
            print("Dry-run: Zatrzymuję się tutaj.")
            return 0

        # Incremental: skip parsing closed sessions whose votes are already cached.
        # Stale safety window protects against late corrections / retroactive
        # vote registrations.
        prev_votes_by_date: dict[tuple[str, str], list[dict]] = {}
        if not force_full:
            kad_file = Path(output_path).parent / f"kadencja-{self.default_kadencja}.json"
            prev_votes_by_date = load_previous_votes_by_date(kad_file)
            if prev_votes_by_date:
                print(
                    f"  Cache: {sum(len(v) for v in prev_votes_by_date.values())} "
                    f"głosowań z poprzedniego runu ({len(prev_votes_by_date)} sesji)"
                )

        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=incremental_window_days)).isoformat()
        print(f"  Incremental window: re-scrape sesji od {cutoff} (granica {incremental_window_days} dni)")

        print("[2/4] Pobieranie głosowań z sesji...")
        all_votes: list[dict] = []
        fresh_count = 0
        cached_count = 0
        for i, session in enumerate(sessions):
            # Cache key matches load_previous_votes_by_date: (date, session_number).
            # Indexing by date only collapsed two sessions on the same date into
            # one bucket and each scrape run doubled the votes for that date.
            cache_key = (session["date"], session.get("number", "") or "")
            cached = prev_votes_by_date.get(cache_key)
            if cached and session["date"] < cutoff:
                print(f"  [{i+1}/{len(sessions)}] CACHED Sesja {session['date']} ({len(cached)} głosowań)")
                all_votes.extend(cached)
                cached_count += len(cached)
                continue
            print(f"  [{i+1}/{len(sessions)}] Sesja {session.get('id') or session['date']} ({session['date']})")
            try:
                raw_votes = list(self.parse_session_votes(session))
            except Exception as exc:
                print(f"    Blad parsowania sesji {session['date']}: {exc}")
                raw_votes = []
            for idx, v in enumerate(raw_votes):
                normalized = self._normalize_vote(v, session, idx)
                if sum(normalized["counts"].values()) > 0:
                    all_votes.append(normalized)
                    fresh_count += 1
        # Defensive dedup: historical data may already contain duplicates from
        # the legacy by-date cache key. Dedup by (date, number, id) keeps the
        # first occurrence. Belt and suspenders alongside the cache key fix.
        seen_keys: set[tuple[str, str, str]] = set()
        deduped: list[dict] = []
        for v in all_votes:
            key = (
                v.get("session_date") or "",
                v.get("session_number") or "",
                v.get("id") or "",
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(v)
        dropped = len(all_votes) - len(deduped)
        if dropped:
            print(f"  Dedup: usunieto {dropped} duplikatow (legacy by-date cache bug)")
        all_votes = deduped
        print(f"  Pobrano {fresh_count} fresh + {cached_count} cached = {len(all_votes)} głosowań\n")

        print("[3/4] Budowanie danych...")
        existing_profiles = load_profiles(profiles_path)
        councilors = self.build_councilors(all_votes, sessions, existing_profiles)
        sessions_data = self.build_sessions(sessions, all_votes)
        sim_top, sim_bottom = self.compute_similarity(all_votes, councilors)

        club_counts: dict[str, int] = defaultdict(int)
        for c in councilors:
            club_counts[c["club"]] += 1
        print(f"  {len(sessions_data)} sesji, {len(all_votes)} głosowań, {len(councilors)} radnych")
        print(f"  Kluby: {dict(club_counts)}\n")

        kid = self.default_kadencja
        kad_output = {
            "id": kid,
            "label": self.kadencje[kid]["label"],
            "clubs": {club: count for club, count in sorted(club_counts.items())},
            "sessions": sessions_data,
            "total_sessions": len(sessions_data),
            "total_votes": len(all_votes),
            "total_councilors": len(councilors),
            "councilors": councilors,
            "votes": all_votes,
            "similarity_top": sim_top,
            "similarity_bottom": sim_bottom,
        }
        output = {
            "generated": datetime.now().isoformat(),
            "default_kadencja": kid,
            "kadencje": [kad_output],
        }

        print("[4/4] Zapisywanie danych...")
        out_path = Path(output_path)
        save_split_output(output, out_path)
        print(f"Gotowe! Zapisano do {out_path}")

        build_profiles_json(output, profiles_path)
        return 0

    def run_cli(self, prog_name: str | None = None) -> int:
        parser = argparse.ArgumentParser(description=prog_name or "BIP scraper")
        parser.add_argument("--output", default="docs/data.json")
        parser.add_argument("--profiles", default="docs/profiles.json")
        parser.add_argument("--max-sessions", type=int, default=0)
        parser.add_argument("--delay", type=float, default=1.0)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--cache-dir", default=None,
                            help="Optional cache dir for fetched HTML/PDFs (speeds up reruns)")
        parser.add_argument("--pdf-dir", default=None,
                            help="Alias for --cache-dir, kept for scrape_all.sh compatibility")
        parser.add_argument(
            "--incremental-window", type=int, default=30,
            help="Re-scrape sessions newer than N days (default 30); older sessions reuse cached votes",
        )
        parser.add_argument(
            "--full", action="store_true",
            help="Force full re-scrape, ignoring previous kadencja JSON",
        )
        args = parser.parse_args()
        if args.delay != 1.0:
            self.delay = args.delay
        if args.cache_dir:
            self.cache_dir = Path(args.cache_dir)
        elif args.pdf_dir:
            self.cache_dir = Path(args.pdf_dir)
        return self.run(
            incremental_window_days=args.incremental_window,
            force_full=args.full,
            output_path=args.output,
            profiles_path=args.profiles,
            max_sessions=args.max_sessions,
            dry_run=args.dry_run,
        )


# ---------------------------------------------------------------------------
# Stub example: not a working scraper, just shows the minimum subclass shape.
# Per-city implementations live in radoskop/cities/{slug}/scripts/scrape_{slug}.py.
# ---------------------------------------------------------------------------

class _ExampleStub(BipScraper):
    def discover_sessions(self) -> list[dict]:
        # Replace with real BIP HTML parsing.
        return []

    def parse_session_votes(self, session: dict) -> list[dict]:
        # Replace with real session page parsing.
        return []
