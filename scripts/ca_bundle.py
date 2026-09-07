"""ca_bundle — zaufany bundle CA + uzupełniające certyfikaty intermediate.

Niektóre polskie BIP-y (np. bip.um.wroc.pl) wysyłają w TLS handshake TYLKO
certyfikat właściwy bez pośredniego — requests z certifi zwraca wtedy
CERTIFICATE_VERIFY_FAILED (kod 21). Łańcuch domyka publiczny intermediate z
repo `certs/` (zweryfikowany: openssl verify -untrusted intermediate leaf).

Rozwiązanie: SSL_CERT_FILE + REQUESTS_CA_BUNDLE na bundle (system CA +
certs/*.pem). requests/urllib/curl respektują te zmienne; certifi wchodzi do
gry dopiero gdy ich brak.

`import ca_bundle` przy starcie scrapera = auto-ustawienie. Idempotentne.
"""
from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _certs_dir() -> Path | None:
    for cand in (_HERE.parent / "certs",                      # radoskop/scripts -> radoskop/certs
                 _HERE.parent.parent / "radoskop" / "certs",  # premium runtime (zagnieżdżony radoskop)
                 _HERE.parent.parent.parent / "radoskop" / "certs"):  # premium dev workspace
        if cand.is_dir() and list(cand.glob("*.pem")):
            return cand
    return None


def system_ca_file() -> str | None:
    for p in ("/etc/ssl/certs/ca-certificates.crt",
              "/etc/pki/tls/certs/ca-bundle.crt"):
        if Path(p).is_file():
            return p
    return None


def build_bundle(dest=None) -> str:
    cd = _certs_dir()
    if cd is None:
        raise RuntimeError("brak katalogu certs/ z intermediate")
    if dest is None:
        import tempfile
        dest = Path(tempfile.gettempdir()) / "radoskop-ca-bundle.pem"
    dest = Path(dest)
    parts = []
    sys_ca = system_ca_file()
    if sys_ca:
        parts.append(Path(sys_ca).read_text(encoding="utf-8", errors="ignore"))
    for e in sorted(cd.glob("*.pem")):
        parts.append(e.read_text(encoding="utf-8", errors="ignore"))
    dest.write_text("\n".join(parts), encoding="utf-8")
    return str(dest)


def ensure_ca_bundle() -> str | None:
    """Ustaw SSL_CERT_FILE/REQUESTS_CA_BUNDLE na bundle z uzupełnieniami."""
    cd = _certs_dir()
    if cd is None or system_ca_file() is None:
        return None
    existing = os.environ.get("SSL_CERT_FILE") or ""
    if existing and "radoskop-ca-bundle" in existing and Path(existing).is_file():
        return existing
    try:
        bundle = build_bundle()
    except OSError:
        return None
    os.environ["SSL_CERT_FILE"] = bundle
    os.environ["REQUESTS_CA_BUNDLE"] = bundle
    return bundle


# Celowy efekt importu: każdy `import ca_bundle` konfiguruje środowisko.
ensure_ca_bundle()
