"""
HTTP disk cache helper dla scraperów Radoskop.

Każdy scraper interpelacji ma swoją funkcję `fetch()` opartą na requests.get
albo session.get. Zamiast duplikować cache logikę 13x, scrapery importują
tę bibliotekę i opakowują własne fetche w `cached_get()`.

Wzorzec użycia w scraperze:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    from http_cache import init_cache, cached_fetch_text

    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--cache-dir", default=None)
        ...
        args = parser.parse_args()
        init_cache(args.cache_dir)

    def fetch(url):
        text = cached_fetch_text(url, session=_session, headers=HEADERS, encoding=None)
        return BeautifulSoup(text, "lxml")

Cache key: md5(url)[:16]. Pliki HTML w katalogu zadanym przez init_cache().
Session list i fresh-pages omijają cache przez `force=True`.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


_CACHE_DIR: Path | None = None
_DEFAULT_DELAY = 1.0


def init_cache(cache_dir: str | Path | None) -> None:
    """Aktywuje disk cache dla wszystkich kolejnych `cached_fetch_text` calls.

    Wywołaj raz przy starcie scrapera, po sparsowaniu --cache-dir. Bez tego
    cache jest no-op (każdy fetch idzie do HTTP).
    """
    global _CACHE_DIR
    if cache_dir:
        _CACHE_DIR = Path(cache_dir)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    else:
        _CACHE_DIR = None


def cache_active() -> bool:
    return _CACHE_DIR is not None


def _cache_path(url: str) -> Path | None:
    if _CACHE_DIR is None:
        return None
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / f"{h}.html"


def _cache_path_json(url: str, params: dict | None = None) -> Path | None:
    """Cache key dla JSON endpointów: hash(url + sorted_params)."""
    if _CACHE_DIR is None:
        return None
    key = url
    if params:
        import urllib.parse
        key = url + "?" + urllib.parse.urlencode(sorted(params.items()))
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / f"{h}.json"


def cached_fetch_json(
    url: str,
    session: Any = None,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    delay: float = _DEFAULT_DELAY,
    force: bool = False,
) -> Any:
    """JSON endpoint version of cached_fetch_text.

    Cache key uwzględnia URL + sorted params (sopot BIP API ma paginację
    przez query params: ?offset=N&limit=N).
    """
    import json as _json
    cache_file = _cache_path_json(url, params) if not force else None
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 10:
        return _json.loads(cache_file.read_text(encoding="utf-8"))

    if requests is None:
        raise RuntimeError("requests biblioteka niedostępna")

    time.sleep(delay)
    print(f"  GET {url}" + (f" params={params}" if params else ""))
    if session is not None:
        resp = session.get(url, headers=headers, params=params, timeout=timeout)
    else:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if cache_file:
        try:
            cache_file.write_text(_json.dumps(data), encoding="utf-8")
        except Exception:
            pass
    return data


def cached_fetch_text(
    url: str,
    session: Any = None,
    headers: dict[str, str] | None = None,
    encoding: str | None = None,
    timeout: int = 30,
    delay: float = _DEFAULT_DELAY,
    force: bool = False,
) -> str:
    """Pobiera URL z disk cache albo HTTP. Zwraca tekst HTML.

    Args:
        url: pełny URL
        session: requests.Session albo None (użyje requests.get)
        headers: dodatkowe nagłówki dla HTTP request
        encoding: wymuszone kodowanie response (np. "windows-1250" dla eSesja)
        timeout: HTTP timeout w sekundach
        delay: time.sleep przed HTTP (politeness)
        force: pomiń cache, zawsze HTTP. Dla list sesji / fresh content.

    Returns:
        str z body. Cache hit z dysku albo świeży HTTP.
    """
    cache_file = _cache_path(url) if not force else None
    if cache_file and cache_file.exists() and cache_file.stat().st_size > 100:
        return cache_file.read_text(encoding="utf-8")

    if requests is None:
        raise RuntimeError("requests biblioteka niedostępna, nie mogę zrobić HTTP")

    time.sleep(delay)
    print(f"  GET {url}")
    if session is not None:
        resp = session.get(url, headers=headers, timeout=timeout)
    else:
        resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    if encoding:
        resp.encoding = encoding
    text = resp.text
    if cache_file:
        try:
            cache_file.write_text(text, encoding="utf-8")
        except Exception:
            pass
    return text
