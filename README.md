# Archetype Synergy Map

Interactive maps of a Limited set's cards, built from 17Lands data. Open
`output/<SET>/index.html`, pick a chart, and click any card to see it full size
with its win rate in every archetype.

**Live site:** https://xioshiva.github.io/archetype-synergy-map/

To update the site, rebuild into `docs/` (GitHub Pages serves that folder from `main`), then commit and push:

```bash
py synergy_map.py TRK --period LAST_WEEK --no-png --out-dir docs
```

**One chart per main archetype.** Each card that is playable there (its colors fit, enough games) is placed by:

- **Y — win rate:** the card's GIH WR in this archetype minus the archetype's win rate.
  Up = the card wins more than the archetype's average deck.
- **X — synergy:** the card's overall GIH WR minus its GIH WR in this archetype.
  Center = it plays the same here as everywhere else. Right = worse in this archetype. Left = better (synergy).

For example, a 55% card in a 55% archetype that also wins 55% of its games in that archetype sits dead center.
If the card does worse in this archetype, it moves down and to the right.

**A global "All cards" chart.** Each card is placed by:

- **Y:** the card's overall GIH WR minus the format's average win rate.
- **X — archetype dependence:** the gap between the card's best and worst win rate across the main archetypes.
  Right = depends on the deck it's in. Cards without win rates in 2+ archetypes yet are faded at 0.

```bash
pip install -r requirements.txt
py synergy_map.py TRK --period LAST_WEEK
```

| Option | Description |
|--------|-------------|
| `--period` | 17Lands time window: `ALL_TIME` (default), `LAST_DAY`, `LAST_WEEK`, `LAST_TWO_WEEKS`, `LATEST_EVENT`, `LAST_TWO_EVENTS`, `FIRST_WEEK`, `ALL_EXCEPT_FIRST_WEEK` |
| `--format` | 17Lands format (default `PremierDraft`) |
| `--min-games` | Minimum GIH games for a card to be plotted (default 100) |
| `--min-share` | Minimum share of decks for an archetype to get a chart (default 0.03) |
| `--fresh` | Ignore the 1-hour cache |
| `--no-png` | Only write the interactive page (skip the static PNGs) |

Notes:

- 17Lands hides any win rate based on fewer than 500 games. When the in-deck WR and the
  never-drawn WR are both still shown, the GIH WR is rebuilt from them and marked "≈" in the zoom panel.
  Against cards where the real value is shown, this estimate has a median error of about 0.1 points.
  Cards with neither number shown can't be plotted until 17Lands publishes the raw game data
  (usually 2–3 weeks after release, at 17lands.com/public_datasets).
- 17Lands' old `card_ratings/data` endpoint ignores the archetype (colors) filter. This project uses `/api/card_data`,
  which needs the colors in WUBRG order.
- Card images are loaded from Scryfall.
