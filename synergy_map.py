"""Archetype synergy maps from 17Lands data.

For each main archetype of a set, plot every playable card on two axes:

  Y (win rate): the card's GIH WR *inside this archetype*, relative to the
     archetype's own win rate. Above the middle = the card wins more than the
     archetype's average deck.
  X (synergy):  the card's overall GIH WR minus its GIH WR in this archetype.
     Middle = the card performs the same here as everywhere. To the RIGHT = it
     performs worse in this archetype (poor fit); to the LEFT = better (synergy).

A 55% card in a 55% archetype with 55% WR in that archetype sits dead center.

Plus one global map of all cards:

  Y: the card's overall GIH WR relative to the format's average win rate.
  X: archetype dependence -- the gap between the card's best and worst win rate
     across the main archetypes it fits. Right = depends a lot on the deck.

Usage:  py synergy_map.py TRK [--period LAST_WEEK] [--min-games 150]
Output: output/<SET>/index.html (interactive, click a card to zoom) + one PNG per chart
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
from matplotlib.offsetbox import AnnotationBbox, OffsetImage
from PIL import Image

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
HEADERS = {"User-Agent": "archetype-synergy-map/0.1", "Accept": "application/json"}
CARD_DATA_URL = "https://www.17lands.com/api/card_data"
COLORS_URL = "https://www.17lands.com/color_ratings/data"
TTL = 3600  # 17Lands refreshes through the day; refetch after an hour
# 17Lands time windows (same options as the site's "Date range" filter).
PERIODS = ["ALL_TIME", "LAST_DAY", "LAST_WEEK", "LAST_TWO_WEEKS", "LATEST_EVENT",
           "LAST_TWO_EVENTS", "FIRST_WEEK", "ALL_EXCEPT_FIRST_WEEK"]
COLOR_HEX = {"W": "#c9b26b", "U": "#2f7bd1", "B": "#4a4a4a", "R": "#d0412f",
             "G": "#2e8b47", "M": "#b8860b", "C": "#8a8a8a"}


# ---------------------------------------------------------------- 17Lands data

def fetch(url: str, params: dict, cache_name: str, fresh: bool) -> list:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{cache_name}.json"
    if not fresh and path.exists() and time.time() - path.stat().st_mtime < TTL:
        return json.loads(path.read_text(encoding="utf-8"))
    resp = requests.get(url, params=params, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list):
        data = []
    path.write_text(json.dumps(data), encoding="utf-8")
    time.sleep(0.5)  # be polite to 17Lands
    return data


def base_params(set_code: str, fmt: str, period: str) -> dict:
    return {"expansion": set_code.upper(), "event_type": fmt, "time_period": period}


def archetypes(set_code, fmt, period, fresh, min_share):
    """Main archetypes [(short, name, win_rate, share)] and the format's average WR."""
    rows = fetch(COLORS_URL, {**base_params(set_code, fmt, period), "combine_splash": "true"},
                 f"colors_{set_code}_{fmt}_{period}", fresh)
    rows = [r for r in rows if not r.get("is_summary") and r.get("games")]
    all_games = sum(r["games"] for r in rows) or 1
    fmt_avg = sum(r["wins"] for r in rows) / all_games
    multi = [r for r in rows
             if len([c for c in str(r.get("short_name", "")) if c in "WUBRG"]) >= 2]
    total = sum(r["games"] for r in multi) or 1
    multi = [r for r in multi if r["games"] / total >= min_share]
    multi.sort(key=lambda r: r["games"], reverse=True)
    return [(r["short_name"], r.get("color_name", r["short_name"]),
             r["wins"] / r["games"], r["games"] / total) for r in multi], fmt_avg


def card_rates(set_code, fmt, period, fresh, colors=None):
    # The old card_ratings endpoint silently ignores the colors filter; the
    # /api/card_data endpoint (used by 17lands.com itself) honors it, but only
    # with colors in WUBRG order.
    params = base_params(set_code, fmt, period)
    if colors:
        colors = "".join(c for c in "WUBRG" if c in colors)
        params["colors"] = colors
    rows = fetch(CARD_DATA_URL, params,
                 f"cards2_{set_code}_{fmt}_{period}_{colors or 'all'}", fresh)
    out = {}
    for r in rows:
        wr, n = r.get("ever_drawn_win_rate"), r.get("ever_drawn_game_count") or 0
        est = False
        if wr is None:
            wr, est = estimate_gih(r), True
        if r.get("name") and wr is not None:
            out[r["name"]] = {"wr": wr, "games": n, "color": r.get("color") or "",
                              "rarity": r.get("rarity", ""), "est": est,
                              "img": r.get("url") or None}
    return out


