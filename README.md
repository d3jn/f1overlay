# F1 Overlay

A compact, always-visible race overlay for **F1 25 with the 2026 Season Pack**.
It listens for the game's UDP telemetry and shows everything on one
fixed-size, color-coded window.

```
 now   +5m  +10m  +15m  +30m  +45m  +60m      weather: time offset + rain %
  0%    5%   20%   45%   70%   80%   60%      (strip color = weather type)
 ─────────────────────────────────────────
 ●  DRIVER       GAP  OT ERS     T  WEAR  FW
 4  NORRIS     -4.21  OT  H 62%  S   41% 12/0
 5  LECLERC    -0.83      M 88%  M   33%   –
 6  HAMILTON   -0.31  OT  B 15%  S   52%   –
 7  YANIUK                M 74%  M   29%   –   ← you
 8  ALONSO     +0.62  OT  N 91%  H   18%   –
 9  GASLY      +2.40      ·  ·   M    ·    ·   ← restricted telemetry
10  OCON         +1L      H 55%  H   22% 0/30
 ─────────────────────────────────────────
 PIT 22s  P7→P9 (-2)  ▲ 1.4  ▼ 3.1  ! +1L ▼0.8
 ─────────────────────────────────────────
 PACE  S  (M +0.503  H +1.120)
```

The overlay is active in race sessions (R, R2, R3). In practice and
qualifying it shows the weather and waits.

## Rows

**Weather.** One column for each forecast sample of the current session.
The colored strip shows the weather type: amber clear, light grey light
cloud, grey overcast, light blue light rain, blue heavy rain, purple storm.

**Standings.** Seven drivers: up to three ahead of you, you, and up to three
behind. Near the front or back of the field the window shifts so it stays
full.

| Column | Meaning |
|---|---|
| GAP | Time from you to that car (negative = ahead). `+1L` / `-1L` for a car a lap down / up. Under 1 s is bold. |
| OT | Overtake Mode: solid green = active, outlined = available, blank = unavailable or pre-2026 car. |
| ERS | Deploy mode (N none, M medium, H hotlap, B boost), then store %. Red under 20%. |
| T | Visual tyre compound: S red, M yellow, H white, I green, W blue. |
| WEAR | Most-worn tyre. Green < 40%, yellow < 60%, orange < 75%, red above. |
| FW | Front wing damage, left/right %. |

A driver with "Your Telemetry" set to Restricted has their ERS, tyre wear and
wing damage zeroed by the game. Those cells show `·` instead of a fake 0.

**Pit.** What pitting this lap would cost:

- `PIT 22s` is the time lost, from `pitstop_time_lost` in settings. Under SC or
  VSC the `sc_pitstop_time` is used and an `SC`/`VSC` badge appears. An
  unserved stop-go that must be served at the stop adds 10 s.
- `P7→P9 (-2)` is your position now and after the stop. Positions lost are the
  cars behind you whose gap is smaller than the pit loss. Lapped cars never
  count.
- `▲ 1.4` / `▼ 3.1` are the gaps to the cars ahead and behind you after the
  stop. Red means under a second.
- `! +1L ▼0.8` warns that a lapped (`+1L`) or lapping (`-1L`) car will be
  within 2 s of you when you rejoin: ▲ just ahead, ▼ just behind. This assumes
  every car runs your last lap pace.

**Pace.** The theoretical lap per compound: the best S1 + S2 + S3 set on it by
any car over their previous and current lap. The fastest compound comes
first, then the gap of each other compound. Sectors driven in the pit lane,
under a safety car, or where the lap went invalid are skipped.

## Running

Requires Python 3.10+ with Tkinter. There are no other dependencies.
On Debian/Ubuntu, install `python3-tk`.

```bash
python3 main.py
```

In game, under Settings → Telemetry: turn UDP Telemetry on, set **UDP Format
to 2026**, and set the port to match `udp_port`.

Window: drag with the left mouse button. Right-click for **Always on top**
and **Exit**. The window size is fixed and is derived from `font_size`.

## Settings

`settings.json` lives next to `main.py`, or next to the `.exe` when frozen.
Every key is optional.

| Key | Default | |
|---|---|---|
| `udp_port` | `20777` | Port to listen on. |
| `borderless` | `false` | Remove the window frame. |
| `always_on_top` | `false` | Start on top (also toggled from the right-click menu). |
| `opacity` | `1` | Window opacity, 0–1. |
| `font_size` | `10` | Scales the whole window. |
| `ers_capacity_j` | `4000000` | ERS store capacity used for the %. The 2026 spec doesn't state it; 4 MJ is the 2025 value. |
| `drivers` | `[]` | `[{"name": "Me", "number": "7"}]` replaces the name of the car with that race number. |
| `pitstop_time_lost` | | Per-track `{"pitstop_time": 21, "sc_pitstop_time": 14}` in seconds, keyed by track slug (see `TRACK_SLUG_TO_ID` in `main.py`). Unlisted tracks use 21 / 14. |

## Development

```bash
python3 -m unittest discover -t . -s tests -v   # parser + race logic tests
python3 scripts/fake_feed.py                    # synthetic 2026 race on 127.0.0.1:20777
python3 scripts/fake_feed.py --sc 1 --pit       # under SC, player in the pits
```

Windows build: `scripts\build-windows.bat` produces `dist\f1overlay.exe` plus
`settings.json` (needs `pip install pyinstaller`).
