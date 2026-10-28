"""Tests for the race logic: standings window, gaps, pit projection, traffic and
compound pace."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import race

TRACK = 5000.0


def lap(position, delta=0.0, total=None, lap_num=10, lap_distance=None, pit_status=0,
        result_status=2, last_lap_ms=90_000, s1_ms=0, s2_ms=0, sector=0, invalid=0,
        penalties=0, unserved_dt=0, unserved_sg=0, serve_pen=0):
    """A car's lap dict. Distance defaults to a fixed spot on the lap."""
    if total is None:
        total = (lap_num - 1) * TRACK + 2500.0
    if lap_distance is None:
        lap_distance = total % TRACK
    return {
        "last_lap_ms": last_lap_ms, "s1_ms": s1_ms, "s2_ms": s2_ms,
        "delta_to_leader": delta, "lap_distance": lap_distance, "total_distance": total,
        "position": position, "lap_num": lap_num, "pit_status": pit_status, "sector": sector,
        "lap_invalid": invalid, "penalties": penalties, "unserved_dt": unserved_dt,
        "unserved_sg": unserved_sg, "result_status": result_status,
        "serve_pen_at_stop": serve_pen,
    }


def field(n):
    """n cars one second apart, car index == position - 1."""
    return [lap(p, delta=float(p - 1)) for p in range(1, n + 1)]


class StandingsTest(unittest.TestCase):

    def window(self, n, player_pos):
        return race.standings_window(race.race_order(field(n)), player_pos - 1)

    def test_midfield_is_three_either_side(self):
        self.assertEqual(self.window(20, 10), list(range(6, 13)))

    def test_front_fills_from_behind(self):
        self.assertEqual(self.window(20, 1), list(range(0, 7)))
        self.assertEqual(self.window(20, 2), list(range(0, 7)))

    def test_back_fills_from_ahead(self):
        self.assertEqual(self.window(20, 20), list(range(13, 20)))

    def test_small_field(self):
        self.assertEqual(self.window(4, 2), [0, 1, 2, 3])

    def test_retired_cars_drop_out(self):
        laps = field(10)
        laps[3]["result_status"] = 7
        self.assertNotIn(3, [i for i, _ in race.race_order(laps)])

    def test_relative_gap_sign(self):
        me = lap(5, delta=10.0)
        self.assertEqual(race.relative_gap(lap(4, delta=8.5), me, TRACK), (0, -1.5))
        self.assertEqual(race.relative_gap(lap(6, delta=12.0), me, TRACK), (0, 2.0))

    def test_lapped_car_shows_laps(self):
        me = lap(5, delta=10.0, lap_num=10)
        lapped = lap(15, delta=100.0, total=me["total_distance"] - TRACK - 100)
        self.assertEqual(race.relative_gap(lapped, me, TRACK), (1, None))

    def test_almost_a_lap_down_is_still_seconds(self):
        me = lap(5, delta=10.0)
        car = lap(9, delta=95.0, total=me["total_distance"] - 0.95 * TRACK)
        self.assertEqual(race.relative_gap(car, me, TRACK), (0, 85.0))


class PenaltyTest(unittest.TestCase):

    def test_clean_car_owes_nothing(self):
        self.assertEqual(race.penalty_seconds(lap(1)), 0)

    def test_time_penalties_and_stop_gos_add_up(self):
        self.assertEqual(race.penalty_seconds(lap(1, penalties=5, unserved_sg=2)), 25)

    def test_served_stop_go_leaves_the_total(self):
        before = race.penalty_seconds(lap(1, penalties=5, unserved_sg=1))
        after = race.penalty_seconds(lap(1, penalties=5, unserved_sg=0))
        self.assertEqual((before, after), (15, 5))

    def test_drive_through_has_no_time(self):
        self.assertEqual(race.penalty_seconds(lap(1, unserved_dt=1)), 0)


