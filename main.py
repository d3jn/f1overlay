"""F1 overlay: entry point and settings.

Reads settings.json from next to this file, or next to the executable when
frozen, then hands a Settings object to the Tk overlay in ui.py.
"""

import json
import os
import sys
from dataclasses import dataclass, field

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

# Pit loss (green, safety car) in seconds for a track settings.json doesn't list.
DEFAULT_PIT_LOSS = (21.0, 14.0)

# settings.json `pitstop_time_lost` slug -> game track id (2026 spec appendix).
TRACK_SLUG_TO_ID = {
    "melbourne": 0,
    "shanghai": 2,
    "sakhir": 3,
    "catalunya": 4,
    "monaco": 5,
    "montreal": 6,
    "silverstone": 7,
    "hungaroring": 9,
    "spa_francorchamps": 10,
    "monza": 11,
    "marina_bay": 12,
    "suzuka": 13,
    "yas_marina": 14,
    "austin": 15,
    "interlagos": 16,
    "red_bull_ring": 17,
    "mexico_city": 19,
    "baku": 20,
    "zandvoort": 26,
    "imola": 27,
    "jeddah": 29,
    "miami": 30,
    "las_vegas": 31,
    "losail": 32,
    "silverstone_reverse": 39,
    "red_bull_ring_reverse": 40,
    "zandvoort_reverse": 41,
    "madrid": 42,
}


@dataclass(frozen=True)
class Settings:
    udp_port: int = 20777
    borderless: bool = False
    always_on_top: bool = False
    opacity: float = 1.0
    font_size: int = 10
    # ERS store capacity used to turn Joules into a percentage. The 2026 spec
    # doesn't state it; 4 MJ is the 2025 value.
    ers_capacity_j: float = 4_000_000.0
    # Normalised race number -> display name, overriding the telemetry name.
    drivers: dict = field(default_factory=dict)
    # Track id -> (green, safety car) pit loss in seconds.
    pit_times: dict = field(default_factory=dict)

    def pit_loss(self, track_id):
        return self.pit_times.get(track_id, DEFAULT_PIT_LOSS)


def _warn(message):
    # The message can quote settings.json text such as driver names. On
    # Windows a redirected stderr uses the legacy code page, which can't encode
    # everything; a warning must never crash startup. (In the --noconsole exe
    # stderr is None and print() is a no-op.)
    text = f"Warning: {message}"
    try:
        print(text, file=sys.stderr)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), file=sys.stderr)


def normalize_race_number(num):
    s = str(num).strip().lstrip("0")
    return s if s else "0"


def _positive_int(value, default):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _positive_number(value, default):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return default


def _opacity(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 1:
        return float(value)
    return 1.0


def _drivers(entries):
    drivers = {}
    if not isinstance(entries, list):
        _warn("'drivers' in settings.json must be a list")
        return drivers
    for entry in entries:
        if not isinstance(entry, dict) or "number" not in entry or "name" not in entry:
            _warn(f"skipping driver entry {entry!r}: needs 'name' and 'number'")
            continue
        key = normalize_race_number(entry["number"])
        if key in drivers:
            _warn(f"duplicate driver number '{key}' in settings.json")
        drivers[key] = str(entry["name"])
    return drivers


def _pit_times(user_dict):
    pit_times = {}
    if not isinstance(user_dict, dict):
        _warn("'pitstop_time_lost' in settings.json must be an object")
        return pit_times
    for slug, values in user_dict.items():
        track_id = TRACK_SLUG_TO_ID.get(slug)
        if track_id is None:
            _warn(f"unknown track slug '{slug}' in pitstop_time_lost")
            continue
        if not isinstance(values, dict):
            _warn(f"pitstop_time_lost['{slug}'] must be an object")
            continue
        pit_times[track_id] = (
            _positive_number(values.get("pitstop_time"), DEFAULT_PIT_LOSS[0]),
            _positive_number(values.get("sc_pitstop_time"), DEFAULT_PIT_LOSS[1]),
        )
    return pit_times


def load_settings(path=SETTINGS_PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    except (json.JSONDecodeError, OSError) as e:
        _warn(f"could not read settings.json ({e}); using built-in defaults")
        raw = {}
    if not isinstance(raw, dict):
        _warn("settings.json is not a JSON object; using built-in defaults")
        raw = {}

    defaults = Settings()
    return Settings(
        udp_port=_positive_int(raw.get("udp_port"), defaults.udp_port),
        borderless=bool(raw.get("borderless", defaults.borderless)),
        always_on_top=bool(raw.get("always_on_top", defaults.always_on_top)),
        opacity=_opacity(raw.get("opacity", defaults.opacity)),
        font_size=_positive_int(raw.get("font_size"), defaults.font_size),
        ers_capacity_j=_positive_number(raw.get("ers_capacity_j"), defaults.ers_capacity_j),
        drivers=_drivers(raw.get("drivers", [])),
        pit_times=_pit_times(raw.get("pitstop_time_lost", {})),
    )


def main():
    # Imported here so the settings code above stays usable without Tk.
    from ui import OverlayApp

    OverlayApp(load_settings()).run()


if __name__ == "__main__":
    main()
