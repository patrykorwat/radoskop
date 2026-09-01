import sys
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/cities/czarna-bialostocka/scripts")
import scrape_czarna_bialostocka as S
import hashlib

name = "https://bip-umczarnabialostocka.podlaskie.eu/resource/11894/Wykaz+g%C5%82osowa%C5%84+na+XXVII+sesji+Rady+Miejskiej+w+Czarnej+Bia%C5%82ostockiej+w+dniu+15+lipca+2026.doc"
data = open("/opt/data/workspace/radoskoppl/_test_cb/cache/" + hashlib.md5(name.encode()).hexdigest() + ".bin", "rb").read()
v, rost = S.parse_pdf_votes(data, "2026-07-15", 27)
print("votes:", len(v))
raw = data.decode("utf-16le", errors="ignore")
idx = raw.find("WYKAZ G\u0141OSOWA\u0143")
raw = raw[idx:] if idx > 0 else raw
txt = raw.replace("\x07", "\n").replace("\r", "\n")
lines = [l.strip() for l in txt.split("\n") if l.strip()]
g = [l for l in lines if l.lower() == "g\u0142osowanie"]
print("glosowanie markers:", len(g), "| 'Wyniki imienne':", lines.count("Wyniki imienne"))