class PitProjectionTest(unittest.TestCase):

    def project(self, laps, player_idx, sc_status=0, green=20.0, sc=12.0):
        return race.project_pit(laps, player_idx, TRACK, green, sc, sc_status)

    def gapped_field(self, gaps_behind):
        """Player in P3; cars behind at the given gaps to the player."""
        laps = [lap(1, 0.0), lap(2, 3.0), lap(3, 5.0)]
        for n, gap in enumerate(gaps_behind):
            laps.append(lap(4 + n, 5.0 + gap))
        return laps

    def test_counts_cars_inside_the_pit_loss(self):
        proj = self.project(self.gapped_field([2.0, 8.0, 19.5, 20.5, 30.0]), 2)
        self.assertEqual((proj.position_now, proj.position_after), (3, 6))
        self.assertEqual(proj.positions_lost, 3)
        # Rejoin 0.5 s behind the car on 19.5 and 0.5 s ahead of the one on 20.5.
        self.assertEqual(proj.ahead[0], 0)
        self.assertAlmostEqual(proj.ahead[1], 0.5)
        self.assertEqual(proj.behind[0], 0)
        self.assertAlmostEqual(proj.behind[1], 0.5)

    def test_no_loss_keeps_car_ahead(self):
        proj = self.project(self.gapped_field([25.0]), 2)
        self.assertEqual(proj.positions_lost, 0)
        # Car ahead is 2 s up the road now, 22 s after the stop.
        self.assertAlmostEqual(proj.ahead[1], 22.0)
        self.assertAlmostEqual(proj.behind[1], 5.0)

    def test_safety_car_uses_sc_loss(self):
        laps = self.gapped_field([10.0, 15.0])
        self.assertEqual(self.project(laps, 2, sc_status=race.SC_FULL).positions_lost, 1)
        self.assertEqual(self.project(laps, 2, sc_status=race.SC_VIRTUAL).loss, 12.0)
        self.assertEqual(self.project(laps, 2, sc_status=0).positions_lost, 2)

    def test_stop_go_served_at_stop_adds_loss(self):
        laps = self.gapped_field([25.0])
        laps[2].update(unserved_sg=1, serve_pen_at_stop=1)
        proj = self.project(laps, 2)
        self.assertEqual(proj.loss, 30.0)
        self.assertEqual(proj.positions_lost, 1)

    def test_lapped_car_never_counts(self):
        laps = self.gapped_field([])
        me = laps[2]
        # A lapped car physically just behind us on track.
        laps.append(lap(4, 95.0, total=me["total_distance"] - TRACK - 50))
        proj = self.project(laps, 2)
        self.assertEqual(proj.positions_lost, 0)
        self.assertEqual(proj.behind, (1, None))

    def test_cars_in_pits_are_skipped(self):
        laps = self.gapped_field([5.0])
        laps[3]["pit_status"] = 1
        self.assertEqual(self.project(laps, 2).positions_lost, 0)

    def test_player_in_pits(self):
        laps = self.gapped_field([5.0])
        laps[2]["pit_status"] = 2
        self.assertTrue(self.project(laps, 2).in_pit)

    def test_leader_has_no_car_ahead(self):
        proj = self.project([lap(1, 0.0), lap(2, 30.0)], 0)
        self.assertEqual(proj.positions_lost, 0)
        self.assertIsNone(proj.ahead)

    def test_retired_player(self):
        laps = self.gapped_field([5.0])
        laps[2]["result_status"] = 4
        self.assertIsNone(self.project(laps, 2))

    def test_traffic_flags_lapped_car_at_rejoin_point(self):
        laps = self.gapped_field([])
        me = laps[2]
        # 90 s laps over 5000 m: a car 20.5 s behind on track is 1139 m back.
        # A 20 s stop lets it close to 0.5 s behind us, a lap down.
        back = 20.5 / 90 * TRACK
        laps.append(lap(4, 120.0, total=me["total_distance"] - TRACK - back))
        laps.append(lap(5, 140.0, total=me["total_distance"] - TRACK - 2000))
        laps[2]["last_lap_ms"] = 90_000
        traffic = self.project(laps, 2).traffic
        self.assertEqual(traffic[0], 1)
        self.assertAlmostEqual(traffic[1], -0.5, places=3)

    def test_traffic_ignores_same_lap_cars(self):
        laps = self.gapped_field([20.5])
        self.assertIsNone(self.project(laps, 2).traffic)

    def test_traffic_flags_car_lapping_us(self):
        laps = self.gapped_field([])
        me = laps[2]
        # The leader, a lap up, 19 s behind us on track: 1 s ahead after the stop.
        laps[0] = lap(1, 0.0, total=me["total_distance"] + TRACK - 19.0 / 90 * TRACK)
        traffic = self.project(laps, 2).traffic
        self.assertEqual(traffic[0], -1)
        self.assertAlmostEqual(traffic[1], 1.0, places=3)


