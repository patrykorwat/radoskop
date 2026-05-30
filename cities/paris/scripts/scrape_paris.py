#!/usr/bin/env python3
"""Scraper Conseil de Paris.

USTALENIE PO RESEARCHU (2026-05-24, zweryfikowane na żywym API):
opendata.paris.fr NIE publikuje wyników głosowań per grupa. Sprawdzone:
  * katalog datasetów dla "vote/scrutin/délibération" zwraca tylko: agendy
    (ordre du jour), "Délibération Emploi" (HR), état du personnel, wyniki
    wyborów. Żadnego datasetu z wynikami głosowań rady.
  * dataset agendy "ordre-du-jour-du-conseil-de-paris-..." (15 488 rekordów)
    ma pola seance/reference/objet/type/elu_depositaire/entite_depositaire/
    rapporteur — czyli CO było głosowane, ale BEZ wyniku i bez rozbicia na
    grupy.

Per-group tableau des votes istnieje tylko w procès-verbaux (PDF) na paris.fr
i tylko dla "scrutins publics" (głosowań rejestrowanych na wniosek grupy) —
to strukturalny bloker Tier 4 z eu_council_voting_analysis.md.

CO ROBI TEN SCRAPER (realne, działające):
  --agenda   pobiera z żywego API agendę Conseil de Paris, grupuje pozycje po
             sesji (séance) i zapisuje strukturę sesji + projektów délibération.
             To NIE są głosowania frakcyjne — to porządek obrad. Daje szkielet
             sesji i listę spraw, gotowy do podpięcia wyników, gdy pojawią się
             tableaux par groupe z PV.

ŚCIEŻKA GŁOSOWAŃ FRAKCYJNYCH (gdy są dane z PV):
  Surowe liczniki per grupa buduje się rekordem przez
  lib_faction_votes.make_faction_vote(...). Funkcja build_faction_vote_from_tableau
  poniżej to adapter: bierze {kod_grupy: {za,przeciw,...}} i zwraca rekord vote
  zgodny z frontem. PV parser (PDF) to osobny, większy task — patrz README sekcja.

Kontrakt danych i tryb frakcyjny: radoskop-premium/strategia/GLOSOWANIA_FRAKCYJNE.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CITY_DIR = Path(__file__).resolve().parents[1]          # cities/paris
REPO_DIR = CITY_DIR.parents[1]                          # radoskop
sys.path.insert(0, str(REPO_DIR / "scripts"))

from lib_faction_votes import make_faction_vote  # noqa: E402

KADENCJA_ID = "2026-2032"

# ---------------------------------------------------------------------------
# Radni kadencji 2026-2032 (źródło: Wikipedia + paris.fr, stan 2026-03-29)
# Slug: imię-nazwisko z normalizacją znaków diakrytycznych.
# ---------------------------------------------------------------------------
import unicodedata as _ud

def _slug(name: str) -> str:
    nfkd = _ud.normalize("NFKD", name)
    ascii_ = nfkd.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")


# (name, club_key) — club_key musi odpowiadać kluczowi w config.json["clubs"]
_COUNCILLORS_2026: list[tuple[str, str]] = [
    # GSDG - Groupe Socialiste et divers gauche (53)
    ("Emmanuel Grégoire", "SOCIALISTE_DG"), ("Lamia El Aaraje", "SOCIALISTE_DG"),
    ("Marine Rosset", "SOCIALISTE_DG"), ("Éric Pliez", "SOCIALISTE_DG"),
    ("Jérôme Coumet", "SOCIALISTE_DG"), ("Alexandra Cordebard", "SOCIALISTE_DG"),
    ("Isabelle Rocca", "SOCIALISTE_DG"), ("François Vauglin", "SOCIALISTE_DG"),
    ("Johanne Kouassi", "SOCIALISTE_DG"), ("Halima Jemni", "SOCIALISTE_DG"),
    ("Ariel Weil", "SOCIALISTE_DG"), ("Paul Simondon", "SOCIALISTE_DG"),
    ("Emma Rafowicz", "SOCIALISTE_DG"), ("Éric Lejoindre", "SOCIALISTE_DG"),
    ("François Dagnaud", "SOCIALISTE_DG"), ("Théa Fourdrinier", "SOCIALISTE_DG"),
    ("Antoine Guillou", "SOCIALISTE_DG"), ("Marion Waller", "SOCIALISTE_DG"),
    ("Alexandre Menucci", "SOCIALISTE_DG"), ("Juliette Sabatier", "SOCIALISTE_DG"),
    ("Richard Bouigue", "SOCIALISTE_DG"), ("Thomas Chevandier", "SOCIALISTE_DG"),
    ("Florian Sitbon", "SOCIALISTE_DG"), ("Carine Rolland", "SOCIALISTE_DG"),
    ("Kévin Havet", "SOCIALISTE_DG"), ("Gaston Laval", "SOCIALISTE_DG"),
    ("Mathieu Delmestre", "SOCIALISTE_DG"), ("Gabrielle Siry-Houari", "SOCIALISTE_DG"),
    ("Adji Ahoudian", "SOCIALISTE_DG"), ("Dominique Kielemoës", "SOCIALISTE_DG"),
    ("Elisa Yavchitz", "SOCIALISTE_DG"), ("Camille Vizioz-Brami", "SOCIALISTE_DG"),
    ("Luc Lebon", "SOCIALISTE_DG"), ("Kadiatou Coulibaly", "SOCIALISTE_DG"),
    ("Geneviève Garrigos", "SOCIALISTE_DG"), ("Daniel Tran", "SOCIALISTE_DG"),
    ("Charles Mergey", "SOCIALISTE_DG"), ("Yvain Bourgeat-Lami", "SOCIALISTE_DG"),
    ("Saïd Benmouffok", "SOCIALISTE_DG"), ("Karine Barbagli", "SOCIALISTE_DG"),
    ("Pierre Lombard", "SOCIALISTE_DG"), ("Audrey Pulvar", "SOCIALISTE_DG"),
    ("Pierre Rabadan", "SOCIALISTE_DG"), ("Maxime Sauvage", "SOCIALISTE_DG"),
    ("Maxime des Gayets", "SOCIALISTE_DG"), ("Alexandra Jardin", "SOCIALISTE_DG"),
    ("Céline Hervieu", "SOCIALISTE_DG"), ("Agnès Bertrand", "SOCIALISTE_DG"),
    ("Karim Ziady", "SOCIALISTE_DG"), ("Anouch Toranian", "SOCIALISTE_DG"),
    ("Yasmina Merzi", "SOCIALISTE_DG"), ("Valentin Guénanen", "SOCIALISTE_DG"),
    ("Carine Ekon", "SOCIALISTE_DG"),
    # ESP - Écologiste et Social de Paris (36)
    ("David Belliard", "ECOLOGISTE"), ("Anne-Claire Boux", "ECOLOGISTE"),
    ("Fatoumata Koné", "ECOLOGISTE"), ("Guillaume Durand", "ECOLOGISTE"),
    ("Dan Lert", "ECOLOGISTE"), ("Amina Bouri", "ECOLOGISTE"),
    ("Carine Petit", "ECOLOGISTE"), ("Antoine Dupont", "ECOLOGISTE"),
    ("Laurent Sorel", "ECOLOGISTE"), ("Maxime Crosnier", "ECOLOGISTE"),
    ("Mélody Tonolli", "ECOLOGISTE"), ("Marie-Pierre Marchand", "ECOLOGISTE"),
    ("Mams Yaffa", "ECOLOGISTE"), ("Aminata Niakaté", "ECOLOGISTE"),
    ("Alice Timsit", "ECOLOGISTE"), ("Pierre Benassaya", "ECOLOGISTE"),
    ("Sylvain Raifaud", "ECOLOGISTE"), ("Rania Kissi", "ECOLOGISTE"),
    ("Arnaud Lehoux", "ECOLOGISTE"), ("Nour Durand-Raucher", "ECOLOGISTE"),
    ("Manon Havet", "ECOLOGISTE"), ("Tom Rouffio", "ECOLOGISTE"),
    ("Frédérique Dutreuil", "ECOLOGISTE"), ("Simon Duquerroir", "ECOLOGISTE"),
    ("Sylvain Maschino", "ECOLOGISTE"), ("Jeanne Ouvret", "ECOLOGISTE"),
    ("Laëtitia Vipard", "ECOLOGISTE"), ("Irénée Frerot", "ECOLOGISTE"),
    ("Bechir Saket Bouderbala", "ECOLOGISTE"), ("Marion-Émi Alix", "ECOLOGISTE"),
    ("Azadeh Akrami-Castanon", "ECOLOGISTE"), ("Lucie Castets", "ECOLOGISTE"),
    ("Antoine Alibert", "ECOLOGISTE"), ("Annah Bikouloulou", "ECOLOGISTE"),
    ("Nicolas Rouveau", "ECOLOGISTE"), ("Ophélie Madinier", "ECOLOGISTE"),
    # GCP - Groupe Communiste de Paris (13)
    ("Ian Brossat", "COMMUNISTE"), ("Raphaëlle Primet", "COMMUNISTE"),
    ("Adrien Tiberti", "COMMUNISTE"), ("Laurence Patrice", "COMMUNISTE"),
    ("Jean-Noël Aqua", "COMMUNISTE"), ("Barbara Gomes", "COMMUNISTE"),
    ("Jacques Baudrier", "COMMUNISTE"), ("Nicolas Bonnet Oulaldj", "COMMUNISTE"),
    ("Hélène Bidard", "COMMUNISTE"), ("Ladji Sakho", "COMMUNISTE"),
    ("Camille Naget", "COMMUNISTE"), ("Gwenaëlle Austin", "COMMUNISTE"),
    ("Rym Karaoun Gouezou", "COMMUNISTE"),
    # NI - Non inscrits (1)
    ("Lila Bouadma", "NZ"),
    # PL - Paris Liberté (32)
    ("Rachida Dati", "PARIS_LIBERTE"), ("Sylvain Maillard", "PARIS_LIBERTE"),
    ("Eric Schahl", "PARIS_LIBERTE"), ("Agnès Evren", "PARIS_LIBERTE"),
    ("Geoffroy Boulard", "PARIS_LIBERTE"), ("Benjamin Haddad", "PARIS_LIBERTE"),
    ("Jean-Pierre Lecoq", "PARIS_LIBERTE"), ("Nelly Garnier", "PARIS_LIBERTE"),
    ("Philippe Goujon", "PARIS_LIBERTE"), ("Jérémy Redler", "PARIS_LIBERTE"),
    ("Valérie Montandon", "PARIS_LIBERTE"), ("Catherine Dumas", "PARIS_LIBERTE"),
    ("Emmanuelle Dauvergne", "PARIS_LIBERTE"), ("Grégory Canal", "PARIS_LIBERTE"),
    ("David Alphand", "PARIS_LIBERTE"), ("François-Marie Didier", "PARIS_LIBERTE"),
    ("Aurélien Véron", "PARIS_LIBERTE"), ("Inès de Raguenel", "PARIS_LIBERTE"),
    ("Anne Biraben", "PARIS_LIBERTE"), ("Jérôme Sterkers", "PARIS_LIBERTE"),
    ("Catherine Lécuyer", "PARIS_LIBERTE"), ("Pierre Liscia", "PARIS_LIBERTE"),
    ("Jean-Baptiste Olivier", "PARIS_LIBERTE"), ("Valentine Serino", "PARIS_LIBERTE"),
    ("Karl Astie", "PARIS_LIBERTE"), ("Antoine Beauquier", "PARIS_LIBERTE"),
    ("Alexandra Nicol", "PARIS_LIBERTE"), ("Frédéric Péchenard", "PARIS_LIBERTE"),
    ("Véronique Baldini", "PARIS_LIBERTE"), ("Anne-Claire Tyssandier", "PARIS_LIBERTE"),
    ("Thierry Guerrier", "PARIS_LIBERTE"), ("Eléonore Creuze", "PARIS_LIBERTE"),
    # PA - Paris apaisé (11)
    ("Emmanuelle Hoffman", "PARIS_APAISE"),
    ("Florence Berthout", "PARIS_APAISE"), ("Daniel-Georges Courtois", "PARIS_APAISE"),
    ("Marlène Schiappa", "PARIS_APAISE"), ("Julie Boillot", "PARIS_APAISE"),
    ("Antoine Lesieur", "PARIS_APAISE"), ("Catherine Ibled", "PARIS_APAISE"),
    ("Clara Chassaniol", "PARIS_APAISE"), ("Rachel-Flore Pardo", "PARIS_APAISE"),
    ("Alexia Germont", "PARIS_APAISE"), ("Abdoulaye Kanté", "PARIS_APAISE"),
    # PAC - Paris au centre (8)
    ("Maud Gatel", "PARIS_CENTRE"), ("Delphine Bürkli", "PARIS_CENTRE"), ("Pierre Baty", "PARIS_CENTRE"),
    ("Béatrice Lecouturier", "PARIS_CENTRE"), ("Séverine de Compreignac", "PARIS_CENTRE"),
    ("Pierre Casanova", "PARIS_CENTRE"), ("Jules Pasquier", "PARIS_CENTRE"),
    ("Sandro Gozi", "PARIS_CENTRE"),
    # NPP - Nouveau Paris Populaire (9)
    ("Sophia Chikirou", "NOUVEAU_PARIS"), ("Émile Meunier", "NOUVEAU_PARIS"),
    ("Céline Verzeletti", "NOUVEAU_PARIS"), ("Roland Timsit", "NOUVEAU_PARIS"),
    ("Sonia Chaouche", "NOUVEAU_PARIS"), ("Rodrigo Arenas", "NOUVEAU_PARIS"),
    ("Sophie de La Rochefoucauld", "NOUVEAU_PARIS"),
    ("Christophe Prudhomme", "NOUVEAU_PARIS"), ("Sabrina Nouri", "NOUVEAU_PARIS"),
]


def _build_profiles(scraped_at: str) -> dict:
    profiles = []
    for name, club in _COUNCILLORS_2026:
        profiles.append({
            "slug": _slug(name),
            "name": name,
            "club": club,
            "kadencja": KADENCJA_ID,
            "is_suppleant": False,
            # Zagnieżdżone kadencje{} jak w copenhagen — showProfile w template
            # czyta profile.kadencje[kid], bez tego strona profilu się wywala.
            # Faction-mode: brak głosów imiennych per radny, brak metryk.
            "kadencje": {
                KADENCJA_ID: {
                    "club": club,
                    "komisje": [],
                    "has_voting_data": False,
                }
            },
        })
    return {"scraped_at": scraped_at, "profiles": profiles, "total": len(profiles)}


# Opendatasoft (portal opendata.paris.fr), Explore API v2.1.
ODS_BASE = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets"
AGENDA_DATASET = "ordre-du-jour-du-conseil-de-paris-conseil-municipal-et-departemental"
AGENDA_EXPORT_URL = f"{ODS_BASE}/{AGENDA_DATASET}/exports/json"
AGENDA_RECORDS_URL = f"{ODS_BASE}/{AGENDA_DATASET}/records"

# Typy pozycji porządku obrad (pole "type").
TYPE_LABELS = {
    "PJ": "Projet de délibération",
    "PP": "Proposition",
    "V": "Vœu",
    "Q": "Question",
    "DESIGNATION": "Désignation",
}

FR_MONTHS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "decembre": 12,
}


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def parse_seance_date(seance: str | None) -> str | None:
    """'du lundi 15 février 2016 au mardi 16 février 2016' -> '2016-02-15'.

    Bierze PIERWSZĄ datę z zakresu (początek sesji). Toleruje pojedynczą datę.
    Zwraca ISO YYYY-MM-DD albo None, gdy nie da się sparsować.
    """
    if not seance:
        return None
    norm = _strip_accents(seance.lower())
    m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", norm)
    if not m:
        return None
    day, month_name, year = m.group(1), m.group(2), m.group(3)
    month = FR_MONTHS.get(month_name)
    if not month:
        return None
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"


def agenda_records_to_sessions(records: Iterable[dict]) -> list[dict]:
    """Pogrupuj rekordy agendy po sesji (séance) -> lista sesji.

    Każda sesja: {seance, date, item_count, items:[{reference,type,type_label,
    objet,group,elu,rapporteur}]}. Posortowane malejąco po dacie.
    """
    by_seance: dict[str, dict] = {}
    for r in records:
        seance = (r.get("seance") or "").strip()
        if not seance:
            continue
        sess = by_seance.get(seance)
        if sess is None:
            sess = {
                "seance": seance,
                "date": parse_seance_date(seance),
                "items": [],
            }
            by_seance[seance] = sess
        typ = (r.get("type") or "").strip()
        sess["items"].append({
            "reference": (r.get("reference") or "").strip() or None,
            "type": typ or None,
            "type_label": TYPE_LABELS.get(typ, typ or None),
            "objet": (r.get("objet") or "").strip() or None,
            "group": (r.get("entite_depositaire") or "").strip() or None,
            "elu": (r.get("elu_depositaire") or "").strip() or None,
            "rapporteur": (r.get("rapporteur") or "").strip() or None,
        })
    sessions = list(by_seance.values())
    for s in sessions:
        s["item_count"] = len(s["items"])
    sessions.sort(key=lambda s: (s["date"] or "", s["seance"]), reverse=True)
    return sessions


def fetch_agenda_records(limit: int | None = None) -> list[dict]:
    """Pobierz rekordy agendy z żywego API (wymaga `requests`).

    Najpierw próbuje endpointu exports/json (cały dataset jednym strzałem),
    fallback na paginację records (limit/offset, max offset 10000 w ODS).
    """
    import requests  # lazy: import tylko gdy realnie scrapujemy

    headers = {"User-Agent": "radoskop-paris/1.0 (+https://radoskop.eu)"}
    try:
        r = requests.get(AGENDA_EXPORT_URL, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        records = data if isinstance(data, list) else data.get("results", [])
        if limit:
            records = records[:limit]
        return records
    except Exception as e:  # fallback paginacja
        print(f"export/json nie zadziałał ({e}), paginacja records...", file=sys.stderr)

    out: list[dict] = []
    offset = 0
    page = 100
    while True:
        params = {"limit": page, "offset": offset}
        r = requests.get(AGENDA_RECORDS_URL, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            break
        out.extend(results)
        offset += page
        if (limit and len(out) >= limit) or offset >= 10000:
            break
    return out[:limit] if limit else out


def write_agenda(out_dir: Path, limit: int | None = None) -> Path:
    records = fetch_agenda_records(limit=limit)
    sessions = agenda_records_to_sessions(records)
    payload = {
        "kadencja": KADENCJA_ID,
        "source": AGENDA_EXPORT_URL,
        "generated_by": "scrape_paris.py --agenda",
        "note": (
            "Porządek obrad Conseil de Paris z opendata.paris.fr. "
            "NIE zawiera wyników głosowań ani rozbicia na grupy — te są tylko "
            "w procès-verbaux (PDF) dla scrutins publics."
        ),
        "session_count": len(sessions),
        "item_count": sum(s["item_count"] for s in sessions),
        "sessions": sessions,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "agenda.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_file


# ── Procès-verbal / compte rendu sommaire (wyniki głosowań) ──────────────────
# Compte rendu sommaire (PDF na cdn.paris.fr, ~8 dni po sesji) podaje dla każdej
# pozycji WYNIK: "Le projet de délibération DSP 72 est adopté à main levée.",
# "Le vœu n° 17, est retiré.", "... est adopté à l'unanimité ...". To realne,
# parsowalne wyniki głosowań — większość à main levée (bez liczb), część przez
# scrutin public (z tableau par groupe w PV intégral).

# Wynik pozycji. "repoussé" = odrzucony (częsty dla voeux z avis défavorable),
# "rejeté au scrutin public" = odrzucony w głosowaniu imiennym.
_OUTCOME = r"(adopt[ée]|rejet[ée]|repouss[ée]|retir[ée]|ajourn[ée])"
# Linia wyniku: "Le projet de délibération DSP 72 est adopté à main levée."
PV_RESULT_PJ_RE = re.compile(
    r"Le projet de d[ée]lib[ée]ration\s+([A-Z]{1,6}\s*\d+)\b.*?\b"
    r"est\s+" + _OUTCOME + r"\b([^.]*)",
    re.IGNORECASE,
)
# Linia wyniku dla vœu/amendement: "Le vœu n° 17, est retiré."
# "vœu" to v+œ+u (ligatura), wariant bez ligatury to "voeu" — łapiemy oba.
PV_RESULT_VOEU_RE = re.compile(
    r"L(?:e|['’])\s*(v(?:œu|oeu)|amendement)\s+n°?\s*(\d+\s*(?:bis|ter)?)\b.*?\b"
    r"est\s+" + _OUTCOME + r"\b([^.]*)",
    re.IGNORECASE,
)

# Scrutin public: nagłówek + blok wyników (zbiorcze liczby, bez rozbicia na
# osoby — to jest w skanowanych aneksach). Pour/Contre/Abstentions/NPPV.
SCRUTIN_HEADER_RE = re.compile(
    r"Scrutin public sur\s+(?:l['’]|le\s+|la\s+)?"
    r"(v(?:œu|oeu)|amendement|projet de d[ée]lib[ée]ration)\s+n°?\s*(\d+\s*(?:bis|ter)?)",
    re.IGNORECASE,
)
# Nagłówek projektu délibération: "2025 DSP 72 Subvention ...".
PV_HEADER_PJ_RE = re.compile(r"^(20\d\d)\s+([A-Z]{1,6})\s+(\d+)\s+(.+)$")
# Nagłówek vœu: "Vœu n° 16 déposé par le groupe Paris en commun relatif à ...".
PV_HEADER_VOEU_RE = re.compile(
    r"^(?:V(?:œu|oeu)|Amendement)\s+n°?\s*(\d+\s*(?:bis|ter)?)\b\s*(.*)$",
    re.IGNORECASE,
)
PV_DEPOSANT_RE = re.compile(r"d[ée]pos[ée](?:\s+par)?\s+(?:le groupe\s+)?(.+?)(?:\s+relatif|\s+relative|\.|$)", re.IGNORECASE)

# Nazwa grupy w PV/aneksie -> kod klubu z config["clubs"] (substring po stripie
# akcentów). Pokrywa truncację z OCR ("Ecologiste et Social de" bez "Paris").
# Grupy Conseil de Paris kadencji od 2026 (z aneksów scrutin public) + starsze.
GROUP_NAME_TO_CODE = [
    ("socialiste et divers", "SOCIALISTE_DG"),   # 1 - Socialiste et Divers Gauche
    ("ecologiste et social", "ECOLOGISTE"),       # 2 - Écologiste et Social de Paris
    ("communiste", "COMMUNISTE"),                 # 3 - Communiste ... de Paris
    ("nouveau paris", "NOUVEAU_PARIS"),           # 4 - Nouveau Paris Populaire
    ("paris liberte", "PARIS_LIBERTE"),           # 5 - Paris Liberté !
    ("paris apaise", "PARIS_APAISE"),             # 6 - Paris apaisé
    ("paris au centre", "PARIS_CENTRE"),          # 7 - Paris au centre
    ("non-inscrit", "NZ"),
    ("non inscrit", "NZ"),
    # starsze nazwy (mandature 2020-2026) — fallback dla deposantów vœu:
    ("paris en commun", "PARIS_EN_COMMUN"),
    ("changer paris", "CHANGER_PARIS"),
    ("republicains", "CHANGER_PARIS"),
    ("ecologiste", "ECOLOGISTE"),
    ("modem", "MODEM"),
    ("democrates", "MODEM"),
]


def _outcome_norm(raw: str) -> tuple[str, bool | None]:
    """'adopté'/'rejeté'/'retiré'/'ajourné' -> (etykieta, passed|None)."""
    r = _strip_accents(raw.lower())
    if r.startswith("adopt"):
        return "adopté", True
    if r.startswith("rejet") or r.startswith("repouss"):
        return "rejeté", False
    if r.startswith("retir"):
        return "retiré", None
    if r.startswith("ajourn"):
        return "ajourné", None
    return raw, None


def _find_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_scrutins(text: str) -> dict:
    """Wyciągnij zbiorcze wyniki scrutins publics → {(kind, num): counts}.

    Compte rendu sommaire podaje przy każdym scrutin public liczby: Pour,
    Contre, Abstentions, NPPV (ne prend pas part au vote), Nombre d'inscrits /
    votants. Rozbicie na osoby jest tylko w skanowanych aneksach, więc bierzemy
    wartości zbiorcze. nieobecni = inscrits - votants.
    kind: 'voeu' | 'amendement' | 'projet_deliberation'.
    """
    out: dict = {}
    heads = list(SCRUTIN_HEADER_RE.finditer(text))
    for i, h in enumerate(heads):
        kw = _strip_accents(h.group(1).lower())
        kind = "voeu" if kw.startswith("v") else ("amendement" if kw.startswith("a") else "projet_deliberation")
        num = re.sub(r"\s+", " ", h.group(2)).strip()
        start = h.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else min(len(text), start + 1500)
        win = text[start:end]
        pour = _find_int(r"Pour\s*:\s*(\d+)", win)
        contre = _find_int(r"Contre\s*:\s*(\d+)", win)
        if pour is None or contre is None:
            continue
        abst = _find_int(r"Abstentions?\s*:\s*(\d+)", win) or 0
        nppv = _find_int(r"NPPV\s*:\s*(\d+)", win) or 0
        inscrits = _find_int(r"inscrits\s*:\s*(\d+)", win)
        votants = _find_int(r"votants\s*:\s*(\d+)", win)
        nieob = (inscrits - votants) if (inscrits and votants and inscrits >= votants) else 0
        out[(kind, num)] = {
            "za": pour, "przeciw": contre, "wstrzymal_sie": abst,
            "brak_glosu": nppv, "nieobecni": nieob,
        }
    return out


def _group_code(name: str | None) -> str | None:
    if not name:
        return None
    low = _strip_accents(name.lower())
    for needle, code in GROUP_NAME_TO_CODE:
        if _strip_accents(needle) in low:
            return code
    return None


# ── OCR aneksów scrutin public (imienny rozkład głosów) ──────────────────────
# Aneksy compte rendu sommaire to SKANOWANE tabele per radny: ID siège, Nom,
# Groupe ("N - Nazwa"), Choix de vote (Pour[++]/Contre[+]/Abst[-]/NPPV[--]).
# OCR (tesseract) odczytuje ~96% wierszy. Liczby ZBIORCZE bierzemy z tekstu
# (parse_scrutins, dokładne); OCR daje przybliżony rozkład PER GRUPA + nazwiska,
# oznaczony jako OCR i dołączany tylko gdy zgadza się z agregatem (walidacja).
ANNEX_HEADER_RE = re.compile(
    r"Annexe\s+n°?\s*\d+\s*-\s*Scrutin public relatif\s+(?:au\s+|à\s+l['’]|à\s+la\s+|aux?\s+)?"
    r"(v(?:œu|oeu)|amendement|projet de d[ée]lib[ée]ration)\s+n°?\s*(\d+\s*(?:bis|ter)?)",
    re.IGNORECASE,
)
ANNEX_ROW_RE = re.compile(
    r"^\s*\d{1,3}\s+(.+?)\s+(\d+)\s*[-—]\s*(.+?)\s+"
    r"(Pour|Contre|Abst\w*|NPPV|Non[\s-]?votant|Ne\s+prend)\b.*$",
    re.IGNORECASE,
)


def _annex_choice(s: str) -> str:
    s = s.lower()
    if s.startswith("pour"):
        return "za"
    if s.startswith("contre"):
        return "przeciw"
    if s.startswith("abst"):
        return "wstrzymal_sie"
    return "brak_glosu"  # NPPV / Non votant / Ne prend pas part


def parse_annex_text(text: str) -> dict:
    """OCR-tekst jednego aneksu -> {faction_votes, named_votes, counts}.

    faction_votes kluczowane czytelną etykietą grupy "N - Nazwa" (numer grupy
    jest stabilny mimo zawijania nazwy). counts = sumy per kategoria z OCR
    (przybliżone — walidowane potem względem agregatu z tekstu).
    """
    empty = lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    by_num: dict[str, dict] = {}
    gname: dict[str, str] = {}
    named: dict[str, list] = {k: [] for k in empty()}
    counts = empty()
    for ln in text.splitlines():
        m = ANNEX_ROW_RE.match(ln)
        if not m:
            continue
        name = re.sub(r"\s*\[A\]", "", m.group(1)).strip().rstrip("_").strip()
        gnum = m.group(2)
        cat = _annex_choice(m.group(4))
        counts[cat] += 1
        by_num.setdefault(gnum, empty())[cat] += 1
        lbl = re.sub(r"\s+", " ", m.group(3)).strip()
        if len(lbl) > len(gname.get(gnum, "")):
            gname[gnum] = lbl
        named[cat].append(name)
    # Kluczuj faction_votes kanonicznym kodem klubu (stabilny mimo truncacji
    # OCR nazwy). Nieznane grupy → "GR{numer}" zamiast gubić głosy.
    faction_votes: dict = {}
    for gnum, c in by_num.items():
        code = _group_code(gname.get(gnum, "")) or f"GR{gnum}"
        tgt = faction_votes.setdefault(code, {k: 0 for k in counts})
        for k, v in c.items():
            tgt[k] += v
    return {"faction_votes": faction_votes, "named_votes": named, "counts": counts}


def parse_compte_rendu_sommaire(text: str) -> list[dict]:
    """Parsuj compte rendu sommaire -> lista wyników głosowań.

    Każdy wynik: {reference, kind, topic, deposited_by, group_code, result,
    unanimite, modalite, passed}. Wynik bierzemy z linii "est adopté/rejeté/
    retiré ...", a temat/deponenta z najbliższego nagłówka o tej referencji.
    """
    lines = [ln.strip() for ln in text.splitlines()]

    # Pass 1: zbierz nagłówki (objet + ewentualny deponent) po referencji.
    pj_headers: dict[str, str] = {}
    voeu_headers: dict[str, dict] = {}
    cur_pj: tuple[str, list[str]] | None = None
    cur_voeu: tuple[str, list[str]] | None = None

    def _flush():
        nonlocal cur_pj, cur_voeu
        if cur_pj:
            ref, parts = cur_pj
            pj_headers.setdefault(ref, " ".join(p for p in parts if p).strip())
            cur_pj = None
        if cur_voeu:
            num, parts = cur_voeu
            blob = " ".join(p for p in parts if p).strip()
            dep = PV_DEPOSANT_RE.search(blob)
            voeu_headers.setdefault(num, {
                "topic": blob,
                "deposited_by": dep.group(1).strip().strip('"') if dep else None,
            })
            cur_voeu = None

    for ln in lines:
        mh = PV_HEADER_PJ_RE.match(ln)
        mv = PV_HEADER_VOEU_RE.match(ln)
        if mh:
            _flush()
            ref = f"{mh.group(2)} {mh.group(3)}"
            cur_pj = (ref, [mh.group(4)])
        elif mv:
            _flush()
            num = re.sub(r"\s+", " ", mv.group(1)).strip()
            cur_voeu = (num, [mv.group(2) or ""])
        elif cur_pj is not None and not ln.startswith("Le projet"):
            cur_pj[1].append(ln)
        elif cur_voeu is not None and not re.match(r"^Le v(?:œu|oeu)", ln, re.IGNORECASE):
            cur_voeu[1].append(ln)

    _flush()

    # Pass 2: linie wyników -> rekordy.
    results: list[dict] = []
    for ln in lines:
        mp = PV_RESULT_PJ_RE.search(ln)
        mvr = PV_RESULT_VOEU_RE.search(ln)
        if mp:
            ref = re.sub(r"\s+", " ", mp.group(1)).strip()
            outcome, passed = _outcome_norm(mp.group(2))
            tail = _strip_accents(mp.group(3).lower())
            results.append({
                "reference": ref,
                "kind": "projet_deliberation",
                "topic": pj_headers.get(ref),
                "deposited_by": None,
                "group_code": None,
                "result": outcome,
                "passed": passed,
                "unanimite": "unanimite" in tail,
                "modalite": "main levée" if "main lev" in tail else ("scrutin" if "scrutin" in tail else None),
            })
        elif mvr:
            num = re.sub(r"\s+", " ", mvr.group(2)).strip()
            outcome, passed = _outcome_norm(mvr.group(3))
            tail = _strip_accents(mvr.group(4).lower())
            hdr = voeu_headers.get(num, {})
            dep = hdr.get("deposited_by")
            results.append({
                "reference": f"n° {num}",
                "kind": "voeu" if _strip_accents(mvr.group(1).lower()).startswith("v") else "amendement",
                "topic": hdr.get("topic"),
                "deposited_by": dep,
                "group_code": _group_code(dep),
                "result": outcome,
                "passed": passed,
                "unanimite": "unanimite" in tail,
                "modalite": "main levée" if "main lev" in tail else ("scrutin" if "scrutin" in tail else None),
            })

    # Dołącz zbiorcze liczby ze scrutins publics do pasujących pozycji.
    scrutins = parse_scrutins(text)
    for r in results:
        if r["kind"] not in ("voeu", "amendement"):
            continue
        num = r["reference"].replace("n°", "").strip()
        counts = scrutins.get((r["kind"], num))
        if counts:
            r["counts"] = counts
            r["scrutin"] = True
            r["modalite"] = "scrutin"
            # Wynik z liczb (spójny z linią "adopté/rejeté au scrutin public").
            r["passed"] = counts["za"] > counts["przeciw"]
    return results


def extract_session_date(text: str) -> str | None:
    """'Séance des 7, 8, 9 et 10 octobre 2025' -> '2025-10-07' (pierwszy dzień)."""
    norm = _strip_accents(text.lower())
    months = "|".join(FR_MONTHS.keys())
    m = re.search(
        r"seance[^\n]*?(\d{1,2})(?:[\s,]+(?:et\s+)?\d{1,2})*\s+(" + months + r")\s+(\d{4})",
        norm,
    )
    if not m:
        m = re.search(r"(\d{1,2})\s+(" + months + r")\s+(\d{4})", norm)
    if not m:
        return None
    day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
    return f"{year:04d}-{FR_MONTHS[month_name]:02d}-{day:02d}"


def _ref_slug(reference: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", reference.lower()).strip("_")


def build_votes_from_pv_results(
    results: list[dict], session_date: str | None
) -> list[dict]:
    """Wyniki z compte rendu sommaire -> rekordy vote w schemacie Radoskop.

    Głosowania à main levée nie mają liczb — zapisujemy je jako vote_mode
    'show_of_hands' z wynikiem (result/passed/modalite) i, dla voeux, grupą
    wnioskującą. Front pokazuje wynik i modalité zamiast pustych list i mylących
    zer. Scrutins publics z tableau par groupe budujemy osobno przez
    build_faction_vote_from_tableau (vote_mode 'faction').
    """
    sd = session_date or "0000-00-00"
    empty = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    votes: list[dict] = []
    for r in results:
        ref = r.get("reference") or "?"
        is_scrutin = bool(r.get("scrutin"))
        has_faction = bool(r.get("faction_votes"))
        counts = r.get("counts") or dict(empty)
        named = r.get("named_votes") or {k: [] for k in empty}
        # Hierarchia trybu: faction (scrutin + OCR rozkładu na grupy) > scrutin
        # (zbiorcze liczby z tekstu) > show_of_hands (à main levée, bez liczb).
        if has_faction:
            mode = "faction"
        elif is_scrutin:
            mode = "scrutin"
        else:
            mode = "show_of_hands"
        votes.append({
            "id": f"paris_{sd}_{_ref_slug(ref)}",
            "session_date": session_date,
            "session_number": None,
            "topic": r.get("topic") or ref,
            "reference": ref,
            "item_kind": r.get("kind"),
            "deposited_by": r.get("deposited_by"),
            "deposited_by_code": r.get("group_code"),
            "result": r.get("result"),
            "modalite": r.get("modalite"),
            "unanimite": bool(r.get("unanimite")),
            "passed": r.get("passed"),
            "vote_mode": mode,
            "counts": {k: counts.get(k, 0) for k in empty},
            "named_votes": {k: list(named.get(k, [])) for k in empty},
            "faction_votes": r.get("faction_votes") or {},
            "faction_votes_source": "ocr" if r.get("faction_ocr") else None,
        })
    return votes


# Strona z listą comptes rendus (linki do PDF sommaire na cdn.paris.fr).
COMPTES_RENDUS_URL = "https://www.paris.fr/pages/comptes-rendus-et-debats-et-deliberations-du-conseil-224"
# Link do compte rendu sommaire: cdn.paris.fr/.../<...sommaire...>.pdf
PV_LINK_RE = re.compile(
    r"https://cdn\.paris\.fr/[^\s\"'<>]*?sommaire[^\s\"'<>]*?\.pdf",
    re.IGNORECASE,
)


def discover_pv_urls(html: str) -> list[str]:
    """Wyciągnij z HTML strony comptes rendus linki do PDF sommaire.

    Tylko pliki z 'sommaire' w nazwie (compte rendu sommaire), bo to one
    zawierają wynik per pozycja. Kolejność jak na stronie (najnowsze u góry),
    bez duplikatów.
    """
    seen: list[str] = []
    for m in PV_LINK_RE.finditer(html):
        url = m.group(0)
        if url not in seen:
            seen.append(url)
    return seen


def fetch_text(url: str) -> str:
    import requests
    r = requests.get(url, headers={"User-Agent": "radoskop-paris/1.0"}, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


_STABLE_DAYS = 7  # sesja starsza niż N dni — PDF nie zmieni się już


def _load_url_map(out_dir: Path) -> dict:
    """Wczytaj sidecar {url -> session_date} z poprzednich runów."""
    p = out_dir / "_paris_url_map.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_url_map(out_dir: Path, url_map: dict) -> None:
    p = out_dir / "_paris_url_map.json"
    p.write_text(json.dumps(url_map, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_stable_session(session_date: str | None) -> bool:
    if not session_date:
        return False
    try:
        from datetime import date, timedelta
        d = date.fromisoformat(session_date)
        return (date.today() - d).days > _STABLE_DAYS
    except Exception:
        return False


def scrape(out_dir: Path, limit_sessions: int | None = None, cache_dir: Path | None = None) -> Path:
    """Pełny scrape: odkryj PV sommaire, sparsuj każdy, zbuduj kadencja-{id}.json.

    Agreguje wyniki ze wszystkich sesji w jeden plik kadencji. To jest tryb
    uruchamiany przez scheduled pipeline (--scrape). cache_dir cache'uje OCR.
    Stabilne sesje (starsze niż _STABLE_DAYS dni) są pomijane jeśli już
    przetworzone i ich wyniki są w istniejącym kadencja-*.json.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"kadencja-{KADENCJA_ID}.json"

    # Wczytaj istniejące wyniki — zbuduj słownik session_date -> [votes]
    existing_by_date: dict[str, list[dict]] = {}
    if out_file.exists():
        try:
            existing = json.loads(out_file.read_text(encoding="utf-8"))
            for v in existing.get("votes", []):
                sd = v.get("session_date") or ""
                existing_by_date.setdefault(sd, []).append(v)
        except Exception:
            pass

    # Sidecar mapujący url -> session_date z poprzednich runów
    url_map = _load_url_map(out_dir)

    html = fetch_text(COMPTES_RENDUS_URL)
    urls = discover_pv_urls(html)
    if limit_sessions:
        urls = urls[:limit_sessions]

    all_votes: list[dict] = []
    sessions_done = 0
    url_map_dirty = False

    for url in urls:
        # Sprawdź czy sesja jest już przetworzona i stabilna
        cached_date = url_map.get(url)
        if cached_date and _is_stable_session(cached_date) and cached_date in existing_by_date:
            cached_votes = existing_by_date[cached_date]
            all_votes.extend(cached_votes)
            sessions_done += 1
            print(f"  [CACHE] {url}: {len(cached_votes)} pozycji (sesja {cached_date})", file=sys.stderr)
            continue

        pdf_path = None
        try:
            pdf_path = _download_pdf(url)
            results, sd = process_pv_file(pdf_path, cache_dir=cache_dir)
            votes = build_votes_from_pv_results(results, sd)
            all_votes.extend(votes)
            sessions_done += 1
            n_scrutin = sum(1 for v in votes if v["vote_mode"] in ("scrutin", "faction"))
            n_ocr = sum(1 for v in votes if v.get("faction_votes_source") == "ocr")
            print(f"  {url}: {len(votes)} pozycji, {n_scrutin} scrutins, "
                  f"{n_ocr} z rozkładem OCR (sesja {sd})", file=sys.stderr)
            if sd and sd not in url_map:
                url_map[url] = sd
                url_map_dirty = True
        except Exception as e:
            print(f"  POMINIĘTO {url}: {e}", file=sys.stderr)
        finally:
            if pdf_path is not None:
                try:
                    pdf_path.unlink()
                except Exception:
                    pass

    if url_map_dirty:
        _save_url_map(out_dir, url_map)

    payload = {
        "kadencja": KADENCJA_ID,
        "source": COMPTES_RENDUS_URL,
        "generated_by": "scrape_paris.py --scrape",
        "vote_mode": "show_of_hands",
        "total_sessions": sessions_done,
        "votes": all_votes,
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    _write_manifest(out_dir, all_votes, sessions_done)
    return out_file


def _config_kadencja_label() -> str:
    try:
        cfg = load_config()
        return cfg.get("kadencje", {}).get(KADENCJA_ID, {}).get("label", KADENCJA_ID)
    except Exception:
        return KADENCJA_ID


def _write_manifest(out_dir: Path, votes: list[dict], sessions_done: int) -> None:
    """Zapisz docs/data.json (manifest dla API /data) i profiles.json z radnymi."""
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    profiles_data = _build_profiles(now)
    councillors = [
        {"name": p["name"], "slug": p["slug"], "club": p["club"]}
        for p in profiles_data["profiles"]
    ]
    data_payload = {
        "scraped_at": now,
        "generated": True,
        "default_kadencja": KADENCJA_ID,
        "vote_mode": "show_of_hands",
        "kadencje": [{
            "id": KADENCJA_ID,
            "label": _config_kadencja_label(),
            "total_votes": len(votes),
            "total_sessions": sessions_done,
            "total_councilors": len(councillors),
            "councilors": councillors,
        }],
    }
    if not votes:
        data_payload["_status"] = "no_data"
    (out_dir / "data.json").write_text(
        json.dumps(data_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "profiles.json").write_text(
        json.dumps(profiles_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch_pv_text(url: str) -> str:
    """Pobierz PDF compte rendu / PV i wyciągnij tekst (requests + pdfplumber)."""
    import io
    import requests
    import pdfplumber

    r = requests.get(url, headers={"User-Agent": "radoskop-paris/1.0"}, timeout=90)
    r.raise_for_status()
    parts = []
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _download_pdf(url: str) -> Path:
    import requests
    import tempfile
    r = requests.get(url, headers={"User-Agent": "radoskop-paris/1.0"}, timeout=120)
    r.raise_for_status()
    f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    f.write(r.content)
    f.close()
    return Path(f.name)


def _pdf_text(pdf_path: Path) -> str:
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _tess_lang() -> str:
    import subprocess
    try:
        langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
        return "fra" if re.search(r"\bfra\b", langs) else "eng"
    except Exception:
        return "eng"


def _pdf_md5(pdf_path: Path) -> str:
    import hashlib
    h = hashlib.md5()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def ocr_pdf_pages(
    pdf_path: Path, first: int, last: int, dpi: int = 300,
    cache_dir: Path | None = None, doc_id: str | None = None,
) -> str:
    """OCR stron [first..last] PDF (pdftoppm @dpi + tesseract). Pusty string gdy
    brak narzędzi.

    Cache: gdy podany cache_dir + doc_id (md5 PDF), wynik OCR jest zapisywany do
    ocr_{doc_id}_{first}-{last}_{lang}.txt i odczytywany przy kolejnych runach.
    OCR aneksów jest drogie (300dpi + tesseract), a PDF-y na cdn.paris.fr są
    niezmienne (hash w nazwie), więc cache eliminuje ponowne OCR co godzinę.
    """
    import glob
    import subprocess
    import tempfile
    lang = _tess_lang()
    cache_file = None
    if cache_dir is not None and doc_id:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"ocr_{doc_id}_{first}-{last}_{lang}.txt"
        if cache_file.exists():
            try:
                return cache_file.read_text(encoding="utf-8")
            except Exception:
                pass
    with tempfile.TemporaryDirectory() as td:
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(dpi), "-f", str(first), "-l", str(last),
                 str(pdf_path), f"{td}/pg"],
                check=True, capture_output=True,
            )
        except Exception as e:
            print(f"    pdftoppm padł: {e}", file=sys.stderr)
            return ""
        parts = []
        for img in sorted(glob.glob(f"{td}/pg-*.png")):
            try:
                parts.append(subprocess.run(
                    ["tesseract", img, "stdout", "-l", lang, "--psm", "6"],
                    capture_output=True, text=True,
                ).stdout)
            except Exception:
                pass
    text = "\n".join(parts)
    if cache_file is not None and text.strip():
        try:
            cache_file.write_text(text, encoding="utf-8")
        except Exception:
            pass
    return text


def annex_ranges(pdf_path: Path) -> list[tuple[str, str, int, int]]:
    """Znajdź zakresy stron aneksów (skany scrutins publics) po nagłówku tekstowym
    'Annexe n° X - Scrutin public relatif au ... n° Y'. Zwraca (kind, num, first, last)."""
    import pdfplumber
    heads: list[tuple[str, str, int]] = []
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            m = ANNEX_HEADER_RE.search(page.extract_text() or "")
            if m:
                kw = _strip_accents(m.group(1).lower())
                kind = "voeu" if kw.startswith("v") else ("amendement" if kw.startswith("a") else "projet_deliberation")
                num = re.sub(r"\s+", " ", m.group(2)).strip()
                heads.append((kind, num, i))
    out = []
    for idx, (kind, num, pg) in enumerate(heads):
        last = (heads[idx + 1][2] - 1) if idx + 1 < len(heads) else n
        out.append((kind, num, pg, last))
    return out


def parse_scrutin_annexes(pdf_path: Path, cache_dir: Path | None = None) -> dict:
    """OCR każdego aneksu -> {(kind, num): parse_annex_text(...)}. Z cache OCR."""
    doc_id = _pdf_md5(pdf_path) if cache_dir is not None else None
    out: dict = {}
    for kind, num, first, last in annex_ranges(pdf_path):
        text = ocr_pdf_pages(pdf_path, first, last, cache_dir=cache_dir, doc_id=doc_id)
        if text.strip():
            out[(kind, num)] = parse_annex_text(text)
    return out


def enrich_with_annexes(results: list[dict], pdf_path: Path, cache_dir: Path | None = None) -> list[dict]:
    """Dołącz imienny/grupowy rozkład z OCR aneksów do pozycji scrutin public.

    Liczby ZBIORCZE (counts) zostają z tekstu (dokładne). OCR dokłada
    faction_votes (per grupa) + named_votes (per osoba) TYLKO gdy suma głosów z
    OCR pokrywa >=85% agregatu (walidacja — OCR bywa niekompletny). Flaga
    faction_ocr=True oznacza dane z OCR (przybliżone).
    """
    cats = ("za", "przeciw", "wstrzymal_sie", "brak_glosu")
    try:
        annexes = parse_scrutin_annexes(pdf_path, cache_dir=cache_dir)
    except Exception as e:
        print(f"  OCR aneksów pominięty: {e}", file=sys.stderr)
        return results
    for r in results:
        if not r.get("scrutin") or r["kind"] not in ("voeu", "amendement"):
            continue
        num = r["reference"].replace("n°", "").strip()
        a = annexes.get((r["kind"], num))
        if not a:
            continue
        agg = r.get("counts") or {}
        agg_total = sum(agg.get(c, 0) for c in cats)
        ocr_total = sum(a["counts"].get(c, 0) for c in cats)
        if agg_total > 0 and ocr_total >= 0.85 * agg_total:
            r["faction_votes"] = a["faction_votes"]
            r["named_votes"] = a["named_votes"]
            r["faction_ocr"] = True
    return results


def process_pv_file(pdf_path: Path, cache_dir: Path | None = None) -> tuple[list[dict], str | None]:
    """Pełne przetworzenie jednego PDF compte rendu: tekst + OCR aneksów."""
    text = _pdf_text(pdf_path)
    results = parse_compte_rendu_sommaire(text)
    session_date = extract_session_date(text)
    enrich_with_annexes(results, pdf_path, cache_dir=cache_dir)
    return results, session_date


def write_pv_results(url: str, out_dir: Path, cache_dir: Path | None = None) -> tuple[Path, Path]:
    """Pobierz PV, zapisz surowe wyniki ORAZ kadencja-{id}.json dla pipeline'u.

    Zwraca (pv_results_path, kadencja_path).
    """
    pdf_path = _download_pdf(url)
    try:
        results, session_date = process_pv_file(pdf_path, cache_dir=cache_dir)
    finally:
        try:
            pdf_path.unlink()
        except Exception:
            pass
    out_dir.mkdir(parents=True, exist_ok=True)

    results_payload = {
        "kadencja": KADENCJA_ID,
        "source": url,
        "session_date": session_date,
        "generated_by": "scrape_paris.py --pv",
        "note": (
            "Wyniki z compte rendu sommaire Conseil de Paris. 'adopté/rejeté/"
            "retiré' + modalité. Bez liczb dla głosowań à main levée; rozbicie "
            "na grupy tylko dla scrutins publics (tableau w PV intégral)."
        ),
        "result_count": len(results),
        "results": results,
    }
    results_file = out_dir / "pv-results.json"
    with open(results_file, "w", encoding="utf-8") as f:
        json.dump(results_payload, f, ensure_ascii=False, indent=2)

    votes = build_votes_from_pv_results(results, session_date)
    kadencja_payload = {
        "kadencja": KADENCJA_ID,
        "source": url,
        "generated_by": "scrape_paris.py --pv",
        "vote_mode": "show_of_hands",
        "votes": votes,
    }
    kadencja_file = out_dir / f"kadencja-{KADENCJA_ID}.json"
    with open(kadencja_file, "w", encoding="utf-8") as f:
        json.dump(kadencja_payload, f, ensure_ascii=False, indent=2)
    return results_file, kadencja_file


def build_faction_vote_from_tableau(
    vote_id: str,
    session_date: str,
    topic: str,
    tableau: dict[str, dict[str, int]],
    **kwargs: Any,
) -> dict:
    """Adapter: tableau des votes par groupe -> rekord vote frakcyjny.

    `tableau`: {kod_grupy: {"za":int,"przeciw":int,"wstrzymal_sie":int, ...}}.
    Kody grup muszą pasować do config["clubs"] (PARIS_EN_COMMUN, CHANGER_PARIS,
    ...). Używać, gdy mamy realne liczniki z PV — wynik wkłada się do
    kadencja-2020-2026.json. To jedyne źródło danych frakcyjnych dla Paryża.
    """
    return make_faction_vote(
        vote_id=vote_id,
        session_date=session_date,
        topic=topic,
        faction_tallies=tableau,
        **kwargs,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Scraper Conseil de Paris")
    ap.add_argument(
        "--agenda", action="store_true",
        help="pobierz agendę z opendata.paris.fr i zapisz agenda.json",
    )
    ap.add_argument(
        "--pv", metavar="URL",
        help="pobierz jeden compte rendu sommaire (PDF) i wyciągnij wyniki",
    )
    ap.add_argument(
        "--scrape", action="store_true",
        help="pełny scrape: odkryj wszystkie PV sommaire i zbuduj kadencja json",
    )
    ap.add_argument(
        "--pv-file", metavar="PATH",
        help="przetwórz lokalny PDF compte rendu (tekst + OCR aneksów), debug",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="ogranicz liczbę rekordów (debug)",
    )
    ap.add_argument(
        "--out", default=str(CITY_DIR / "docs"),
        help="katalog wyjściowy (domyślnie cities/paris/docs/, gitignored)",
    )
    ap.add_argument(
        "--cache-dir", default=os.environ.get("RADOSKOP_OCR_CACHE", str(CITY_DIR / ".cache" / "ocr")),
        help="katalog cache OCR aneksów (domyślnie cities/paris/.cache/ocr, gitignored)",
    )
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    if args.agenda:
        out = write_agenda(Path(args.out), limit=args.limit)
        print(f"Zapisano agendę: {out}")
        return 0

    if args.scrape:
        out = scrape(Path(args.out), limit_sessions=args.limit, cache_dir=cache_dir)
        print(f"Zapisano kadencja (wszystkie sesje): {out}")
        return 0

    if args.pv:
        results_file, kadencja_file = write_pv_results(args.pv, Path(args.out), cache_dir=cache_dir)
        print(f"Zapisano wyniki głosowań: {results_file}")
        print(f"Zapisano kadencja (dla pipeline'u): {kadencja_file}")
        return 0

    if args.pv_file:
        results, sd = process_pv_file(Path(args.pv_file), cache_dir=cache_dir)
        votes = build_votes_from_pv_results(results, sd)
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / f"kadencja-{KADENCJA_ID}.json", "w", encoding="utf-8") as f:
            json.dump({"kadencja": KADENCJA_ID, "source": args.pv_file,
                       "generated_by": "scrape_paris.py --pv-file",
                       "session_date": sd, "votes": votes}, f, ensure_ascii=False, indent=2)
        n_scrutin = sum(1 for v in votes if v["vote_mode"] in ("scrutin", "faction"))
        n_ocr = sum(1 for v in votes if v.get("faction_votes_source") == "ocr")
        print(f"Sesja {sd}: {len(votes)} pozycji, {n_scrutin} scrutins, {n_ocr} z rozkładem OCR")
        return 0

    print(
        "Wyniki głosowań Conseil de Paris NIE są w open data (zweryfikowane).\n"
        "  --agenda      pobiera realny porządek obrad (sesje + projekty).\n"
        "Głosowania frakcyjne wymagają parsowania PV (PDF, scrutins publics)\n"
        "i wstawienia liczników przez build_faction_vote_from_tableau(...).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
