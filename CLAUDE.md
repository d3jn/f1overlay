# CLAUDE.md

## Project

A single-page Tkinter overlay for F1 25 **2026 Season Pack** UDP telemetry
(packet format 2026 only, 24 cars). It is the trimmed successor of
`../f1-racing-companion`. The struct layouts follow
`../f1-race-control/specs/25vs26/`, the same source telemetro uses.

## Commands

- Run: `python3 main.py` (needs `python3-tk`; reads `settings.json` beside it)
- Tests: `python3 -m unittest discover -t . -s tests -v` (stdlib only, no Tk)
- Fake game: `python3 scripts/fake_feed.py [--sc 1|2] [--pit] [--format 2025]`
- Windows exe: `scripts\build-windows.bat`

## Layout

- `telemetry.py` holds the 2026 struct layouts and per-packet parsers. They
  return plain dicts, or None for a short packet. It is the only module that
  knows offsets.
- `race.py` is pure logic: race order, the standings window, gaps, the pit
  projection with traffic, the `CompoundPace` tracker, and fixed-width
  formatters. No Tk, so it is fully unit-tested.
- `ui.py` is the Tk overlay. `Layout` measures fonts once against worst-case
  strings (`DRIVER_COLUMNS`, `WORST_PIT`, `WORST_PACE`), so the window size
  never changes. Every refresh is a full canvas redraw.
- `main.py` handles settings parsing and the entry point. It imports `ui`
  lazily so the settings code works without Tk.

## Things to be careful of

- Threading: the UDP thread only does whole-attribute replacement on the app.
  `CompoundPace` is touched only by the UDP thread, which publishes
  `pace_ranking` as a new list. Don't mutate published lists in place.
- Gap sign convention everywhere: positive = the other car is behind the
  player (seconds or laps).
- `tests/test_telemetry.py` restates the layouts independently from the spec.
  Keep it that way, so that a parser typo can't agree with itself.
- Restricted telemetry (`your_telemetry == 0`) zeroes ERS store/mode, tyre
  wear and wing damage for other cars. The UI shows `·` for those.
- Adding a column: add it to `DRIVER_COLUMNS` with its worst-case content,
  then draw it in `OverlayApp._draw_driver`.
- `font_size` and the display DPI scale the window proportionally: fonts are
  in points, and `Layout.scale` = font_size / 10 × `tk scaling` / (96/72).
  Size anything that isn't text with `Layout.px(n)` (n in pixels at size 10
  on a 96-DPI display), never with a bare pixel literal, or it stops scaling.
- Windows: `ui._enable_dpi_awareness()` must run before `tk.Tk()`. The
  `--noconsole` exe has `sys.stderr = None` (print is then a no-op). Keep
  stderr output going through `main._warn` / `ui._print_safe`, which survive
  a legacy code page.
