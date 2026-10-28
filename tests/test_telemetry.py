"""Tests for the 2026 telemetry parsers.

The packets are synthetic. Their layouts are written out field by field from
the 2026 Season Pack spec rather than reused from telemetry.py, so an offset or
width typo in a parser cannot quietly agree with itself here.

Run from the project root:

    python3 -m unittest discover -t . -s tests -v
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telemetry


# --- layouts restated from the spec -------------------------------------------

# PacketHeader: packetFormat, gameYear, gameMajor, gameMinor, packetVersion,
# packetId, sessionUID, sessionTime, frameIdentifier, overallFrameIdentifier,
# playerCarIndex, secondaryPlayerCarIndex.
HEADER = "<HBBBBBQfIIBB"

NUM_CARS = 24

# PacketSessionData up to the weather samples: weather, trackTemperature,
# airTemperature, totalLaps, trackLength, sessionType, trackId, formula,
# sessionTimeLeft, sessionDuration, pitSpeedLimit, gamePaused, isSpectating,
# spectatorCarIndex, sliProNativeSupport, numMarshalZones, 21 x (float zoneStart,
# int8 zoneFlag), safetyCarStatus, networkGame, numWeatherForecastSamples.
SESSION_HEAD = "<BbbBHBbBHHBBBBBB" + "fb" * 21 + "BBB"
# WeatherForecastSample: sessionType, timeOffset, weather, trackTemperature,
# trackTemperatureChange, airTemperature, airTemperatureChange, rainPercentage.
WEATHER_SAMPLE = "<BBBbbbbB"

# LapData: lastLapTime, currentLapTime, four (ms, minutes) time pairs (sector 1,
# sector 2, delta to car in front, delta to leader), lapDistance, totalDistance,
# safetyCarDelta, carPosition, currentLapNum, pitStatus, numPitStops, sector,
# currentLapInvalid, penalties, totalWarnings, cornerCuttingWarnings,
# numUnservedDriveThroughPens, numUnservedStopGoPens, gridPosition,
# driverStatus, resultStatus, pitLaneTimerActive, pitLaneTimeInLaneInMS,
# pitStopTimerInMS, pitStopShouldServePen, speedTrapFastestSpeed,
# speedTrapFastestLap.
LAP = "<2I" + "HB" * 4 + "3f" + "5B" + "10B" + "2H" + "B" + "f" + "B"

# ParticipantData: aiControlled, driverId, networkId, teamId (uint16 each in
# 2026), myTeam, raceNumber, nationality, name[32], yourTelemetry,
# showOnlineNames, techLevel, platform, numColours, 4x RGB livery.
PARTICIPANT = "<BHHHBBB32sBBHBB12B"

# CarStatusData: tractionControl, antiLockBrakes, fuelMix, frontBrakeBias,
# pitLimiterStatus, fuelInTank, fuelCapacity, fuelRemainingLaps, maxRPM, idleRPM,
# maxGears, drsAllowed, drsActivationDistance, actualTyreCompound,
# visualTyreCompound, tyresAgeLaps, vehicleFIAFlags, enginePowerICE,
# enginePowerMGUK, ersStoreEnergy, ersDeployMode, ersHarvestedThisLapMGUK,
# ersHarvestedThisLapMGUH, ersHarvestLimitPerLap, ersDeployedThisLap,
# networkPaused.
CAR_STATUS = "<5B" + "3f" + "2H" + "2B" + "H" + "3B" + "b" + "3f" + "B" + "4f" + "B"

# CarDamageData: tyresWear[4] (f), tyresDamage[4], brakesDamage[4],
# tyreBlisters[4], frontLeftWing, frontRightWing, rearWing, floor, diffuser,
# sidepod, drsFault, ersFault, gearBox, engine, six engine wear values,
# engineBlown, engineSeized.
CAR_DAMAGE = "<4f" + "4B4B4B" + "6B" + "2B" + "2B" + "6B" + "2B"

# CarTelemetry2Data: activeAeroMode, activeAeroAvailable,
# activeAeroActivationDistance, overtakeAvailable, overtakeActive,
# overtakeActivationDistance, 2026Regulations, drivingWrongWay.
CAR_TELEMETRY2 = "<BBHBBHBB"

# Packet sizes the spec states, as an independent check on our arithmetic.
SPEC_SIZES = {"session": 926, "lap": 1399, "participants": 1470, "car_status": 1445,
              "car_damage": 1133, "car_telemetry2": 269}


# --- packet builders ------------------------------------------------------------

def header(packet_id, packet_format=2026, player_idx=0, session_uid=0xABCD):
    return struct.pack(HEADER, packet_format, 26, 1, 0, 1, packet_id, session_uid, 12.5,
                       99, 99, player_idx, 255)


def per_car(entries, default):
    return b"".join(entries.get(i, default) for i in range(NUM_CARS))


def session_packet(session_type=15, track_id=42, track_length=5470, sc_status=0,
                   samples=(), **kw):
    head = struct.pack(SESSION_HEAD, 0, 30, 25, 57, track_length, session_type, track_id,
                       13, 3600, 7200, 80, 0, 0, 255, 0, 21, *([0.0, 0] * 21),
                       sc_status, 1, len(samples))
    weather = b"".join(struct.pack(WEATHER_SAMPLE, st, offset, w, 30, 2, 25, 2, rain)
                       for st, offset, w, rain in samples)
    weather += bytes(8 * (64 - len(samples)))
    # Everything after the samples, up to the spec's 926 bytes.
    tail = bytes(926 - 29 - len(head) - len(weather))
    return header(1, **kw) + head + weather + tail


def lap_entry(last_lap=0, s1=(0, 0), s2=(0, 0), leader=(0, 0), lap_distance=0.0,
              total_distance=0.0, position=1, lap_num=1, pit_status=0, sector=0,
              invalid=0, penalties=0, unserved_dt=0, unserved_sg=0, result_status=2,
              serve_pen=0):
    return struct.pack(LAP, last_lap, 0, s1[0], s1[1], s2[0], s2[1], 0, 0, leader[0], leader[1],
                       lap_distance, total_distance, 0.0,
                       position, lap_num, pit_status, 0, sector,
                       invalid, penalties, 0, 0, unserved_dt, unserved_sg, 1, 4, result_status,
                       0, 0, 0, serve_pen, 0.0, 255)


def lap_packet(entries=None, **kw):
    return header(2, **kw) + per_car(entries or {}, lap_entry()) + b"\xff\xff"


def participant(name=b"", race_number=0, your_telemetry=1, team_id=0):
    return struct.pack(PARTICIPANT, 1, 0, 0, team_id, 0, race_number, 0, name,
                       your_telemetry, 1, 0, 1, 0, *([0] * 12))


def participants_packet(entries=None, **kw):
    entries = entries or {}
    return header(4, **kw) + bytes([len(entries)]) + per_car(entries, participant())


def status_car(visual_tyre=16, ers_store=0.0, ers_mode=0):
    return struct.pack(CAR_STATUS, 0, 0, 1, 55, 0, 50.0, 100.0, 20.0, 13000, 3500,
                       8, 0, 0, 18, visual_tyre, 3, 0, 500000.0, 120000.0, ers_store, ers_mode,
                       1.0, 2.0, 3.0, 4.0, 0)


def status_packet(cars=None, **kw):
    return header(7, **kw) + per_car(cars or {}, status_car())


def damage_car(wear=(0.0, 0.0, 0.0, 0.0), fw_left=0, fw_right=0):
    return struct.pack(CAR_DAMAGE, *wear, *([7] * 12), fw_left, fw_right, *([7] * 16))


def damage_packet(cars=None, **kw):
    return header(10, **kw) + per_car(cars or {}, damage_car())


def telemetry2_car(overtake_available=0, overtake_active=0, regs_2026=1):
    return struct.pack(CAR_TELEMETRY2, 0, 1, 0, overtake_available, overtake_active, 120,
                       regs_2026, 0)


def telemetry2_packet(cars=None, **kw):
    return header(16, **kw) + per_car(cars or {}, telemetry2_car())


BUILDERS = {
    "session": session_packet,
    "lap": lap_packet,
    "participants": participants_packet,
    "car_status": status_packet,
    "car_damage": damage_packet,
    "car_telemetry2": telemetry2_packet,
}


def payload(packet):
    h = telemetry.parse_header(packet)
    assert h is not None and h["packet_format"] == telemetry.PACKET_FORMAT
    parsed = telemetry.parse_payload(h["packet_id"], packet)
    assert parsed is not None, "packet rejected"
    return parsed


LAST = NUM_CARS - 1


class LayoutTest(unittest.TestCase):

    def test_header_size(self):
        self.assertEqual(telemetry.HEADER_SIZE, 29)

    def test_builders_match_spec_sizes(self):
        for kind, size in SPEC_SIZES.items():
            with self.subTest(kind=kind):
                self.assertEqual(len(BUILDERS[kind]()), size)

    def test_parser_layouts_match_independent_layouts(self):
        self.assertEqual(telemetry.NUM_CARS, NUM_CARS)
        self.assertEqual(telemetry.LAP_DATA.size, struct.calcsize(LAP))
        self.assertEqual(telemetry.PARTICIPANT.size, struct.calcsize(PARTICIPANT))
        self.assertEqual(telemetry.CAR_STATUS.size, struct.calcsize(CAR_STATUS))
        self.assertEqual(telemetry.CAR_DAMAGE.size, struct.calcsize(CAR_DAMAGE))
        self.assertEqual(telemetry.CAR_TELEMETRY2.size, struct.calcsize(CAR_TELEMETRY2))
        self.assertEqual(telemetry.WEATHER_SAMPLE.size, struct.calcsize(WEATHER_SAMPLE))


class ParseTest(unittest.TestCase):
    """Every field the overlay reads round-trips, in the last car slot where a
    wrong per-car size would show."""

    def test_header(self):
        h = telemetry.parse_header(header(2, player_idx=19, session_uid=77))
        self.assertEqual(h, {"packet_format": 2026, "packet_id": 2, "session_uid": 77,
                             "player_car_index": 19})

    def test_session(self):
        samples = [(15, 0, 0, 0), (15, 5, 3, 40), (15, 30, 5, 100), (16, 0, 1, 10)]
        s = payload(session_packet(session_type=15, track_id=42, track_length=5470,
                                   sc_status=2, samples=samples))
        self.assertEqual(s["session_type"], 15)
        self.assertEqual(s["track_id"], 42)
        self.assertEqual(s["track_length"], 5470)
        self.assertEqual(s["total_laps"], 57)
        self.assertEqual(s["safety_car_status"], 2)
        self.assertEqual(s["weather"], [
            {"session_type": st, "time_offset": o, "weather": w, "rain_pct": r}
            for st, o, w, r in samples
        ])

    def test_lap(self):
        entry = lap_entry(last_lap=91234, s1=(12345, 1), s2=(500, 0), leader=(2500, 1),
                          lap_distance=2500.5, total_distance=12500.25, position=7, lap_num=3,
                          pit_status=1, sector=2, invalid=1, penalties=15, unserved_dt=1,
                          unserved_sg=2, result_status=3, serve_pen=1)
        lap = payload(lap_packet({LAST: entry}))[LAST]
        self.assertEqual(lap, {
            "last_lap_ms": 91234,
            "s1_ms": 72345,
            "s2_ms": 500,
            "delta_to_leader": 62.5,
            "lap_distance": 2500.5,
            "total_distance": 12500.25,
            "position": 7,
            "lap_num": 3,
            "pit_status": 1,
            "sector": 2,
            "lap_invalid": 1,
            "penalties": 15,
            "unserved_dt": 1,
            "unserved_sg": 2,
            "result_status": 3,
            "serve_pen_at_stop": 1,
        })

    def test_participants(self):
        # 485 only fits the 2026 uint16 team id.
        entry = participant(b"Driver \xc3\x96ne", 27, your_telemetry=0, team_id=485)
        p = payload(participants_packet({LAST: entry}))[LAST]
        self.assertEqual(p, {"name": "Driver Öne", "race_number": 27, "your_telemetry": 0})

    def test_car_status(self):
        s = payload(status_packet({LAST: status_car(visual_tyre=8, ers_store=3_000_000.0,
                                                    ers_mode=3)}))[LAST]
        self.assertEqual(s, {"visual_tyre": 8, "ers_store": 3_000_000.0, "ers_mode": 3})

    def test_car_damage(self):
        d = payload(damage_packet({LAST: damage_car((1.5, 2.5, 3.5, 4.5), 12, 40)}))[LAST]
        self.assertEqual(d, {"tyre_wear": (1.5, 2.5, 3.5, 4.5), "fw_left": 12, "fw_right": 40})

    def test_car_telemetry2(self):
        cars = payload(telemetry2_packet({
            0: telemetry2_car(overtake_available=1, overtake_active=0, regs_2026=0),
            LAST: telemetry2_car(overtake_available=1, overtake_active=1, regs_2026=1),
        }))
        self.assertEqual(cars[0], {"overtake_available": 1, "overtake_active": 0, "regs_2026": 0})
        self.assertEqual(cars[LAST], {"overtake_available": 1, "overtake_active": 1,
                                      "regs_2026": 1})

    def test_rejects_short_packets(self):
        trailers = {"lap": 2, "session": 926 - 29 - struct.calcsize(SESSION_HEAD) - 8 * 64}
        for kind, build in BUILDERS.items():
            with self.subTest(kind=kind):
                packet = build()
                packet = packet[:len(packet) - trailers.get(kind, 0) - 1]
                h = telemetry.parse_header(packet)
                self.assertIsNone(telemetry.parse_payload(h["packet_id"], packet))

    def test_truncated_header(self):
        self.assertIsNone(telemetry.parse_header(b"\x00" * 28))

    def test_2025_format_is_identified(self):
        h = telemetry.parse_header(lap_packet(packet_format=2025))
        self.assertNotEqual(h["packet_format"], telemetry.PACKET_FORMAT)

    def test_ignored_packet_ids(self):
        for packet_id in (0, 3, 6, 11, 15):
            self.assertIsNone(telemetry.parse_payload(packet_id, header(packet_id) + bytes(2000)))


if __name__ == "__main__":
    unittest.main()