def estimate_gih(r: dict) -> float | None:
    """Rebuild a GIH WR that 17Lands hid (it hides any rate under 500 games).

    Games with the card in the deck = games where it was drawn + games where it
    never was, so when the in-deck WR and the never-drawn WR are both shown:
    GIH wins = in-deck wins - never-drawn wins.
    """
    gp_wr, gp_n = r.get("win_rate"), r.get("game_count") or 0
    nd_wr, nd_n = r.get("never_drawn_win_rate"), r.get("never_drawn_game_count") or 0
    if gp_wr is None or nd_wr is None or gp_n <= nd_n:
        return None
    return min(1.0, max(0.0, (gp_wr * gp_n - nd_wr * nd_n) / (gp_n - nd_n)))


def fits(card_color: str, arch: str) -> bool:
    return all(c in arch for c in card_color)  # colorless always fits


# ---------------------------------------------------------------- Scryfall images

def _safe(name: str) -> str:
    return re.sub(r"[^\w\- ]", "_", name)


_IMAGES: dict[str, dict] = {}  # name -> {'small': url, 'normal': url}, from load_images()


def _scryfall_post(identifiers: list[dict]) -> dict:
    """POST /cards/collection, retrying when Scryfall rate-limits us."""
    for attempt in range(6):
        resp = requests.post("https://api.scryfall.com/cards/collection",
                             json={"identifiers": identifiers}, headers=HEADERS, timeout=60)
        time.sleep(0.2)  # Scryfall asks for well under 10 requests/second
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    return {"data": [], "not_found": identifiers}


def _uris(card: dict) -> dict | None:
    uris = card.get("image_uris") or (card.get("card_faces") or [{}])[0].get("image_uris")
    return {"small": uris["small"], "normal": uris["normal"]} if uris else None


def load_images(names, set_code: str, fallback: dict[str, str | None]) -> int:
    """Look up card images on Scryfall, 75 cards per request, cached on disk.

    Only successful lookups are cached, so a bad run never sticks. Cards Scryfall
    can't find fall back to the 17Lands card image. Returns how many have none.
    """
    d = CACHE / "scryfall" / set_code
    d.mkdir(parents=True, exist_ok=True)
    missing = []
    for name in names:
        path = d / f"{_safe(name)}.json"
        info = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if info:
            _IMAGES[name] = info
        else:
            missing.append(name)

    def lookup(batch: list[str], with_set: bool) -> list[str]:
        ids = [{"name": n, "set": set_code.lower()} if with_set else {"name": n} for n in batch]
        res = _scryfall_post(ids)
        found = {}
        for card in res.get("data", []):
            info = _uris(card)
            if not info:
                continue
            # Match on the full name and on each face ("A // B", "A", "B").
            for key in [card["name"], *[f["name"] for f in card.get("card_faces", [])]]:
                found.setdefault(key, info)
        still = []
        for n in batch:
            info = found.get(n) or found.get(n.split(" // ")[0])
            if info:
                _IMAGES[n] = info
                (d / f"{_safe(n)}.json").write_text(json.dumps(info), encoding="utf-8")
            else:
                still.append(n)
        return still

    for i in range(0, len(missing), 75):
        batch = lookup(missing[i:i + 75], with_set=True)
        if batch:  # e.g. guest cards printed in another set
            lookup(batch, with_set=False)

    none = 0
    for name in names:
        if name not in _IMAGES:
            if fallback.get(name):
                _IMAGES[name] = {"small": fallback[name], "normal": fallback[name]}
            else:
                none += 1
    return none


def scryfall_images(name: str, set_code: str) -> dict | None:
    """{'small': url, 'normal': url} for a card (after load_images)."""
    return _IMAGES.get(name)


