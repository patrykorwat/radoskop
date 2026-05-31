#!/usr/bin/env python3
"""
lib_ckan_ua.py — parser ukraińskiego standardu 5-tabelowego CSV (KMU 835).

Używany przez scraper Dniepru i inne miasta UA, które publikują dane
w krajowym standardzie na data.dniprorada.gov.ua lub data.gov.ua.

Standard 5 tabel:
  convocations  — sklikania (kadencje)
  sessions      — sesje plenarne
  motions       — punkty porządku obrad
  voteEvents    — zdarzenia głosowań (wyniki sumaryczne)
  vote          — poіменне głosowanie (jeden wiersz = jeden radny)

Tabela radnych (opcjonalna, osobny dataset):
  deputies      — votingIdentifier / familyName / name / factionName

Mapowanie głosów do schematu Radoskop:
  За             → za
  Проти          → przeciw
  Утримався      → wstrzymal_sie
  Не голосував   → brak_glosu
  Відсутній      → nieobecny

Mapowanie wyniku głosowania:
  Прийнято       → PRZYJETE
  Не прийнято    → ODRZUCONE
  Не голосували  → ODRZUCONE
  Не розглядали  → (pomijamy głosowanie)

Użycie:
  from lib_ckan_ua import CkanUaClient
  client = CkanUaClient(
      ckan_base="https://data.dniprorada.gov.ua",
      votes_dataset_id="cd170f44-...",
      deputies_dataset_id="ed6dab52-...",  # opcjonalny
  )
  data = client.build_kadencja(config, kadencja_id="2020-2025")
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 60

CATEGORIES = ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")

VOTE_TOKEN_MAP: dict[str, str] = {
    "За": "za",
    "ПРОТИ": "przeciw",
    "Проти": "przeciw",
    "Утримався": "wstrzymal_sie",
    "Утрималась": "wstrzymal_sie",
    "Утримались": "wstrzymal_sie",
    "Не голосував": "brak_glosu",
    "Не голосувала": "brak_glosu",
    "Не голосували": "brak_glosu",
    "Відсутній": "nieobecny",
    "Відсутня": "nieobecny",
    "Відсутні": "nieobecny",
}

RESULT_TOKEN_MAP: dict[str, str] = {
    "Прийнято": "PRZYJETE",
    "Не прийнято": "ODRZUCONE",
    "Не голосували": "ODRZUCONE",
    "Не розглядали": "",   # puste = pomiń
}


def _http_get(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Pobiera URL z prostym retry (3 próby, exponential backoff)."""
    print(f"  GET {url}", file=sys.stderr)
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/3 after {wait}s ({exc})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed after 3 attempts: {last_err}")


def _parse_csv(data: bytes) -> list[dict[str, str]]:
    """Parsuje CSV (UTF-8 z opcjonalnym BOM). Zwraca listę wierszy."""
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def _ckan_package_show(ckan_base: str, dataset_id: str) -> dict[str, Any]:
    """Wywołuje CKAN API package_show i zwraca result."""
    url = f"{ckan_base}/api/3/action/package_show?id={dataset_id}"
    raw = _http_get(url, timeout=30)
    result = json.loads(raw)
    if not result.get("success"):
        raise RuntimeError(f"CKAN API error for {dataset_id}: {result}")
    return result["result"]


def _parse_resources_from_html(raw: bytes, page_url: str) -> list[dict[str, str]]:
    """Parsuje HTML strony datasetu CKAN i wyciąga linki download do CSV.

    Oddziela parsowanie od fetchowania — pozwala używać z circuit breakerem
    który pobiera raw bytes przez własny mechanizm z krótkim timeoutem.
    """
    import re as _re
    html = raw.decode("utf-8", errors="replace")
    pattern = _re.compile(
        r'href=["\']([^"\'?#]*?/download/([^"\'?#/\s]+\.csv))["\']',
        _re.IGNORECASE,
    )
    base = page_url.split("/dataset/")[0]
    seen: set[str] = set()
    resources: list[dict[str, str]] = []
    for m in pattern.finditer(html):
        href, filename = m.group(1), m.group(2)
        url = href if href.startswith("http") else f"{base}{href}"
        if url not in seen:
            seen.add(url)
            resources.append({"url": url, "name": filename, "format": "CSV"})
    print(f"  [html-fallback] {len(resources)} zasobów CSV", file=sys.stderr)
    return resources


