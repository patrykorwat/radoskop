import random, cairosvg

INDIGO="#4f46e5"; DARK="#1a1d27"; GRAY="#6b7280"
PALETTE=["#4f46e5","#dc2626","#7c3aed","#ea580c","#0284c7","#0891b2","#059669","#f59e0b"]
FONT="Liberation Sans, DejaVu Sans, sans-serif"

# ---------- BANNER 1500x500 (3:1) ----------
random.seed(7)
W,H=1500,500
bars=[]
x=40; base=H-2
while x < W-40:
    bw=random.choice([8,9,10,11,12])
    bh=random.randint(8,66)
    col=random.choice(PALETTE)
    op=random.choice([0.55,0.7,0.85,0.6,0.75])
    bars.append(f'<rect x="{x}" y="{base-bh}" width="{bw}" height="{bh}" rx="3" fill="{col}" opacity="{op}"/>')
    x += bw + random.choice([10,12,14,16])
bars_svg="\n".join(bars)

banner=f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.4" y2="1">
      <stop offset="0%" stop-color="#f8f9fb"/>
      <stop offset="100%" stop-color="#eceef3"/>
    </linearGradient>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <text x="{W/2}" y="248" font-family="{FONT}" font-size="138" font-weight="700" fill="{DARK}" text-anchor="middle" letter-spacing="-2">Radoskop</text>
  <rect x="{W/2-70}" y="286" width="140" height="9" rx="4.5" fill="{INDIGO}"/>
  <text x="{W/2}" y="358" font-family="{FONT}" font-size="46" fill="{GRAY}" text-anchor="middle">Jak głosują radni Twojego miasta</text>
  <text x="{W/2}" y="416" font-family="{FONT}" font-size="30" fill="{INDIGO}" text-anchor="middle">monitoring rad miejskich  ·  open source  ·  radoskop.eu</text>
  {bars_svg}
</svg>'''
open("bluesky-banner.svg","w",encoding="utf-8").write(banner)
cairosvg.svg2png(bytestring=banner.encode(),write_to="bluesky-banner.png",output_width=W,output_height=H)

# ---------- AVATAR 1000x1000 (circle-safe) ----------
A=1000
avatar=f'''<svg xmlns="http://www.w3.org/2000/svg" width="{A}" height="{A}" viewBox="0 0 {A} {A}">
  <defs>
    <linearGradient id="ind" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#5b53f0"/>
      <stop offset="100%" stop-color="#4338ca"/>
    </linearGradient>
  </defs>
  <rect width="{A}" height="{A}" fill="url(#ind)"/>
  <text x="500" y="500" font-family="{FONT}" font-size="640" font-weight="700" fill="#ffffff" text-anchor="middle" dominant-baseline="central">R</text>
</svg>'''
open("bluesky-avatar.svg","w",encoding="utf-8").write(avatar)
cairosvg.svg2png(bytestring=avatar.encode(),write_to="bluesky-avatar.png",output_width=A,output_height=A)
print("done")