def card_image(name: str, set_code: str):
    """Small card image as an array for the PNG charts (None if unavailable)."""
    d = CACHE / "images" / set_code
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe(name)}.jpg"
    if not path.exists():
        info = scryfall_images(name, set_code)
        if not info:
            return None
        resp = requests.get(info["small"], headers={"User-Agent": HEADERS["User-Agent"]},
                            timeout=30)
        if not resp.ok:
            return None
        path.write_bytes(resp.content)
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


# ---------------------------------------------------------------- charts

def plot_png(chart: dict, out_png: Path, set_code: str) -> None:
    pts = chart["points"]
    fig, ax = plt.subplots(figsize=(18, 12), dpi=120)
    ax.axhline(0, color="#999", lw=1)
    ax.axvline(0, color="#999", lw=1)
    zoom = 0.42 if len(pts) <= 60 else 0.3
    # Draw bottom cards first so higher-win-rate cards sit on top of overlaps.
    for i, p in enumerate(sorted(pts, key=lambda p: p["y"])):
        img = card_image(p["n"], set_code)
        if img is None:
            col = p["c"]
            hexc = COLOR_HEX["C"] if not col else COLOR_HEX["M"] if len(col) > 1 else COLOR_HEX[col]
            ax.scatter(p["x"], p["y"], s=60, color=hexc, edgecolor="black", lw=0.5, zorder=3 + i)
            ax.annotate(p["n"], (p["x"], p["y"]), xytext=(4, 3), textcoords="offset points",
                        fontsize=7, zorder=3 + i)
            continue
        ax.add_artist(AnnotationBbox(OffsetImage(img, zoom=zoom), (p["x"], p["y"]),
                                     frameon=False, pad=0, zorder=3 + i))
    ax.set_xlim(chart["xmin"], chart["xmax"])
    ax.set_ylim(chart["ymin"], chart["ymax"])
    ax.set_xlabel(f"{chart['xlabel']}\n{chart['xhint']}")
    ax.set_ylabel(chart["ylabel"])
    ax.set_title(f"{chart['title']} — {chart['subtitle']}")
    kw = dict(transform=ax.transAxes, fontsize=10, color="#555", alpha=0.8)
    tl, tr, bl, br = chart["quads"]
    ax.text(0.01, 0.98, tl, va="top", **kw)
    ax.text(0.99, 0.98, tr, va="top", ha="right", **kw)
    ax.text(0.01, 0.01, bl, **kw)
    ax.text(0.99, 0.01, br, ha="right", **kw)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)


def point(name, x, y, card, overall, arch_stats, set_code, **extra) -> dict:
    img = scryfall_images(name, set_code) or {}
    return {"n": name, "x": round(x, 2), "y": round(y, 2), "c": card["color"],
            "r": card["rarity"], "s": img.get("small"), "l": img.get("normal"),
            "wr": overall["wr"], "g": overall["games"], "e": overall["est"],
            "arch": arch_stats.get(name, {}), **extra}


def archetype_chart(short, name, arch_wr, share, cards, overall, arch_stats,
                    min_games, set_code) -> dict | None:
    pts = []
    for cname, c in cards.items():
        o = overall.get(cname)
        if not o or c["games"] < min_games or not fits(c["color"], short):
            continue
        x = (o["wr"] - c["wr"]) * 100          # right = worse here than overall
        y = (c["wr"] - arch_wr) * 100          # up = beats the archetype average
        pts.append(point(cname, x, y, c, o, arch_stats, set_code,
                         aw=c["wr"], ag=c["games"], ae=c["est"]))
    if not pts:
        return None
    # Symmetric limits keep the "average card" in the exact center, with
    # headroom so cards at the edges aren't clipped.
    lx = max(3, max(abs(p["x"]) for p in pts) * 1.2)
    ly = max(3, max(abs(p["y"]) for p in pts) * 1.25)
    return {"id": short, "title": name,
            "subtitle": f"archetype WR {arch_wr:.1%}, {share:.0%} of decks, "
                        f"{len(pts)} cards (≥{min_games} games)",
            "xlabel": "Synergy: overall GIH WR − GIH WR in this archetype (pts)",
            "xhint": "← better in this archetype          worse in this archetype →",
            "ylabel": f"GIH WR in archetype vs archetype average {arch_wr:.1%} (pts)",
            "quads": ["Strong & synergistic", "Strong despite poor fit",
                      "Fits, but weak", "Avoid here"],
            "xmin": -lx, "xmax": lx, "ymin": -ly, "ymax": ly, "points": pts}