def _discover_resources_from_html(page_url: str, timeout: int = 30) -> list[dict[str, str]]:
    """HTML fallback (pełna wersja z własnym fetchem)."""
    print(f"  [html-fallback] GET {page_url}", file=sys.stderr)
    raw = _http_get(page_url, timeout=timeout)
    return _parse_resources_from_html(raw, page_url)


def _find_latest_csv(resources: list[dict], name_prefix: str) -> dict | None:
    """Znajduje zasób CSV o podanym prefiksie nazwy, posortowany malejąco.

    Zasoby CKAN mają nazwy jak 'vote_2026-05-20.csv' — sortujemy po nazwie
    malejąco żeby dostać najświeższy plik.
    """
    matches = [
        r for r in resources
        if r.get("name", "").lower().startswith(name_prefix.lower())
        and r.get("format", "").upper() == "CSV"
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda r: r.get("name", ""), reverse=True)[0]


def _map_vote(token: str) -> str | None:
    """Mapuje token głosu UA na kategorię Radoskop."""
    t = token.strip()
    if t in VOTE_TOKEN_MAP:
        return VOTE_TOKEN_MAP[t]
    # Fallback: normalizacja wielkości liter
    for key, val in VOTE_TOKEN_MAP.items():
        if key.lower() == t.lower():
            return val
    return None


def _map_result(token: str) -> str:
    """Mapuje wynik głosowania na kategorię Radoskop."""
    t = token.strip()
    return RESULT_TOKEN_MAP.get(t, t)


def _kadencja_for_date(date_str: str, kadencje: dict) -> str | None:
    """Dopasowuje datę do kadencji (tak samo jak w scrape_balsavimai.py)."""
    if not date_str:
        return None
    sorted_kad = sorted(kadencje.items(), key=lambda kv: kv[1].get("start", ""), reverse=True)
    for kid, kdef in sorted_kad:
        if date_str >= kdef.get("start", ""):
            return kid
    return None