class CompoundPaceTest(unittest.TestCase):

    def drive_lap(self, pace, car, lap_num, compound, s1, s2, s3, sc_status=0,
                  pit_sectors=(), invalid_from=None):
        """Feed one lap of observations: a packet per sector, then the rollover."""
        for sector in range(3):
            pit = 1 if sector in pit_sectors else 0
            invalid = 1 if invalid_from is not None and sector >= invalid_from else 0
            pace.observe(car, lap(1, lap_num=lap_num, sector=sector, pit_status=pit,
                                  invalid=invalid,
                                  s1_ms=s1 if sector >= 1 else 0,
                                  s2_ms=s2 if sector >= 2 else 0),
                         compound, sc_status)
        pace.observe(car, lap(1, lap_num=lap_num + 1, sector=0, last_lap_ms=s1 + s2 + s3),
                     compound, 0)

    def test_ranks_by_theoretical_lap(self):
        pace = race.CompoundPace()
        self.drive_lap(pace, 0, 5, "S", 30_000, 31_000, 29_000)
        self.drive_lap(pace, 1, 5, "M", 30_200, 31_100, 29_200)
        # A second soft runner with a better S2 improves the soft theoretical.
        self.drive_lap(pace, 2, 5, "S", 30_100, 30_900, 29_100)
        self.assertEqual(pace.ranking(), [("S", 89_900), ("M", 90_500)])

    def test_compound_needs_all_three_sectors(self):
        pace = race.CompoundPace()
        self.drive_lap(pace, 0, 5, "S", 30_000, 31_000, 29_000)
        pace.observe(1, lap(1, lap_num=5, sector=1, s1_ms=29_000), "H", 0)
        self.assertEqual(pace.ranking(), [("S", 90_000)])

    def test_only_previous_and_current_lap_count(self):
        pace = race.CompoundPace()
        self.drive_lap(pace, 0, 5, "S", 29_000, 30_000, 28_000)  # lap 5: very fast
        self.drive_lap(pace, 0, 6, "S", 30_000, 31_000, 29_000)
        self.drive_lap(pace, 0, 7, "S", 30_500, 31_500, 29_500)
        # Now on lap 8: only lap 7 (complete) and lap 8 (just started) remain.
        self.assertEqual(pace.ranking(), [("S", 91_500)])

    def test_pit_sectors_are_skipped(self):
        pace = race.CompoundPace()
        self.drive_lap(pace, 0, 5, "S", 30_000, 31_000, 29_000)
        # Out-lap: S1 starts in the pit lane on fresh mediums.
        self.drive_lap(pace, 1, 5, "M", 20_000, 31_100, 29_200, pit_sectors=(0,))
        self.assertEqual(pace.ranking(), [("S", 90_000)])

    def test_invalid_lap_taints_only_the_sector_it_happened_in(self):
        pace = race.CompoundPace()
        self.drive_lap(pace, 0, 5, "S", 25_000, 31_000, 29_000, invalid_from=0)
        self.drive_lap(pace, 1, 5, "S", 30_000, 31_500, 29_500)
        # Car 0's cut S1 is out; its S2 and S3 still count.
        self.assertEqual(pace.ranking(), [("S", 90_000)])

    def test_safety_car_sectors_are_skipped(self):
        pace = race.CompoundPace()
        self.drive_lap(pace, 0, 5, "S", 40_000, 41_000, 39_000, sc_status=race.SC_FULL)
        self.assertEqual(pace.ranking(), [])

    def test_flashback_clears_history(self):
        pace = race.CompoundPace()
        self.drive_lap(pace, 0, 5, "S", 30_000, 31_000, 29_000)
        pace.observe(0, lap(1, lap_num=4, sector=2), "S", 0)
        self.assertEqual(pace.ranking(), [])

    def test_retired_car_is_dropped(self):
        pace = race.CompoundPace()
        self.drive_lap(pace, 0, 5, "S", 30_000, 31_000, 29_000)
        pace.observe(0, lap(1, lap_num=6, result_status=4), "S", 0)
        self.assertEqual(pace.ranking(), [])


class FormatTest(unittest.TestCase):

    def test_gap_fits_five_chars(self):
        cases = {(0, -0.314): "-0.31", (0, 12.34): "+12.3", (0, -123.4): "-123",
                 (0, 5000.0): "+999", (1, None): "+1L", (-2, None): "-2L", (0, None): "",
                 # Values that round up into the next width bracket.
                 (0, 9.999): "+10.0", (0, -99.99): "-100"}
        for args, text in cases.items():
            self.assertEqual(race.format_gap(*args), text)
            self.assertLessEqual(len(text), 5)

    def test_magnitude(self):
        self.assertEqual(race.format_magnitude(0, 0.84), "0.84")
        self.assertEqual(race.format_magnitude(-1, None), "1L")

    def test_pace_gap(self):
        self.assertEqual(race.format_pace_gap(503), "+0.503")
        self.assertEqual(race.format_pace_gap(12_345), "+12.3")

    def test_penalty_fits_four_chars(self):
        self.assertEqual([race.format_penalty(s) for s in (5, 15, 99, 255)],
                         ["+5s", "+15s", "+99s", "+99s"])

    def test_offset(self):
        self.assertEqual([race.format_offset(m) for m in (0, 5, 45, 60, 90, 105, 120)],
                         ["now", "+5m", "+45m", "+1h", "+90m", "105m", "+2h"])

    def test_short_name(self):
        self.assertEqual(race.short_name("NORRIS"), "NORRIS")
        self.assertEqual(race.short_name("Max Verstappen"), "Verstappen")
        self.assertEqual(race.short_name("AVeryLongGamertag123"), "AVeryLongG")
        self.assertEqual(race.short_name("  Lewis   Hamilton  "), "Hamilton")


if __name__ == "__main__":
    unittest.main()
