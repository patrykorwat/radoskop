"""
Radoskop — Metrics builder.
Takes parsed session JSONs and produces per-councilor metrics + dashboard data.
Splits output by kadencja (2018-2023, 2024-2029).

Usage:
    python build_metrics.py data/ --out dashboard/data.json
"""

import json
import sys
import os
from collections import defaultdict


# ── Kadencja definitions ──────────────────────────────────────────────

KADENCJE = [
    {
        "id": "2018-2023",
        "label": "Kadencja 2018–2023",
        "start": "2018-01-01",
        "end": "2024-05-07",  # IX kadencja inauguracja: 7 maja 2024
    },
    {
        "id": "2024-2029",
        "label": "Kadencja 2024–2029",
        "start": "2024-05-07",
        "end": "2030-01-01",
    },
]

CLUBS_BY_KADENCJA = {
    "2018-2023": {
        # KO (wg Wikipedii VIII kadencji — 15 radnych na koniec kadencji)
        "Łukasz Bejm": "KO", "Kamila Błaszczyk": "KO",
        "Anna Golędzinowska": "KO", "Michał Hajduk": "KO",
        "Beata Jankowiak": "KO", "Krystian Kłos": "KO",
        "Andrzej Kowalczys": "KO", "Emilia Lodzińska": "KO",
        "Agnieszka Owczarczak": "KO", "Jan Perucki": "KO",
        "Przemysław Ryś": "KO", "Mateusz Skarbek": "KO",
        "Cezary Śpiewak-Dowbór": "KO", "Lech Wałęsa": "KO",
        "Karol Ważny": "KO",
        # PiS (wg Wikipedii — 12 radnych na koniec kadencji)
        "Piotr Gierszewski": "PiS", "Henryk Hałas": "PiS",
        "Barbara Imianowska": "PiS", "Waldemar Jaroszewicz": "PiS",
        "Kazimierz Koralewski": "PiS", "Alicja Krasula": "PiS",
        "Przemysław Majewski": "PiS", "Przemysław Malak": "PiS",
        "Romuald Plewa": "PiS", "Karol Rabenda": "PiS",
        "Andrzej Skiba": "PiS", "Elżbieta Strzelczyk": "PiS",
        "Joanna Cabaj": "PiS",  # zrzekła się mandatu w trakcie kadencji
        # WdG (wg Wikipedii — 7 radnych na koniec kadencji)
        "Wojciech Błaszkowski": "WdG", "Katarzyna Czerniewska": "WdG",
        "Beata Dunajewska": "WdG", "Piotr Dzik": "WdG",
        "Bogdan Oleszek": "WdG", "Andrzej Stelmasiewicz": "WdG",
        "Teresa Wasilewska": "WdG",
    },
    "2024-2029": {
        # KO (wg Wikipedii: Kluby Radnych IX kadencji)
        "Agnieszka Bartków": "KO", "Łukasz Bejm": "KO", "Kamila Błaszczyk": "KO",
        "Sylwia Cisoń": "KO", "Żaneta Geryk": "KO",
        "Anna Golędzinowska": "KO", "Michał Hajduk": "KO",
        "Beata Jankowiak": "KO", "Krystian Kłos": "KO",
        "Andrzej Kowalczys": "KO", "Marcin Mickun": "KO",
        "Agnieszka Owczarczak": "KO", "Jan Perucki": "KO",
        "Mateusz Skarbek": "KO", "Cezary Śpiewak-Dowbór": "KO",
        "Karol Ważny": "KO",
        # WdG
        "Jolanta Banach": "WdG", "Wojciech Błaszkowski": "WdG",
        "Katarzyna Czerniewska": "WdG", "Piotr Dzik": "WdG",
        "Maximilian Kieturakis": "WdG", "Marta Magott": "WdG",
        "Marcin Makowski": "WdG", "Bogdan Oleszek": "WdG",
        "Sylwia Rydlewska-Kowalik": "WdG", "Łukasz Świacki": "WdG",
        # PiS (wg Wikipedii: 8 radnych)
        "Piotr Gierszewski": "PiS", "Barbara Imianowska": "PiS",
        "Aleksander Jankowski": "PiS", "Kazimierz Koralewski": "PiS",
        "Przemysław Majewski": "PiS", "Karol Rabenda": "PiS",
        "Tomasz Rakowski": "PiS", "Andrzej Skiba": "PiS",
        # Byli radni (wczesne sesje)
        "Teresa Wasilewska": "WdG", "Emilia Lodzińska": "KO",
    },
}