class CkanUaClient:
    """Klient do pobierania i parsowania 5-tabelowego standardu UA z CKAN."""

    def __init__(
        self,
        ckan_base: str,
        votes_dataset_id: str,
        deputies_dataset_id: str | None = None,
        cache_dir: Path | None = None,
        skip_fetch: bool = False,
        votes_browse_url: str | None = None,
        deputies_browse_url: str | None = None,
        html_first: bool = False,
        http_timeout: int = 30,
    ) -> None:
        self.ckan_base = ckan_base.rstrip("/")
        self.votes_dataset_id = votes_dataset_id
        self.deputies_dataset_id = deputies_dataset_id
        self.cache_dir = cache_dir
        self.skip_fetch = skip_fetch
        self.votes_browse_url = votes_browse_url
        self.deputies_browse_url = deputies_browse_url
        # Gdy True: idź od razu w HTML, nie próbuj API.
        self.html_first = html_first
        # Timeout per request dla wywołań discovery (nie dla pobierania CSV).
        self.http_timeout = http_timeout
        # Circuit breaker: po pierwszym timeout discovery wszystkie kolejne
        # tabele od razu rzucają bez czekania na kolejne timeouty.
        self._discovery_failed = False

    def _cache_path(self, name: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / name

    def _load_or_fetch_csv(self, url: str, cache_name: str) -> list[dict[str, str]]:
        """Ładuje CSV z cache lub pobiera i cachuje."""
        path = self._cache_path(cache_name)
        if self.skip_fetch and path and path.exists():
            print(f"  [cache] {cache_name}", file=sys.stderr)
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        raw = _http_get(url, timeout=120)
        rows = _parse_csv(raw)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        return rows

    def _discovery_get(self, url: str) -> bytes:
        """Pobiera URL z krótkim timeoutem (self.http_timeout).

        Circuit breaker: po pierwszym niepowodzeniu ustawia _discovery_failed=True
        i kolejne wywołania rzucają natychmiast bez czekania na timeout.
        """
        if self._discovery_failed:
            raise RuntimeError("serwer nieosiągalny (circuit breaker)")
        try:
            return _http_get(url, timeout=self.http_timeout)
        except RuntimeError as exc:
            self._discovery_failed = True
            raise RuntimeError(f"serwer nieosiągalny: {exc}") from exc

    def _get_votes_resources(self) -> list[dict[str, str]]:
        """Pobiera listę zasobów datasetu głosowań.

        html_first=True: od razu HTML, API pomijane.
        html_first=False: API → HTML fallback przy błędzie.
        Circuit breaker: pierwsze timeout = wszystkie kolejne tabele skip natychmiast.
        """
        if self._discovery_failed:
            raise RuntimeError("serwer nieosiągalny (circuit breaker)")
        if self.html_first and self.votes_browse_url:
            print(f"  [html] GET {self.votes_browse_url}", file=sys.stderr)
            raw = self._discovery_get(self.votes_browse_url)
            return _parse_resources_from_html(raw, self.votes_browse_url)
        try:
            api_url = f"{self.ckan_base}/api/3/action/package_show?id={self.votes_dataset_id}"
            raw = self._discovery_get(api_url)
            result = json.loads(raw)
            if not result.get("success"):
                raise RuntimeError(f"CKAN API error: {result}")
            return result["result"].get("resources", [])
        except RuntimeError as exc:
            if not self.votes_browse_url:
                raise
            print(f"  WARN: {exc}, próbuję HTML fallback", file=sys.stderr)
            raw = self._discovery_get(self.votes_browse_url)
            return _parse_resources_from_html(raw, self.votes_browse_url)

    def _get_deputies_resources(self) -> list[dict[str, str]]:
        """Pobiera listę zasobów datasetu radnych. Analogicznie do _get_votes_resources."""
        if self._discovery_failed:
            raise RuntimeError("serwer nieosiągalny (circuit breaker)")
        if self.html_first and self.deputies_browse_url:
            print(f"  [html] GET {self.deputies_browse_url}", file=sys.stderr)
            raw = self._discovery_get(self.deputies_browse_url)
            return _parse_resources_from_html(raw, self.deputies_browse_url)
        try:
            api_url = f"{self.ckan_base}/api/3/action/package_show?id={self.deputies_dataset_id}"
            raw = self._discovery_get(api_url)
            result = json.loads(raw)
            if not result.get("success"):
                raise RuntimeError(f"CKAN API error: {result}")
            return result["result"].get("resources", [])
        except RuntimeError as exc:
            if not self.deputies_browse_url:
                raise
            print(f"  WARN: {exc}, HTML fallback (deputies)", file=sys.stderr)
            raw = self._discovery_get(self.deputies_browse_url)
            return _parse_resources_from_html(raw, self.deputies_browse_url)

    def fetch_tables(self) -> dict[str, list[dict[str, str]]]:
        """Pobiera 5 tabel głosowań z CKAN. Zwraca dict name→rows."""
        resources = self._get_votes_resources()

        tables: dict[str, list[dict[str, str]]] = {}
        for table_name in ("convocations", "sessions", "motions", "voteEvents", "vote"):
            # Nazwy zasobów mogą być np. "vote_2026-05-20.csv" albo "voteevents_2026-05-20.csv"
            # Szukamy prefiksu (case-insensitive, ignorujemy "voteEvents" vs "voteevents")
            prefix_lower = table_name.lower()
            resource = _find_latest_csv(resources, prefix_lower)
            if resource is None:
                # fallback: "voteEvents" → "voteevent" bez 's'
                alt = prefix_lower.rstrip("s")
                resource = _find_latest_csv(resources, alt)
            if resource is None:
                print(f"  WARN: brak zasobu CSV dla tabeli '{table_name}'", file=sys.stderr)
                tables[table_name] = []
                continue
            url = resource["url"]
            rows = self._load_or_fetch_csv(url, f"{table_name}.json")
            tables[table_name] = rows
            print(f"  [{table_name}] {len(rows)} wierszy", file=sys.stderr)

        return tables

    def fetch_deputies(self) -> list[dict[str, str]]:
        """Pobiera tabelę radnych z opcjonalnego datasetu deputies."""
        if not self.deputies_dataset_id:
            return []
        resources = self._get_deputies_resources()
        resource = _find_latest_csv(resources, "deputies")
        if resource is None:
            print("  WARN: brak zasobu deputies CSV", file=sys.stderr)
            return []
        url = resource["url"]
        rows = self._load_or_fetch_csv(url, "deputies.json")
        print(f"  [deputies] {len(rows)} radnych", file=sys.stderr)
        return rows

    def build_deputies_index(
        self, deputies: list[dict[str, str]]
    ) -> dict[str, dict[str, str]]:
        """Buduje słownik votingIdentifier → {name, faction}.

        Pełne imię: familyName + " " + name + " " + additionalName
        Wyświetlane: familyName + " " + name (skrót inicjały jest OK)
        """
        index: dict[str, dict[str, str]] = {}
        for d in deputies:
            vid = d.get("votingIdentifier", "").strip()
            if not vid:
                continue
            surname = d.get("familyName", "").strip()
            first = d.get("name", "").strip()
            patronymic = d.get("additionalName", "").strip()
            full_name = " ".join(filter(None, [surname, first, patronymic]))
            short_name = " ".join(filter(None, [surname, first]))
            faction = d.get("factionName", "").strip()
            index[vid] = {
                "full_name": full_name,
                "short_name": short_name,
                "faction": faction,
            }
        return index

    def build_kadencja(
        self,
        config: dict[str, Any],
        kadencja_id: str,
    ) -> dict[str, Any] | None:
        """Buduje strukturę kadencja-{id}.json ze standardu 5-tabelowego.

        Zwraca None jeśli brak danych dla tej kadencji.
        """
        kadencje = config.get("kadencje", {})
        if kadencja_id not in kadencje:
            raise ValueError(f"Kadencja '{kadencja_id}' nie istnieje w config")

        tables = self.fetch_tables()
        deputies_rows = self.fetch_deputies()
        deputies_idx = self.build_deputies_index(deputies_rows)

        # Indeks: uid → obiekt
        sessions_by_uid = {r["uid"]: r for r in tables.get("sessions", [])}
        motions_by_uid = {r["uid"]: r for r in tables.get("motions", [])}
        vote_events_by_uid: dict[str, dict] = {
            r["uid"]: r for r in tables.get("voteEvents", [])
        }

        # Grupuj głosy per voteEvent: uid → [vote_row, ...]
        votes_by_event: dict[str, list[dict]] = defaultdict(list)
        for row in tables.get("vote", []):
            uid = row.get("uid", "")
            if uid:
                votes_by_event[uid].append(row)

        # Filtruj zdarzenia głosowań do tej kadencji
        # Łańcuch: voteEvent → motion (via motionUid) → session (via sessionUid) → convocation
        # Alternatywnie: bierzemy datę z motions.date lub voteEvents.startDate

        relevant_event_uids: list[str] = []
        for ve_uid, ve in vote_events_by_uid.items():
            motion_uid = ve.get("motionUid", "")
            motion = motions_by_uid.get(motion_uid, {})
            date_str = (motion.get("date") or ve.get("startDate", "")[:10]).strip()
            if _kadencja_for_date(date_str, kadencje) == kadencja_id:
                relevant_event_uids.append(ve_uid)

        if not relevant_event_uids:
            print(
                f"  [ckan_ua] 0 zdarzeń głosowań dla kadencji {kadencja_id}",
                file=sys.stderr,
            )
            return None

        # Zbierz wszystkich radnych w tej kadencji
        all_voter_ids: set[str] = set()
        for ve_uid in relevant_event_uids:
            for v in votes_by_event.get(ve_uid, []):
                vid = v.get("voterUid", "").strip()
                if vid:
                    all_voter_ids.add(vid)

        # Buduj councilor_index: posortowana lista pełnych imion
        def voter_to_name(vid: str) -> str:
            if vid in deputies_idx:
                return deputies_idx[vid]["full_name"]
            return vid  # fallback: użyj samego ID

        councilor_names: list[str] = sorted(
            {voter_to_name(vid) for vid in all_voter_ids}
        )
        name_to_idx: dict[str, int] = {n: i for i, n in enumerate(councilor_names)}
        vid_to_idx: dict[str, int] = {
            vid: name_to_idx[voter_to_name(vid)] for vid in all_voter_ids
        }

        # Grupuj eventy per sesja → buduj sessions[]
        session_uid_for_event: dict[str, str] = {}
        for ve_uid in relevant_event_uids:
            ve = vote_events_by_uid[ve_uid]
            motion_uid = ve.get("motionUid", "")
            motion = motions_by_uid.get(motion_uid, {})
            s_uid = motion.get("sessionUid", "")
            session_uid_for_event[ve_uid] = s_uid

        sessions_events: dict[str, list[str]] = defaultdict(list)
        for ve_uid, s_uid in session_uid_for_event.items():
            sessions_events[s_uid].append(ve_uid)

        sessions_out: list[dict[str, Any]] = []
        for s_uid, ve_uids in sessions_events.items():
            sess = sessions_by_uid.get(s_uid, {})
            date_str = sess.get("dateFrom", "")[:10]
            label = sess.get("label", s_uid)

            # Zbierz obecnych radnych w tej sesji
            present_vids: set[str] = set()
            for ve_uid in ve_uids:
                for v in votes_by_event.get(ve_uid, []):
                    t = _map_vote(v.get("result", ""))
                    # "nieobecny" to nieobecny, reszta oznacza obecnego
                    if t and t != "nieobecny":
                        vid = v.get("voterUid", "").strip()
                        if vid:
                            present_vids.add(vid)

            attendees = sorted({voter_to_name(vid) for vid in present_vids})
            sessions_out.append({
                "date": date_str,
                "number": date_str,
                "title": label,
                "vote_count": len(ve_uids),
                "attendee_count": len(attendees),
                "attendees": attendees,
                "source_url": "",
            })
        sessions_out.sort(key=lambda s: s["date"])

        # Buduj votes[]
        city_slug = config.get("slug", "ua")
        votes_out: list[dict[str, Any]] = []

        for ve_uid in sorted(
            relevant_event_uids,
            key=lambda uid: vote_events_by_uid[uid].get("startDate", ""),
        ):
            ve = vote_events_by_uid[ve_uid]
            motion = motions_by_uid.get(ve.get("motionUid", ""), {})
            date_str = (motion.get("date") or ve.get("startDate", "")[:10]).strip()

            counts: dict[str, int] = {c: 0 for c in CATEGORIES}
            named_votes_idx: dict[str, list[int]] = {c: [] for c in CATEGORIES}

            for v in votes_by_event.get(ve_uid, []):
                vid = v.get("voterUid", "").strip()
                raw_token = v.get("result", "").strip()
                cat = _map_vote(raw_token)
                if not cat:
                    continue
                # "nieobecny" idzie do nieobecni
                bucket = "nieobecni" if cat == "nieobecny" else cat
                counts[bucket] += 1
                if vid in vid_to_idx:
                    named_votes_idx[bucket].append(vid_to_idx[vid])

            # Sortuj indeksy
            for bucket in named_votes_idx:
                named_votes_idx[bucket].sort()

            result_raw = ve.get("result", "").strip()
            result_cat = _map_result(result_raw)
            if result_cat == "":
                continue  # "Не розглядали" → pomijamy

            topic = ve.get("projectTitle") or motion.get("title", "")
            source_url = ve.get("textUrl", "")

            votes_out.append({
                "id": f"{city_slug}_{ve_uid}",
                "session_date": date_str,
                "session_number": date_str,
                "source_url": source_url,
                "topic": topic,
                "druk": ve.get("projectNumber", ""),
                "resolution": ve.get("text", ""),
                "result": result_cat,
                "result_native": result_raw,
                "counts": counts,
                "named_votes": named_votes_idx,
                "voted_at": ve.get("startDate", ""),
            })

        scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        kadencja_def = kadencje[kadencja_id]

        return {
            "id": kadencja_id,
            "label": kadencja_def.get("label", kadencja_id),
            "scraped_at": scraped_at,
            "sessions": sessions_out,
            "votes": votes_out,
            "councilor_index": councilor_names,
        }