def global_chart(overall, arch_stats, fmt_avg, min_games, set_code) -> dict:
    pts = []
    for cname, o in overall.items():
        if o["games"] < min_games:
            continue
        wrs = [v[0] for v in arch_stats.get(cname, {}).values()]
        spread = (max(wrs) - min(wrs)) * 100 if len(wrs) >= 2 else 0.0
        pts.append(point(cname, spread, (o["wr"] - fmt_avg) * 100, o, o, arch_stats,
                         set_code, low=len(wrs) < 2))
    lx = max(3, max(p["x"] for p in pts) * 1.12)
    ly = max(3, max(abs(p["y"]) for p in pts) * 1.15)
    return {"id": "ALL", "title": "All cards",
            "subtitle": f"format average WR {fmt_avg:.1%}, {len(pts)} cards (≥{min_games} games)"
                        + (f"; {sum(p['low'] for p in pts)} faded cards lack win rates in 2+ "
                           f"archetypes yet" if any(p["low"] for p in pts) else ""),
            "xlabel": "Archetype dependence: best − worst GIH WR across the main archetypes (pts)",
            "xhint": "← plays the same in every deck          depends on the archetype →",
            "ylabel": f"Overall GIH WR vs format average {fmt_avg:.1%} (pts)",
            "quads": ["Good in any deck", "Strong, best in the right deck",
                      "Weak everywhere", "Only works in the right deck"],
            "xmin": -lx * 0.06, "xmax": lx, "ymin": -ly, "ymax": ly, "points": pts}


# ---------------------------------------------------------------- HTML page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --bg:#f6f5f2; --panel:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#d9d7d2;
        --axis:#9a978f; --accent:#2f6fd1; --good:#2e8b47; --bad:#c2412f; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141518; --panel:#1d1f23; --ink:#ececee; --muted:#9a9ca3; --line:#2e3036;
          --axis:#5b5e66; --accent:#6ea0f0; --good:#5cc27a; --bad:#ef6b5a; }
}
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif }
main { max-width:1500px; margin:auto; padding:20px 16px 40px }
h1 { font-size:22px; margin:0 0 2px }
.meta { color:var(--muted); font-size:13px; margin-bottom:14px }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px }
.chip { border:1px solid var(--line); background:var(--panel); color:var(--ink);
        border-radius:999px; padding:5px 12px; font:inherit; font-size:13px; cursor:pointer }
