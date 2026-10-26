"""F1 25 2026 Season Pack UDP parsing.

Reference: "2026 Season Pack Telemetry Output Structures.txt" and "Data Output
from F1 25 2026 Season Pack.pdf", as kept in f1-race-control/specs/25vs26.

Only the 2026 wire format is supported. Every parser turns one datagram into
plain dicts (one per car slot) and returns None when the packet is too short to
trust, which is the only length checking in the program. Nothing outside this
module knows a struct offset.
"""

import struct

PACKET_FORMAT = 2026
NUM_CARS = 24

# PacketHeader: packetFormat, gameYear, gameMajor, gameMinor, packetVersion,
# packetId, sessionUID, sessionTime, frameIdentifier, overallFrameIdentifier,
# playerCarIndex, secondaryPlayerCarIndex.
HEADER = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE = HEADER.size  # 29

PACKET_SESSION = 1
PACKET_LAP = 2
PACKET_PARTICIPANTS = 4
PACKET_CAR_STATUS = 7
PACKET_CAR_DAMAGE = 10
PACKET_CAR_TELEMETRY2 = 16

# Session packet up to m_numMarshalZones: weather, trackTemperature,
# airTemperature, totalLaps, trackLength, sessionType, trackId, formula,
# sessionTimeLeft, sessionDuration, pitSpeedLimit, gamePaused, isSpectating,
# spectatorCarIndex, sliProNativeSupport, numMarshalZones.
SESSION_PREFIX = struct.Struct("<BbbBHBbBHHBBBBBB")
# MarshalZone: zoneStart (float), zoneFlag (int8). The array is always 21 long.
MARSHAL_ZONE_SIZE = struct.calcsize("<fb")
MAX_MARSHAL_ZONES = 21
# After the marshal zones: safetyCarStatus, networkGame, numWeatherForecastSamples.
SESSION_POST_MARSHAL = struct.Struct("<BBB")
# WeatherForecastSample: sessionType, timeOffset, weather, trackTemperature,
# trackTemperatureChange, airTemperature, airTemperatureChange, rainPercentage.
WEATHER_SAMPLE = struct.Struct("<BBBbbbbB")
MAX_WEATHER_SAMPLES = 64

# LapData - 57 bytes, unchanged from 2025. Packet: 29 + 24 * 57 + 2 = 1399 bytes.
LAP_DATA = struct.Struct("<IIHBHBHBHBfffBBBBBBBBBBBBBBBHHBfB")

# ParticipantData - 60 bytes: aiControlled (uint8), driverId, networkId, teamId
# (uint16 each), myTeam, raceNumber, nationality, name[32], yourTelemetry,
# showOnlineNames, techLevel (uint16), platform, numColours, 4 x RGB livery.
# Packet: 29 + 1 + 24 * 60 = 1470 bytes.
PARTICIPANT = struct.Struct("<B3H3B32s2BH2B12B")

# CarStatusData - 59 bytes: the 2025 layout plus m_ersHarvestLimitPerLap after
# m_ersHarvestedThisLapMGUH. Packet: 29 + 24 * 59 = 1445 bytes.
CAR_STATUS = struct.Struct("<BBBBBfffHHBBHBBBbfffBffffB")

# CarDamageData - 46 bytes: tyresWear[4] (float) + 30 uint8 damage values,
# unchanged from 2025. Packet: 29 + 24 * 46 = 1133 bytes.
CAR_DAMAGE = struct.Struct("<4f30B")

# CarTelemetry2Data - 10 bytes: activeAeroMode, activeAeroAvailable (uint8),
# activeAeroActivationDistance (uint16), overtakeAvailable, overtakeActive
# (uint8), overtakeActivationDistance (uint16), 2026Regulations,
# drivingWrongWay (uint8). Packet: 29 + 24 * 10 = 269 bytes.
CAR_TELEMETRY2 = struct.Struct("<BBHBBHBB")


def parse_header(data):
    if len(data) < HEADER_SIZE:
        return None
    h = HEADER.unpack_from(data, 0)
    return {
        "packet_format": h[0],
        "packet_id": h[5],
        "session_uid": h[6],
        "player_car_index": h[10],
    }


def _unpack_cars(data, layout, offset=HEADER_SIZE):
    if len(data) < offset + NUM_CARS * layout.size:
        return None
    return [layout.unpack_from(data, offset + i * layout.size) for i in range(NUM_CARS)]


