#!/usr/bin/env python3
"""
Radoskop scraper: Brzeg (bip.brzeg.pl, platforma SISCO eDcms, wydruki DSSS Vote).

Zrodlo: BIP > Rada Miejska Brzegu > Wykazy imienne z glosowan > kadencja 2024-2029
        (kategoria 5_1478) — po jednym PDF "Wykazy imienne z glosowan sesja DD-MM-YYYY"
        na sesje, generowanych z systemu DSSS Vote (4-cwierciowe listy imienne).

Czysty HTTP (bez headless): lista zalacznikow przez POST
scripts_ajax/serwis_datatable/ajx_dataTableEngine.php (srv_inc=1, bipzalaczniki).

Output: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import json, os, re, ssl, sys, time, urllib.parse, urllib.request
from collections import OrderedDict
from pathlib import Path

try:
    import pymupdf  # nowe API
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore

BASE = "https://bip.brzeg.pl"
CAT_ID = 1478  # Rada Miejska > Wykazy imienne z glosowan > kadencja 2024-2029
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
KAD_ID = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-07"
ENGINE = BASE + "/scripts_ajax/serwis_datatable/ajx_dataTableEngine.php"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def _get(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout, context=_ctx).read()


def list_attachments():
    """Lista PDF-ow sesji przez DataTables AJAX (czysty HTTP, srv_inc=1)."""
    _get(BASE + f"/dokumenty,5_{CAT_ID}")  # sesja cookie
    cols = ["Nazwa", "Opis", "Typ", "Rozmiar", "Data", "dataUdost", "dataMod", "creating", "modifier", "Opcje"]
    form = {}
    for i, c in enumerate(["Lp"] + cols):
        form[f"columns[{i}][data]"] = c
        form[f"columns[{i}][name]"] = ""
        form[f"columns[{i}][searchable]"] = "true"
        form[f"columns[{i}][orderable]"] = "false" if c == "Opcje" else "true"
        form[f"columns[{i}][search][value]"] = ""
        form[f"columns[{i}][search][regex]"] = "false"
    form.update({
        "order[0][column]": "0", "order[0][dir]": "asc", "start": "0", "length": "200",
        "search[value]": "", "search[regex]": "false",
        "uniqueID": f"fileList-{CAT_ID}",
        "options[typeOfUse]": "websiteFileList",
        "options[pageData][pageId]": str(CAT_ID),
        "options[sqlData][uid]": str(CAT_ID),
        "options[sqlData][name]": "bipzalaczniki",
        "options[extraData][lngStr]": "pl",
        "options[extraData][listTyp]": "bipAttachments",
        "options[opeRights][id]": "1",
        "options[opeRights][type]": "superadmin",
        "srv_inc": "1",
    })
    hdrs = {
        "User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": BASE + f"/dokumenty,5_{CAT_ID}",
    }
    req = urllib.request.Request(ENGINE, data=urllib.parse.urlencode(form).encode(), headers=hdrs)
    raw = _post_read(req)
    j = json.loads(raw.decode("utf-8-sig", "ignore"))
    out = []
    for row in j.get("data", []):
        m = re.search(r'href=\\"(/uploaded_files/[^\\]+)\\"[^>]*title=\\"([^\\"]+)', row["Nazwa"].replace('"', '\\"'))
        if not m:
            m2 = re.search(r'href="(/uploaded_files/[^"]+)"[^>]*title="([^"]+)', row["Nazwa"])
            if not m2:
                continue
            href, title = m2.group(1), m2.group(2)
        else:
            href, title = m.group(1), m.group(2)
        out.append({"url": BASE + href, "title": title})
    return out


def _post_read(req):
    return urllib.request.urlopen(req, timeout=45, context=_ctx).read()


MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12}


def norm_name(s):
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace("Ł", "ł") if s[:1].isdigit() else s
    return s


def roman_to_int(r):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total, prev = 0, 0
    for ch in reversed(r):
        v = vals.get(ch, 0)
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total


def parse_session_pdf(path):
    """Zwroc (session_meta, votes, names_seen). session_meta: {date, number, obecni, nieobecni}."""
    d = pymupdf.open(path)
    text0 = d[0].get_text()
    m = re.search(r"Na sesji\s+[“\"]([A-Z]+)\s+sesja[^“\"]*?(\d{1,2})[-.](\d{1,2})[-.](\d{4})", text0)
    if not m:
        d.close()
        return None, [], set()
    number_roman = m.group(1)
    date = f"{m.group(4)}-{int(m.group(3)):02d}-{int(m.group(2)):02d}"

    def name_list(txt, header):
        i = txt.find(header)
        if i == -1:
            return []
        seg = txt[i + len(header):]
        names = []
        for ln in seg.splitlines():
            ln = ln.strip()
            mm = re.match(r"^\d+\.\s*([A-ZŚŹŻÓĆŁĘĄŃ][\wŚśŹżŻóÓĆćŁłĘęĄąŃń\-]{2,})\s+([A-ZŚŹŻÓĆŁĘĄŃ][\wŚśŹżŻóÓĆćŁłĘęĄąŃń\-]{2,})", ln)
            if mm:
                names.append(norm_name(mm.group(1) + " " + mm.group(2)))
            elif names and (not ln or ln.startswith("Nieobecni") or ln.startswith("Obecni") or "głosowan" in ln.lower()):
                break
        return names

    obecni = name_list(text0, "Obecni radni:")
    nieobecni = name_list(text0, "Nieobecni radni:")

    votes = []
    names_seen = set(obecni) | set(nieobecni)
    current = None
    for pno in range(len(d)):
        pg = d[pno]
        txt = pg.get_text()
        new = parse_vote_head(txt)
        if new:
            current = new
            votes.append(current)
        if current is None:
            continue
        if "zagłosowali jak poniżej" not in txt and "zagłosowali" not in txt:
            continue
        za, pr, wt, brak = parse_quadrants(pg)
        names_seen |= set(za) | set(pr) | set(wt) | set(brak)
        for n in za:
            current["named_votes"]["za"].append(n)
        for n in pr:
            current["named_votes"]["przeciw"].append(n)
        for n in wt:
            current["named_votes"]["wstrzymal_sie"].append(n)
        for n in brak:
            current["named_votes"]["brak_glosu"].append(n)
    d.close()

    # walidacja agregatow
    ok_votes = []
    for v in votes:
        nv = v["named_votes"]
        for k in nv:
            nv[k] = sorted(set(nv[k]))
        c = v["counts"]
        if (len(nv["za"]) == c["za"] and len(nv["przeciw"]) == c["przeciw"]
                and len(nv["wstrzymal_sie"]) == c["wstrzymal_sie"]):
            ok_votes.append(v)
        else:
            print(f"  [warn] nierozreconcylowane glosowanie {v['topic'][:50]}: "
                  f"liczby {c} vs lists {len(nv['za'])}/{len(nv['przeciw'])}/{len(nv['wstrzymal_sie'])}",
                  file=sys.stderr)
    meta = {"date": date, "number": roman_to_int(number_roman), "number_roman": number_roman,
            "obecni": obecni, "nieobecni": nieobecni}
    return meta, ok_votes, names_seen


def parse_vote_head(txt):
    m = re.search(r"(Przeprowadzono głosowanie w sprawie|Uchwała\s*\n?numer)\s*[“\"](.+?)[”\"\.](.{0,220}?)proporcją\s*\ngłosów:",
                  txt, re.S)
    if not m:
        m = re.search(r"(Przeprowadzono głosowanie w sprawie|Uchwała\s*\n?numer)\s*[“\"](.+?)[”\"\.](.{0,400}?)proporcją\s*głosów:",
                      txt, re.S)
    if not m:
        return None
    kind, topic, between = m.group(1), m.group(2), m.group(3)
    topic = re.sub(r"\s+", " ", topic).strip().rstrip("”.")
    c = re.search(r"jestem\s+za\s+(\d+),\s*jestem\s+przeciw\s+(\d+),\s*wstrzymuję\s+się\s+(\d+)", txt)
    if not c:
        return None
    res_m = re.search(r"(został|została|zostały)\s+(\w+)", between)
    resolution = res_m.group(2) if res_m else ("podjeta" if "podj" in between else None)
    return {
        "topic": topic,
        "druk": topic if topic.startswith("Druk") else None,
        "resolution": resolution,
        "counts": {"za": int(c.group(1)), "przeciw": int(c.group(2)), "wstrzymal_sie": int(c.group(3))},
        "named_votes": {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []},
    }


NAME_RE = re.compile(r"^\d+\.$")

# stopka DSSS Vote na dole kazdej strony — slowa ktore NIGDY nie sa czescia imienia/nazwiska
FOOTER_WORDS = {"DSSS", "Vote", "App.", "Operatorem", "Wygenerowano", "pośrednictwem",
                "oprogramowania", "systemu"}
FOOTER_HEADS = {"Operatorem", "Wygenerowano", "DSSS"}


def parse_quadrants(pg):
    """4 cwiercie list imiennych DSSS: ZA (gora-lewo), PRZECIW (gora-prawo),
    WSTRZYMUJE (dol-lewo), Obecni-nie-glosowali (dol-prawo)."""
    words = pg.get_text("words")
    y_za = y_wstrz = y_foot = None
    for w in words:
        if w[4] == "Jestem" and w[0] < 250 and y_za is None:
            y_za = w[1]
        if w[4] in ("Wstrzymuję", "Wstrzymuje") and w[0] < 250:
            y_wstrz = w[1]
        if w[4] in FOOTER_HEADS and y_foot is None and y_za and w[1] > y_za + 40:
            y_foot = w[1]
    if y_za is None:
        return [], [], [], []
    if y_wstrz is None:
        y_wstrz = 10 ** 6
    if y_foot is None:
        y_foot = 10 ** 6
    za, pr, wt, brak = [], [], [], []

    def flush(bucket, parts):
        if len(parts) >= 2:
            bucket.append(norm_name(" ".join(parts)))

    for quadrant in range(2):
        y0 = y_za if quadrant == 0 else y_wstrz
        y1 = min(y_wstrz, y_foot) if quadrant == 0 else y_foot
        rows = {}
        for w in words:
            x, y, t = w[0], w[1], w[4]
            if not (y0 + 2 < y < y1 - 4):
                continue
            left = x < 250
            if NAME_RE.match(t) and ((left and x < 85) or (not left and 330 < x < 365)):
                rows.setdefault((round(y), left), []).append(("num", t, x))
            elif re.match(r"^[A-ZŚŹŻÓĆŁĘĄŃ]", t) and ((left and 85 <= x < 250) or (not left and x >= 365)):
                rows.setdefault((round(y), left), []).append(("n", t, x))
            elif t == "BRAK":
                rows.setdefault((round(y), left), []).append(("b", t, x))
        for (ry, left), toks in rows.items():
            toks = [t for t in toks if t[0] != "num"]
            has_brak = any(t[0] == "b" for t in toks)
            toks = [t for t in toks if t[0] == "n"]
            # wiersz stopki DSSS Vote ("... systemu DSSS Vote za pośrednictwem ...") — odrzuc
            if any(t[1] in FOOTER_WORDS for t in toks):
                continue
            if left and has_brak:
                toks = [t for t in toks if t[2] < 250]
            if (not left) and has_brak:
                toks = [t for t in toks if t[2] >= 250]
            toks.sort(key=lambda t: t[2])
            parts = [t[1] for t in toks]
            if quadrant == 0:
                flush(za if left else pr, parts)
            else:
                flush(wt if left else brak, parts)
    return za, pr, wt, brak


def slugify(s):
    tr = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")
    s = s.translate(tr)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def main():
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    cache = Path(os.environ.get("RADOSKOP_CACHE_DIR", "/cache/brzeg/html"))
    cache.mkdir(parents=True, exist_ok=True)

    atts = list_attachments()
    print(f"[brzeg] {len(atts)} PDF-ow sesji")
    sessions, all_votes, roster = [], [], OrderedDict()
    for a in atts:
        fn = a["url"].split("/")[-1]
        path = cache / fn
        if not path.exists() or path.stat().st_size < 5000:
            for attempt in range(3):
                try:
                    path.write_bytes(_get(a["url"], timeout=180))
                    break
                except Exception as e:
                    print(f"  dl retry {attempt+1} {fn}: {e}", file=sys.stderr)
                    time.sleep(3 * (attempt + 1))
            else:
                continue
        try:
            meta, votes, names = parse_session_pdf(str(path))
        except Exception as e:
            print(f"  [err] parse {fn}: {e}", file=sys.stderr)
            continue
        if not meta or not votes:
            print(f"  [skip] {a['title'][:60]} — brak glosowan/brak struktury", file=sys.stderr)
            continue
        if meta["date"] < KAD_START:
            continue
        for n in meta["obecni"] + meta["nieobecni"] + [x for v in votes for lst in v["named_votes"].values() for x in lst]:
            roster.setdefault(n, None)
        sessions.append({"date": meta["date"], "number": meta["number_roman"], "vote_count": len(votes),
                         "attendee_count": len(meta["obecni"]), "attendees": meta["obecni"], "speakers": []})
        for v in votes:
            present = set(meta["obecni"])
            voted = set().union(*(set(v["named_votes"][k]) for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu")))
            v["named_votes"]["nieobecni"] = sorted(present - voted) if present else []
            v["session_date"] = meta["date"]
            v["session_number"] = meta["number_roman"]
            v["id"] = str(len(all_votes) + 1)
            v["source_url"] = a["url"]
            all_votes.append(v)
        print(f"  [ok] sesja {meta['number_roman']} {meta['date']}: {len(votes)} glosowan", flush=True)

    sessions.sort(key=lambda s: s["date"])
    councilors = sorted(roster.keys())
    n = len(councilors)

    # statystyki per radny
    stats = {c: dict(votes_za=0, votes_przeciw=0, votes_wstrzymal=0, votes_brak=0, votes_nieobecny=0) for c in councilors}
    for v in all_votes:
        for k, field in (("za", "votes_za"), ("przeciw", "votes_przeciw"),
                         ("wstrzymal_sie", "votes_wstrzymal"), ("brak_glosu", "votes_brak"),
                         ("nieobecni", "votes_nieobecny")):
            for name in v["named_votes"].get(k, []):
                if name in stats:
                    stats[name][field] += 1
    total = len(all_votes)
    councilor_objs = []
    for c in councilors:
        st = stats[c]
        odd = st["votes_nieobecny"]
        frekw = round(100.0 * (total - odd) / total, 1) if total else 0.0
        councilor_objs.append({"name": c, "club": "", "district": None,
                               "frekwencja": frekw, "aktywnosc": None, "zgodnosc_z_klubem": None,
                               "votes_za": st["votes_za"], "votes_przeciw": st["votes_przeciw"],
                               "votes_wstrzymal": st["votes_wstrzymal"], "votes_brak": st["votes_brak"],
                               "votes_nieobecny": st["votes_nieobecny"], "votes_total": total,
                               "rebellion_count": 0, "rebellions": [], "has_activity_data": False})

    kad = {"id": KAD_ID, "label": KAD_LABEL, "clubs": {}, "sessions": sessions,
           "total_sessions": len(sessions), "total_votes": total, "total_councilors": n,
           "councilors": councilor_objs, "votes": all_votes,
           "similarity_top": [], "similarity_bottom": []}
    (docs / "kadencja-2024-2029.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "default_kadencja": KAD_ID,
            "kadencje": [{"id": KAD_ID, "label": KAD_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {"scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "total": n, "profiles": []}
    for co in councilor_objs:
        profiles["profiles"].append({
            "name": co["name"], "slug": slugify(co["name"]), "club": "", "role": "",
            "photo_url": "", "bio": "", "email": "", "social_links": {}, "voting": None,
            "kadencje": {KAD_ID: {"club": "", "has_voting_data": True, "has_activity_data": False,
                                   "frekwencja": co["frekwencja"], "aktywnosc": None,
                                   "zgodnosc_z_klubem": None, "votes_za": co["votes_za"],
                                   "votes_przeciw": co["votes_przeciw"], "votes_wstrzymal": co["votes_wstrzymal"],
                                   "votes_brak": co["votes_brak"], "votes_nieobecny": co["votes_nieobecny"],
                                   "votes_total": co["votes_total"], "rebellion_count": 0,
                                   "rebellions": [], "roles": [], "notes": ""}},
        })
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[brzeg] DONE sesji={len(sessions)} glosowan={total} radnych={n}")
    if total == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
