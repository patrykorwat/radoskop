#!/usr/bin/env python3
"""
lib_ua_http.py — wspólny klient HTTP dla scraperów ukraińskich (data.gov.ua).

Powstał bo data.gov.ua siedzi za Cloudflare i okresowo odrzuca proste żądania
urllib (HTTP 403). Dwa mechanizmy obronne:

  1. Nagłówki przeglądarkowe — http_get udaje nawigację Chrome (pełny zestaw
     Sec-Fetch-*, Sec-Ch-Ua, Accept-Language, Referer portalu). Pomaga przeciw
     regułom Cloudflare opartym na nagłówkach. NIE pokona wyzwania opartego na
     odcisku TLS (JA3) — od tego jest punkt 2.

  2. Dyskowy fallback discovery — ckan_resources_with_cache zapisuje listę
     zasobów do resources.json po każdym udanym package_show i wczytuje ją gdy
     API odrzuci żądanie. Dzięki temu pojedyncze 403 nie ubija całego runu, gdy
     pliki danych (ZIP/CSV) i tak są już w cache na dysku.

http_get obsługuje gzip/deflate (urllib sam nie rozpakowuje), więc można
reklamować Accept-Encoding bez psucia parsowania.

Użycie:
  from lib_ua_http import http_get, ckan_resources_with_cache

  resources, stale = ckan_resources_with_cache(
      dataset_id="3d8bbfe2-...",
      cache_path=cache_dir / "resources.json",
      skip_fetch=args.skip_fetch,
      timeout=15,
      label="vinnytsia",
  )
  if resources is None:
      return 1   # brak sieci i brak cache — nie ma z czego budować
"""

from __future__ import annotations

import gzip
import json
import sys
import time
import zlib
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CKAN_BASE = "https://data.gov.ua"

# Chrome 120 na Windows — najczęstszy odcisk, mniej podejrzany dla Cloudflare
# niż domyślny "Python-urllib".
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def browser_headers(referer: str | None = None, json_api: bool = False) -> dict[str, str]:
    """Zestaw nagłówków imitujący nawigację Chrome.

    json_api=True dla wywołań /api/3/action/* (Accept: application/json,
    Sec-Fetch-Dest: empty — tak żąda fetch() z poziomu strony portalu).
    json_api=False dla pobierania plików / stron HTML.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }
    if json_api:
        headers["Accept"] = "application/json, text/plain, */*"
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Site"] = "same-origin"
        headers["X-Requested-With"] = "XMLHttpRequest"
    else:
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        )
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "none"
        headers["Sec-Fetch-User"] = "?1"
    if referer:
        headers["Referer"] = referer
        # Gdy przychodzimy z konkretnej strony, site nie jest już "none".
        if not json_api:
            headers["Sec-Fetch-Site"] = "same-origin"
    return headers


def _decompress(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower()
    if enc == "gzip":
        return gzip.decompress(raw)
    if enc == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            # surowy deflate bez nagłówka zlib
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def http_get(
    url: str,
    timeout: int = 60,
    retries: int = 3,
    referer: str | None = None,
    json_api: bool = False,
) -> bytes:
    """Pobiera URL z nagłówkami przeglądarkowymi, retry i obsługą gzip/deflate."""
    headers = browser_headers(referer=referer, json_api=json_api)
    req = Request(url, headers=headers)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return _decompress(raw, resp.headers.get("Content-Encoding", ""))
        except (HTTPError, URLError, TimeoutError) as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{retries} after {wait}s ({exc})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed after {retries} attempts: {last_err}")


def ckan_list_resources(
    dataset_id: str,
    ckan_base: str = CKAN_BASE,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Wywołuje CKAN package_show i zwraca listę zasobów. Rzuca przy błędzie."""
    base = ckan_base.rstrip("/")
    url = f"{base}/api/3/action/package_show?id={dataset_id}"
    referer = f"{base}/dataset/{dataset_id}"
    print(f"  CKAN API {url}", file=sys.stderr)
    raw = http_get(url, timeout=timeout, referer=referer, json_api=True)
    pkg = json.loads(raw)
    if not pkg.get("success"):
        raise RuntimeError(f"CKAN error: {pkg}")
    return pkg["result"]["resources"]


def ckan_resources_with_cache(
    dataset_id: str,
    cache_path: Path,
    skip_fetch: bool = False,
    ckan_base: str = CKAN_BASE,
    timeout: int = 30,
    label: str = "ua",
) -> tuple[list[dict[str, Any]] | None, bool]:
    """Lista zasobów z fallbackiem na dyskowy cache.

    Kolejność:
      1. skip_fetch + cache → wczytaj cache (tryb offline).
      2. Sieć → przy sukcesie nadpisz cache.
      3. Sieć padła (np. 403 Cloudflare) → jeśli cache istnieje, użyj go
         (stale=True). To jest właściwa siatka bezpieczeństwa: pliki danych
         i tak są już zwykle w cache, więc run dokończy z ostatniej znanej listy.
      4. Brak sieci i brak cache → (None, False), caller przerywa.

    Zwraca (resources, stale). stale=True oznacza dane z cache po błędzie sieci.
    """
    cache_path = Path(cache_path)

    if skip_fetch and cache_path.exists():
        print(f"[{label}] using cached resource list (skip-fetch)", file=sys.stderr)
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f), False

    try:
        resources = ckan_list_resources(dataset_id, ckan_base=ckan_base, timeout=timeout)
    except RuntimeError as exc:
        if cache_path.exists():
            print(
                f"[{label}] discovery padło ({exc}) — używam cache resources.json",
                file=sys.stderr,
            )
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f), True
        print(f"[{label}] BŁĄD discovery: {exc}", file=sys.stderr)
        print(f"[{label}] data.gov.ua niedostępne i brak cache — pomiń scrape", file=sys.stderr)
        return None, False

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(resources, f, ensure_ascii=False, indent=2)
    return resources, False
