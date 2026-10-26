"""Race logic behind the overlay: standings window, gaps, pit projection and the
compound pace tracker, plus the small text formatters that keep every value to
a fixed width.

Everything here works on the plain per-car dicts telemetry.py produces and
knows nothing about Tk or sockets, so it can be tested without either.

Sign convention, used everywhere: a positive gap means the other car is
*behind* the player, in seconds or in laps; negative means ahead.
"""

from dataclasses import dataclass

# Result status: 1 inactive, 2 active, 3 finished. Everything else (invalid,
# DNF, DSQ, not classified, retired) drops out of the standings.
ACTIVE_RESULT_STATUSES = {1, 2, 3}
RACE_SESSION_TYPES = {15, 16, 17}
# Safety car status: 1 full, 2 virtual, 3 formation lap.
SC_FULL = 1
SC_VIRTUAL = 2
PIT_LOSS_SC_STATUSES = {SC_FULL, SC_VIRTUAL}
SLOW_LAP_SC_STATUSES = {SC_FULL, SC_VIRTUAL, 3}

TYRE_LETTERS = {16: "S", 17: "M", 18: "H", 7: "I", 8: "W"}
ERS_MODE_LETTERS = {0: "N", 1: "M", 2: "H", 3: "B"}

STOP_GO_SECONDS = 10
STANDINGS_ROWS = 7
# A lapped or lapping car this close to the rejoin point is flagged as traffic.
TRAFFIC_WINDOW_S = 2.0
NAME_WIDTH = 10


# --- standings ---------------------------------------------------------------

def race_order(laps):
    """(car_idx, lap) for every classified car, sorted by race position."""
    cars = [(i, lap) for i, lap in enumerate(laps)
            if lap["position"] > 0 and lap["result_status"] in ACTIVE_RESULT_STATUSES]
    cars.sort(key=lambda car: car[1]["position"])
    return cars


