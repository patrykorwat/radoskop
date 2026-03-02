"""
Radoskop — Councilor profile builder.
Combines Wikipedia biographical data with voting metrics from data.json.

Usage:
    python build_profiles.py --data dashboard/data.json --out dashboard/profiles.json
"""

import json
import sys
import os

# ── Wikipedia data: X kadencja (2024-2029) ─────────────────────────────

COUNCILORS_X = {
    # KO
    "Agnieszka Bartków": {
        "club": "KO", "club_full": "Koalicja Obywatelska",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce, Młyniska",
        "roles": [],
        "komisje": [],
        "notes": "",
    },
    "Łukasz Bejm": {
        "club": "KO", "club_full": "Koalicja Obywatelska",
        "okręg": 6,
        "okręg_dzielnice": "Żabianka-Wejhera-Jelitkowo-Tysiąclecia, Oliwa, Osowa, Matarnia, Kokoszki",
        "roles": ["sekretarz klubu KO"],
        "komisje": ["Komisja Sportu, Rekreacji i Turystyki (przewodniczący)"],
        "notes": "",
    },
    "Kamila Błaszczyk": {
        "club": "KO", "club_full": "Koalicja Obywatelska / Nowoczesna",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": ["wiceprzewodnicząca klubu KO"],
        "komisje": ["Komisja Klimatu, Środowiska i Ekologii (wiceprzewodnicząca)"],
        "notes": "Objęła mandat w miejsce Aleksandry Dulkiewicz.",
        "mid_term": True,
    },
    "Żaneta Geryk": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, VII Dwór",
        "roles": [],
        "komisje": ["Komisja Polityki Społecznej, Zdrowia i Praw Człowieka (przewodnicząca)"],
        "notes": "Objęła mandat w miejsce Piotra Grzelaka.",
        "mid_term": True,
    },
    "Anna Golędzinowska": {
        "club": "KO", "club_full": "bezpartyjna / klub radnych KO",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": [],
        "komisje": ["Komisja Klimatu, Środowiska i Ekologii (przewodnicząca)"],
        "notes": "",
    },
    "Michał Hajduk": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": [],
        "komisje": ["Komisja Mobilności i Transportu (wiceprzewodniczący)"],
        "notes": "Objął mandat w miejsce Piotra Borawskiego.",
        "mid_term": True,
    },
    "Beata Jankowiak": {
        "club": "KO", "club_full": "Koalicja Obywatelska",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": [],
        "komisje": ["Komisja Polityki Gospodarczej i Morskiej (wiceprzewodnicząca)"],
        "notes": "",
    },
    "Krystian Kłos": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, VII Dwór",
        "roles": [],
        "komisje": ["Komisja ds. Skarg, Wniosków i Petycji (wiceprzewodniczący)"],
        "notes": "Objął mandat w miejsce Emilii Lodzińskiej.",
        "mid_term": True,
    },
    "Andrzej Kowalczys": {
        "club": "KO", "club_full": "bezpartyjny / klub radnych KO",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": [],
        "komisje": ["Komisja Edukacji (przewodniczący)"],
        "notes": "",
    },
    "Marcin Mickun": {
        "club": "KO", "club_full": "Koalicja Obywatelska",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": [],
        "komisje": ["Komisja Zagospodarowania Przestrzennego (wiceprzewodniczący)"],
        "notes": "",
    },
    "Agnieszka Owczarczak": {
        "club": "KO", "club_full": "Koalicja Obywatelska",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": ["Przewodnicząca Rady Miasta Gdańska"],
        "komisje": [],
        "notes": "",
    },
    "Jan Perucki": {
        "club": "KO", "club_full": "bezpartyjny / klub radnych KO",
        "okręg": 6,
        "okręg_dzielnice": "Żabianka-Wejhera-Jelitkowo-Tysiąclecia, Oliwa, Osowa, Matarnia, Kokoszki",
        "roles": [],
        "komisje": ["Komisja Strategii, Budżetu i Nadzoru Właścicielskiego (przewodniczący)"],
        "notes": "",
    },
    "Mateusz Skarbek": {
        "club": "KO", "club_full": "Koalicja Obywatelska",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": ["Wiceprzewodniczący Rady Miasta Gdańska"],
        "komisje": [],
        "notes": "",
    },
    "Cezary Śpiewak-Dowbór": {
        "club": "KO", "club_full": "Koalicja Obywatelska",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce, Młyniska",
        "roles": ["przewodniczący klubu KO"],
        "komisje": ["Komisja ds. Skarg, Wniosków i Petycji (przewodniczący)"],
        "notes": "",
    },
    "Karol Ważny": {
        "club": "KO", "club_full": "Koalicja Obywatelska",
        "okręg": 6,
        "okręg_dzielnice": "Żabianka-Wejhera-Jelitkowo-Tysiąclecia, Oliwa, Osowa, Matarnia, Kokoszki",
        "roles": [],
        "komisje": ["Komisja Bezpieczeństwa, Handlu i Współpracy z Mieszkańcami (przewodniczący)"],
        "notes": "",
    },
    "Sylwia Cisoń": {
        "club": "KO", "club_full": "bezpartyjna (zawieszona w klubie KO od 14.09.2025)",
        "okręg": 6,
        "okręg_dzielnice": "Żabianka-Wejhera-Jelitkowo-Tysiąclecia, Oliwa, Osowa, Matarnia, Kokoszki",
        "roles": [],
        "komisje": ["Komisja Kultury (wiceprzewodnicząca)"],
        "notes": "Zawieszona w prawach członka klubu radnych KO od 14.09.2025.",
    },
    # WdG
    "Jolanta Banach": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska / Nowa Lewica",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": [],
        "komisje": ["Komisja Rewizyjna (wiceprzewodnicząca)"],
        "notes": "",
    },
    "Wojciech Błaszkowski": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, VII Dwór",
        "roles": [],
        "komisje": ["Komisja Edukacji (wiceprzewodniczący)"],
        "notes": "",
    },
    "Katarzyna Czerniewska": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce, Młyniska",
        "roles": ["przewodnicząca klubu WdG"],
        "komisje": ["Komisja Zagospodarowania Przestrzennego (przewodnicząca)"],
        "notes": "",
    },
    "Piotr Dzik": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": ["Wiceprzewodniczący Rady Miasta Gdańska"],
        "komisje": ["Komisja Polityki Gospodarczej i Morskiej (przewodniczący)"],
        "notes": "",
    },
    "Maximilian Kieturakis": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": [],
        "komisje": ["Komisja Kultury (przewodniczący)"],
        "notes": "",
    },
    "Marta Magott": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, VII Dwór",
        "roles": ["sekretarz klubu WdG"],
        "komisje": ["Komisja Polityki Społecznej, Zdrowia i Praw Człowieka (wiceprzewodnicząca)"],
        "notes": "",
    },
    "Marcin Makowski": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": ["wiceprzewodniczący klubu WdG"],
        "komisje": ["Komisja Mobilności i Transportu (przewodniczący)"],
        "notes": "",
    },
    "Bogdan Oleszek": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": [],
        "komisje": ["Komisja Bezpieczeństwa, Handlu i Współpracy z Mieszkańcami (wiceprzewodniczący)"],
        "notes": "",
    },
    "Sylwia Rydlewska-Kowalik": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": [],
        "komisje": ["Komisja Strategii, Budżetu i Nadzoru Właścicielskiego (wiceprzewodnicząca)"],
        "notes": "Objęła mandat w miejsce Teresy Wasilewskiej.",
        "mid_term": True,
    },
    "Łukasz Świacki": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": [],
        "komisje": ["Komisja Sportu, Rekreacji i Turystyki (wiceprzewodniczący)"],
        "notes": "",
    },
    # PiS
    "Piotr Gierszewski": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": ["sekretarz klubu PiS"],
        "komisje": [],
        "notes": "",
    },
    "Barbara Imianowska": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": [],
        "komisje": [],
        "notes": "",
    },
    "Aleksander Jankowski": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce, Młyniska",
        "roles": [],
        "komisje": [],
        "notes": "",
    },
    "Kazimierz Koralewski": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": ["wiceprzewodniczący klubu PiS"],
        "komisje": ["Komisja Rewizyjna (przewodniczący)"],
        "notes": "",
    },
    "Przemysław Majewski": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": [],
        "komisje": [],
        "notes": "",
    },
    "Karol Rabenda": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, VII Dwór",
        "roles": ["Wiceprzewodniczący Rady Miasta Gdańska"],
        "komisje": [],
        "notes": "",
    },
    "Tomasz Rakowski": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": ["przewodniczący klubu PiS"],
        "komisje": [],
        "notes": "",
    },
    "Andrzej Skiba": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce, Młyniska",
        "roles": [],
        "komisje": [],
        "notes": "",
    },
    # Byli radni X kadencji
    "Aleksandra Dulkiewicz": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": None, "okręg_dzielnice": "",
        "roles": ["Prezydent Miasta Gdańska"],
        "komisje": [],
        "notes": "Objęła stanowisko prezydenta miasta Gdańska. Zakończyła pełnienie mandatu.",
        "former": True,
    },
    "Piotr Grzelak": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": None, "okręg_dzielnice": "",
        "roles": ["Zastępca Prezydenta Miasta Gdańska"],
        "komisje": [],
        "notes": "Objął stanowisko zastępcy prezydenta miasta Gdańska.",
        "former": True,
    },
    "Piotr Borawski": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": None, "okręg_dzielnice": "",
        "roles": ["Zastępca Prezydenta Miasta Gdańska"],
        "komisje": [],
        "notes": "Objął stanowisko zastępcy prezydenta miasta Gdańska.",
        "former": True,
    },
    "Emilia Lodzińska": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": None, "okręg_dzielnice": "",
        "roles": ["Zastępca Prezydenta Miasta Gdańska"],
        "komisje": [],
        "notes": "Objęła stanowisko zastępcy prezydenta miasta Gdańska.",
        "former": True,
    },
    "Teresa Wasilewska": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": None, "okręg_dzielnice": "",
        "roles": [],
        "komisje": [],
        "notes": "Zmarła 27.05.2025.",
        "former": True,
    },
}

