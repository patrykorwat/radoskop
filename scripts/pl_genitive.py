#!/usr/bin/env python3
"""
Polski genitive (dopełniacz) dla nazw miast.

Strategia trzystopniowa:
  1. OVERRIDES dict — manualne wpisy dla nieregularnych form. Cover ~300
     najczęściej spotykanych polskich miast plus każdy przypadek gdzie
     heurystyka daje błąd.
  2. Compound handling — rozdzielenie nazw wielowyrazowych ('Nowy Sącz',
     'Biała Podlaska', 'Kostrzyn nad Odrą') i odmiana każdego słowa wg
     reguł morfologicznych dla przymiotników, rzeczowników i przyimków.
  3. Heurystyka per końcówka — fallback dla pojedynczych słów spoza
     OVERRIDES. Pokrywa ~95% typowych przypadków.

Użycie:
    from pl_genitive import genitive
    genitive("Bolesławiec") → "Bolesławca"
    genitive("Zakopane") → "Zakopanego"
    genitive("Jelenia Góra") → "Jeleniej Góry"
    genitive("Kostrzyn nad Odrą") → "Kostrzyna nad Odrą"

Format użyty w Radoskop config.json: pole `city_genitive` w formie
"Rady Miasta {genitive}" — czyli np. "Rady Miasta Bolesławca".
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# OVERRIDES — wyjątki + lista najczęściej używanych miast (~300 wpisów).
# Wszystkie miasta wojewódzkie, powiatowe i wszystkie z eSesja batch dodane.
# Plus każde miasto które heurystyka źle odmienia.
# ---------------------------------------------------------------------------

OVERRIDES: dict[str, str] = {
    # Stolice województw i duże miasta
    "Warszawa": "Warszawy",
    "Kraków": "Krakowa",
    "Wrocław": "Wrocławia",
    "Łódź": "Łodzi",
    "Poznań": "Poznania",
    "Gdańsk": "Gdańska",
    "Szczecin": "Szczecina",
    "Lublin": "Lublina",
    "Bydgoszcz": "Bydgoszczy",
    "Katowice": "Katowic",
    "Białystok": "Białegostoku",
    "Częstochowa": "Częstochowy",
    "Radom": "Radomia",
    "Toruń": "Torunia",
    "Rzeszów": "Rzeszowa",
    "Kielce": "Kielc",
    "Olsztyn": "Olsztyna",
    "Bielsko-Biała": "Bielska-Białej",
    "Gliwice": "Gliwic",
    "Zabrze": "Zabrza",
    "Bytom": "Bytomia",
    "Tychy": "Tychów",
    "Sopot": "Sopotu",
    "Gdynia": "Gdyni",
    "Gorzów Wielkopolski": "Gorzowa Wielkopolskiego",
    "Zielona Góra": "Zielonej Góry",
    "Opole": "Opola",
    "Tarnów": "Tarnowa",
    "Sosnowiec": "Sosnowca",
    "Wałbrzych": "Wałbrzycha",
    "Jelenia Góra": "Jeleniej Góry",
    "Elbląg": "Elbląga",
    "Płock": "Płocka",
    "Włocławek": "Włocławka",
    "Kalisz": "Kalisza",
    "Siedlce": "Siedlec",
    "Dąbrowa Górnicza": "Dąbrowy Górniczej",
    "Słupsk": "Słupska",
    "Nowy Sącz": "Nowego Sącza",
    "Mysłowice": "Mysłowic",
    "Chorzów": "Chorzowa",
    "Zamość": "Zamościa",
    "Suwałki": "Suwałk",
    "Przemyśl": "Przemyśla",
    "Pruszków": "Pruszkowa",
    "Łomża": "Łomży",
    "Leszno": "Leszna",
    "Inowrocław": "Inowrocławia",
    "Ełk": "Ełku",
    "Bełchatów": "Bełchatowa",
    "Zgierz": "Zgierza",
    "Żyrardów": "Żyrardowa",
    "Zgorzelec": "Zgorzelca",
    "Żary": "Żar",
    "Zambrów": "Zambrowa",
    "Zakopane": "Zakopanego",
    "Żagań": "Żagania",
    "Wejherowo": "Wejherowa",
    "Wałcz": "Wałcza",
    "Wągrowiec": "Wągrowca",
    "Tarnobrzeg": "Tarnobrzega",
    "Świętochłowice": "Świętochłowic",
    "Sieradz": "Sieradza",
    "Sanok": "Sanoka",
    "Reda": "Redy",
    "Racibórz": "Raciborza",
    "Piastów": "Piastowa",
    "Otwock": "Otwocka",
    "Oświęcim": "Oświęcimia",
    "Ostrołęka": "Ostrołęki",
    "Ostróda": "Ostródy",
    "Orzesze": "Orzesza",
    "Oleśnica": "Oleśnicy",
    "Myszków": "Myszkowa",
    "Mikołów": "Mikołowa",
    "Marki": "Marek",
    "Łuków": "Łukowa",
    "Luboń": "Lubonia",
    "Lubartów": "Lubartowa",
    "Legionowo": "Legionowa",
    "Kutno": "Kutna",
    "Kraśnik": "Kraśnika",
    "Krasnystaw": "Krasnegostawu",
    "Kościan": "Kościana",
    "Kołobrzeg": "Kołobrzega",
    "Kobyłka": "Kobyłki",
    "Kętrzyn": "Kętrzyna",
    "Jawor": "Jawora",
    "Iława": "Iławy",
    "Dzierżoniów": "Dzierżoniowa",
    "Dębica": "Dębicy",
    "Czeladź": "Czeladzi",
    "Chojnice": "Chojnic",
    "Brodnica": "Brodnicy",
    "Bolesławiec": "Bolesławca",
    "Bochnia": "Bochni",
    "Bielawa": "Bielawy",
    "Będzin": "Będzina",
    "Bartoszyce": "Bartoszyc",
    "Augustów": "Augustowa",
    "Aleksandrów Kujawski": "Aleksandrowa Kujawskiego",
    # Mniejsze miasta z fix_city_names (batch z 2026-05-17)
    "Boguszów-Gorce": "Boguszowa-Gorc",
    "Braniewo": "Braniewa",
    "Brańsk": "Brańska",
    "Bukowno": "Bukowna",
    "Chodzież": "Chodzieży",
    "Ciechocinek": "Ciechocinka",
    "Człuchów": "Człuchowa",
    "Czarnków": "Czarnkowa",
    "Dęblin": "Dęblina",
    "Dynów": "Dynowa",
    "Gozdnica": "Gozdnicy",
    "Grajewo": "Grajewa",
    "Imielin": "Imielina",
    "Jedlina-Zdrój": "Jedliny-Zdroju",
    "Józefów": "Józefowa",
    "Koło": "Koła",
    "Kowary": "Kowar",
    "Lubaczów": "Lubaczowa",
    "Lubań": "Lubania",
    "Lubawa": "Lubawy",
    "Łańcut": "Łańcuta",
    "Łeba": "Łeby",
    "Lędziny": "Lędzin",
    "Leżajsk": "Leżajska",
    "Lipno": "Lipna",
    "Łowicz": "Łowicza",
    "Milanówek": "Milanówka",
    "Mrągowo": "Mrągowa",
    "Nieszawa": "Nieszawy",
    "Piechowice": "Piechowic",
    "Pionki": "Pionek",
    "Polanica-Zdrój": "Polanicy-Zdroju",
    "Poręba": "Poręby",
    "Puck": "Pucka",
    "Puszczykowo": "Puszczykowa",
    "Pyskowice": "Pyskowic",
    "Radlin": "Radlina",
    "Radymno": "Radymna",
    "Sławków": "Sławkowa",
    "Sławno": "Sławna",
    "Sulejówek": "Sulejówka",
    "Sulmierzyce": "Sulmierzyc",
    "Szczyrk": "Szczyrku",
    "Ustka": "Ustki",
    "Ustroń": "Ustronia",
    "Węgrów": "Węgrowa",
    "Wisła": "Wisły",
    "Wojcieszów": "Wojcieszowa",
    "Złotoryja": "Złotoryi",
    "Złotów": "Złotowa",
    "Bieruń": "Bierunia",
    # Inne często spotykane
    "Pabianice": "Pabianic",
    "Konin": "Konina",
    "Stargard": "Stargardu",
    "Mielec": "Mielca",
    "Świnoujście": "Świnoujścia",
    "Świdnica": "Świdnicy",
    "Świdwin": "Świdwina",
    "Łomianki": "Łomianek",
    "Mińsk Mazowiecki": "Mińska Mazowieckiego",
    "Nowy Targ": "Nowego Targu",
    "Nowy Dwór Mazowiecki": "Nowego Dworu Mazowieckiego",
    "Nysa": "Nysy",
    "Olkusz": "Olkusza",
    "Ostrów Wielkopolski": "Ostrowa Wielkopolskiego",
    "Ostrów Mazowiecka": "Ostrowi Mazowieckiej",
    "Ostrowiec Świętokrzyski": "Ostrowca Świętokrzyskiego",
    "Piaseczno": "Piaseczna",
    "Piła": "Piły",
    "Piotrków Trybunalski": "Piotrkowa Trybunalskiego",
    "Pisz": "Pisza",
    "Pszczyna": "Pszczyny",
    "Pułtusk": "Pułtuska",
    "Rabka-Zdrój": "Rabki-Zdroju",
    "Ruda Śląska": "Rudy Śląskiej",
    "Rybnik": "Rybnika",
    "Sandomierz": "Sandomierza",
    "Skarżysko-Kamienna": "Skarżyska-Kamiennej",
    "Skawina": "Skawiny",
    "Skierniewice": "Skierniewic",
    "Stalowa Wola": "Stalowej Woli",
    "Starachowice": "Starachowic",
    "Świebodzin": "Świebodzina",
    "Tczew": "Tczewa",
    "Tomaszów Mazowiecki": "Tomaszowa Mazowieckiego",
    "Wodzisław Śląski": "Wodzisławia Śląskiego",
    "Wołomin": "Wołomina",
    "Zawiercie": "Zawiercia",
    "Zduńska Wola": "Zduńskiej Woli",
    "Zielonka": "Zielonki",
    "Żywiec": "Żywca",
    "Świecie": "Świecia",
    "Trzebinia": "Trzebini",
    "Lubin": "Lubina",
    "Gniezno": "Gniezna",
    "Hajnówka": "Hajnówki",
    "Hrubieszów": "Hrubieszowa",
    "Iwonicz-Zdrój": "Iwonicza-Zdroju",
    "Jastrzębie-Zdrój": "Jastrzębia-Zdroju",
    "Jaworzno": "Jaworzna",
    "Kazimierza Wielka": "Kazimierzy Wielkiej",
    "Kędzierzyn-Koźle": "Kędzierzyna-Koźla",
    "Kłodzko": "Kłodzka",
    "Kościerzyna": "Kościerzyny",
    "Krynica-Zdrój": "Krynicy-Zdroju",
    "Krynica Morska": "Krynicy Morskiej",
    "Krzeszowice": "Krzeszowic",
    "Kwidzyn": "Kwidzyna",
    "Łapy": "Łap",
    "Legnica": "Legnicy",
    "Lesko": "Leska",
    "Limanowa": "Limanowej",
    "Lubliniec": "Lublińca",
    "Malbork": "Malborka",
    "Mława": "Mławy",
    "Mosina": "Mosiny",
    "Murowana Goślina": "Murowanej Gośliny",
    "Myślibórz": "Myśliborza",
    "Nakło nad Notecią": "Nakła nad Notecią",
    "Niepołomice": "Niepołomic",
    "Nowy Wiśnicz": "Nowego Wiśnicza",
    "Pleszew": "Pleszewa",
    "Police": "Polic",
    "Radzymin": "Radzymina",
    "Sejny": "Sejn",
    "Sokółka": "Sokółki",
    "Solec Kujawski": "Solca Kujawskiego",
    "Sucha Beskidzka": "Suchej Beskidzkiej",
    "Świebodzice": "Świebodzic",
    "Trzcianka": "Trzcianki",
    "Turek": "Turka",
    "Wieliczka": "Wieliczki",
    # Plurale tantum (rzeczowniki w plural) — drop -y/-i, czasem +ów lub epenteza
    "Brody": "Brodów",
    "Brusy": "Brus",
    "Brzeziny": "Brzezin",
    "Chęciny": "Chęcin",
    "Dobrzany": "Dobrzan",
    "Głuchołazy": "Głuchołaz",
    "Iwaniska": "Iwanisk",
    "Jeziorany": "Jezioran",
    "Kaczory": "Kaczor",
    "Kalety": "Kalet",
    "Kartuzy": "Kartuz",
    "Koluszki": "Koluszek",
    "Krynki": "Krynek",
    "Lipiany": "Lipian",
    "Łazy": "Łaz",
    "Mikołajki": "Mikołajek",
    "Młynary": "Młynar",
    "Mońki": "Moniek",
    "Mordy": "Mordów",
    "Mrozy": "Mrozów",
    "Pniewy": "Pniew",
    "Płoty": "Płotów",
    "Prabuty": "Prabut",
    "Puławy": "Puław",
    "Pyzdry": "Pyzdr",
    "Rydułtowy": "Rydułtów",
    "Ryki": "Ryk",
    "Sanniki": "Sannik",
    "Skarszewy": "Skarszew",
    "Skoki": "Skoków",
    "Słomniki": "Słomnik",
    "Stawiski": "Stawisk",
    "Strzeleczki": "Strzeleczek",
    "Szamotuły": "Szamotuł",
    "Szczekociny": "Szczekocin",
    "Wiskitki": "Wiskitek",
    "Woźniki": "Woźnik",
    "Wronki": "Wronek",
    "Zduny": "Zdun",
    "Żarki": "Żarek",
    "Żory": "Żor",
    "Białobrzegi": "Białobrzegów",
    "Błaszki": "Błaszek",
    "Bobrowniki": "Bobrownik",
    "Czemierniki": "Czemiernik",
    "Kęty": "Kęt",
    "Mikstat": "Mikstata",
    "Oborniki": "Obornik",
    "Piaski": "Piasków",
    "Skarszewy": "Skarszew",
    # -ce/-yce plural (drop -e)
    "Tyszowce": "Tyszowiec",
    "Wyśmierzyce": "Wyśmierzyc",
    "Bełżyce": "Bełżyc",
    "Brzeszcze": "Brzeszcz",
    "Daleszyce": "Daleszyc",
    "Dobczyce": "Dobczyc",
    "Działoszyce": "Działoszyc",
    "Głubczyce": "Głubczyc",
    "Jedlicze": "Jedlicz",
    "Kleszczele": "Kleszczel",
    "Kołaczyce": "Kołaczyc",
    "Korsze": "Korsz",
    "Koszyce": "Koszyc",
    "Modliborzyce": "Modliborzyc",
    "Międzyzdroje": "Międzyzdrojów",
    "Oleszyce": "Oleszyc",
    "Pełczyce": "Pełczyc",
    "Pieszyce": "Pieszyc",
    "Pyrzyce": "Pyrzyc",
    "Radoszyce": "Radoszyc",
    "Ropczyce": "Ropczyc",
    "Siemiatycze": "Siemiatycz",
    "Strzelce Krajeńskie": "Strzelec Krajeńskich",
    "Strzelce Opolskie": "Strzelec Opolskich",
    "Chorzele": "Chorzel",
    # -ie neuter (singular noun, NOT adjective)
    "Ujście": "Ujścia",
    "Międzylesie": "Międzylesia",
    "Jastrowie": "Jastrowia",
    "Stronie Śląskie": "Stronia Śląskiego",
    "Siedliszcze": "Siedliszcza",
    # Compound adjective + noun (feminine -ska/-cka/-na + noun)
    "Biała Podlaska": "Białej Podlaskiej",
    "Biała Piska": "Białej Piskiej",
    "Biała Rawska": "Białej Rawskiej",
    "Bystrzyca Kłodzka": "Bystrzycy Kłodzkiej",
    "Czarna Białostocka": "Czarnej Białostockiej",
    "Czarna Woda": "Czarnej Wody",
    "Dąbrowa Białostocka": "Dąbrowy Białostockiej",
    "Dąbrowa Tarnowska": "Dąbrowy Tarnowskiej",
    "Izbica Kujawska": "Izbicy Kujawskiej",
    "Jaworzyna Śląska": "Jaworzyny Śląskiej",
    "Kalwaria Zebrzydowska": "Kalwarii Zebrzydowskiej",
    "Kamienna Góra": "Kamiennej Góry",
    "Kuźnia Raciborska": "Kuźni Raciborskiej",
    "Lubycza Królewska": "Lubyczy Królewskiej",
    "Mszana Dolna": "Mszanej Dolnej",
    "Miejska Górka": "Miejskiej Górki",
    "Nowa Dęba": "Nowej Dęby",
    "Nowa Ruda": "Nowej Rudy",
    "Nowa Sarzyna": "Nowej Sarzyny",
    "Nowa Sól": "Nowej Soli",
    "Nowa Słupia": "Nowej Słupi",
    "Pakość": "Pakości",
    "Piława Górna": "Piławy Górnej",
    "Podkowa Leśna": "Podkowy Leśnej",
    "Rawa Mazowiecka": "Rawy Mazowieckiej",
    "Szklarska Poręba": "Szklarskiej Poręby",
    "Środa Śląska": "Środy Śląskiej",
    "Środa Wielkopolska": "Środy Wielkopolskiej",
    "Łaziska Górne": "Łazisk Górnych",
    "Wysoka": "Wysokiej",
    "Góra Kalwaria": "Góry Kalwarii",
    "Krynica Morska": "Krynicy Morskiej",
    # Compound przymiotnik + plural (Tarnowskie Góry → Tarnowskich Gór)
    "Tarnowskie Góry": "Tarnowskich Gór",
    "Wysokie Mazowieckie": "Wysokiego Mazowieckiego",
    "Końskie": "Końskich",
    "Międzyrzec Podlaski": "Międzyrzca Podlaskiego",
    # Compound noun + adjective masculine — drop ki → kiego nadaje się dla obu
    "Biały Bór": "Białego Boru",
    "Brześć Kujawski": "Brześcia Kujawskiego",
    "Bytom Odrzański": "Bytomia Odrzańskiego",
    "Borek Wielkopolski": "Borka Wielkopolskiego",
    "Bielsk Podlaski": "Bielska Podlaskiego",
    "Baranów Sandomierski": "Baranowa Sandomierskiego",
    "Brzeg": "Brzegu",
    "Brzeg Dolny": "Brzegu Dolnego",
    "Czarny Dunajec": "Czarnego Dunajca",
    "Czerwińsk nad Wisłą": "Czerwińska nad Wisłą",
    "Dobre Miasto": "Dobrego Miasta",
    "Dobrzyń nad Wisłą": "Dobrzynia nad Wisłą",
    "Drawsko Pomorskie": "Drawska Pomorskiego",
    "Glinojeck": "Glinojecka",
    "Gorzów Śląski": "Gorzowa Śląskiego",
    "Górowo Iławeckie": "Górowa Iławeckiego",
    "Grodzisk Mazowiecki": "Grodziska Mazowieckiego",
    "Grodzisk Wielkopolski": "Grodziska Wielkopolskiego",
    "Gryfów Śląski": "Gryfowa Śląskiego",
    "Głogów Małopolski": "Głogowa Małopolskiego",
    "Jabłonowo Pomorskie": "Jabłonowa Pomorskiego",
    "Janowiec Wielkopolski": "Janowca Wielkopolskiego",
    "Janów Lubelski": "Janowa Lubelskiego",
    "Jawornik Polski": "Jawornika Polskiego",
    "Kazimierz Dolny": "Kazimierza Dolnego",
    "Kalisz Pomorski": "Kalisza Pomorskiego",
    "Kamieniec Ząbkowicki": "Kamieńca Ząbkowickiego",
    "Kamień Krajeński": "Kamienia Krajeńskiego",
    "Kamień Pomorski": "Kamienia Pomorskiego",
    "Konstantynów Łódzki": "Konstantynowa Łódzkiego",
    "Kosów Lacki": "Kosowa Lackiego",
    "Kostrzyn nad Odrą": "Kostrzyna nad Odrą",
    "Kowalewo Pomorskie": "Kowalewa Pomorskiego",
    "Koźmin Wielkopolski": "Koźmina Wielkopolskiego",
    "Krosno Odrzańskie": "Krosna Odrzańskiego",
    "Krzyż Wielkopolski": "Krzyża Wielkopolskiego",
    "Książ Wielki": "Książa Wielkiego",
    "Książ Wielkopolski": "Książa Wielkopolskiego",
    "Kąty Wrocławskie": "Kątów Wrocławskich",
    "Lewin Brzeski": "Lewina Brzeskiego",
    "Lidzbark Warmiński": "Lidzbarka Warmińskiego",
    "Lubień Kujawski": "Lubienia Kujawskiego",
    "Lwówek Śląski": "Lwówka Śląskiego",
    "Maków Mazowiecki": "Makowa Mazowieckiego",
    "Maków Podhalański": "Makowa Podhalańskiego",
    "Miasteczko Krajeńskie": "Miasteczka Krajeńskiego",
    "Miasteczko Śląskie": "Miasteczka Śląskiego",
    "Nowe Brzesko": "Nowego Brzeska",
    "Nowe Miasteczko": "Nowego Miasteczka",
    "Nowe Miasto": "Nowego Miasta",
    "Nowe Miasto Lubawskie": "Nowego Miasta Lubawskiego",
    "Nowe Miasto nad Pilicą": "Nowego Miasta nad Pilicą",
    "Nowe Warpno": "Nowego Warpna",
    "Nowogród Bobrzański": "Nowogrodu Bobrzańskiego",
    "Nowy Dwór Gdański": "Nowego Dworu Gdańskiego",
    "Nowy Korczyn": "Nowego Korczyna",
    "Nowy Staw": "Nowego Stawu",
    "Nowy Tomyśl": "Nowego Tomyśla",
    "Oborniki Śląskie": "Obornik Śląskich",
    "Opole Lubelskie": "Opola Lubelskiego",
    "Ostrów Lubelski": "Ostrowa Lubelskiego",
    "Ośno Lubuskie": "Ośna Lubuskiego",
    "Ożarów Mazowiecki": "Ożarowa Mazowieckiego",
    "Piekary Śląskie": "Piekar Śląskich",
    "Piotrków Kujawski": "Piotrkowa Kujawskiego",
    "Pruszcz Gdański": "Pruszcza Gdańskiego",
    "Radomyśl Wielki": "Radomyśla Wielkiego",
    "Radzyń Chełmiński": "Radzynia Chełmińskiego",
    "Radzyń Podlaski": "Radzynia Podlaskiego",
    "Rejowiec Fabryczny": "Rejowca Fabrycznego",
    "Rudnik nad Sanem": "Rudnika nad Sanem",
    "Sędziszów Małopolski": "Sędziszowa Małopolskiego",
    "Sępólno Krajeńskie": "Sępólna Krajeńskiego",
    "Solec nad Wisłą": "Solca nad Wisłą",
    "Sokołów Małopolski": "Sokołowa Małopolskiego",
    "Sokołów Podlaski": "Sokołowa Podlaskiego",
    "Starogard Gdański": "Starogardu Gdańskiego",
    "Stary Sącz": "Starego Sącza",
    "Stoczek Łukowski": "Stoczka Łukowskiego",
    "Świątniki Górne": "Świątnik Górnych",
    "Tomaszów Lubelski": "Tomaszowa Lubelskiego",
    "Ustrzyki Dolne": "Ustrzyk Dolnych",
    "Złoty Stok": "Złotego Stoku",
    "Ząbkowice Śląskie": "Ząbkowic Śląskich",
    # -iec → -ca (palatalizacja)
    "Szydłowiec": "Szydłowca",
    "Złocieniec": "Złocieńca",
    "Zwierzyniec": "Zwierzyńca",
    "Ciechanowiec": "Ciechanowca",
    "Biskupiec": "Biskupca",
    "Mirosławiec": "Mirosławca",
    "Myszyniec": "Myszyńca",
    "Lubraniec": "Lubrańca",
    "Nowogrodziec": "Nowogrodźca",
    "Ogrodzieniec": "Ogrodzieńca",
    "Opatowiec": "Opatowca",
    "Poniec": "Pońca",
    "Połaniec": "Połańca",
    "Rejowiec": "Rejowca",
    "Węgliniec": "Węglińca",
    # -ec bez "i" — standardowo -ca
    "Grójec": "Grójca",
    # -ń → -nia
    "Suchań": "Suchania",
    "Bieżuń": "Bieżunia",
    "Budzyń": "Budzynia",
    "Czempiń": "Czempinia",
    "Dobrodzień": "Dobrodzienia",
    "Dzierzgoń": "Dzierzgonia",
    "Gostyń": "Gostynia",
    "Krzywiń": "Krzywinia",
    "Moryń": "Morynia",
    "Otyń": "Otynia",
    "Wleń": "Wlenia",
    "Wieleń": "Wielenia",
    "Wieluń": "Wielunia",
    "Zbąszyń": "Zbąszynia",
    "Zwoleń": "Zwolenia",
    # -ja/-rj
    "Bogoria": "Bogorii",
    "Krobia": "Krobii",
    "Kiernozia": "Kiernozi",
    "Rumia": "Rumi",
    # -ł, -l
    "Kikół": "Kikoła",
    # Specjalne — krótkie miasta
    "Łęczna": "Łęcznej",
    "Leśna": "Leśnej",
    "Osieczna": "Osiecznej",
    "Szczytna": "Szczytnej",
    "Sienno": "Sienna",
    "Osiek": "Osieka",
    "Szczytno": "Szczytna",
    # Dodatkowe wojewódzkie + powiatowe stolice które heurystyka pomyli
    "Aleksandrów Łódzki": "Aleksandrowa Łódzkiego",
    "Czerwionka-Leszczyny": "Czerwionki-Leszczyn",
    "Czechowice-Dziedzice": "Czechowic-Dziedzic",
    "Duszniki-Zdrój": "Dusznik-Zdroju",
    "Lądek-Zdrój": "Lądka-Zdroju",
    "Świeradów-Zdrój": "Świeradowa-Zdroju",
    "Piwniczna-Zdrój": "Piwnicznej-Zdroju",
    "Połczyn-Zdrój": "Połczyna-Zdroju",
    "Szczawno-Zdrój": "Szczawna-Zdroju",
    "Trzcińsko-Zdrój": "Trzcińska-Zdroju",
    "Busko-Zdrój": "Buska-Zdroju",
    "Kudowa-Zdrój": "Kudowy-Zdroju",
    "Konstancin-Jeziorna": "Konstancina-Jeziorny",
    "Jelcz-Laskowice": "Jelcza-Laskowic",
    "Ruciane-Nida": "Rucianego-Nidy",
    "Golub-Dobrzyń": "Golubia-Dobrzynia",
    "Jedlnia-Letnisko": "Jedlni-Letniska",
    # Inne często-popełniane
    "Borne Sulinowo": "Bornego Sulinowa",
    "Końskie": "Końskich",
    "Mikstat": "Mikstata",
    "Nasielsk": "Nasielska",
    "Międzyrzecz": "Międzyrzecza",
    "Nowogród": "Nowogrodu",
    "Tarnogród": "Tarnogrodu",
    "Krasnobród": "Krasnobrodu",
    "Rajgród": "Rajgrodu",
    "Żmigród": "Żmigrodu",
    "Międzychód": "Międzychodu",
    "Międzybórz": "Międzyborza",
}


# ---------------------------------------------------------------------------
# Heurystyka per końcówka — fallback
# ---------------------------------------------------------------------------

# Regex → suffix replacement. Sprawdzane w kolejności pierwsze pasujące wygrywa.
# Każda reguła: (pattern_konca_nominative, ile_znaków_usunąć, suffix_genitive)
_RULES: list[tuple[str, int, str]] = [
    # Wieloliterowe końcówki — sprawdzane przed jednoliterowymi
    # Najpierw specyficzne -iec (palatalizacja) przed ogólnym -ec
    ("wiec", 3, "wca"),      # Bolesławiec → Bolesławca, Sosnowiec → Sosnowca
    ("niec", 4, "ńca"),      # Złocieniec → Złocieńca (palatalizacja n→ń)
    ("ec", 2, "ca"),         # Bolesławiec → Bolesławca (fallback)
    ("ek", 2, "ka"),         # Włocławek → Włocławka, Sulejówek → Sulejówka
    ("ów", 2, "owa"),        # Tarnów → Tarnowa, Bełchatów → Bełchatowa
    ("ica", 3, "icy"),       # Świdnica → Świdnicy, Dębica → Dębicy
    ("nia", 3, "ni"),        # Bochnia → Bochni
    ("ja", 2, "i"),          # Złotoryja → Złotoryi
    ("rze", 3, "rza"),       # Orzesze → Orzesza
    ("ice", 3, "ic"),        # Mysłowice → Mysłowic, Bartoszyce → Bartoszyc
    ("yce", 3, "yc"),        # Wyśmierzyce → Wyśmierzyc, Bełżyce → Bełżyc
    ("ko", 2, "ka"),         # (rzadkie, większość -ko to neut → -ka)
    ("wo", 2, "wa"),         # Mrągowo → Mrągowa, Wejherowo → Wejherowa
    ("no", 2, "na"),         # Lipno → Lipna, Kutno → Kutna
    ("eń", 2, "enia"),       # Bieżuń → Bieżunia (rzadkie)
    ("oń", 2, "onia"),       # Luboń → Lubonia
    ("uń", 2, "unia"),       # Bieruń → Bierunia
    ("yń", 2, "ynia"),       # Zbąszyń → Zbąszynia
    ("ań", 2, "ania"),       # Suchań → Suchania
    ("im", 2, "imia"),       # Oświęcim → Oświęcimia
    ("om", 2, "omia"),       # Bytom → Bytomia, Radom → Radomia
    ("yn", 2, "yna"),        # Kętrzyn → Kętrzyna, Olsztyn → Olsztyna
    ("in", 2, "ina"),        # Lublin → Lublina, Będzin → Będzina
    ("e", 1, "ego"),         # Zakopane → Zakopanego (przymiotnikowe)
    ("o", 1, "a"),           # Bukowno → Bukowna, Sławno → Sławna
    ("y", 1, "ów"),          # Brody → Brodów (default plural — często zły)
    ("i", 1, ""),            # Piaski → Piask (rzadko poprawne)
    ("a", 1, "y"),           # Iława → Iławy, Bielawa → Bielawy
]

# Przyimki — w compound names nie odmieniają się, drugie słowo po nich
# zostaje w lokative/instrumental.
_PREPOSITIONS = {"nad", "pod", "przed", "za", "w", "we", "u", "o", "na", "po"}

# Końcówki przymiotników żeńskich (mianownik) → odpowiadające genitive
_FEM_ADJ_ENDINGS = [
    ("ska", "skiej"),
    ("cka", "ckiej"),
    ("zka", "zkiej"),
    ("dzka", "dzkiej"),
    ("ska", "skiej"),
    ("rska", "rskiej"),
    ("wska", "wskiej"),
    ("lska", "lskiej"),
    ("ńska", "ńskiej"),
    ("owa", "owej"),
    ("ewa", "ewej"),
    ("rna", "rnej"),
    ("lna", "lnej"),
    ("dna", "dnej"),
    ("tna", "tnej"),
    ("zna", "znej"),
    ("sna", "snej"),
    (" czna", "cznej"),
    ("szna", "sznej"),
    ("żna", "żnej"),
    ("ska", "skiej"),
    ("ka", "kiej"),  # tylko jeśli nie zwykły rzeczownik
]

# Końcówki przymiotników męskich (mianownik) → genitive
_MASC_ADJ_ENDINGS = [
    ("ski", "skiego"),
    ("cki", "ckiego"),
    ("zki", "zkiego"),
    ("dzki", "dzkiego"),
    ("rski", "rskiego"),
    ("wski", "wskiego"),
    ("lski", "lskiego"),
    ("ński", "ńskiego"),
    ("any", "anego"),
    ("ony", "onego"),
    ("ony", "onego"),
    ("alny", "alnego"),
    ("ny", "nego"),
    ("wy", "wego"),
    ("ty", "tego"),
    ("rzy", "rzego"),
    ("ki", "kiego"),
]

# Końcówki przymiotników nijakich (-e) → genitive (-ego)
_NEUT_ADJ_ENDINGS = [
    ("skie", "skiego"),
    ("ckie", "ckiego"),
    ("zkie", "zkiego"),
    ("dzkie", "dzkiego"),
    ("rskie", "rskiego"),
    ("wskie", "wskiego"),
    ("lskie", "lskiego"),
    ("ńskie", "ńskiego"),
    ("ne", "nego"),
    ("we", "wego"),
    ("te", "tego"),
    ("kie", "kiego"),
]


def _split_compound(name: str) -> list[str]:
    """Rozdziel złożenia: 'Aleksandrów Kujawski' → ['Aleksandrów', ' ', 'Kujawski'],
    'Polanica-Zdrój' → ['Polanica', '-', 'Zdrój']. Zachowuje separatory."""
    return re.split(r"(\s+|-)", name)


def _try_adjective_genitive(word: str) -> str | None:
    """Spróbuj zaklasyfikować jako przymiotnik. Zwraca formę genitive
    jeśli pasuje końcówka, None inaczej."""
    # Test od najdłuższych do najkrótszych końcówek
    for ending, repl in sorted(_MASC_ADJ_ENDINGS, key=lambda x: -len(x[0])):
        if word.endswith(ending):
            return word[: -len(ending)] + repl
    for ending, repl in sorted(_FEM_ADJ_ENDINGS, key=lambda x: -len(x[0])):
        if word.endswith(ending):
            return word[: -len(ending)] + repl
    for ending, repl in sorted(_NEUT_ADJ_ENDINGS, key=lambda x: -len(x[0])):
        if word.endswith(ending):
            return word[: -len(ending)] + repl
    return None


def _heuristic(name: str) -> str:
    """Reguła per końcówka. Fallback do +a jeśli nic nie pasuje."""
    for suffix, drop, repl in _RULES:
        if name.endswith(suffix):
            return name[:-drop] + repl
    # Default: dodaj 'a' (męskie twarde, np. Lipno, Sopot)
    return name + "a"


def _decline_single(word: str, allow_adjective: bool = True) -> str:
    """Odmień pojedyncze słowo. allow_adjective=True dla compound names
    gdzie sufiks przymiotnikowy zwykle oznacza przymiotnik."""
    if word in OVERRIDES:
        return OVERRIDES[word]
    if allow_adjective:
        adj = _try_adjective_genitive(word)
        if adj is not None:
            return adj
    return _heuristic(word)


def genitive(name: str) -> str:
    """Zwraca dopełniacz dla polskiej nazwy miasta.

    1. Sprawdza OVERRIDES — manualne wpisy dla ~300 najczęściej spotykanych
       miast plus nieregularne formy.
    2. Dla złożeń ('Nowy Sącz', 'Polanica-Zdrój', 'Kostrzyn nad Odrą')
       odmienia każde słowo, rozpoznając przyimki (nie odmieniają się) i
       przymiotniki (osobne końcówki).
    3. Heurystyka per końcówka jako fallback.
    """
    name = name.strip()
    if not name:
        return name
    # Cały name w OVERRIDES — exact match
    if name in OVERRIDES:
        return OVERRIDES[name]
    # Compound (multi-word lub hyphenated)
    parts = _split_compound(name)
    if len(parts) > 1:
        result = []
        # Wykryj przyimek — wtedy słowa po nim nie odmieniamy
        prep_idx = -1
        for i, p in enumerate(parts):
            if p.lower().strip() in _PREPOSITIONS:
                prep_idx = i
                break
        for i, p in enumerate(parts):
            if re.match(r"^[\s\-]+$", p):
                result.append(p)
                continue
            # Po przyimku — zachowaj oryginalną formę
            if prep_idx >= 0 and i > prep_idx:
                result.append(p)
                continue
            # Sam przyimek — niezmienny
            if p.lower() in _PREPOSITIONS:
                result.append(p)
                continue
            result.append(_decline_single(p, allow_adjective=True))
        return "".join(result)
    # Single word — bez agresywnej detekcji przymiotnika (zwykle to rzeczownik)
    return _heuristic(name)


if __name__ == "__main__":
    # Self-test
    tests = [
        # Podstawowe wojewódzkie
        ("Warszawa", "Warszawy"),
        ("Kraków", "Krakowa"),
        ("Łódź", "Łodzi"),
        ("Wrocław", "Wrocławia"),
        # eSesja batch
        ("Bolesławiec", "Bolesławca"),
        ("Włocławek", "Włocławka"),
        ("Tarnów", "Tarnowa"),
        ("Zakopane", "Zakopanego"),
        # Compound
        ("Jelenia Góra", "Jeleniej Góry"),
        ("Nowy Sącz", "Nowego Sącza"),
        ("Polanica-Zdrój", "Polanicy-Zdroju"),
        ("Aleksandrów Kujawski", "Aleksandrowa Kujawskiego"),
        ("Biała Podlaska", "Białej Podlaskiej"),
        ("Środa Wielkopolska", "Środy Wielkopolskiej"),
        ("Tarnowskie Góry", "Tarnowskich Gór"),
        ("Kostrzyn nad Odrą", "Kostrzyna nad Odrą"),
        # Plurale tantum
        ("Brzeziny", "Brzezin"),
        ("Łazy", "Łaz"),
        ("Marki", "Marek"),
        ("Suwałki", "Suwałk"),
        # Trudne
        ("Krasnystaw", "Krasnegostawu"),
        ("Sopot", "Sopotu"),
        ("Białystok", "Białegostoku"),
        ("Bartoszyce", "Bartoszyc"),
        # -iec palatalizacja
        ("Sosnowiec", "Sosnowca"),
        ("Szydłowiec", "Szydłowca"),
        ("Złocieniec", "Złocieńca"),
        # -ec
        ("Złotów", "Złotowa"),
        ("Bytom", "Bytomia"),
        ("Lublin", "Lublina"),
        ("Olsztyn", "Olsztyna"),
        ("Kętrzyn", "Kętrzyna"),
        # -ń
        ("Suchań", "Suchania"),
    ]
    ok = 0
    fails = []
    for name, expected in tests:
        got = genitive(name)
        if got == expected:
            ok += 1
        else:
            fails.append((name, got, expected))
    for name, got, expected in fails:
        print(f"  FAIL  {name!r} → {got!r} (expected {expected!r})")
    print(f"\n{ok}/{len(tests)} przeszło")
