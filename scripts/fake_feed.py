"""Send a synthetic 2026-format race to the overlay, for checking the layout
without the game.

The feed is built to hit worst-case widths: 24 cars, 10+ character and long
gamertag names, a restricted car, a pre-2026-regs car, two lapped cars, every
compound and ERS mode, 100/100 wing damage, every penalty cell (+5s, +15s from
a stop-go, a lone DT, +99s with a DT) and 12 weather samples.

    python3 scripts/fake_feed.py                # race on 127.0.0.1:20777
    python3 scripts/fake_feed.py --sc 1         # under safety car (2 = VSC)
    python3 scripts/fake_feed.py --pit          # player in the pit lane
    python3 scripts/fake_feed.py --format 2025  # wrong UDP format
"""

import argparse
import math
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telemetry as t

TRACK = 5000.0
PLAYER = 6
RESTRICTED = 9
PRE_2026 = 11
LAPPED = (22, 23)
# Car index -> (time penalty seconds, unserved drive-throughs, unserved stop-gos).
PENALTIES = {3: (5, 0, 0), 5: (5, 0, 1), PLAYER: (10, 0, 0), 7: (0, 1, 0), 8: (99, 1, 0)}
NAMES = ["NORRIS", "LECLERC", "HAMILTON", "Max Verstappen", "RUSSELL", "PIASTRI", "YANIUK",
         "ALONSO", "GASLY", "AVeryLongGamertag123", "OCON", "STROLL", "HULKENBERG",
         "BORTOLETO", "ANTONELLI", "HADJAR", "LAWSON", "ALBON", "SAINZ", "BEARMAN",
         "COLAPINTO", "TSUNODA", "PEREZ", "BOTTAS"]
COMPOUNDS = (16, 17, 18)  # soft, medium, hard
COMPOUND_PACE = {16: -0.4, 17: 0.0, 18: 0.6}
WEATHER = [(0, 0, 0), (5, 1, 0), (10, 1, 5), (15, 2, 15), (20, 2, 30), (25, 3, 45),
           (30, 3, 60), (45, 4, 80), (60, 4, 100), (75, 5, 90), (90, 3, 50), (120, 1, 10)]


def header(fmt, packet_id):
    return t.HEADER.pack(fmt, fmt % 100, 1, 0, 1, packet_id, 0xF1, 1.0, 0, 0, PLAYER, 255)


def lap_time(i):
    return 88.0 + i * 0.12 + COMPOUND_PACE[COMPOUNDS[i % 3]]