.chip[aria-pressed=true] { background:var(--accent); border-color:var(--accent); color:#fff }
.card-panel { background:var(--panel); border:1px solid var(--line); border-radius:10px;
              padding:14px 14px 10px }
h2 { font-size:17px; margin:0 }
.sub { color:var(--muted); font-size:13px; margin-bottom:8px }
.frame { display:grid; grid-template-columns:28px 1fr; grid-template-rows:1fr auto; gap:4px }
.ylab { writing-mode:vertical-rl; transform:rotate(180deg); text-align:center;
        color:var(--muted); font-size:12px }
.xlab { grid-column:2; text-align:center; color:var(--muted); font-size:12px; padding-top:18px }
.plot { position:relative; aspect-ratio:3/2; border:1px solid var(--line);
        margin-left:30px; overflow:visible }
.grid { position:absolute; background:var(--line); opacity:.6 }
.zero { position:absolute; background:var(--axis) }
.tick { position:absolute; font-size:11px; color:var(--muted); white-space:nowrap }
.quad { position:absolute; font-size:12px; color:var(--muted); padding:6px 8px; pointer-events:none }
.c { position:absolute; transform:translate(-50%,-50%); border-radius:4%;
     cursor:zoom-in; transition:transform .12s; box-shadow:0 1px 3px rgba(0,0,0,.4);
     background:#222; border:0; padding:0 }
.c img { width:100%; display:block; border-radius:4% }
.c:hover, .c:focus-visible { transform:translate(-50%,-50%) scale(1.35); z-index:9999 !important;
     outline:2px solid var(--accent) }
.c.low { opacity:.45 }
.c.hide { display:none }
.filters { display:flex; flex-wrap:wrap; align-items:center; gap:6px 14px; margin:0 0 12px }
.fgroup { display:flex; flex-wrap:wrap; align-items:center; gap:4px }
.flabel { font-size:12px; color:var(--muted); margin-right:2px }
.tog { border:1px solid var(--line); background:var(--panel); color:var(--ink); border-radius:6px;
       padding:3px 9px; font:inherit; font-size:12.5px; cursor:pointer; display:inline-flex;
       align-items:center; gap:5px }
.tog[aria-pressed=false] { opacity:.4; text-decoration:line-through }
.pip { width:11px; height:11px; border-radius:50%; border:1px solid rgba(0,0,0,.35) }
.reset { border:0; background:none; color:var(--accent); font:inherit; font-size:12.5px; cursor:pointer }
.count { font-size:12px; color:var(--muted) }
.dot { aspect-ratio:63/88; background:#3a3c42 !important; color:#eee; font-size:9px;
       line-height:1.15; padding:3px; overflow:hidden; display:flex; align-items:center;
       justify-content:center; text-align:center }
.modal { position:fixed; inset:0; background:rgba(0,0,0,.72); display:none;
         align-items:center; justify-content:center; padding:16px; z-index:100000 }
.modal.open { display:flex }
.box { background:var(--panel); color:var(--ink); border-radius:12px; padding:16px;
       display:flex; gap:18px; max-width:860px; width:100%; max-height:92vh; overflow:auto }
.box img { width:min(340px,60vw); border-radius:4.5%; align-self:flex-start }
.stats { flex:1; min-width:200px }
.stats h3 { margin:0 0 8px; font-size:18px }
.stats table { border-collapse:collapse; width:100%; font-size:13px }
.stats td { padding:3px 6px; border-bottom:1px solid var(--line) }
.stats td.n { text-align:right; font-variant-numeric:tabular-nums }
.here td { font-weight:700 }
.pos { color:var(--good) } .neg { color:var(--bad) }
.close { float:right; border:0; background:none; color:var(--muted); font-size:22px; cursor:pointer }
.hint { color:var(--muted); font-size:12px; margin-top:8px }
@media (max-width:640px) { .box { flex-direction:column } .box img { width:100%; max-width:340px } }
</style>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <div class="meta">__META__ · Click a card to zoom in.</div>
  <div class="chips" id="chips"></div>
  <section class="card-panel">
    <h2 id="title"></h2>
    <div class="sub" id="subtitle"></div>
    <div class="filters" id="filters"></div>
    <div class="frame">
      <div class="ylab" id="ylabel"></div>
      <div class="plot" id="plot"></div>
      <div class="xlab" id="xlabel"></div>
    </div>
  </section>
</main>
<div class="modal" id="modal" role="dialog" aria-modal="true">
  <div class="box" id="box"></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const charts = DATA.charts, archNames = DATA.archNames;
const $ = id => document.getElementById(id);
const pct = v => (v * 100).toFixed(1) + '%';
const signed = v => (v > 0 ? '+' : '') + v.toFixed(1);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// ---- color / rarity filters (kept across charts, remembered per browser)
const COLORS = [['W', 'White', '#f3ecd2'], ['U', 'Blue', '#2f7bd1'], ['B', 'Black', '#3a3a3a'],
  ['R', 'Red', '#d0412f'], ['G', 'Green', '#2e8b47'], ['M', 'Multicolor', '#d4af37'],
  ['C', 'Colorless', '#a7a7a7']];
const RARITIES = [['common', 'Common', '#1d1d1f'], ['uncommon', 'Uncommon', '#8fa3b5'],
  ['rare', 'Rare', '#c9a54a'], ['mythic', 'Mythic', '#e0612b']];
const colorGroup = c => !c ? 'C' : c.length > 1 ? 'M' : c;
let filt = {color: COLORS.map(c => c[0]), rarity: RARITIES.map(r => r[0])};
try { const s = JSON.parse(localStorage.getItem('synergy-filters')); if (s && s.color && s.rarity) filt = s; } catch (e) {}

function buildFilters() {
  const box = $('filters'); box.innerHTML = '';
  const group = (label, key, items) => {
    const g = document.createElement('div'); g.className = 'fgroup';
    g.innerHTML = `<span class="flabel">${label}</span>`;
    items.forEach(([id, name, hex]) => {
      const b = document.createElement('button'); b.className = 'tog'; b.dataset.key = key; b.dataset.id = id;
      b.innerHTML = `<span class="pip" style="background:${hex}"></span>${name}`;
      b.title = `Click to show/hide ${name.toLowerCase()} cards. Shift-click to show only ${name.toLowerCase()}.`;
      b.onclick = e => {
        const set = new Set(filt[key]);
        if (e.shiftKey) { set.clear(); set.add(id); }
        else set.has(id) ? set.delete(id) : set.add(id);
        filt[key] = [...set]; saveFilters(); applyFilters();
      };
      g.appendChild(b);
    });
    box.appendChild(g);
  };
  group('Color', 'color', COLORS);
  group('Rarity', 'rarity', RARITIES);
  const reset = document.createElement('button'); reset.className = 'reset'; reset.textContent = 'Show all';
  reset.onclick = () => { filt = {color: COLORS.map(c => c[0]), rarity: RARITIES.map(r => r[0])};
    saveFilters(); applyFilters(); };
  const count = document.createElement('span'); count.className = 'count'; count.id = 'count';
  box.append(reset, count);
}
function saveFilters() { try { localStorage.setItem('synergy-filters', JSON.stringify(filt)); } catch (e) {} }
function applyFilters() {
  const known = new Set(RARITIES.map(r => r[0]));
  document.querySelectorAll('.tog').forEach(b => b.setAttribute('aria-pressed', filt[b.dataset.key].includes(b.dataset.id)));
  const cards = [...document.querySelectorAll('#plot .c')];
  let shown = 0;
  cards.forEach(b => {
    const ok = filt.color.includes(b.dataset.color) &&
      (filt.rarity.includes(b.dataset.rarity) || !known.has(b.dataset.rarity));
    b.classList.toggle('hide', !ok); if (ok) shown++;
  });
  $('count').textContent = `Showing ${shown} of ${cards.length} cards · shift-click a filter to show only that one`;
}

function niceStep(range) {
  const raw = range / 8, p = Math.pow(10, Math.floor(Math.log10(raw)));
  return [1, 2, 2.5, 5, 10].map(m => m * p).find(s => s >= raw);
}

function render(id) {
  const ch = charts.find(c => c.id === id) || charts[0];
  document.querySelectorAll('.chip').forEach(b => b.setAttribute('aria-pressed', b.dataset.id === ch.id));
  $('title').textContent = ch.title;
  $('subtitle').textContent = ch.subtitle;
  $('xlabel').innerHTML = esc(ch.xlabel) + '<br>' + esc(ch.xhint);
  $('ylabel').textContent = ch.ylabel;
  const plot = $('plot'); plot.innerHTML = '';
  const X = x => (x - ch.xmin) / (ch.xmax - ch.xmin) * 100;
  const Y = y => (ch.ymax - y) / (ch.ymax - ch.ymin) * 100;
  const add = (cls, css, text) => { const d = document.createElement('div'); d.className = cls;
    Object.assign(d.style, css); if (text != null) d.textContent = text; plot.appendChild(d); return d; };
  const sx = niceStep(ch.xmax - ch.xmin), sy = niceStep(ch.ymax - ch.ymin);
  for (let v = Math.ceil(ch.xmin / sx) * sx; v <= ch.xmax; v += sx) {
    add(Math.abs(v) < 1e-9 ? 'zero' : 'grid', {left: X(v) + '%', top: 0, bottom: 0, width: '1px'});
    add('tick', {left: X(v) + '%', top: '100%', transform: 'translate(-50%,4px)'}, +v.toFixed(2));
  }
  for (let v = Math.ceil(ch.ymin / sy) * sy; v <= ch.ymax; v += sy) {
    add(Math.abs(v) < 1e-9 ? 'zero' : 'grid', {top: Y(v) + '%', left: 0, right: 0, height: '1px'});
    add('tick', {top: Y(v) + '%', right: '100%', transform: 'translate(-6px,-50%)'}, +v.toFixed(2));
  }
  const [tl, tr, bl, br] = ch.quads;
  add('quad', {left: 0, top: 0}, tl); add('quad', {right: 0, top: 0}, tr);
  add('quad', {left: 0, bottom: 0}, bl); add('quad', {right: 0, bottom: 0}, br);
  const w = ch.points.length > 80 ? 3.6 : ch.points.length > 40 ? 4.6 : 5.6;
  [...ch.points].sort((a, b) => a.y - b.y).forEach((p, i) => {
    const b = document.createElement('button');
    b.className = 'c' + (p.low ? ' low' : '') + (p.s ? '' : ' dot');
    b.title = p.n; b.setAttribute('aria-label', p.n);
    b.dataset.color = colorGroup(p.c); b.dataset.rarity = p.r;
    Object.assign(b.style, {left: X(p.x) + '%', top: Y(p.y) + '%', width: w + '%', zIndex: 10 + i});
    if (p.s) { const im = new Image(); im.src = p.s; im.alt = p.n; im.loading = 'lazy'; b.appendChild(im); }
    else b.textContent = p.n;
    b.onclick = () => openCard(p, ch);
    plot.appendChild(b);
  });
  applyFilters();
  try { history.replaceState(null, '', '#' + ch.id); } catch (e) {}
}

function openCard(p, ch) {
  const wrCell = (wr, est) => `<td class="n">${est ? '≈' : ''}${pct(wr)}</td>`;
  const rows = Object.entries(p.arch).sort((a, b) => b[1][0] - a[1][0]).map(([k, [wr, g, est]]) => {
    const d = (wr - p.wr) * 100;
    return `<tr class="${k === ch.id ? 'here' : ''}"><td>${esc(archNames[k] || k)}</td>` +
      wrCell(wr, est) + `<td class="n ${d >= 0 ? 'pos' : 'neg'}">${signed(d)}</td>` +
      `<td class="n">${g.toLocaleString()}</td></tr>`;
  }).join('');
  const here = p.aw != null ? `<tr class="here"><td>In ${esc(ch.title)}</td>${wrCell(p.aw, p.ae)}` +
    `<td class="n">${p.ag.toLocaleString()} games</td></tr>` : '';
  const anyEst = p.e || p.ae || Object.values(p.arch).some(v => v[2]);
  $('box').innerHTML =
    (p.l ? `<img src="${esc(p.l)}" alt="${esc(p.n)}">` : '') +
    `<div class="stats"><button class="close" aria-label="Close">×</button><h3>${esc(p.n)}</h3>` +
    `<table><tr><td>Overall GIH WR</td>${wrCell(p.wr, p.e)}<td class="n">${p.g.toLocaleString()} games</td></tr>${here}</table>` +
    (rows ? `<p class="hint" style="margin:14px 0 4px">GIH WR by archetype (vs overall, pts)</p>` +
      `<table>${rows}</table>` : `<p class="hint">Not enough games in any main archetype yet.</p>`) +
    (anyEst ? `<p class="hint">≈ estimated: 17Lands hides win rates under 500 games, so this one is ` +
      `rebuilt from the in-deck and never-drawn win rates.</p>` : '') +
    `<p class="hint">Click outside or press Esc to close.</p></div>`;
  $('box').querySelector('.close').onclick = closeCard;
  $('modal').classList.add('open');
}
function closeCard() { $('modal').classList.remove('open'); }
$('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeCard(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeCard(); });

charts.forEach(c => {
  const b = document.createElement('button'); b.className = 'chip'; b.dataset.id = c.id;
  b.textContent = c.title; b.onclick = () => render(c.id); $('chips').appendChild(b);
});
buildFilters();
render(location.hash.slice(1));
</script>
</body>
</html>
"""


def write_html(path: Path, title: str, meta: str, charts: list, arch_names: dict) -> None:
    data = json.dumps({"charts": charts, "archNames": arch_names}).replace("</", "<\\/")
    page = (PAGE.replace("__TITLE__", html.escape(title))
                .replace("__META__", html.escape(meta))
                .replace("__DATA__", data))
    path.write_text(page, encoding="utf-8")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("set_code")
    ap.add_argument("--format", default="PremierDraft")
    ap.add_argument("--period", default="ALL_TIME", choices=PERIODS, type=str.upper,
                    help="17Lands time window, e.g. LAST_WEEK or LAST_DAY for the latest data")
    ap.add_argument("--min-games", type=int, default=100,
                    help="min GIH games for a card to be plotted")
    ap.add_argument("--min-share", type=float, default=0.03,
                    help="min share of decks for an archetype to get a chart")
    ap.add_argument("--fresh", action="store_true", help="ignore the 1-hour cache")
    ap.add_argument("--no-png", action="store_true", help="only write the interactive page")
    ap.add_argument("--out-dir", default=str(ROOT / "output"),
                    help="where to write <SET>/index.html (use 'docs' for GitHub Pages)")
    a = ap.parse_args()
    s = a.set_code.upper()
    out_root = Path(a.out_dir).resolve()
    out_dir = out_root / s
    out_dir.mkdir(parents=True, exist_ok=True)

    overall = card_rates(s, a.format, a.period, a.fresh)
    archs, fmt_avg = archetypes(s, a.format, a.period, a.fresh, a.min_share)
    if not overall or not archs:
        raise SystemExit(f"No 17Lands data for {s} / {a.format} / {a.period}.")
    print(f"{s} ({a.period}): {len(overall)} cards with an overall win rate, "
          f"{len(archs)} archetypes")

    per_arch = {short: card_rates(s, a.format, a.period, a.fresh, short)
                for short, *_ in archs}
    # name -> {archetype: [wr, games]} for every main archetype the card fits.
    # A lower bar than --min-games so the global map's spread axis and the
    # zoom panel cover more cards early in a format (games are shown there).
    min_arch_games = max(50, a.min_games // 3)
    arch_stats: dict[str, dict] = {}
    for short, cards in per_arch.items():
        for cname, c in cards.items():
            if c["games"] >= min_arch_games and fits(c["color"], short):
                arch_stats.setdefault(cname, {})[short] = [c["wr"], c["games"], c["est"]]

    names = set(overall) | {n for cards in per_arch.values() for n in cards}
    fallback = {n: c["img"] for cards in [overall, *per_arch.values()]
                for n, c in cards.items() if c.get("img")}
    no_img = load_images(sorted(names), s, fallback)
    print(f"  Card images: {len(names) - no_img} of {len(names)} found")

    charts = [global_chart(overall, arch_stats, fmt_avg, a.min_games, s)]
    print(f"  All cards: {len(charts[0]['points'])} cards plotted")
    for short, name, wr, share in archs:
        ch = archetype_chart(short, name, wr, share, per_arch[short], overall,
                             arch_stats, a.min_games, s)
        print(f"  {name}: {len(ch['points']) if ch else 0} cards plotted")
        if ch:
            charts.append(ch)

    if not a.no_png:
        for ch in charts:
            plot_png(ch, out_dir / f"{ch['id']}.png", s)

    write_html(out_dir / "index.html", f"{s} archetype synergy maps",
               f"17Lands {a.format}, {a.period.replace('_', ' ').lower()}",
               charts, {short: name for short, name, *_ in archs})
    write_site_index(out_root)
    print(f"Done -> {out_dir / 'index.html'}")


def write_site_index(out_root: Path) -> None:
    """A landing page linking every set built into out_root (the GitHub Pages root)."""
    sets = sorted(p.name for p in out_root.iterdir() if (p / "index.html").exists())
    links = "".join(f'<li><a href="{html.escape(s)}/">{html.escape(s)}</a></li>' for s in sets)
    (out_root / "index.html").write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Limited synergy maps</title><style>"
        ":root{--bg:#f6f5f2;--ink:#1d1d1f;--muted:#6b6b70;--accent:#2f6fd1}"
        "@media (prefers-color-scheme:dark){:root{--bg:#141518;--ink:#ececee;--muted:#9a9ca3;--accent:#6ea0f0}}"
        "body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}"
        "main{max-width:720px;margin:auto;padding:32px 16px}a{color:var(--accent)}"
        "p{color:var(--muted)}li{font-size:20px;margin:6px 0}</style></head><body><main>"
        "<h1>Limited synergy maps</h1><p>For each archetype of a Magic set: which cards win, "
        "and which ones fit the deck. Built from 17Lands data, card images from Scryfall.</p>"
        f"<ul>{links}</ul></main></body></html>", encoding="utf-8")
    (out_root / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    main()
