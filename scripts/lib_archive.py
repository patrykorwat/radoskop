#!/usr/bin/env python3
"""In-memory archive reader (RAR5/ZIP) przez ctypes + systemowy libarchive.

Kontener pipeline ma libarchive.so.13 (pakiet libarchive13) ale żadnych CLI
archiwizujących (unrar/7z/bsdtar nie zainstalowane, licencja unrar ograniczona).
Miasta takie jak Giżycko publikują raporty głosowań w archiwach RAR5/ZIP —
ten moduł pozwala je rozpakować bez żadnych pip-zależności.

Uwaga API (ustalona empirycznie): restype archive_read_new MUSI być c_void_p.
Domyślny c_int obcina wskaźnik 64-bit -> libarchive dostaje śmieć -> segfault.

Użycie:
    from lib_archive import read_archive_bytes
    entries = read_archive_bytes(open('plik.rar','rb').read())  # {nazwa: bajty}
"""
from __future__ import annotations

import ctypes
import os
import tempfile

ARCHIVE_OK = 0
ARCHIVE_EOF = 1

_LA = None


def _load():
    global _LA
    if _LA is not None:
        return _LA
    for name in ("libarchive.so.13", "libarchive.so"):
        try:
            la = ctypes.CDLL(name)
        except OSError:
            continue
        la.archive_read_new.restype = ctypes.c_void_p
        la.archive_read_new.argtypes = []
        for f in ("archive_read_support_format_all",
                  "archive_read_support_format_rar5",
                  "archive_read_support_format_zip",
                  "archive_read_support_filter_all",
                  "archive_read_close", "archive_read_free"):
            fn = getattr(la, f)
            fn.restype = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p]
        la.archive_read_open_filename.restype = ctypes.c_int
        la.archive_read_open_filename.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        la.archive_read_next_header.restype = ctypes.c_int
        la.archive_read_next_header.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        # int archive_read_data_block(archive*, const void **buffer, size_t *size, la_int64_t *offset)
        la.archive_read_data_block.restype = ctypes.c_int
        la.archive_read_data_block.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                               ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_longlong)]
        la.archive_entry_pathname.restype = ctypes.c_char_p
        la.archive_entry_pathname.argtypes = [ctypes.c_void_p]
        la.archive_error_string.restype = ctypes.c_char_p
        la.archive_error_string.argtypes = [ctypes.c_void_p]
        _LA = la
        return la
    raise RuntimeError("libarchive.so nie znaleziono (libarchive13?)")


def read_archive_bytes(blob: bytes) -> dict:
    """Rozpakuj archiwum (RAR5/ZIP, dowolny filtr) z bajtów -> {nazwa: bajty}."""
    la = _load()
    tmp = tempfile.NamedTemporaryFile(suffix=".arch", delete=False)
    try:
        tmp.write(blob)
        tmp.close()
        a = la.archive_read_new()
        if not a:
            raise RuntimeError("archive_read_new zwrócił NULL")
        la.archive_read_support_filter_all(a)
        la.archive_read_support_format_all(a)
        rc = la.archive_read_open_filename(a, tmp.name.encode(), 16384)
        if rc != ARCHIVE_OK:
            raise RuntimeError("archive open rc=%d: %s" % (rc, la.archive_error_string(a)))
        out: dict[str, bytes] = {}
        while True:
            entry = ctypes.c_void_p()
            r = la.archive_read_next_header(a, ctypes.byref(entry))
            if r == ARCHIVE_EOF:
                break
            if r < ARCHIVE_OK:
                break
            name = la.archive_entry_pathname(entry) or b"?"
            name = name.decode("utf-8", "replace")
            chunks = []
            while True:
                buf = ctypes.c_void_p()
                size = ctypes.c_size_t()
                off = ctypes.c_longlong()
                rr = la.archive_read_data_block(a, ctypes.byref(buf), ctypes.byref(size), ctypes.byref(off))
                if rr == ARCHIVE_EOF:
                    break
                if rr < ARCHIVE_OK:
                    break
                chunks.append(ctypes.string_at(buf, size.value))
            out[name] = b"".join(chunks)
        la.archive_read_close(a)
        la.archive_read_free(a)
        return out
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


if __name__ == "__main__":
    import sys
    res = read_archive_bytes(open(sys.argv[1], "rb").read())
    for i, (k, v) in enumerate(res.items()):
        if i < 6:
            print(f"{len(v):8d} {k}")
    print("entries:", len(res))