# ── Wikipedia data: VIII kadencja (2018-2024) ──────────────────────────

COUNCILORS_VIII = {
    # KO
    "Łukasz Bejm": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 6,
        "okręg_dzielnice": "Żabianka-Wejhera-Jelitkowo-Tysiąclecia, Oliwa, Osowa, Matarnia, Kokoszki, VII Dwór",
        "roles": ["sekretarz klubu KO"],
    },
    "Kamila Błaszczyk": {
        "club": "KO", "club_full": "Koalicja Obywatelska / Nowoczesna",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": ["wiceprzewodnicząca klubu KO"],
    },
    "Anna Golędzinowska": {
        "club": "KO", "club_full": "KO / bezpartyjna",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": [],
    },
    "Michał Hajduk": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": [],
        "mid_term": True,
        "notes": "Objął mandat w miejsce Piotra Borawskiego.",
    },
    "Beata Jankowiak": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": [],
    },
    "Krystian Kłos": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, Młyniska",
        "roles": [],
    },
    "Andrzej Kowalczys": {
        "club": "KO", "club_full": "KO / bezpartyjny",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": [],
    },
    "Emilia Lodzińska": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, Młyniska",
        "roles": [],
    },
    "Agnieszka Owczarczak": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": ["Przewodnicząca Rady Miasta Gdańska"],
    },
    "Jan Perucki": {
        "club": "KO", "club_full": "KO / bezpartyjny",
        "okręg": 6,
        "okręg_dzielnice": "Żabianka-Wejhera-Jelitkowo-Tysiąclecia, Oliwa, Osowa, Matarnia, Kokoszki, VII Dwór",
        "roles": [],
    },
    "Przemysław Ryś": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": [],
    },
    "Mateusz Skarbek": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": ["Wiceprzewodniczący Rady Miasta Gdańska"],
    },
    "Cezary Śpiewak-Dowbór": {
        "club": "KO", "club_full": "Koalicja Obywatelska",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce",
        "roles": ["przewodniczący klubu KO"],
    },
    "Lech Wałęsa": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce",
        "roles": [],
    },
    "Karol Ważny": {
        "club": "KO", "club_full": "Koalicja Obywatelska / PO",
        "okręg": 6,
        "okręg_dzielnice": "Żabianka-Wejhera-Jelitkowo-Tysiąclecia, Oliwa, Osowa, Matarnia, Kokoszki, VII Dwór",
        "roles": [],
    },
    # PiS
    "Piotr Gierszewski": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": ["Wiceprzewodniczący Rady Miasta Gdańska", "sekretarz klubu PiS"],
    },
    "Henryk Hałas": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 6,
        "okręg_dzielnice": "Żabianka-Wejhera-Jelitkowo-Tysiąclecia, Oliwa, Osowa, Matarnia, Kokoszki, VII Dwór",
        "roles": [],
        "mid_term": True,
        "notes": "Objął mandat w miejsce Dawida Krupeja.",
    },
    "Barbara Imianowska": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": [],
        "mid_term": True,
        "notes": "Objęła mandat w miejsce Jana Kanthaka.",
    },
    "Waldemar Jaroszewicz": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce",
        "roles": [],
    },
    "Kazimierz Koralewski": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": ["przewodniczący klubu PiS"],
    },
    "Alicja Krasula": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": [],
        "mid_term": True,
        "notes": "Objęła mandat w miejsce Joanny Cabaj.",
    },
    "Przemysław Majewski": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": [],
        "mid_term": True,
        "notes": "Objął mandat w miejsce Kacpra Płażyńskiego.",
    },
    "Przemysław Malak": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": [],
    },
    "Romuald Plewa": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, Młyniska",
        "roles": [],
    },
    "Karol Rabenda": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, Młyniska",
        "roles": ["wiceprzewodniczący klubu PiS"],
    },
    "Andrzej Skiba": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce",
        "roles": [],
    },
    "Elżbieta Strzelczyk": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": 5,
        "okręg_dzielnice": "Zaspa-Młyniec, Zaspa-Rozstaje, Przymorze Wielkie, Przymorze Małe",
        "roles": [],
    },
    # WdG
    "Wojciech Błaszkowski": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 4,
        "okręg_dzielnice": "Wrzeszcz Dolny, Wrzeszcz Górny, Strzyża, Brętowo, Młyniska",
        "roles": [],
        "mid_term": True,
        "notes": "Objął mandat w miejsce Piotra Grzelaka.",
    },
    "Katarzyna Czerniewska": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": [],
        "mid_term": True,
        "notes": "Objęła mandat w miejsce Pawła Adamowicza.",
    },
    "Beata Dunajewska": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 1,
        "okręg_dzielnice": "Brzeźno, Nowy Port, Letnica, Przeróbka, Stogi, Krakowiec-Górki Zachodnie, Wyspa Sobieszewska, Rudniki, Olszynka, Orunia-Św. Wojciech-Lipce",
        "roles": ["przewodnicząca klubu WdG"],
    },
    "Piotr Dzik": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 3,
        "okręg_dzielnice": "Aniołki, Siedlce, Wzgórze Mickiewicza, Suchanino, Piecki-Migowo, Jasień",
        "roles": [],
    },
    "Bogdan Oleszek": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": [],
    },
    "Andrzej Stelmasiewicz": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 6,
        "okręg_dzielnice": "Żabianka-Wejhera-Jelitkowo-Tysiąclecia, Oliwa, Osowa, Matarnia, Kokoszki, VII Dwór",
        "roles": [],
    },
    "Teresa Wasilewska": {
        "club": "WdG", "club_full": "Wszystko dla Gdańska",
        "okręg": 2,
        "okręg_dzielnice": "Śródmieście, Chełm, Ujeścisko-Łostowice, Orunia Górna-Gdańsk Południe",
        "roles": ["Wiceprzewodnicząca Rady Miasta Gdańska (2019–2024)"],
        "mid_term": True,
        "notes": "Objęła mandat w miejsce Aleksandry Dulkiewicz.",
    },
    "Joanna Cabaj": {
        "club": "PiS", "club_full": "Prawo i Sprawiedliwość",
        "okręg": None, "okręg_dzielnice": "",
        "roles": [],
        "notes": "Zrzekła się mandatu w trakcie kadencji.",
        "former": True,
    },
}

