#!/usr/bin/env python3
"""Regenerate the SVG assets and the data-driven README text of the hallzyx profile.

Run by .github/workflows/update-profile.yml every week (and on demand):

    python scripts/generate_profile.py                  # live data from the GitHub API
    python scripts/generate_profile.py --data api.json  # offline: {"u": <user>, "r": [<repos>]}

Read from the API on every run: public repository count, TypeScript-first repos,
latest public push and the "recent activity" timeline (the 4 most recently pushed
featured projects). Curated by hand in this file: hackathon awards, the featured
project cards, the stack and the language percentages.

Layout notes: 400px tiles sit two per row on desktop and the 1000px banner has a
narrow layout for phones. Text widths are measured with DejaVu Sans (wider than
SF / Segoe / Helvetica), so a fit here is a fit on real platforms; asserts guard
every line.
"""
import argparse
import html
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from PIL import ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = str(ROOT / "assets")
os.makedirs(OUT, exist_ok=True)

F = next((d for d in ("/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/")
          if os.path.exists(d + "DejaVuSans.ttf")), "/usr/share/fonts/truetype/dejavu/")

_fonts = {}
def tw(text, size, bold=False, mono=False):
    key = (size, bold, mono)
    if key not in _fonts:
        name = ("DejaVuSansMono" if mono else "DejaVuSans") + ("-Bold" if bold else "") + ".ttf"
        _fonts[key] = ImageFont.truetype(F + name, size)
    return _fonts[key].getlength(text)

def wrap(text, size, maxw, bold=False, mono=False):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if tw(t, size, bold, mono) <= maxw:
            cur = t
        else:
            lines.append(cur); cur = w
    lines.append(cur)
    assert all(tw(l, size, bold, mono) <= maxw for l in lines), text
    return lines

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"

# Accent = the cyan of the hallzyx avatar logo (hexagon + trident, sampled #00FCFC).
# Light accent is a deep teal of the same hue that passes WCAG AA on every light surface.
THEMES = {
    "dark": dict(bg="#07090b", surface="#0c1114", surface2="#121a1e", border="#25323a",
                 text="#e6f0f2", muted="#97a6ae", accent="#00fcfc", ink="#021316", accentsoft="#00fcfc1a"),
    "light": dict(bg="#f3f7f8", surface="#ffffff", surface2="#eaf1f3", border="#c9d6db",
                  text="#0e191d", muted="#4a5961", accent="#00717a", ink="#ffffff", accentsoft="#00717a12"),
}

def svg(w, h, title, desc, body, t, extra=""):
    style = (f"<style>\n.s{{font-family:{SANS}}}.m{{font-family:{MONO}}}"
             f".tx{{fill:{t['text']}}}.mu{{fill:{t['muted']}}}.ac{{fill:{t['accent']}}}\n{extra}\n"
             "@media (prefers-reduced-motion:reduce){*{animation:none!important}}\n</style>")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'role="img" aria-labelledby="t d">\n<title id="t">{escape(title)}</title>\n'
            f'<desc id="d">{escape(desc)}</desc>\n{style}\n{body}\n</svg>\n')