# ── Name normalization ────────────────────────────────────────────────

NAME_ALIASES = {
    "Cezary Śpiewak- Dowbór": "Cezary Śpiewak-Dowbór",
    "Sylwia Rydlewska- Kowalik": "Sylwia Rydlewska-Kowalik",
    "AndrzejSkiba": "Andrzej Skiba",
    "ElżbietaStrzelczyk": "Elżbieta Strzelczyk",
}

# Fix known session number parsing errors: (date, wrong_number) -> correct_number
SESSION_NUMBER_FIXES = {
    "2023-09-28": "LXVIII",  # parsed as XLVIII, should be LXVIII
}


def normalize_name(name):
    return NAME_ALIASES.get(name, name)


def is_valid_councilor_name(name):
    import re
    if len(name) > 35 or len(name) < 5:
        return False
    if re.search(r'\d', name):
        return False
    if any(c in name for c in '();.,/'):
        return False
    words = name.split()
    if len(words) < 2 or len(words) > 4:
        return False
    if not name[0].isupper():
        return False
    return True


# ── Data loading ──────────────────────────────────────────────────────

ARABIC_TO_ROMAN = {
    1:'I',2:'II',3:'III',4:'IV',5:'V',6:'VI',7:'VII',8:'VIII',9:'IX',10:'X',
    11:'XI',12:'XII',13:'XIII',14:'XIV',15:'XV',16:'XVI',17:'XVII',18:'XVIII',
    19:'XIX',20:'XX',21:'XXI',22:'XXII',23:'XXIII',24:'XXIV',25:'XXV',
    26:'XXVI',27:'XXVII',28:'XXVIII',29:'XXIX',30:'XXX',
    31:'XXXI',32:'XXXII',33:'XXXIII',34:'XXXIV',35:'XXXV',
    36:'XXXVI',37:'XXXVII',38:'XXXVIII',39:'XXXIX',40:'XL',
    41:'XLI',42:'XLII',43:'XLIII',44:'XLIV',45:'XLV',
    46:'XLVI',47:'XLVII',48:'XLVIII',49:'XLIX',50:'L',
    51:'LI',52:'LII',53:'LIII',54:'LIV',55:'LV',56:'LVI',57:'LVII',
    58:'LVIII',59:'LIX',60:'LX',61:'LXI',62:'LXII',63:'LXIII',
    64:'LXIV',65:'LXV',66:'LXVI',67:'LXVII',68:'LXVIII',69:'LXIX',
    70:'LXX',71:'LXXI',72:'LXXII',73:'LXXIII',74:'LXXIV',75:'LXXV',
    76:'LXXVI',77:'LXXVII',78:'LXXVIII',79:'LXXIX',80:'LXXX',
}