WIKI_BY_KADENCJA = {
    "2018-2023": COUNCILORS_VIII,
    "2024-2029": COUNCILORS_X,
}


def make_slug(name):
    """Create URL-safe slug from Polish name."""
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
        'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z',
    }
    slug = name.lower()
    for pl, ascii_c in replacements.items():
        slug = slug.replace(pl, ascii_c)
    slug = slug.replace(' ', '-').replace("'", "")
    return slug


def load_activity_data(activity_path):
    """Load activity.json from protocol parser if it exists."""
    if activity_path and os.path.exists(activity_path):
        with open(activity_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def build_profiles(data_json_path, out_path, activity_path=None):
    with open(data_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    activity = load_activity_data(activity_path)

    # Collect all unique councilor names across kadencje
    all_names = set()
    for k in data['kadencje']:
        for c in k['councilors']:
            all_names.add(c['name'])

    # Also add wiki-only entries (former councilors)
    for kid, wiki in WIKI_BY_KADENCJA.items():
        for name in wiki:
            all_names.add(name)

    profiles = []
    for name in sorted(all_names):
        slug = make_slug(name)
        profile = {
            "name": name,
            "slug": slug,
            "kadencje": {},
        }

        for k in data['kadencje']:
            kid = k['id']
            # Find voting data
            councilor_data = None
            for c in k['councilors']:
                if c['name'] == name:
                    councilor_data = c
                    break

            # Find wiki data
            wiki = WIKI_BY_KADENCJA.get(kid, {}).get(name, None)

            if not councilor_data and not wiki:
                continue

            entry = {}

            # Wiki info
            if wiki:
                entry["club"] = wiki.get("club", "?")
                entry["club_full"] = wiki.get("club_full", "")
                entry["okręg"] = wiki.get("okręg")
                entry["okręg_dzielnice"] = wiki.get("okręg_dzielnice", "")
                entry["roles"] = wiki.get("roles", [])
                entry["komisje"] = wiki.get("komisje", [])
                entry["notes"] = wiki.get("notes", "")
                entry["mid_term"] = wiki.get("mid_term", False)
                entry["former"] = wiki.get("former", False)

            # Voting metrics
            if councilor_data:
                entry["club"] = entry.get("club") or councilor_data["club"]
                entry["frekwencja"] = councilor_data["frekwencja"]
                entry["aktywnosc"] = councilor_data["aktywnosc"]
                entry["zgodnosc_z_klubem"] = councilor_data["zgodnosc_z_klubem"]
                entry["votes_za"] = councilor_data["votes_za"]
                entry["votes_przeciw"] = councilor_data["votes_przeciw"]
                entry["votes_wstrzymal"] = councilor_data["votes_wstrzymal"]
                entry["votes_brak"] = councilor_data["votes_brak"]
                entry["votes_nieobecny"] = councilor_data["votes_nieobecny"]
                entry["votes_total"] = councilor_data["votes_total"]
                entry["rebellion_count"] = councilor_data["rebellion_count"]
                entry["rebellions"] = councilor_data.get("rebellions", [])
                entry["has_voting_data"] = True
            else:
                entry["has_voting_data"] = False

            # Speaking activity from protocol analysis — split by kadencja
            if name in activity:
                act = activity[name]
                all_sessions = act.get("sessions", [])

                # Filter sessions by kadencja date range
                # IX kadencja started 2024-05-07
                KADENCJA_IX_START = "2024-05-07"
                if kid == "2024-2029":
                    kad_sessions = [s for s in all_sessions if s.get("date", "") >= KADENCJA_IX_START]
                else:  # 2018-2023
                    kad_sessions = [s for s in all_sessions if s.get("date", "") < KADENCJA_IX_START]

                if kad_sessions:
                    total_stmts = sum(s["statements"] for s in kad_sessions)
                    total_words = sum(s["words"] for s in kad_sessions)
                    spoke = len(kad_sessions)
                    entry["activity"] = {
                        "sessions_spoke": spoke,
                        "total_statements": total_stmts,
                        "total_words": total_words,
                        "avg_statements_per_session": round(total_stmts / spoke, 1) if spoke else 0,
                        "avg_words_per_session": round(total_words / spoke) if spoke else 0,
                        "sessions": sorted(kad_sessions, key=lambda s: s.get("date", "")),
                    }
                    entry["has_activity_data"] = True
                else:
                    entry["has_activity_data"] = False
            else:
                entry["has_activity_data"] = False

            profile["kadencje"][kid] = entry

        if profile["kadencje"]:
            profiles.append(profile)

    output = {
        "profiles": profiles,
        "total": len(profiles),
    }

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Built {len(profiles)} councilor profiles → {out_path}")

    # Stats
    for kid in ["2018-2023", "2024-2029"]:
        count = sum(1 for p in profiles if kid in p["kadencje"])
        with_votes = sum(1 for p in profiles if kid in p["kadencje"] and p["kadencje"][kid].get("has_voting_data"))
        print(f"  {kid}: {count} profiles ({with_votes} with voting data)")


if __name__ == "__main__":
    data_path = "dashboard/data.json"
    out_path = "dashboard/profiles.json"

    if "--data" in sys.argv:
        idx = sys.argv.index("--data")
        if idx + 1 < len(sys.argv):
            data_path = sys.argv[idx + 1]

    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            out_path = sys.argv[idx + 1]

    activity_path = "data/protokoly/activity.json"
    if "--activity" in sys.argv:
        idx = sys.argv.index("--activity")
        if idx + 1 < len(sys.argv):
            activity_path = sys.argv[idx + 1]

    build_profiles(data_path, out_path, activity_path)