def write(name, content):
    # 400px tiles get a 12px transparent bottom margin so they don't touch when
    # GitHub stacks them one per row on narrow screens.
    import re
    m = re.match(r'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="(\d+)" viewBox="0 0 400 (\d+)"', content)
    if m:
        h = int(m.group(1)) + 12
        content = content.replace(m.group(0), f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="{h}" viewBox="0 0 400 {h}"', 1)
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(content)

# ---------------------------------------------------------------- shared angular vocabulary
def n(v):
    """compact number formatting for path data"""
    s = f"{v:.1f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s

def cut(w, h, c, o=0.5, corners="tl br"):
    """Polygon path of a w x h box (stroke-aligned by o) with chamfered corners."""
    tl, tr, br, bl = (c if k in corners.split() else 0 for k in ("tl", "tr", "br", "bl"))
    pts = [(o + tl, o), (w - o - tr, o), (w - o, o + tr), (w - o, h - o - br), (w - o - br, h - o),
           (o + bl, h - o), (o, h - o - bl), (o, o + tl)]
    out = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    return "M" + "L".join(f"{n(x)} {n(y)}" for x, y in out) + "Z"

def hexpath(cx, cy, r, flat=True):
    """Hexagon as a short relative path (flat-top by default, pointy-top otherwise)."""
    h = r * 0.8660254
    if flat:
        return f"M{n(cx-r/2)} {n(cy-h)}h{n(r)}l{n(r/2)} {n(h)}l-{n(r/2)} {n(h)}h-{n(r)}l-{n(r/2)}-{n(h)}z"
    return f"M{n(cx)} {n(cy-r)}l{n(h)} {n(r/2)}v{n(r)}l-{n(h)} {n(r/2)}l-{n(h)}-{n(r/2)}v-{n(r)}z"

def hexaflake(cx, cy, R, depth):
    """Recursive hexaflake: each flat-top hexagon -> centre + 6 children at 1/3 scale."""
    if depth == 0:
        return [(cx, cy, R)]
    out, r = [], R / 3
    out += hexaflake(cx, cy, r, depth - 1)
    for k in range(6):
        a = math.radians(60 * k)
        out += hexaflake(cx + 2 * r * math.cos(a), cy + 2 * r * math.sin(a), r, depth - 1)
    return out

def flake_mark(t, cx, cy, R, cls=""):
    """Tiny depth-1 hexaflake: the 'fractal' signature mark (cards, divider)."""
    d = "".join(hexpath(x, y, r * .78) for x, y, r in hexaflake(cx, cy, R, 1))
    return f'<path{cls} d="{d}" fill="{t["accent"]}"/>'

def chip(t, x, y, label, size, h, pad=16, notch=5):
    w = tw(label, size, mono=True) + pad
    return w, (f'<g transform="translate({x:.0f} {y:.0f})"><path d="{cut(w, h, notch, corners="br")}" '
               f'fill="{t["surface2"]}" stroke="{t["border"]}"/><text x="{w/2:.1f}" y="{h/2 + size*0.36:.1f}" '
               f'text-anchor="middle" class="m tx" font-size="{size}">{escape(label)}</text></g>')

def panel(t, W, H, c=14):
    """Chamfered panel (cut top-left + bottom-right) with the accent bracket riding the
    top-left chamfer and a short accent segment on the bottom-right chamfer."""
    a = t["accent"]
    return (f'<path d="{cut(W, H, c)}" fill="{t["surface"]}" stroke="{t["border"]}"/>'
            f'<path d="M1 {c+18}V{c}L{c} 1H{c+40}" fill="none" stroke="{a}" stroke-width="2"/>'
            f'<path d="M{W-c-1} {H-1}L{W-1} {H-c-1}" stroke="{a}" stroke-width="2"/>')

FOCUS = ["AI Agents", "Full-stack", "Cloud", "Web3"]
BANNER_TITLE = "Brayan Roberto Ccarita Cruz (hallzyx)"
BANNER_DESC = ("Software Engineering student in Lima, Peru. Focus areas: AI Agents, Full-stack, Cloud, Web3. "
               "Portfolio: iam.arroz.dev")
BLINK = ".cur{animation:blink 1.2s steps(1) infinite}@keyframes blink{50%{opacity:0}}"

# ---------------------------------------------------------------- banner (wide)
def banner_frame(t, W, H, c):
    """Fragmented chamfered frame: border broken into segments with notched gaps,
    accent brackets riding the two cut corners, ruler ticks, a circuit trace and
    small square data nodes. No solid edge bar."""
    B, A, o = t["border"], t["accent"], .5
    seg = [  # border fragments (gaps at x 600-614 top, y 118-138 right, x 300-318 bottom, y 150-166 left)
        f"M{c+70} {o}H600", f"M614 {o}H{W-o}V118", f"M{W-o} 138V{H-c-50}",
        f"M{W-c-74} {H-o}H318", f"M300 {H-o}H{o}V166", f"M{o} 150V{c+52}",
    ]
    notches = [  # little hooks turning into the gaps
        f"M600 {o}v6", f"M614 {o}v6", f"M{W-o} 118h-6", f"M{W-o} 138h-6",
        f"M318 {H-o}v-6", f"M300 {H-o}v-6", f"M{o} 150h6", f"M{o} 166h6",
    ]
    ticks = "".join(f"M{o} {y}h{8 if i % 2 else 4}" for i, y in enumerate(range(c + 64, 146, 8)))
    ticks += "".join(f"M{W-o} {y}h-{8 if i % 2 else 4}" for i, y in enumerate(range(150, H - c - 54, 8)))
    trace = f"M{W-c-96} {H-12}H760l-14-14H700"            # circuit trace, bottom right
    trace2 = f"M{W-12} 40V70l-12 12v30"                   # short trace, top right
    nodes = "".join(f'<rect x="{x-2.5}" y="{y-2.5}" width="5" height="5"/>' for x, y in
                    ((700, H-26), (W-24, 112), (600, 6.5), (318, H-6.5)))
    return (f'<path d="{"".join(seg)}{"".join(notches)}{ticks}" fill="none" stroke="{B}"/>'
            f'<path d="{trace}{trace2}" fill="none" stroke="{B}"/>'
            f'<g fill="{A}">{nodes}</g>'
            # accent brackets on the chamfers (top-left / bottom-right)
            f'<path d="M{o} {c+40}V{c}L{c} {o}H{c+56}M{W-c-62} {H-o}H{W-c}L{W-o} {H-c}V{H-c-38}" '
            f'fill="none" stroke="{A}" stroke-width="2.5"/>'
            f'<path class="blip" d="M{c+62} {o}h8M{W-c-70} {H-o}h-4" stroke="{A}" stroke-width="2.5"/>')

def banner_art(t, theme, W, H):
    """Right-side fractal: hex tessellation fading in from the left + a depth-2 hexaflake
    (49 cells) around the logo; a few cells are 'lit' and flicker."""
    A = t["accent"]; dark = theme == "dark"
    r = 14; h = r * 0.8660254
    tile = "".join(hexpath(x, y, r) for x, y in ((0, 0), (3*r, 0), (1.5*r, h), (0, 2*h), (3*r, 2*h)))
    cells = hexaflake(800, 150, 138, 2)
    outline = "".join(hexpath(x, y, rr * .9) for x, y, rr in cells)
    lit_idx = (8, 17, 23, 30, 39, 46, 3, 12)
    lit = "".join(f'<path class="fl f{i % 3}" d="{hexpath(*cells[k][:2], cells[k][2] * .9)}"/>' for i, k in enumerate(lit_idx))
    return f"""<defs>
<pattern id="hx" width="{3*r}" height="{n(2*h)}" patternUnits="userSpaceOnUse"><path d="{tile}" fill="none" stroke="{A}" stroke-width=".8"/></pattern>
<linearGradient id="fade" x1="0" x2="1"><stop offset="0" stop-color="{t['bg']}"/><stop offset=".55" stop-color="{t['bg']}" stop-opacity="0"/></linearGradient>
<radialGradient id="glow"><stop offset="0" stop-color="{A}" stop-opacity=".35"/><stop offset="1" stop-color="{A}" stop-opacity="0"/></radialGradient>
</defs>
<g class="side">
<rect x="540" width="{W-540}" height="{H}" fill="url(#hx)" opacity="{'.13' if dark else '.09'}"/>
<rect x="610" y="10" width="380" height="280" fill="url(#glow)" opacity="{'.5' if dark else '.35'}"/>
<rect x="540" width="460" height="{H}" fill="url(#fade)"/>
<path d="{outline}" fill="{t['bg']}" fill-opacity=".55" stroke="{A}" stroke-opacity="{'.42' if dark else '.5'}"/>
<g fill="{A}" opacity="{'.3' if dark else '.22'}">{lit}</g>
<rect class="scan" x="560" y="0" width="2" height="{H}" fill="{A}" opacity="0"/>
</g>"""

def banner(theme):
    t = THEMES[theme]; W, H, C = 1000, 300, 26
    chips, x = [], 48
    for f in FOCUS:
        w, g = chip(t, x, 212, f, 20, 38, pad=30, notch=8); chips.append(g); x += w + 10
    assert x < 600, x
    extra = BLINK + """
.fl{animation:fl 4.8s steps(1) infinite}.f1{animation-delay:-1.6s}.f2{animation-delay:-3.2s}
@keyframes fl{0%,100%{opacity:1}40%{opacity:.25}55%{opacity:1}}
.scan{animation:scan 9s linear infinite}
@keyframes scan{0%{transform:translateX(0);opacity:0}8%{opacity:.5}92%{opacity:.5}100%{transform:translateX(430px);opacity:0}}
.blip{animation:blip 3s steps(1) infinite}@keyframes blip{50%{opacity:.2}}
"""
    assert tw("BRAYAN ROBERTO", 52, True) - 14 < 560
    # Narrow mode (image rendered < 700 CSS px wide, e.g. phones): bigger text, no side art.
    NS = 84
    assert tw("BRAYAN ROBERTO", NS, True) - 28 < W - 80, tw("BRAYAN ROBERTO", NS, True)
    NARROW_FOCUS = "AI Agents · Full-stack · Cloud · Web3"
    assert tw(NARROW_FOCUS, 38) < W - 80, tw(NARROW_FOCUS, 38)
    extra += """
.narrow{display:none}
@media (max-width:700px){.wide{display:none}.narrow{display:inline}.side{display:none}}"""
    ptw = round(tw("<arroz/>_", 56, True, True) + 48)
    body = f"""<defs><clipPath id="clip"><path d="{cut(W, H, C, 0)}"/></clipPath></defs>
<g clip-path="url(#clip)">
<rect width="{W}" height="{H}" fill="{t['bg']}"/>
{banner_art(t, theme, W, H)}
</g>
{banner_frame(t, W, H, C)}
<g class="wide">
<text x="48" y="58" class="m ac" font-size="18" font-weight="700" letter-spacing="2.5">HALLZYX · &lt;ARROZ/&gt;</text>
<text x="48" y="112" class="s tx" font-size="52" font-weight="800" letter-spacing="-1">BRAYAN ROBERTO</text>
<text x="48" y="163" class="s tx" font-size="52" font-weight="800" letter-spacing="-1">CCARITA CRUZ</text>
<text x="48" y="196" class="s mu" font-size="22">Software Engineering student · Lima, Peru</text>
{''.join(chips)}
<text x="48" y="282" class="m mu" font-size="18">→ <tspan class="ac" font-weight="700">iam.arroz.dev</tspan></text>
<g transform="translate(800 160)" text-anchor="middle">
<g transform="translate({n(-ptw/2)} -66)"><path d="{cut(ptw, 128, 12)}" fill="{t['bg']}" fill-opacity=".78" stroke="{t['border']}"/>
<path d="M.5 30V12L12 .5h30M{n(ptw-42)} 127.5h30L{n(ptw-.5)} 116V98" fill="none" stroke="{t['accent']}" stroke-width="2"/>
<path d="M{n(ptw-.5)} 40v48M.5 60v28" stroke="{t['bg']}" stroke-width="3"/></g>
<text class="m ac" font-size="56" font-weight="700">&lt;arroz/&gt;<tspan class="cur">_</tspan></text>
<text y="44" class="m mu" font-size="18" letter-spacing="2">builds end to end</text>
</g>
</g>
<g class="narrow">
<text x="48" y="78" class="m ac" font-size="40" font-weight="700" letter-spacing="2">&lt;ARROZ/&gt;<tspan class="cur">_</tspan></text>
<text x="48" y="160" class="s tx" font-size="{NS}" font-weight="800" letter-spacing="-2">BRAYAN ROBERTO</text>
<text x="48" y="236" class="s tx" font-size="{NS}" font-weight="800" letter-spacing="-2">CCARITA CRUZ</text>
<text x="48" y="282" class="s mu" font-size="38">{NARROW_FOCUS}</text>
</g>"""
    return svg(W, H, BANNER_TITLE, BANNER_DESC, body, t, extra)

# ---------------------------------------------------------------- stats (wide strip + compact 2x2)
STATS = [("110", "public repositories", "per GitHub API"), ("5", "hackathon awards", "per portfolio"),
         ("36", "TypeScript-first", "repos of 92 owned"), ("Oct 12", "latest public push", "2026")]
STATS_DESC = "; ".join(f"{n} {l} ({s})" for n, l, s in STATS) + "."
# Only "additive" motion: every animation's resting frame shows all content, so a
# paused/offscreen image (Chrome does not tick offscreen SVG images) never hides data.
GROW = """.sheen{animation:sheen 3.2s ease-in-out infinite}
@keyframes sheen{0%,100%{opacity:.0}50%{opacity:.35}}"""
STATS2 = [("110", ["public", "repositories"], "per GitHub API"), ("5", ["hackathon", "awards"], "per portfolio"),
          ("36", ["TypeScript-first", "repos"], "of 92 owned"), ("Oct 12", ["latest", "public push"], "2026")]
def stats(theme, H):
    t = THEMES[theme]; W, gap = 400, 12
    w, h = (W - gap) // 2, (H - gap) // 2
    NS = 42
    out = []
    for i, (num, label, sub) in enumerate(STATS2):
        x, y = (i % 2) * (w + gap), (i // 2) * (h + gap)
        inner = w - 34
        assert tw(num, NS, True) <= inner and all(tw(l, 18, True) <= inner for l in label) and tw(sub, 15, mono=True) <= inner, num
        out.append(f'<g transform="translate({x} {y})">{panel(t, w, h)}'
                   f'<path d="M{w-30} 22h12M{w-14} 22h4M{w-14} 22v4" fill="none" stroke="{t["accent"]}" stroke-width="2"/>'
                   f'<text x="{w-34}" y="27" text-anchor="end" class="m mu" font-size="15">0{i+1}</text>'
                   f'<text x="17" y="{h-98}" class="s tx" font-size="{NS}" font-weight="800" letter-spacing="-1">{num}</text>'
                   f'<text x="17" y="{h-64}" class="s tx" font-size="18" font-weight="700">{escape(label[0])}</text>'
                   f'<text x="17" y="{h-41}" class="s tx" font-size="18" font-weight="700">{escape(label[1])}</text>'
                   f'<text x="17" y="{h-17}" class="m mu" font-size="15">{escape(sub)}</text></g>')
    return svg(W, H, "GitHub at a glance", STATS_DESC, "".join(out), t)

# ---------------------------------------------------------------- recent activity timeline
ACTIVITY = [("Oct 12, 2026", "payzap", "price-guarantee demo with PayPal"),
            ("Oct 5, 2026", "hedera-swapy", "treasury template on Hedera testnet"),
            ("Aug 29, 2026", "zeroslop", "finalist at The Next Craft"),
            ("Aug 12, 2026", "alpacto", "winner at Ethereum Lima")]
def timeline(theme, H):
    t = THEMES[theme]; W = 400
    out = [panel(t, W, H), head("RECENT ACTIVITY")]
    y0, step = 84, (H - 84 - 26) / 4
    lx = PAD + 7
    out.append(f'<line x1="{lx}" x2="{lx}" y1="{y0-6}" y2="{y0 + 3*step + 46}" stroke="{t["border"]}" stroke-width="2" stroke-dasharray="14 4"/>')
    for i, (d, repo, what) in enumerate(ACTIVITY):
        y = y0 + i * step
        what = fit_text(what, 17, W - PAD - 32 - PAD)
        repo = fit_text(repo, 18, W - PAD - 28 - PAD, bold=True)
        dm = lambda r: f"M{lx} {y-6-r}l{r} {r}l-{r} {r}l-{r}-{r}z"
        dot = (f'<path d="{hexpath(lx, y-6, 13, False)}" class="ac pulse" opacity=".25"/><path d="{dm(8)}" class="ac"/>'
               if i == 0 else f'<path d="{dm(7)}" fill="{t["surface"]}" stroke="{t["accent"]}" stroke-width="2"/>')
        out.append(f'{dot}<text x="{PAD+28}" y="{y}" class="m ac" font-size="15" font-weight="700">{d}</text>'
                   f'<text x="{PAD+28}" y="{y+25}" class="s tx" font-size="18" font-weight="700">{escape(repo)}</text>'
                   f'<text x="{PAD+28}" y="{y+49}" class="s mu" font-size="17">{escape(what)}</text>')
    extra = ".pulse{animation:pulse 2.4s ease-in-out infinite}@keyframes pulse{0%,100%{opacity:.1}50%{opacity:.4}}"
    desc = " ".join(f"{d}: {r}, {w}." for d, r, w in ACTIVITY)
    return svg(W, H, "Recent activity", desc, "".join(out), t, extra)

# ---------------------------------------------------------------- project cards
PROJECTS = [
    dict(slug="cryptoon", name="Cryptoon", tier="win", badge="WINNER", event="ETH Global Buenos Aires 2025",
         desc="Web3 webtoon reader where creators keep 100% of what they earn: chapters at 0.01 USDC and an agent that buys for you.",
         stack=["TypeScript", "React", "Coinbase AgentKit", "x402", "Base"]),
    dict(slug="alpacto", name="Alpacto", tier="win", badge="WINNER", event="Ethereum Lima Hackathon 2026",
         desc="Fair payments for alpaca fiber with on-chain escrow and an AI auditor, built for producers who never touch crypto.",
         stack=["TypeScript", "Rust", "Arbitrum Stylus", "ZeroDev", "PostgreSQL", "Docker"]),
    dict(slug="licita-verify", name="LicitaVerify", tier="place", badge="2ND PLACE", event="PunaTech 2026",
         desc="Citizen audit of public procurement: milestones anchored on Arkiv and a Telegram agent that answers questions in natural language.",
         stack=["Next.js", "TypeScript", "Arkiv SDK", "MCP", "Python"]),
    dict(slug="zeroslop", name="ZeroSlop", tier="place", badge="FINALIST", event="The Next Craft 2026 · team of 3",
         desc="Checks that whoever opens a PR understands their own change, using a conceptual question and a hypothetical dangerous mutation.",
         stack=["Next.js 16", "TypeScript", "Convex", "Clerk", "MCP"]),
    dict(slug="hedera-swapy", name="Treasury Swap Guard", tier="plain", badge="BOUNTY", event="Scaffold-HBAR Template Bounty",
         desc="Template for DAO treasuries on Hedera: HBAR→token swaps on SaucerSwap with limits, allowlists and contract-enforced approvals.",
         stack=["Solidity", "Next.js", "Hardhat", "wagmi", "Hedera HCS"]),
    dict(slug="payzap", name="PayZap", tier="plain", badge="DEMO", event="Hackathon demo · Oct 2026",
         desc="Shows merchants what refunds will cost them before cutting prices, and honors price guarantees via PayPal Sandbox.",
         stack=["Next.js", "TypeScript", "PayPal API"]),
]
CW, PAD = 400, 22
INNER = CW - 2 * PAD
D_SIZE, D_LH, C_SIZE, C_H, C_GAP = 17, 25, 15, 28, 7

def layout(p):
    lines = wrap(p["desc"], D_SIZE, INNER)
    rows, row, x = [], [], 0
    for s in p["stack"]:
        w = tw(s, C_SIZE, mono=True) + 16
        if x + w > INNER and row:
            rows.append(row); row, x = [], 0
        row.append((s, x)); x += w + 6
    rows.append(row)
    return lines, rows

MAX_LINES = max(len(layout(p)[0]) for p in PROJECTS)
MAX_ROWS = max(len(layout(p)[1]) for p in PROJECTS)
DESC_Y = 138
CHIPS_Y = DESC_Y + (MAX_LINES - 1) * D_LH + 20
FOOT_Y = CHIPS_Y + MAX_ROWS * (C_H + C_GAP) + 10
CARD_H = FOOT_Y + 48

def card(theme, p):
    t = THEMES[theme]; W, H = CW, CARD_H
    lines, rows = layout(p)
    bs = 15
    bw = tw(p["badge"], bs, True, True) + 24
    if p["tier"] == "win":
        pill = (f'<path d="{cut(bw, 28, 8, 0, "tl br")}" fill="{t["accent"]}"/>'
                f'<text x="{bw/2:.1f}" y="19.4" text-anchor="middle" class="m" font-size="{bs}" font-weight="700" fill="{t["ink"]}">{p["badge"]}</text>')
    elif p["tier"] == "place":
        pill = (f'<path d="{cut(bw, 28, 8, .75, "tl br")}" fill="{t["accentsoft"]}" stroke="{t["accent"]}" stroke-width="1.5"/>'
                f'<text x="{bw/2:.1f}" y="19.4" text-anchor="middle" class="m ac" font-size="{bs}" font-weight="700">{p["badge"]}</text>')
    else:
        pill = (f'<path d="{cut(bw, 28, 8, .5, "tl br")}" fill="{t["surface2"]}" stroke="{t["border"]}"/>'
                f'<text x="{bw/2:.1f}" y="19.4" text-anchor="middle" class="m mu" font-size="{bs}" font-weight="700">{p["badge"]}</text>')
    assert tw(p["name"], 26, True) <= INNER, p["name"]
    assert tw(p["event"], 17, True) <= INNER, p["event"]
    slug = "hallzyx/" + p["slug"]
    assert tw(slug, 15, mono=True) + 14 + tw("REPO →", 15, True, True) <= INNER, slug
    desc = "".join(f'<text x="{PAD}" y="{DESC_Y + i*D_LH}" class="s mu" font-size="{D_SIZE}">{escape(l)}</text>'
                   for i, l in enumerate(lines))
    chips = "".join(chip(t, PAD + x, CHIPS_Y + r * (C_H + C_GAP), s, C_SIZE, C_H)[1]
                    for r, row in enumerate(rows) for s, x in row)
    ev_class = "ac" if p["tier"] != "plain" else "tx"
    body = f"""{panel(t, W, H)}
{flake_mark(t, W-PAD-14, 36, 14)}
<g transform="translate({PAD} 22)">{pill}</g>
<text x="{PAD}" y="84" class="s tx" font-size="26" font-weight="800" letter-spacing="-.3">{escape(p['name'])}</text>
<text x="{PAD}" y="109" class="s {ev_class}" font-size="17" font-weight="700">{escape(p['event'])}</text>
{desc}
{chips}
<path d="M{PAD} {FOOT_Y}H{W-PAD-40}M{W-PAD-32} {FOOT_Y}H{W-PAD}" stroke="{t['border']}"/><path d="M{W-PAD-40} {FOOT_Y}v-5M{W-PAD-32} {FOOT_Y}v-5" stroke="{t['border']}"/>
<text x="{PAD}" y="{FOOT_Y+30}" class="m mu" font-size="15">{slug}</text>
<text x="{W-PAD}" y="{FOOT_Y+30}" text-anchor="end" class="m ac" font-size="15" font-weight="700">REPO →</text>"""
    title = f"{p['name']} · {p['badge'].capitalize()}, {p['event']}"
    return svg(W, H, title, f"{p['desc']} Stack: {', '.join(p['stack'])}.", body, t)

# ---------------------------------------------------------------- languages + stack (equal-height 400px panels)
LANGS = [("TypeScript", 85.9, "#3178c6"), ("JavaScript", 4.0, "#f1e05a"), ("CSS", 3.8, "#663399"),
         ("Rust", 2.9, "#dea584"), ("Solidity", 1.1, "#aa6746"), ("Other", 2.2, "#8b949e")]
STACK = [
    ("AI AGENTS", ["Coinbase AgentKit", "MCP", "x402"]),
    ("FULL-STACK", ["TypeScript", "React", "Next.js", "Convex", "Clerk", "Python"]),
    ("WEB3", ["Solidity", "Rust", "Arbitrum Stylus", "Hardhat", "wagmi", "Base", "Hedera"]),
    ("DATA · INFRA · APIS", ["PostgreSQL", "Docker", "PayPal API"]),
]
PW = 400
def head(label):
    assert tw(label, 16, True, True) + len(label) * 1.2 <= PW - 2 * PAD, label
    return f'<text x="{PAD}" y="40" class="m mu" font-size="16" font-weight="700" letter-spacing="1.2">{label}</text>'

def stack_body(t):
    out, y = [], 76
    for label, items in STACK:
        out.append(f'<text x="{PAD}" y="{y}" class="m ac" font-size="15" font-weight="700" letter-spacing="1.2">{label}</text>')
        y += 10; x = 0
        for s in items:
            w = tw(s, C_SIZE, mono=True) + 16
            if x + w > PW - 2 * PAD:
                x = 0; y += C_H + C_GAP
            out.append(chip(t, PAD + x, y, s, C_SIZE, C_H)[1]); x += w + 6
        y += C_H + 30
    return "".join(out), y - 30 + 22

def lang_body(t):
    bx, bw, by = PAD, PW - 2 * PAD, 62
    segs, x = [], bx
    for n, pct, c in LANGS:
        w = bw * pct / 100
        segs.append(f'<rect x="{x:.2f}" y="{by}" width="{max(w - 2, 1.5):.2f}" height="16" fill="{c}"/>'); x += w
    rows = []
    for i, (n, pct, c) in enumerate(LANGS):
        ly = 128 + i * 36
        mini = (PW - 2 * PAD - 210) * pct / 100
        rows.append(f'<rect x="{PAD+1}" y="{ly-12}" width="12" height="12" fill="{c}"/>'
                    f'<text x="{PAD+24}" y="{ly}" class="s tx" font-size="17">{escape(fit_name(n))}</text>'
                    f'<rect x="{PAD+136}" y="{ly-9}" width="{max(mini, 3):.1f}" height="6" fill="{c}"/>'
                    f'<text x="{PW-PAD}" y="{ly}" text-anchor="end" class="m mu" font-size="17">{pct:.1f}%</text>')
    bar = cut(bw, 16, 6, 0, "tl br")
    body = (f'<defs><clipPath id="bc"><path transform="translate({bx} {by})" d="{bar}"/></clipPath></defs>'
            f'<path transform="translate({bx} {by})" d="{bar}" fill="{t["surface2"]}"/>'
            f'<g clip-path="url(#bc)">{"".join(segs)}<rect class="sheen" x="{bx}" y="{by}" width="{bw}" height="8" fill="#ffffff" opacity="0"/></g>{"".join(rows)}')
    return body, 128 + 5 * 36

NOTE = ["Share of code by size across all", "owned repos (forks excluded)."]
for _l in NOTE:
    assert tw(_l, 16) <= PW - 2 * PAD, _l

SHORT_LANG = {"Jupyter Notebook": "Jupyter", "Vim Script": "Vim", "Objective-C": "Obj-C", "Dockerfile": "Docker"}
def fit_name(name, maxw=106):
    """Language names must stay clear of the mini bar that starts at x = PAD+136."""
    name = SHORT_LANG.get(name, name)
    base = name
    while tw(name, 17) > maxw and len(base) > 1:
        base = base[:-1]
        name = base + "…"
    return name

def panels(theme):
    t = THEMES[theme]
    sb, sh = stack_body(t)
    lb, lh = lang_body(t)
    H = max(sh, lh + 78) + 4
    lang = svg(PW, H, "Languages across all repositories",
               "; ".join(f"{n} {p:.1f}%" for n, p, _ in LANGS) + ". Share of code by size across all owned repositories, forks excluded.",
               f'{panel(t, PW, H)}{head("LANGUAGES · ALL REPOS")}{lb}'
               f'<text x="{PAD}" y="{H-44}" class="s mu" font-size="16">{NOTE[0]}</text>'
               f'<text x="{PAD}" y="{H-21}" class="s mu" font-size="16">{NOTE[1]}</text>', t, GROW)
    stk = svg(PW, H, "Tech stack used in featured projects",
              " ".join(f"{l.title()}: {', '.join(i)}." for l, i in STACK),
              f'{panel(t, PW, H)}{head("STACK · IN THE PROJECTS")}{sb}', t)
    return lang, stk, H

# ---------------------------------------------------------------- footer divider (decorative)
def divider(theme):
    t = THEMES[theme]; W, H = 600, 60
    A, B = t["accent"], t["border"]
    extra = ".pulse{animation:pulse 2.4s ease-in-out infinite}@keyframes pulse{0%,100%{opacity:.45}50%{opacity:1}}"
    pw = 200; px = W / 2 - pw / 2
    # broken line: fragments shrink toward the edges, with square nodes at the breaks
    frag, nodes, xs = [], [], [(px - 12, 70), (px - 96, 46), (px - 158, 28), (px - 204, 14), (px - 236, 6)]
    for x2, L in xs:
        frag.append(f"M{n(x2-L)} 30h{L}M{n(W-x2)} 30h{L}")
    for x2, L in xs[:3]:
        nodes.append(f'<rect x="{n(x2-L-7)}" y="28" width="4" height="4"/><rect x="{n(W-x2+L+3)}" y="28" width="4" height="4"/>')
    body = f"""<path d="{''.join(frag)}" stroke="{A}" stroke-width="2" opacity=".7"/>
<g fill="{A}" opacity=".7">{''.join(nodes)}</g>
<path transform="translate({n(px)} 8)" d="{cut(pw, 44, 10)}" fill="{t['bg']}" stroke="{B}"/>
<path transform="translate({n(px)} 8)" d="M.5 18V10L10 .5h18M{pw-28} 43.5h18L{pw-.5} 34v-8" fill="none" stroke="{A}" stroke-width="2"/>
<g class="pulse">{flake_mark(t, px + 30, 30, 12)}</g>
<text x="{n(W/2+14)}" y="37" text-anchor="middle" class="m ac" font-size="21" font-weight="700">&lt;arroz/&gt;</text>"""
    return svg(W, H, "arroz", "Decorative divider with the arroz logo.", body, t, extra)

# ---------------------------------------------------------------- link bars (action strip docked under each card)
LINKS = [
    dict(slug="cryptoon", name="Cryptoon", short="CRYPTOON", label="SHOWCASE", host="ethglobal.com/showcase"),
    dict(slug="alpacto", name="Alpacto", short="ALPACTO", label="LIVE DEMO", host="alpacto.arroz.dev"),
    dict(slug="licita-verify", name="LicitaVerify", short="LICITAVERIFY", label="LIVE DEMO", host="licita-verify.arroz.dev"),
    dict(slug="zeroslop", name="ZeroSlop", short="ZEROSLOP", label="CASE STUDY", host="iam.arroz.dev/projects/zeroslop"),
    dict(slug="hedera-swapy", name="Treasury Swap Guard", short="SWAP GUARD", label="TESTNET", host="hedera-swapy/TESTNET_EVIDENCE.md"),
    dict(slug="payzap", name="PayZap", short="PAYZAP", label="README", host="github.com/hallzyx/payzap#readme"),
]

def linkbar(theme, L):
    t = THEMES[theme]; W, H, CAP, c = 400, 58, 58, 10
    A, B = t["accent"], t["border"]
    TX = 40
    assert tw(L["host"], 14, mono=True) <= W - CAP - 14 - TX, L["host"]
    full = L["short"] + " · " + L["label"]
    assert tw(full, 16, True, True) + len(full) * 1.2 <= W - CAP - 40 - TX, full
    cx, cy = W - CAP / 2 - 1, H / 2
    # arrow drawn as a path (no dependency on a font having U+2197)
    arrow = (f'<path d="M{n(cx-8)} {n(cy+8)}L{n(cx+8)} {n(cy-8)}M{n(cx-5)} {n(cy-8)}H{n(cx+8)}V{n(cy+5)}" '
             f'fill="none" stroke="{t["ink"]}" stroke-width="3"/>')
    # "//" tick marks just left of the cap, the same slash vocabulary as the banner traces
    slashes = "".join(f"M{W-CAP-27+i*9} {H-17}l8-12" for i in range(2))
    extra = ".nd{animation:nd 2.4s ease-in-out infinite}@keyframes nd{0%,100%{opacity:.45}50%{opacity:1}}"
    body = f"""<path d="{cut(W, H, c, corners='tl br')}" fill="{t['surface']}" stroke="{B}"/>
<path d="M1 {c+16}V{c}L{c} 1H{c+34}" fill="none" stroke="{A}" stroke-width="2"/>
<path d="M{W-CAP} .5H{W-.5}V{H-c-.5}L{W-c-.5} {H-.5}H{W-CAP}Z" fill="{A}"/>
{arrow}
<rect class="nd" x="22" y="14" width="8" height="8" fill="{A}"/>
<text x="{TX}" y="23" class="m tx" font-size="16" font-weight="700" letter-spacing="1.2">{L['short']}<tspan class="mu"> · </tspan><tspan class="ac">{L['label']}</tspan></text>
<text x="{TX}" y="44" class="m mu" font-size="14">{escape(L['host'])}</text>
<path d="{slashes}" stroke="{B}" stroke-width="2"/>"""
    return svg(W, H, f"{L['name']}: {L['label'].title()}", f"Opens {L['host']}.", body, t, extra)


# ---------------------------------------------------------------- static badges
BH, BC, FS, LS = 28, 8, 11.5, 1.3
GH = ("M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z")

def badge(name, label, msg, label_bg, msg_bg, label_fg, msg_fg, logo=False, title="", desc=""):
    H, C = BH, BC
    pad = 14
    lw_text = tw(label, FS, True) + len(label) * LS
    logo_w = 22 if logo else 0
    wl = pad + logo_w + lw_text + pad
    wm = pad + tw(msg, FS, True) + len(msg) * LS + pad
    W = round(wl + wm)
    wl = round(wl)
    ty = H / 2 + FS * 0.36
    left = f"M{C} 0H{wl}V{H}H0V{C}Z"
    right = f"M{wl} 0H{W}V{H-C}L{W-C} {H}H{wl}Z"
    logo_el = ""
    if logo:
        s = 14 / 16
        logo_el = f'<path transform="translate({pad} {(BH-14)/2:.1f}) scale({s})" d="{GH}" fill="{label_fg}"/>'
    tx = pad + logo_w
    body = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d">\n'
            f'<title id="t">{escape(title)}</title>\n<desc id="d">{escape(desc)}</desc>\n'
            f'<path d="{left}" fill="{label_bg}"/><path d="{right}" fill="{msg_bg}"/>\n{logo_el}'
            f'<text x="{tx}" y="{ty:.1f}" fill="{label_fg}" font-family="{SANS}" font-size="{FS}" font-weight="700" letter-spacing="{LS}">{escape(label)}</text>\n'
            f'<text x="{wl + pad}" y="{ty:.1f}" fill="{msg_fg}" font-family="{SANS}" font-size="{FS}" font-weight="700" letter-spacing="{LS}">{escape(msg)}</text>\n</svg>\n')
    open(os.path.join(OUT, name), "w", encoding="utf-8").write(body)


def write_badges():
    # Portfolio: dark label with white text, logo-cyan message with near-black ink (~14.7:1)
    badge("badge-portfolio.svg", "PORTFOLIO", "IAM.ARROZ.DEV", "#151c21", "#00fcfc", "#ffffff", "#021316",
          title="Portfolio: iam.arroz.dev", desc="Link to the portfolio at iam.arroz.dev.")
    badge("badge-github.svg", "GITHUB", "HALLZYX", "#555555", "#2b323a", "#ffffff", "#ffffff", logo=True,
          title="GitHub: hallzyx", desc="Link to the GitHub profile hallzyx.")

# ---------------------------------------------------------------- live data (GitHub API)
USER = "hallzyx"
AWARDS = 5  # hackathon awards, per the portfolio: not available from the API

# One-line text per repo for the "recent activity" timeline. Priority: this dict (add a line to
# override any repo), then the repo's GitHub description, then "<language> project".
# Repos listed in ACTIVITY_EXCLUDE never show up in the timeline.
ACTIVITY_EXCLUDE = {USER}  # the profile repo: this very workflow commits to it
ACTIVITY_BLURBS = {
    "cryptoon": "winner at ETH Global Buenos Aires",
    "alpacto": "winner at Ethereum Lima",
    "licita-verify": "2nd place at PunaTech",
    "zeroslop": "finalist at The Next Craft",
    "hedera-swapy": "treasury template on Hedera testnet",
    "payzap": "price-guarantee demo with PayPal",
}

# GitHub's own linguist colours for the languages most likely to show up.
LANG_COLORS = {
    "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Python": "#3572a5", "Java": "#b07219", "Kotlin": "#a97bff",
    "Rust": "#dea584", "Go": "#00add8", "C": "#555555", "C++": "#f34b7d", "C#": "#178600", "HTML": "#e34c26",
    "CSS": "#663399", "SCSS": "#c6538c", "Shell": "#89e051", "Solidity": "#aa6746", "Dart": "#00b4ab",
    "Swift": "#f05138", "PHP": "#4f5d95", "Ruby": "#701516", "Vue": "#41b883", "Svelte": "#ff3e00",
    "Astro": "#ff5a03", "Jupyter Notebook": "#da5b0b", "Dockerfile": "#384d54", "Lua": "#000080",
    "Makefile": "#427819", "MDX": "#fcb32c", "PLpgSQL": "#336790", "TeX": "#3d6117", "Move": "#4a137a",
    "Batchfile": "#c1f12e", "PowerShell": "#012456", "Objective-C": "#438eff", "R": "#198ce7", "Scala": "#c22d40",
    "Elixir": "#6e4a7e", "Haskell": "#5e5086", "Zig": "#ec915c", "Nix": "#7e7eff", "GLSL": "#5686a5",
    "WGSL": "#1a5e9a", "Cairo": "#ff4a48", "Vim Script": "#199f4b", "CMake": "#da3434", "Handlebars": "#f7931e",
}
OTHER_COLOR = "#8b949e"

_SYMBOLS = re.compile("[\u2600-\u27bf\U0001f000-\U0001faff\ufe0f\u200d]")

def clean_text(text):
    """One tidy line: no emoji or control characters, single spaces."""
    text = _SYMBOLS.sub("", text or "")
    text = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    return re.sub(r"\s+", " ", text).strip()

def fit_text(text, size, maxw, bold=False, mono=False):
    """Shorten at a word boundary with an ellipsis so the text fits maxw pixels."""
    text = clean_text(text)
    if tw(text, size, bold, mono) <= maxw:
        return text
    base = text
    while base and tw(base.rstrip() + "…", size, bold, mono) > maxw:
        base = base[:-1]
    if " " in base and base.rfind(" ") > len(base) * 0.5:
        base = base[:base.rfind(" ")]
    return base.rstrip(" ,;:-·") + "…"

def md_escape(text):
    return re.sub(r"([\\`*_{}\[\]<>#|~])", r"\\\1", text)

def api(path, token=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hallzyx-profile-bot"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request("https://api.github.com" + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def load_data(path=None):
    if path:
        d = json.load(open(path, encoding="utf-8"))
        return d["u"], d["r"], d.get("langs")  # "langs": {repo: {language: bytes}} is optional offline
    token = os.environ.get("GITHUB_TOKEN")
    user = api(f"/users/{USER}", token)
    repos, page = [], 1
    while True:
        batch = api(f"/users/{USER}/repos?per_page=100&type=owner&sort=pushed&page={page}", token)
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    return user, repos, None

def fetch_languages(repos):
    """Sum the bytes per language over every owned (non-fork) repo except the profile repo."""
    token = os.environ.get("GITHUB_TOKEN")
    totals, ok = {}, 0
    for r in repos:
        if r.get("fork") or r["name"] == USER:
            continue
        try:
            data = api(f"/repos/{USER}/{r['name']}/languages", token)
        except Exception as e:  # one broken repo must not break the whole refresh
            print(f"::warning::languages of {r['name']} not available: {e}")
            continue
        ok += 1
        for lang, size in data.items():
            totals[lang] = totals.get(lang, 0) + size
    assert ok and totals, "no language data could be read"
    return totals

def lang_rows(totals):
    """Top 5 languages by bytes plus 'Other', as (name, percent, colour)."""
    total = sum(totals.values())
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    rows = [(n, b * 100 / total, LANG_COLORS.get(n, OTHER_COLOR)) for n, b in ranked[:5]]
    rows.append(("Other", sum(b for _, b in ranked[5:]) * 100 / total, OTHER_COLOR))
    return rows

def day(iso):
    d = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    return f"{d:%b} {d.day}", str(d.year)

def compute(user, repos):
    own = [r for r in repos if not r.get("fork")]
    ts = [r for r in own if r.get("language") == "TypeScript"]
    # the profile repo is excluded from "latest push": this very workflow commits to it
    last = max((r for r in own if r["name"] != USER), key=lambda r: r["pushed_at"])
    md, yr = day(last["pushed_at"])
    stats = [(str(user["public_repos"]), "public repositories", "per GitHub API"),
             (str(AWARDS), "hackathon awards", "per portfolio"),
             (str(len(ts)), "TypeScript-first", f"repos of {len(own)} owned"),
             (md, "latest public push", yr)]
    stats2 = [(str(user["public_repos"]), ["public", "repositories"], "per GitHub API"),
              (str(AWARDS), ["hackathon", "awards"], "per portfolio"),
              (str(len(ts)), ["TypeScript-first", "repos"], f"of {len(own)} owned"),
              (md, ["latest", "public push"], yr)]
    pool = [r for r in own if r["name"] not in ACTIVITY_EXCLUDE]
    recent = sorted(pool, key=lambda r: r["pushed_at"], reverse=True)[:4]
    assert len(recent) == 4, "expected at least 4 public repos for the activity timeline"
    activity = []
    for r in recent:
        m, y = day(r["pushed_at"])
        what = ACTIVITY_BLURBS.get(r["name"]) or clean_text(r.get("description")).rstrip(".") \
            or (f"{r['language']} project" if r.get("language") else "Public repository")
        activity.append((f"{m}, {y}", r["name"], fit_text(what, 17, 400 - 22 - 32 - 22)))
    return stats, stats2, activity, user["public_repos"], len(ts), len(own), f"{md}, {yr}"

# ---------------------------------------------------------------- README text that mirrors the data
def patch_readme(stats, activity, pub, ts, own, last_push, today, langs=None):
    p = ROOT / "README.md"
    md = p.read_text(encoding="utf-8")
    def sub(pattern, repl, text, flags=0):
        new, k = re.subn(pattern, lambda m: repl, text, count=0, flags=flags)
        if k == 0:
            print(f"::warning::README pattern not found, left untouched: {pattern}")
        return new
    md = sub(r'alt="GitHub at a glance:[^"]*"',
             f'alt="GitHub at a glance: {pub} public repositories (per GitHub API), {AWARDS} hackathon awards (per portfolio), '
             f'{ts} repos with TypeScript as main language out of {own} owned, latest public push {last_push}."', md)
    md = sub(r'alt="Recent activity:[^"]*"',
             'alt="Recent activity: ' + html.escape(" ".join(f"{d}, {r}: {w}." for d, r, w in activity), quote=True) + '"', md)
    items = "\n".join(f"- **{d}** · [{md_escape(r)}](https://github.com/{USER}/{r}): {md_escape(w)}" for d, r, w in activity)
    md2, k = re.subn(r'(<summary>Recent activity as text</summary>\n\n).*?(\n\n</details>)',
                     lambda m: m.group(1) + items + m.group(2), md, flags=re.S)
    if k == 0:
        print("::warning::README activity list not found, left untouched")
    md = md2
    md = sub(r'Data verified on [A-Z][a-z]{2} \d{1,2}, \d{4}', f"Data verified on {today}", md)
    if langs:
        md = sub(r'alt="Languages (?:in featured projects|across all owned repositories)[^"]*"',
                 'alt="Languages across all owned repositories, by code size (forks excluded): '
                 + ", ".join(f"{n} {p:.1f}%" for n, p, _ in langs) + '."', md)
        md = sub(r'Languages measured over [^<\n]*', "Languages measured over all owned repositories", md)
    p.write_text(md, encoding="utf-8")

def main():
    global STATS, STATS2, STATS_DESC, ACTIVITY, LANGS, NOTE
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="offline JSON {u: user, r: repos} instead of the live API")
    args = ap.parse_args()
    user, repos, offline_langs = load_data(args.data)
    STATS, STATS2, ACTIVITY, pub, ts, own, last_push = compute(user, repos)
    if args.data:
        totals = {}
        for per_repo in (offline_langs or {}).values():
            for lang, size in per_repo.items():
                totals[lang] = totals.get(lang, 0) + size
    else:
        totals = fetch_languages(repos)
    langs = None
    if totals:
        langs = LANGS = lang_rows(totals)
        NOTE = ["Share of code by size across all", f"{own} owned repos (forks excluded)."]
        for _l in NOTE:
            assert tw(_l, 16) <= PW - 2 * PAD, _l
    STATS_DESC = "; ".join(f"{n} {l} ({s})" for n, l, s in STATS) + "."
    t = datetime.now(timezone.utc)
    today = f"{t:%b} {t.day}, {t.year}"
    for th in THEMES:
        for L in LINKS:
            write(f"link-{L['slug']}-{th}.svg", linkbar(th, L))
        write(f"banner-{th}.svg", banner(th))
        for p in PROJECTS:
            write(f"card-{p['slug']}-{th}.svg", card(th, p))
        lang, stk, ph = panels(th)
        write(f"languages-{th}.svg", lang)
        write(f"stack-{th}.svg", stk)
        write(f"stats-{th}.svg", stats(th, ph))
        write(f"activity-{th}.svg", timeline(th, ph))
        write(f"divider-{th}.svg", divider(th))
    write_badges()
    patch_readme(STATS, ACTIVITY, pub, ts, own, last_push, today, langs)
    print(f"ok: {pub} public repos, {ts}/{own} TypeScript-first, latest push {last_push}, "
          f"activity: {', '.join(r for _, r, _ in ACTIVITY)}")
    if langs:
        print("languages: " + ", ".join(f"{n} {p:.1f}%" for n, p, _ in langs))

if __name__ == "__main__":
    main()
