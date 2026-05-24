#!/usr/bin/env python3
"""
Test parsera bloku "Szavazás eredménye" na rzeczywistej próbce z sesji
Fővárosi Közgyűlés 2026-02-25 (głosowanie Száma 26.02.25/0/0/A/KT).

Próbka pochodzi z końcowego załącznika jegyzőkönyv (imienna tabela
Név/Voks/Frakció). Sprawdza: liczbę radnych, rozkład głosów zgodny z
agregatem (Igen 16, Nem 0, Tartózkodik 12, Távol 5), poprawne mapowanie
głosu na kategorię oraz przypisanie frakcji (w tym przypadki sklejone
bez spacji typu 'IgenDEMOKRATIKUS KOALICIÓ').

Uruchom: python3 test_parse_budapest.py
"""

from scrape_budapest import parse_jegyzokonyv_text

SAMPLE = """Szavazás eredménye
#: 70 Száma: 26.02.25/0/0/A/KT
Ideje: 2026 február 25 09:21
Típusa: Nyílt
Határozat; Elfogadva
Egyszerű
Tárgya: 27.-28. 2. utáni tárgyalása
Eredménye Voks: Szav% Össz%
Igen 16 57.14 48.49
Nem 0 0.00 0.00
Tartózkodik 12 42.86 36.36
Szavazott 28 84.85
Nem szavazott 0 0.00 0.00
Távol 5 15.15
Összesen 33 100.00
Megjegyzés:
Név Voks Frakció
Balogh Balázs Igen TISZA PÁRT
Barabás Richárd Igen PÁRBESZÉD-ZÖLDEK
Barna Judit Annamária Igen TISZA PÁRT
Béres András Igen PÁRBESZÉD-ZÖLDEK
Bujdosó Andrea Igen TISZA PÁRT
Déri Tibor IgenDEMOKRATIKUS KOALICIÓ
Gál József IgenPODMANICZKY MOZGALOM
Gerzsenyi Gabriella Igen TISZA PÁRT
Karácsony Gergely Igen FŐPOLGÁRMESTER
dr. Kollár Kinga Igen TISZA PÁRT
Molnár Dániel Igen TISZA PÁRT
Orbán Árpád István Igen TISZA PÁRT
Porcher Áron Igen TISZA PÁRT
Szilágyi Anna Margit IgenPODMANICZKY MOZGALOM
Tüttő Kata Igen FÜGGETLEN
Vitézy Dávid IgenPODMANICZKY MOZGALOM
Böjthe Péter Tart. FIDESZ-KDNP
Böröcz László Tart. FIDESZ-KDNP
Gulyás Gergely Kristóf Tart. FIDESZ-KDNP
Havasi Zoltán Tart. FIDESZ-KDNP
Janó-Veilandics Franciska Tart. FIDESZ-KDNP
Keszthelyi Dorottya Tart.DEMOKRATIKUS KOALICIÓ
dr. Lehoczki Ádám Tart. FIDESZ-KDNP
Radics Béla Tart. FIDESZ-KDNP
Szaniszló Sándor Tart.DEMOKRATIKUS KOALICIÓ
Szécsényi Dániel Tart. FIDESZ-KDNP
Szentkirályi Alexandra Tart. FIDESZ-KDNP
Szepesfalvy Anna Tart. FIDESZ-KDNP
Baranyi Krisztina TávolMAGYAR KÉTFARKÚ KUTYA PÁRT
Bovier György Távol TISZA PÁRT
Döme Zsuzsanna TávolMAGYAR KÉTFARKÚ KUTYA PÁRT
Gémes Szilvia Távol TISZA PÁRT
Kovács Gergely TávolMAGYAR KÉTFARKÚ KUTYA PÁRT
70 Száma: 2026.02.25/0/0/A/KT
"""


def run() -> int:
    blocks = parse_jegyzokonyv_text(SAMPLE)
    assert len(blocks) == 1, f"oczekiwano 1 bloku, jest {len(blocks)}"
    b = blocks[0]

    # Metadane bloku.
    assert b["szama"] == "26.02.25/0/0/A/KT", b["szama"]
    assert b["voted_at"] == "2026-02-25T09:21:00", b["voted_at"]
    assert b["session_date"] == "2026-02-25", b["session_date"]
    assert b["tipus"] == "Nyílt", b["tipus"]
    assert b["result_native"] == "Elfogadva", b["result_native"]
    assert "utáni" in b["topic"], b["topic"]

    members = b["members"]
    assert len(members) == 33, f"oczekiwano 33 radnych, jest {len(members)}"

    cats = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
    by_name = {}
    for name, cat, frak in members:
        cats[cat] += 1
        by_name[name] = (cat, frak)

    assert cats["za"] == 16, cats
    assert cats["przeciw"] == 0, cats
    assert cats["wstrzymal_sie"] == 12, cats
    assert cats["brak_glosu"] == 0, cats
    assert cats["nieobecni"] == 5, cats

    # Frakcje, w tym przypadki sklejone bez spacji.
    assert by_name["Karácsony Gergely"] == ("za", "FŐPOLGÁRMESTER"), by_name["Karácsony Gergely"]
    assert by_name["Balogh Balázs"] == ("za", "TISZA PÁRT"), by_name["Balogh Balázs"]
    assert by_name["Déri Tibor"] == ("za", "DEMOKRATIKUS KOALICIÓ"), by_name["Déri Tibor"]
    assert by_name["Gál József"] == ("za", "PODMANICZKY MOZGALOM"), by_name["Gál József"]
    assert by_name["Böjthe Péter"] == ("wstrzymal_sie", "FIDESZ-KDNP"), by_name["Böjthe Péter"]
    assert by_name["Gulyás Gergely Kristóf"] == ("wstrzymal_sie", "FIDESZ-KDNP"), by_name["Gulyás Gergely Kristóf"]
    assert by_name["Keszthelyi Dorottya"] == ("wstrzymal_sie", "DEMOKRATIKUS KOALICIÓ"), by_name["Keszthelyi Dorottya"]
    assert by_name["Baranyi Krisztina"] == ("nieobecni", "MAGYAR KÉTFARKÚ KUTYA PÁRT"), by_name["Baranyi Krisztina"]
    assert by_name["dr. Kollár Kinga"] == ("za", "TISZA PÁRT"), by_name["dr. Kollár Kinga"]

    print("OK: 33 radnych, 16/0/12/0/5, frakcje (w tym sklejone) poprawne")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