def standings_window(order, player_idx, rows=STANDINGS_ROWS):
    """Car indices to show: up to rows // 2 either side of the player, shifted so
    the window stays full near the front or back of the field."""
    idxs = [i for i, _ in order]
    if player_idx not in idxs:
        return idxs[:rows]
    p = idxs.index(player_idx)
    start = max(0, min(p - rows // 2, len(idxs) - rows))
    return idxs[start:start + rows]


def laps_behind(other, me, track_length):
    """Whole laps `other` is behind `me` (negative: ahead), by distance covered.

    Truncates, so a car only counts as lapped once it is a full lap down. That
    keeps the S/F line crossing, where lap numbers differ for a moment, from
    flashing a lap marker. Falls back to lap numbers before the first Session
    packet supplies the track length.
    """
    if track_length > 0:
        return int((me["total_distance"] - other["total_distance"]) / track_length)
    return me["lap_num"] - other["lap_num"]


def relative_gap(other, me, track_length):
    """(laps, seconds) of `other` relative to `me`. `seconds` is None when the
    cars are on different laps, where only the lap count is meaningful."""
    laps = laps_behind(other, me, track_length)
    if laps:
        return laps, None
    return 0, other["delta_to_leader"] - me["delta_to_leader"]


# --- pit projection ----------------------------------------------------------

@dataclass
class PitProjection:
    loss: float
    sc_status: int
    in_pit: bool = False
    position_now: int = 0
    position_after: int = 0
    # (laps, seconds) to the race-order neighbours after the stop, as positive
    # magnitudes: how far the car ahead is up the road, how far the car behind
    # is back. `seconds` is None on a different lap. None: no such car.
    ahead: tuple = None
    behind: tuple = None
    # (laps_behind, seconds) of the lapped or lapping car nearest the rejoin
    # point; seconds > 0 means it will be just ahead of us on track.
    traffic: tuple = None

    @property
    def positions_lost(self):
        return self.position_after - self.position_now


def project_pit(laps, player_idx, track_length, loss_green, loss_sc, sc_status):
    """What a stop this lap would cost, or None without a classified player.

    Positions lost are the cars behind in race order whose gap to us is smaller
    than the pit loss. A lapped car is over a lap behind, so it never counts.
    Cars currently in the pits are left out: they are losing time themselves.
    """
    me = laps[player_idx]
    if me["position"] <= 0 or me["result_status"] not in ACTIVE_RESULT_STATUSES:
        return None

    loss = loss_sc if sc_status in PIT_LOSS_SC_STATUSES else loss_green
    if me["serve_pen_at_stop"]:
        loss += me["unserved_sg"] * STOP_GO_SECONDS
    if me["pit_status"] != 0:
        return PitProjection(loss, sc_status, in_pit=True)

    others = [(i, lap) for i, lap in race_order(laps)
              if i != player_idx and lap["pit_status"] == 0]
    ahead = [lap for _, lap in others if lap["position"] < me["position"]]
    passed, still_behind = [], []
    for _, lap in others:
        if lap["position"] < me["position"]:
            continue
        laps_b, gap = relative_gap(lap, me, track_length)
        (passed if laps_b == 0 and gap < loss else still_behind).append(lap)

    projected = me["delta_to_leader"] + loss
    proj = PitProjection(loss, sc_status,
                         position_now=me["position"],
                         position_after=me["position"] + len(passed))

    ahead_car = passed[-1] if passed else (ahead[-1] if ahead else None)
    if ahead_car is not None:
        laps_a = -laps_behind(ahead_car, me, track_length)
        proj.ahead = (laps_a, None if laps_a else projected - ahead_car["delta_to_leader"])
    if still_behind:
        behind_car = still_behind[0]
        laps_b = laps_behind(behind_car, me, track_length)
        proj.behind = (laps_b, None if laps_b else behind_car["delta_to_leader"] - projected)

    proj.traffic = _rejoin_traffic(me, [lap for _, lap in others], track_length, loss)
    return proj


def _rejoin_traffic(me, others, track_length, loss):
    """The lapped or lapping car physically nearest where we would rejoin.

    Converts on-track distance to time with our last lap time, so it assumes
    everyone runs our pace. That is good enough to warn about traffic.
    Cars within half a lap in race terms are skipped: they are race-order
    neighbours, already covered by the ahead/behind gaps.
    """
    lap_time = me["last_lap_ms"] / 1000.0
    if lap_time <= 0 or track_length <= 0:
        return None
    nearest = None
    for lap in others:
        race_distance = me["total_distance"] - lap["total_distance"]
        if abs(race_distance) <= track_length / 2:
            continue
        # Seconds the car is ahead of us on track now, in [0, lap_time).
        on_track = (lap["lap_distance"] - me["lap_distance"]) % track_length / track_length * lap_time
        # We lose `loss` seconds, so after the stop it is that much further ahead.
        offset = (on_track + loss) % lap_time
        if offset > lap_time / 2:
            offset -= lap_time
        if abs(offset) <= TRAFFIC_WINDOW_S and (nearest is None or abs(offset) < abs(nearest[1])):
            nearest = (round(race_distance / track_length), offset)
    return nearest


# --- compound pace -----------------------------------------------------------

class CompoundPace:
    """Fastest S1/S2/S3 per visual compound over each car's previous and
    current lap, across the whole field.

    S1 and S2 are latched when the car's sector index moves past them; S3 is
    derived at lap rollover as lastLapTime - S1 - S2. Each sector is tagged with
    the compound the car was on when it finished. A sector is tainted, and
    skipped, if any observation during it saw the car in the pit lane, under a
    safety car, or the lap becoming invalid.

    Not thread-safe: observe() and ranking() are both called from the UDP
    thread, which publishes the ranking list for the UI.
    """

    HISTORY_LAPS = 2

    def __init__(self):
        # car_idx -> {lap_num -> entry}
        self._laps = {}
        # car_idx -> {"lap": current lap_num, "invalid": last lap_invalid seen}
        self._state = {}

    def observe(self, car_idx, lap, compound, sc_status):
        if lap["result_status"] not in ACTIVE_RESULT_STATUSES:
            self._laps.pop(car_idx, None)
            self._state.pop(car_idx, None)
            return
        lap_num = lap["lap_num"]
        if lap_num <= 0:
            return

        laps = self._laps.setdefault(car_idx, {})
        state = self._state.get(car_idx)
        if state is not None and lap_num != state["lap"]:
            if lap_num == state["lap"] + 1:
                self._close_lap(laps.get(state["lap"]), lap["last_lap_ms"], compound)
            else:
                # Flashback or missed laps: nothing in the history lines up any more.
                laps.clear()
            state = None
        if state is None:
            state = self._state[car_idx] = {"lap": lap_num, "invalid": False}

        entry = laps.get(lap_num)
        if entry is None:
            entry = laps[lap_num] = {
                "times": [None, None, None],
                "compounds": [None, None, None],
                "tainted": [False, False, False],
                # S1/S2 as reported, tainted or not, for the S3 arithmetic.
                "raw": [None, None],
            }

        sector = lap["sector"]
        if 0 <= sector <= 2:
            # Taint only the sector where the lap became invalid: the flag then
            # stays set for the rest of the lap, but earlier sectors were clean.
            newly_invalid = lap["lap_invalid"] and not state["invalid"]
            if lap["pit_status"] or newly_invalid or sc_status in SLOW_LAP_SC_STATUSES:
                entry["tainted"][sector] = True
        state["invalid"] = bool(lap["lap_invalid"])

        for i, key in ((0, "s1_ms"), (1, "s2_ms")):
            if sector > i and entry["raw"][i] is None and lap[key] > 0:
                entry["raw"][i] = lap[key]
                if not entry["tainted"][i]:
                    entry["times"][i] = lap[key]
                    entry["compounds"][i] = compound

        for old in [n for n in laps if n <= lap_num - self.HISTORY_LAPS]:
            del laps[old]

    @staticmethod
    def _close_lap(entry, last_lap_ms, compound):
        if entry is None or last_lap_ms <= 0 or entry["tainted"][2]:
            return
        s1, s2 = entry["raw"]
        if s1 is None or s2 is None:
            return
        s3 = last_lap_ms - s1 - s2
        if s3 > 0:
            entry["times"][2] = s3
            entry["compounds"][2] = compound

    def ranking(self):
        """[(compound, theoretical_lap_ms)], fastest first. A compound is listed
        only once it has a time in all three sectors."""
        best = {}
        for laps in self._laps.values():
            for entry in laps.values():
                for i in range(3):
                    t, compound = entry["times"][i], entry["compounds"][i]
                    if t is None or compound is None:
                        continue
                    sectors = best.setdefault(compound, [None, None, None])
                    if sectors[i] is None or t < sectors[i]:
                        sectors[i] = t
        totals = sorted((sum(s), c) for c, s in best.items() if None not in s)
        return [(c, total) for total, c in totals]


# --- fixed-width text --------------------------------------------------------

def format_gap(laps, seconds):
    """At most 5 characters: '+1L', '-0.31', '+12.3', '-123'."""
    if laps:
        return f"{laps:+d}L"
    if seconds is None:
        return ""
    # Thresholds sit just under the rounding points: 9.999 would print as
    # "+10.00" with two decimals, 99.99 as "+100.0" with one.
    a = abs(seconds)
    if a < 9.995:
        return f"{seconds:+.2f}"
    if a < 99.95:
        return f"{seconds:+.1f}"
    return f"{max(-999, min(999, seconds)):+.0f}"


def format_magnitude(laps, seconds):
    """Unsigned pit-row gap: '1L', '0.84', '12.3', '123'."""
    if laps:
        return f"{abs(laps)}L"
    return format_gap(0, abs(seconds)).lstrip("+")


def format_pace_gap(ms):
    s = ms / 1000.0
    return f"+{s:.3f}" if s < 10 else f"+{min(s, 99.9):.1f}"


def format_offset(minutes):
    """Weather sample offset in at most 4 characters: 'now', '+5m', '+90m',
    '+1h', '+2h', and '105m' for the odd long offset."""
    if minutes == 0:
        return "now"
    if minutes >= 60 and minutes % 60 == 0:
        return f"+{minutes // 60}h"
    if minutes >= 100:
        return f"{minutes}m"
    return f"+{minutes}m"


def short_name(name, width=NAME_WIDTH):
    """Fit a driver name to `width`: the surname if the full name is too long,
    hard-truncated if even that does not fit."""
    name = " ".join(name.split())
    if len(name) <= width:
        return name
    return name.split(" ")[-1][:width]