def load_sessions(data_dir):
    combined = os.path.join(data_dir, "all_sessions.json")
    if os.path.exists(combined):
        with open(combined, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    else:
        raw = []
        for fn in sorted(os.listdir(data_dir)):
            if fn.startswith("session_") and fn.endswith(".json"):
                with open(os.path.join(data_dir, fn), 'r', encoding='utf-8') as f:
                    raw.append(json.load(f))

    # Fix missing number_roman and known errors
    for s in raw:
        d = s.get("date", "")
        if d in SESSION_NUMBER_FIXES:
            s["number_roman"] = SESSION_NUMBER_FIXES[d]
        elif not s.get("number_roman") and s.get("number"):
            try:
                n = int(s["number"])
                s["number_roman"] = ARABIC_TO_ROMAN.get(n, str(n))
            except (ValueError, TypeError):
                pass

    # Deduplicate by date (keep the one with more votes)
    by_date = {}
    for s in raw:
        d = s.get("date", "")
        if d not in by_date or s.get("vote_count", 0) >= by_date[d].get("vote_count", 0):
            by_date[d] = s
    sessions = sorted(by_date.values(), key=lambda s: s.get("date") or "")
    if len(sessions) < len(raw):
        print(f"  Deduplicated: {len(raw)} → {len(sessions)} sessions")
    return sessions


def split_sessions_by_kadencja(sessions):
    """Split sessions into kadencje by date."""
    result = {k["id"]: [] for k in KADENCJE}
    for s in sessions:
        d = s.get("date", "")
        if not d:
            continue
        for k in KADENCJE:
            if k["start"] <= d < k["end"]:
                result[k["id"]].append(s)
                break
    return result


# ── Metrics computation ───────────────────────────────────────────────

def build_councilor_metrics(sessions, clubs):
    """Build per-councilor metrics for a single kadencja."""

    # Collect known councilors (from clubs + actually appearing in data)
    all_names = set(clubs.keys())
    name_vote_count = defaultdict(int)
    for s in sessions:
        for a in s.get("attendees", []):
            n = normalize_name(a)
            if is_valid_councilor_name(n):
                all_names.add(n)
        for v in s.get("votes", []):
            nv = v.get("named_votes", {})
            for cat in nv.values():
                for name in cat:
                    n = normalize_name(name)
                    if is_valid_councilor_name(n):
                        all_names.add(n)
                        name_vote_count[n] += 1

    # Only keep names in the club list or appearing in ≥10 votes
    all_names = {n for n in all_names if n in clubs or name_vote_count[n] >= 10}

    stats = {}
    for name in sorted(all_names):
        stats[name] = {
            "name": name,
            "club": clubs.get(name, "?"),
            "sessions_present": 0, "sessions_total": 0,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
            "votes_brak": 0, "votes_nieobecny": 0, "votes_total": 0,
            "votes_with_club_majority": 0, "votes_against_club_majority": 0,
            "rebellions": [],
        }

    for session in sessions:
        session_date = session.get("date", "?")
        attendees = {normalize_name(a) for a in session.get("attendees", [])}

        for name in all_names:
            stats[name]["sessions_total"] += 1
            if name in attendees:
                stats[name]["sessions_present"] += 1

        for vote in session.get("votes", []):
            nv = vote.get("named_votes", {})

            councilor_vote = {}
            for cat_key, names in nv.items():
                for raw_n in names:
                    n = normalize_name(raw_n)
                    if n in all_names:
                        councilor_vote[n] = cat_key

            # Club majority
            club_votes = defaultdict(lambda: defaultdict(int))
            for n, v_type in councilor_vote.items():
                club = clubs.get(n, "?")
                if v_type in ("za", "przeciw", "wstrzymal_sie"):
                    club_votes[club][v_type] += 1

            club_majorities = {}
            for club, vc in club_votes.items():
                if vc:
                    club_majorities[club] = max(vc, key=vc.get)

            for name in all_names:
                v_type = councilor_vote.get(name)
                if v_type is None:
                    continue

                stats[name]["votes_total"] += 1
                if v_type == "za":
                    stats[name]["votes_za"] += 1
                elif v_type == "przeciw":
                    stats[name]["votes_przeciw"] += 1
                elif v_type == "wstrzymal_sie":
                    stats[name]["votes_wstrzymal"] += 1
                elif v_type == "brak_glosu":
                    stats[name]["votes_brak"] += 1
                elif v_type == "nieobecni":
                    stats[name]["votes_nieobecny"] += 1

                club = clubs.get(name, "?")
                if v_type in ("za", "przeciw", "wstrzymal_sie") and club in club_majorities:
                    if v_type == club_majorities[club]:
                        stats[name]["votes_with_club_majority"] += 1
                    else:
                        stats[name]["votes_against_club_majority"] += 1
                        stats[name]["rebellions"].append({
                            "session": session_date,
                            "topic": vote.get("topic", "?")[:100],
                            "their_vote": v_type,
                            "club_majority": club_majorities[club],
                        })

    # Derived metrics
    for name, s in stats.items():
        active = s["votes_za"] + s["votes_przeciw"] + s["votes_wstrzymal"]
        s["frekwencja"] = round(s["sessions_present"] / max(s["sessions_total"], 1) * 100, 1)
        s["aktywnosc"] = round(active / max(s["votes_total"], 1) * 100, 1)
        alignment_total = s["votes_with_club_majority"] + s["votes_against_club_majority"]
        s["zgodnosc_z_klubem"] = round(
            s["votes_with_club_majority"] / max(alignment_total, 1) * 100, 1
        )
        s["rebellion_count"] = len(s["rebellions"])

    return stats


def build_similarity_matrix(sessions, clubs):
    """Build councilor-councilor voting similarity matrix for a kadencja."""
    all_names = set()
    vote_records = []

    for session in sessions:
        for vote in session.get("votes", []):
            nv = vote.get("named_votes", {})
            record = {}
            for cat_key, names in nv.items():
                for raw_n in names:
                    n = normalize_name(raw_n)
                    if is_valid_councilor_name(n) and n in clubs:
                        record[n] = cat_key
                        all_names.add(n)
            vote_records.append(record)

    names = sorted(all_names)
    matrix = {}
    for a in names:
        matrix[a] = {}
        for b in names:
            if a == b:
                matrix[a][b] = 100.0
                continue
            agree = total = 0
            for record in vote_records:
                va, vb = record.get(a), record.get(b)
                if va in ("za", "przeciw", "wstrzymal_sie") and vb in ("za", "przeciw", "wstrzymal_sie"):
                    total += 1
                    if va == vb:
                        agree += 1
            matrix[a][b] = round(agree / total * 100, 1) if total >= 20 else -1

    return matrix


# ── Dashboard output ──────────────────────────────────────────────────

def _extract_pairs(matrix, n, reverse):
    pairs = []
    seen = set()
    for a in matrix:
        for b in matrix[a]:
            if a >= b:
                continue
            score = matrix[a][b]
            if score < 0:
                continue
            key = (a, b)
            if key not in seen:
                seen.add(key)
                pairs.append({"a": a, "b": b, "score": score})
    pairs.sort(key=lambda x: x["score"], reverse=reverse)
    return pairs[:n]


def build_kadencja_data(kadencja_def, sessions, clubs):
    """Build complete dashboard data for one kadencja."""

    stats = build_councilor_metrics(sessions, clubs)
    similarity = build_similarity_matrix(sessions, clubs)

    # Load speaking activity data for per-session breakdown
    activity_file = os.path.join(os.path.dirname(sessions[0].get("_data_dir", "")) or "data", "protokoly", "activity.json")
    # Try standard path
    for try_path in [activity_file, "data/protokoly/activity.json",
                     os.path.join(os.path.dirname(out_file) if 'out_file' in dir() else '', '..', 'data', 'protokoly', 'activity.json')]:
        if os.path.exists(try_path):
            with open(try_path, 'r', encoding='utf-8') as f:
                _activity_raw = json.load(f)
            break
    else:
        _activity_raw = {}

    # Build per-session speaking stats: {session_date: [{name, statements, words}, ...]}
    _session_speakers = defaultdict(list)
    for person_name, pdata in _activity_raw.items():
        for sess in pdata.get("sessions", []):
            if sess.get("statements", 0) > 0:
                _session_speakers[sess["date"]].append({
                    "name": person_name,
                    "statements": sess["statements"],
                    "words": sess["words"],
                })
    # Sort speakers by word count descending
    for d in _session_speakers:
        _session_speakers[d].sort(key=lambda x: x["words"], reverse=True)

    session_list = [{
        "date": s["date"],
        "number": s.get("number_roman", "?"),
        "vote_count": s["vote_count"],
        "attendee_count": s["attendee_count"],
        "attendees": [normalize_name(a) for a in s.get("attendees", [])],
        "source_url": s.get("source_url"),
        "speakers": _session_speakers.get(s["date"], [])[:30],
    } for s in sessions]

    councilors = []
    for name in sorted(stats.keys()):
        s = stats[name]
        # Skip councilors with 0 votes in this kadencja
        if s["votes_total"] == 0:
            continue
        councilors.append({
            "name": s["name"], "club": s["club"],
            "frekwencja": s["frekwencja"], "aktywnosc": s["aktywnosc"],
            "zgodnosc_z_klubem": s["zgodnosc_z_klubem"],
            "votes_za": s["votes_za"], "votes_przeciw": s["votes_przeciw"],
            "votes_wstrzymal": s["votes_wstrzymal"], "votes_brak": s["votes_brak"],
            "votes_nieobecny": s["votes_nieobecny"], "votes_total": s["votes_total"],
            "rebellion_count": s["rebellion_count"],
            "rebellions": s["rebellions"][:10],
        })

    all_votes = []
    for session in sessions:
        for vi, vote in enumerate(session.get("votes", [])):
            # Normalize named_votes names
            raw_nv = vote.get("named_votes", {})
            nv = {}
            for cat, names in raw_nv.items():
                nv[cat] = [normalize_name(n) for n in names]
            all_votes.append({
                "id": f"{session['date']}_{vi}",
                "session_date": session["date"],
                "session_number": session.get("number_roman", "?"),
                "source_url": session.get("source_url"),
                "topic": vote.get("topic", "")[:150],
                "druk": vote.get("druk"),
                "resolution": vote.get("resolution"),
                "counts": vote.get("counts", {}),
                "named_votes": nv,
            })

    return {
        "id": kadencja_def["id"],
        "label": kadencja_def["label"],
        "sessions": session_list,
        "total_sessions": len(sessions),
        "total_votes": sum(s["vote_count"] for s in sessions),
        "total_councilors": len(councilors),
        "councilors": councilors,
        "votes": all_votes,
        "similarity_top": _extract_pairs(similarity, 20, reverse=True),
        "similarity_bottom": _extract_pairs(similarity, 20, reverse=False),
    }


# ── Main ──────────────────────────────────────────────────────────────

def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    out_file = "dashboard/data.json"
    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            out_file = sys.argv[idx + 1]

    print(f"Loading sessions from {data_dir}...")
    sessions = load_sessions(data_dir)
    print(f"Loaded {len(sessions)} sessions")

    by_kadencja = split_sessions_by_kadencja(sessions)

    kadencje = []
    for kdef in KADENCJE:
        kid = kdef["id"]
        ksessions = by_kadencja.get(kid, [])
        if not ksessions:
            print(f"\n{kdef['label']}: no sessions, skipping")
            continue

        clubs = CLUBS_BY_KADENCJA[kid]
        print(f"\n{kdef['label']}: {len(ksessions)} sessions")
        kdata = build_kadencja_data(kdef, ksessions, clubs)
        kadencje.append(kdata)

        print(f"  Głosowań: {kdata['total_votes']}")
        print(f"  Radnych: {kdata['total_councilors']}")

        ranked = sorted(kdata["councilors"], key=lambda x: x["frekwencja"])
        print(f"  Najniższa frekwencja: {ranked[0]['name']} ({ranked[0]['club']}) — {ranked[0]['frekwencja']}%")
        print(f"  Najwyższa frekwencja: {ranked[-1]['name']} ({ranked[-1]['club']}) — {ranked[-1]['frekwencja']}%")

        rebels = sorted(kdata["councilors"], key=lambda x: x["rebellion_count"], reverse=True)
        if rebels[0]["rebellion_count"] > 0:
            print(f"  Największy buntownik: {rebels[0]['name']} ({rebels[0]['club']}) — {rebels[0]['rebellion_count']}x")

    dashboard = {
        "generated": True,
        "kadencje": kadencje,
        "default_kadencja": kadencje[-1]["id"] if kadencje else None,
    }

    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(dashboard, f, ensure_ascii=False, indent=2)

    print(f"\nSaved to: {out_file}")


if __name__ == "__main__":
    main()