class Feed:

    def __init__(self, args):
        self.args = args
        self.t0 = time.monotonic()
        # Spread the field ~1.1 s apart, two cars a lap down.
        self.start = []
        for i in range(t.NUM_CARS):
            v = TRACK / lap_time(i)
            d = 9 * TRACK + 4000 - i * 1.1 * v
            if i in LAPPED:
                d -= TRACK
            self.start.append(d)

    def distances(self, now):
        return [self.start[i] + TRACK / lap_time(i) * now for i in range(t.NUM_CARS)]

    def lap_packet(self, fmt, now):
        dist = self.distances(now)
        order = sorted(range(t.NUM_CARS), key=lambda i: -dist[i])
        leader = order[0]
        body = b""
        for i in range(t.NUM_CARS):
            lt = lap_time(i)
            lap_num = int(dist[i] // TRACK) + 1
            lap_dist = dist[i] % TRACK
            sector = min(2, int(lap_dist // (TRACK / 3)))
            # Per-lap jitter so the pace row moves.
            jitter = 0.15 * math.sin(lap_num * 1.7 + i)
            s1 = int((lt / 3 + jitter) * 1000) if sector >= 1 else 0
            s2 = int((lt / 3 - jitter / 2) * 1000) if sector >= 2 else 0
            last = int((lt + jitter / 2) * 1000)
            delta = (dist[leader] - dist[i]) / (TRACK / lt)
            pit = 1 if self.args.pit and i == PLAYER else 0
            pen, dt, sg = PENALTIES.get(i, (0, 0, 0))
            body += t.LAP_DATA.pack(
                last, 0, s1 % 60_000, s1 // 60_000, s2 % 60_000, s2 // 60_000, 0, 0,
                int(delta * 1000) % 60_000, int(delta * 1000) // 60_000,
                lap_dist, dist[i], 0.0, order.index(i) + 1, lap_num, pit, 0, sector,
                0, pen, 0, 0, dt, sg, i + 1, 4, 2, 0, 0, 0, 0, 0.0, 255)
        return header(fmt, t.PACKET_LAP) + body + b"\xff\xff"

    def session_packet(self, fmt):
        head = t.SESSION_PREFIX.pack(0, 30, 25, 57, int(TRACK), 15, 42, 13, 3600, 7200,
                                     80, 0, 0, 255, 0, 21)
        zones = struct.pack("<" + "fb" * 21, *([0.0, 0] * 21))
        post = t.SESSION_POST_MARSHAL.pack(self.args.sc, 1, len(WEATHER))
        samples = b"".join(t.WEATHER_SAMPLE.pack(15, off, w, 30, 2, 25, 2, rain)
                           for off, w, rain in WEATHER)
        samples += bytes(8 * (64 - len(WEATHER)))
        body = head + zones + post + samples
        return header(fmt, t.PACKET_SESSION) + body + bytes(926 - 29 - len(body))

    def participants_packet(self, fmt):
        body = bytes([t.NUM_CARS])
        for i, name in enumerate(NAMES):
            body += t.PARTICIPANT.pack(1, 0, 0, 0, 0, i + 1, 0, name.encode(),
                                       0 if i == RESTRICTED else 1, 1, 0, 1, 0, *([0] * 12))
        return header(fmt, t.PACKET_PARTICIPANTS) + body

    def status_packet(self, fmt, now):
        body = b""
        for i in range(t.NUM_CARS):
            store = 4_000_000.0 * (0.5 + 0.5 * math.sin(now / 7 + i))
            mode = (i + int(now / 5)) % 4
            if i == RESTRICTED:
                store, mode = 0.0, 0
            body += t.CAR_STATUS.pack(0, 0, 1, 55, 0, 50.0, 100.0, 20.0, 13000, 3500, 8, 0, 0,
                                      18, COMPOUNDS[i % 3] if i != 4 else 7, 3, 0,
                                      500000.0, 120000.0, store, mode, 1.0, 2.0, 3.0, 4.0, 0)
        return header(fmt, t.PACKET_CAR_STATUS) + body

    def damage_packet(self, fmt, now):
        body = b""
        for i in range(t.NUM_CARS):
            wear = min(100.0, 10 + i * 4 + now / 10)
            fw = {3: (12, 0), 5: (100, 100), 10: (0, 30)}.get(i, (0, 0))
            if i == RESTRICTED:
                wear, fw = 0.0, (0, 0)
            body += t.CAR_DAMAGE.pack(wear * 0.8, wear * 0.9, wear * 0.7, wear, *([0] * 12),
                                      *fw, *([0] * 16))
        return header(fmt, t.PACKET_CAR_DAMAGE) + body

    def telemetry2_packet(self, fmt, now):
        body = b""
        for i in range(t.NUM_CARS):
            available = 1 if i % 2 else 0
            active = 1 if available and int(now / 3 + i) % 3 == 0 else 0
            body += t.CAR_TELEMETRY2.pack(0, 1, 0, available, active, 0,
                                          0 if i == PRE_2026 else 1, 0)
        return header(fmt, t.PACKET_CAR_TELEMETRY2) + body


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=20777)
    parser.add_argument("--sc", type=int, default=0, choices=(0, 1, 2),
                        help="safety car status: 1 full, 2 virtual")
    parser.add_argument("--pit", action="store_true", help="player in the pit lane")
    parser.add_argument("--format", type=int, default=2026, choices=(2025, 2026))
    args = parser.parse_args()

    feed = Feed(args)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.host, args.port)
    print(f"Sending a synthetic race to {target[0]}:{target[1]}, Ctrl+C to stop")
    tick = 0
    while True:
        now = time.monotonic() - feed.t0
        packets = [feed.lap_packet(args.format, now), feed.status_packet(args.format, now),
                   feed.damage_packet(args.format, now), feed.telemetry2_packet(args.format, now)]
        if tick % 20 == 0:
            packets += [feed.session_packet(args.format), feed.participants_packet(args.format)]
        for packet in packets:
            sock.sendto(packet, target)
        tick += 1
        time.sleep(0.05)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