def parse_session(data):
    offset = HEADER_SIZE + SESSION_PREFIX.size + MARSHAL_ZONE_SIZE * MAX_MARSHAL_ZONES
    if len(data) < offset + SESSION_POST_MARSHAL.size + MAX_WEATHER_SAMPLES * WEATHER_SAMPLE.size:
        return None
    pre = SESSION_PREFIX.unpack_from(data, HEADER_SIZE)
    post = SESSION_POST_MARSHAL.unpack_from(data, offset)
    offset += SESSION_POST_MARSHAL.size

    weather = []
    for i in range(min(post[2], MAX_WEATHER_SAMPLES)):
        s = WEATHER_SAMPLE.unpack_from(data, offset + i * WEATHER_SAMPLE.size)
        weather.append({
            "session_type": s[0],
            "time_offset": s[1],
            # 0 clear, 1 light cloud, 2 overcast, 3 light rain, 4 heavy rain, 5 storm.
            "weather": s[2],
            "rain_pct": s[7],
        })

    return {
        "total_laps": pre[3],
        "track_length": pre[4],
        "session_type": pre[5],
        "track_id": pre[6],
        # 0 none, 1 full safety car, 2 virtual, 3 formation lap.
        "safety_car_status": post[0],
        "weather": weather,
    }


def parse_lap(data):
    cars = _unpack_cars(data, LAP_DATA)
    if cars is None:
        return None
    return [{
        "last_lap_ms": l[0],
        # Sector times come split as (msPart, minutesPart); each is 0 until the
        # car crosses that sector's end, then latched for the rest of the lap.
        "s1_ms": l[3] * 60_000 + l[2],
        "s2_ms": l[5] * 60_000 + l[4],
        "delta_to_leader": l[9] * 60.0 + l[8] / 1000.0,
        "lap_distance": l[10],
        "total_distance": l[11],
        "position": l[13],
        "lap_num": l[14],
        # 0 none, 1 pitting, 2 in pit area.
        "pit_status": l[15],
        # 0 = S1, 1 = S2, 2 = S3.
        "sector": l[17],
        "lap_invalid": l[18],
        "unserved_sg": l[23],
        # 0 invalid, 1 inactive, 2 active, 3 finished, 4 DNF, 5 DSQ, 6 NC, 7 retired.
        "result_status": l[26],
        "serve_pen_at_stop": l[30],
    } for l in cars]


def parse_participants(data):
    cars = _unpack_cars(data, PARTICIPANT, HEADER_SIZE + 1)  # skip m_numActiveCars
    if cars is None:
        return None
    return [{
        "name": p[7].split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip(),
        "race_number": p[5],
        # 0 = restricted: the game zeroes this car's ERS, tyre wear and wing
        # damage for everyone but its driver. 1 = public.
        "your_telemetry": p[8],
    } for p in cars]


def parse_car_status(data):
    cars = _unpack_cars(data, CAR_STATUS)
    if cars is None:
        return None
    return [{
        # 16 soft, 17 medium, 18 hard, 7 inter, 8 wet.
        "visual_tyre": s[14],
        # Joules.
        "ers_store": s[19],
        # 0 none, 1 medium, 2 hotlap, 3 boost.
        "ers_mode": s[20],
    } for s in cars]


def parse_car_damage(data):
    cars = _unpack_cars(data, CAR_DAMAGE)
    if cars is None:
        return None
    return [{
        # Percentages, ordered RL, RR, FL, FR.
        "tyre_wear": (d[0], d[1], d[2], d[3]),
        "fw_left": d[16],
        "fw_right": d[17],
    } for d in cars]


def parse_car_telemetry2(data):
    cars = _unpack_cars(data, CAR_TELEMETRY2)
    if cars is None:
        return None
    return [{
        "overtake_available": t[3],
        "overtake_active": t[4],
        # 0 = the car runs pre-2026 regulations and has no Overtake Mode.
        "regs_2026": t[6],
    } for t in cars]


PARSERS = {
    PACKET_SESSION: parse_session,
    PACKET_LAP: parse_lap,
    PACKET_PARTICIPANTS: parse_participants,
    PACKET_CAR_STATUS: parse_car_status,
    PACKET_CAR_DAMAGE: parse_car_damage,
    PACKET_CAR_TELEMETRY2: parse_car_telemetry2,
}


def parse_payload(packet_id, data):
    """Parse a 2026-format datagram whose header is already known.

    Returns None for packet types this app ignores and for short packets.
    """
    parser = PARSERS.get(packet_id)
    return parser(data) if parser else None
