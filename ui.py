"""Tk overlay: one fixed-size canvas, redrawn from the latest telemetry.

Threading: a daemon thread blocks on the UDP socket, parses each datagram and
replaces whole attributes on the app (`self.laps = new_list`). It never
touches Tk. A size-1 queue wakes `_poll` on the Tk thread, which coalesces
packets into at most one redraw per POLL_MS. Attribute assignment is atomic in
CPython and published lists are never mutated in place, so there is no lock.

Geometry is measured once from the fonts against worst-case strings (Layout),
so the window never resizes and no value can clip.
"""

import queue
import socket
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import race
import telemetry

POLL_MS = 100
# The font size the fixed pixel sizes in Layout are tuned for.
BASE_FONT_SIZE = 10
STALE_S = 2.0
NO_SIGNAL_S = 10.0

# --- palette -------------------------------------------------------------------

BG = "#101317"
PLAYER_BG = "#1d2b3f"
LINE = "#262b33"
FG = "#e5e7eb"
BRIGHT = "#ffffff"
DIM = "#6b7280"
FAINT = "#3a404a"
GOOD = "#22c55e"
WARN = "#facc15"
ALERT = "#fb923c"
BAD = "#ef4444"
OT_AVAILABLE = "#3f8f5c"

TYRE_COLORS = {"S": "#ef4444", "M": "#facc15", "H": "#f3f4f6", "I": "#22c55e", "W": "#3b82f6"}
# ERS deploy mode letter -> (pill fill, text).
ERS_MODE_COLORS = {
    "N": ("#374151", "#9ca3af"),
    "M": ("#1d4ed8", "#e5e7eb"),
    "H": ("#d97706", "#111827"),
    "B": ("#e11d48", "#ffffff"),
}
# Weather type -> strip color: clear, light cloud, overcast, light rain,
# heavy rain, storm.
WEATHER_COLORS = {0: "#f59e0b", 1: "#d1d5db", 2: "#6b7280", 3: "#7dd3fc", 4: "#2563eb", 5: "#7c3aed"}


def wear_color(pct):
    if pct < 40:
        return GOOD
    if pct < 60:
        return WARN
    if pct < 75:
        return ALERT
    return BAD


def damage_color(pct):
    if pct < 25:
        return WARN
    if pct < 50:
        return ALERT
    return BAD


def rain_color(pct):
    if pct == 0:
        return DIM
    if pct < 25:
        return "#9ca3af"
    if pct < 50:
        return "#7dd3fc"
    if pct < 75:
        return "#38bdf8"
    return "#3b82f6"


# --- layout --------------------------------------------------------------------

# Driver row columns: key, worst-case content, kind, header label. Kinds: "w" /
# "e" text anchored left / right, "pill" text on a rectangle, "badge" a letter
# in a ring.
DRIVER_COLUMNS = (
    ("pos", "24", "e", ""),
    ("name", "W" * race.NAME_WIDTH, "w", "DRIVER"),
    ("gap", "+99.99", "e", "GAP"),
    ("ot", "OT", "pill", "OT"),
    ("mode", "B", "pill", "ERS"),
    ("ers", "100%", "e", ""),
    ("tyre", "S", "badge", "T"),
    ("wear", "100%", "e", "WEAR"),
    ("fw", "100/100", "e", "FW"),
    ("pen", race.format_penalty(99), "pill", "PEN"),
    ("dt", "DT", "pill", ""),
)
# Header labels that start at their column's left edge and span the next one.
SPANNING_LABELS = {"mode", "pen"}

# The widest pit and pace rows the window must fit.
WORST_PIT = race.PitProjection(loss=99, sc_status=race.SC_VIRTUAL, position_now=10,
                               position_after=24, ahead=(0, 99.99), behind=(0, 99.99),
                               traffic=(-9, -9.9))
WORST_PACE = [("S", 0), ("M", 9_999), ("H", 9_999)]
# Forecast samples the window always has room for, at any font size.
MIN_WEATHER_COLS = 12


class Column:
    def __init__(self, x0, width, kind):
        self.x0 = x0
        self.x1 = x0 + width
        self.kind = kind

    @property
    def center(self):
        return (self.x0 + self.x1) / 2


class Layout:
    """Pixel geometry, measured once from the fonts.

    Text-driven sizes come from font metrics. The few fixed pixel sizes (strips,
    spacing, outlines, the signal dot) are given at BASE_FONT_SIZE and scaled
    by `scale`, so the whole window grows and shrinks proportionally.
    """

    def __init__(self, font, bold, small, scale=1.0):
        self.font, self.bold, self.small = font, bold, small
        self.scale = scale
        self.strip_h = self.px(3)
        self.sep_h = self.px(7)
        self.inset = self.px(2)
        self.line_w = self.px(1)
        self.ring_w = self.px(2)
        self.dot_r = self.px(3)

        self.cw = font.measure("0")
        self.row_h = font.metrics("linespace") + self.px(4)
        self.small_h = small.metrics("linespace")
        self.pill_h = self.row_h - self.px(4)
        self.pad = self.cw
        self.pad_y = self.cw // 2 + self.inset

        widths = []
        for _, sample, kind, _ in DRIVER_COLUMNS:
            w = bold.measure(sample)
            if kind == "pill":
                w += self.cw
            elif kind == "badge":
                w = max(w, self.pill_h)
            widths.append(w)
        gaps = len(widths) - 1
        min_weather_col = max(small.measure("+90m"), small.measure("100%")) + self.cw // 2
        self.width = max(
            sum(widths) + gaps * self.cw + 2 * self.pad,
            segments_width(self, pit_segments(self, WORST_PIT)) + 2 * self.pad,
            segments_width(self, pace_segments(self, WORST_PACE, None)) + 2 * self.pad,
            MIN_WEATHER_COLS * min_weather_col + 2 * self.pad,
        )
        self.inner_width = self.width - 2 * self.pad

        # Spread any width the other rows need across the column gaps, so the
        # standings span the window instead of leaving a dead band on the right.
        gap = (self.inner_width - sum(widths)) / gaps
        self.columns = {}
        x = self.pad
        for (key, _, kind, _), w in zip(DRIVER_COLUMNS, widths):
            self.columns[key] = Column(x, w, kind)
            x += w + gap

        self.max_weather_cols = int(self.inner_width // min_weather_col)

        self.weather_h = self.strip_h + self.inset + 2 * self.small_h
        self.header_h = self.small_h + self.inset
        self.height = (2 * self.pad_y + self.weather_h + self.header_h
                       + race.STANDINGS_ROWS * self.row_h
                       + 3 * self.sep_h + 2 * self.row_h)

    def px(self, n):
        """A pixel size given at BASE_FONT_SIZE, scaled; never below 1."""
        return max(1, round(n * self.scale))


# --- one-line rows built from segments -------------------------------------------

def _text(text, color, font):
    return ("text", text, color, font, None)


def _pill(text, fg, font, fill):
    return ("pill", text, fg, font, fill)


def segment_width(layout, segment):
    kind, text, _, font, _ = segment
    w = font.measure(text)
    return w + layout.cw if kind == "pill" else w


def segments_width(layout, segments):
    return sum(segment_width(layout, s) for s in segments)


def pit_segments(layout, proj):
    f, b = layout.font, layout.bold
    if proj is None:
        return [_text("PIT", DIM, f), _text("  –", DIM, f)]

    segs = [_text(f"PIT {proj.loss:.0f}s", FG, f)]
    if proj.sc_status == race.SC_FULL:
        segs += [_text(" ", FG, f), _pill("SC", BG, b, WARN)]
    elif proj.sc_status == race.SC_VIRTUAL:
        segs += [_text(" ", FG, f), _pill("VSC", BG, b, WARN)]
    if proj.in_pit:
        return segs + [_text("  IN PIT", WARN, b)]

    lost = proj.positions_lost
    segs += [
        _text(f"  P{proj.position_now}→P{proj.position_after}", FG, b),
        _text(f" (-{lost})" if lost else " (0)", BAD if lost else GOOD, f),
    ]
    for arrow, gap in (("▲", proj.ahead), ("▼", proj.behind)):
        if gap is None:
            segs.append(_text(f"  {arrow}–", DIM, f))
            continue
        laps, seconds = gap
        close = seconds is not None and seconds < 1.0
        segs.append(_text(f"  {arrow}{race.format_magnitude(laps, seconds)}",
                          BAD if close else (DIM if laps else FG), b if close else f))
    if proj.traffic is not None:
        laps, offset = proj.traffic
        arrow = "▲" if offset >= 0 else "▼"
        segs += [_text("  ", FG, f), _pill("!", BG, b, WARN),
                 _text(f" {laps:+d}L {arrow}{abs(offset):.1f}", WARN, f)]
    return segs


def pace_segments(layout, ranking, max_width):
    """'PACE  S  (M +0.503  H +1.120)'. Trailing compounds that would not fit
    in `max_width` are dropped; None means no limit."""
    f, b = layout.font, layout.bold
    segs = [_text("PACE  ", DIM, f)]
    if not ranking:
        return segs + [_text("–", DIM, f)]

    best_compound, best_ms = ranking[0]
    segs.append(_text(best_compound, TYRE_COLORS[best_compound], b))
    groups = []
    for compound, total_ms in ranking[1:]:
        lead = "  (" if not groups else "  "
        groups.append([_text(lead, DIM, f), _text(compound, TYRE_COLORS[compound], b),
                       _text(f" {race.format_pace_gap(total_ms - best_ms)}", FG, f)])
    closing = _text(")", DIM, f)
    while groups and max_width is not None:
        total = segments_width(layout, segs + [s for g in groups for s in g] + [closing])
        if total <= max_width:
            break
        groups.pop()
    if groups:
        segs += [s for g in groups for s in g] + [closing]
    return segs


# --- the app -------------------------------------------------------------------

def _enable_dpi_awareness():
    """On Windows, declare the process DPI-aware before Tk starts.

    Otherwise a scaled display (125%, 150%) bitmap-stretches the window and
    the text blurs. Once aware, Tk sizes fonts by the real DPI, and Layout
    follows it through `tk scaling`. System-aware only: Tk 8.6 cannot re-layout
    when the window moves to a monitor with another DPI.
    """
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Windows 8.1+
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Vista+
        except (AttributeError, OSError):
            pass


def _pick_family():
    available = set(tkfont.families())
    for family in ("Consolas", "DejaVu Sans Mono", "Liberation Mono", "Menlo", "Courier New"):
        if family in available:
            return family
    return "Courier"


class OverlayApp:

    def __init__(self, settings):
        self.settings = settings

        _enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("F1 Overlay")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        if settings.borderless:
            self.root.overrideredirect(True)
        if settings.opacity < 1.0:
            self.root.wm_attributes("-alpha", settings.opacity)
        self._topmost = tk.BooleanVar(value=settings.always_on_top)
        self._apply_topmost()
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        family = _pick_family()
        size = settings.font_size
        self.layout = Layout(
            tkfont.Font(root=self.root, family=family, size=size),
            tkfont.Font(root=self.root, family=family, size=size, weight="bold"),
            tkfont.Font(root=self.root, family=family, size=max(6, size - 1)),
            # Fonts are in points, so they already grow with the display DPI;
            # `tk scaling` is pixels per point, 96 / 72 on an unscaled display.
            scale=size / BASE_FONT_SIZE * float(self.root.tk.call("tk", "scaling")) / (96 / 72),
        )
        self.canvas = tk.Canvas(self.root, width=self.layout.width, height=self.layout.height,
                                bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._start_move)
        self.canvas.bind("<B1-Motion>", self._do_move)
        self.canvas.bind("<Button-3>", self._show_menu)

        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_checkbutton(label="Always on top", variable=self._topmost,
                                   command=self._apply_topmost)
        self._menu.add_separator()
        self._menu.add_command(label="Exit", command=self.root.destroy)

        # Published by the UDP thread, read by the Tk thread.
        self.session = None
        self.laps = None
        self.statuses = None
        self.damages = None
        self.telemetry2 = None
        self.participants = None
        self.player_idx = 0
        self.pace_ranking = []
        self.wrong_format = False
        self.socket_error = None
        self._last_packet_ts = 0.0
        self._got_packet = False
        # UDP thread only.
        self._session_uid = None
        self._pace = race.CompoundPace()

        self._wakeup = queue.Queue(maxsize=1)
        self._last_signal = None
        threading.Thread(target=self._udp_loop, daemon=True).start()
        self.root.after(POLL_MS, self._poll)

    def run(self):
        self.root.mainloop()

    # --- window behaviour ---

    def _apply_topmost(self):
        self.root.wm_attributes("-topmost", self._topmost.get())

    def _start_move(self, event):
        self._drag_dx = event.x_root - self.root.winfo_x()
        self._drag_dy = event.y_root - self.root.winfo_y()

    def _do_move(self, event):
        self.root.geometry(f"+{event.x_root - self._drag_dx}+{event.y_root - self._drag_dy}")

    def _show_menu(self, event):
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    # --- UDP thread ---

    def _udp_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", self.settings.udp_port))
        except OSError as e:
            # Set before printing: the print can fail (see main._warn) and the UI
            # must still learn why there is no data.
            self.socket_error = str(e)
            _print_safe(f"Error: cannot listen on UDP {self.settings.udp_port}: {e}")
            self._signal()
            return

        while True:
            data, _ = sock.recvfrom(4096)
            header = telemetry.parse_header(data)
            if header is None:
                continue
            self.wrong_format = header["packet_format"] != telemetry.PACKET_FORMAT
            if not self.wrong_format:
                self._handle_packet(header, data)
            self._last_packet_ts = time.monotonic()
            self._got_packet = True
            self._signal()

    def _handle_packet(self, header, data):
        if header["session_uid"] != self._session_uid:
            self._session_uid = header["session_uid"]
            self._pace = race.CompoundPace()
            self.pace_ranking = []
            self.session = self.laps = self.statuses = None
            self.damages = self.telemetry2 = self.participants = None

        payload = telemetry.parse_payload(header["packet_id"], data)
        if payload is None:
            return
        self.player_idx = header["player_car_index"]
        pid = header["packet_id"]
        if pid == telemetry.PACKET_SESSION:
            self.session = payload
        elif pid == telemetry.PACKET_LAP:
            self.laps = payload
            self._observe_pace(payload)
        elif pid == telemetry.PACKET_PARTICIPANTS:
            self.participants = payload
        elif pid == telemetry.PACKET_CAR_STATUS:
            self.statuses = payload
        elif pid == telemetry.PACKET_CAR_DAMAGE:
            self.damages = payload
        elif pid == telemetry.PACKET_CAR_TELEMETRY2:
            self.telemetry2 = payload

    def _observe_pace(self, laps):
        statuses = self.statuses
        sc_status = self.session["safety_car_status"] if self.session else 0
        for i, lap in enumerate(laps):
            compound = race.TYRE_LETTERS.get(statuses[i]["visual_tyre"]) if statuses else None
            self._pace.observe(i, lap, compound, sc_status)
        self.pace_ranking = self._pace.ranking()

    def _signal(self):
        try:
            self._wakeup.put_nowait(True)
        except queue.Full:
            pass

    # --- Tk thread ---

    def _poll(self):
        had_data = False
        try:
            while True:
                self._wakeup.get_nowait()
                had_data = True
        except queue.Empty:
            pass
        signal = self._signal_state()
        if had_data or signal != self._last_signal:
            self._last_signal = signal
            self._redraw(signal)
        self.root.after(POLL_MS, self._poll)

    def _signal_state(self):
        if not self._got_packet:
            return "no_data"
        age = time.monotonic() - self._last_packet_ts
        if age > NO_SIGNAL_S:
            return "no_signal"
        if age > STALE_S:
            return "stale"
        return "fresh"

    def _message(self):
        """Text that replaces the standings when there is nothing to show."""
        port = self.settings.udp_port
        if self.socket_error:
            return f"Cannot listen on UDP {port}"
        if not self._got_packet:
            return f"Waiting for telemetry on UDP {port}…"
        if self.wrong_format:
            return "Set in-game UDP Format to 2026"
        session, laps = self.session, self.laps
        if session is None or laps is None or self.player_idx >= len(laps):
            return "Waiting for session data…"
        if session["session_type"] not in race.RACE_SESSION_TYPES:
            return "Waiting for a race session"
        return None

    def _redraw(self, signal):
        L = self.layout
        self.canvas.delete("all")

        y = L.pad_y
        self._draw_weather(y)
        y = self._separator(y + L.weather_h)
        self._draw_header(y, signal)
        rows_top = y + L.header_h
        y = self._separator(rows_top + race.STANDINGS_ROWS * L.row_h)
        pit_mid = y + L.row_h / 2
        y = self._separator(y + L.row_h)
        pace_mid = y + L.row_h / 2

        message = self._message()
        if message:
            self.canvas.create_text(L.width / 2, rows_top + race.STANDINGS_ROWS * L.row_h / 2,
                                    text=message, fill=DIM, font=L.font)
            return

        laps, session = self.laps, self.session
        track_length = session["track_length"]
        order = race.race_order(laps)
        me = laps[self.player_idx]
        if all(i != self.player_idx for i, _ in order):
            me = None  # retired or spectating: no reference for gaps
        for row, idx in enumerate(race.standings_window(order, self.player_idx)):
            self._draw_driver(idx, rows_top + row * L.row_h, me, track_length)

        green, sc = self.settings.pit_loss(session["track_id"])
        proj = race.project_pit(laps, self.player_idx, track_length, green, sc,
                                session["safety_car_status"])
        self._draw_segments(pit_mid, pit_segments(L, proj))
        self._draw_segments(pace_mid, pace_segments(L, self.pace_ranking, L.inner_width))

    def _separator(self, y):
        L = self.layout
        mid = y + L.sep_h // 2
        self.canvas.create_line(L.pad, mid, L.width - L.pad, mid, fill=LINE, width=L.line_w)
        return y + L.sep_h

    def _draw_weather(self, top):
        L, c, session = self.layout, self.canvas, self.session
        if session is None or self.wrong_format:
            return
        samples = [s for s in session["weather"] if s["session_type"] == session["session_type"]]
        samples = samples[:L.max_weather_cols]
        if not samples:
            c.create_text(L.width / 2, top + L.weather_h / 2, text="No forecast", fill=DIM,
                          font=L.small)
            return
        col_w = L.inner_width / len(samples)
        offset_mid = top + L.strip_h + L.inset + L.small_h / 2
        for i, s in enumerate(samples):
            x0 = L.pad + i * col_w
            c.create_rectangle(x0 + L.inset, top, x0 + col_w - L.inset, top + L.strip_h,
                               fill=WEATHER_COLORS.get(s["weather"], DIM), width=0)
            cx = x0 + col_w / 2
            c.create_text(cx, offset_mid, text=race.format_offset(s["time_offset"]), fill=FG,
                          font=L.small)
            c.create_text(cx, offset_mid + L.small_h, text=f"{s['rain_pct']}%",
                          fill=rain_color(s["rain_pct"]), font=L.small)

    def _draw_header(self, top, signal):
        L, c = self.layout, self.canvas
        mid = top + L.header_h / 2
        dot = {"fresh": GOOD, "stale": WARN, "no_signal": BAD}.get(signal, FAINT)
        col = L.columns["pos"]
        r = L.dot_r
        c.create_oval(col.x1 - 2 * r, mid - r, col.x1, mid + r, fill=dot, width=0)
        for key, _, kind, label in DRIVER_COLUMNS:
            color = DIM
            if key == "name" and signal in ("stale", "no_signal"):
                label, color = ("STALE", WARN) if signal == "stale" else ("NO SIGNAL", BAD)
            if label:
                # "ERS" spans the mode pill and the percentage beside it, "PEN"
                # the time penalty and drive-through pills.
                self._cell(key, mid, label, color, L.small,
                           "w" if key in SPANNING_LABELS else None)

    def _draw_driver(self, idx, top, me, track_length):
        L, c = self.layout, self.canvas
        mid = top + L.row_h / 2
        lap = self.laps[idx]
        is_player = idx == self.player_idx
        if is_player:
            c.create_rectangle(0, top, L.width, top + L.row_h, fill=PLAYER_BG, width=0)

        self._cell("pos", mid, str(lap["position"]), FG if is_player else DIM)
        self._cell("name", mid, race.short_name(self._driver_name(idx)),
                   BRIGHT if is_player else FG, L.bold if is_player else None)
        if me is not None and not is_player:
            laps_b, seconds = race.relative_gap(lap, me, track_length)
            close = seconds is not None and abs(seconds) < 1.0
            self._cell("gap", mid, race.format_gap(laps_b, seconds),
                       DIM if laps_b else (BRIGHT if close else FG), L.bold if close else None)

        status = _car(self.statuses, idx)
        damage = _car(self.damages, idx)
        telem2 = _car(self.telemetry2, idx)
        part = _car(self.participants, idx)
        # Restricted cars report zeros for ERS, wear and wing damage.
        restricted = not is_player and part is not None and part["your_telemetry"] == 0

        if telem2 is not None and telem2["regs_2026"]:
            if telem2["overtake_active"]:
                self._draw_pill("ot", mid, "OT", BG, GOOD)
            elif telem2["overtake_available"]:
                self._draw_pill("ot", mid, "OT", OT_AVAILABLE, None, outline=OT_AVAILABLE)

        if status is not None:
            if restricted:
                self._cell("mode", mid, "·", FAINT)
                self._cell("ers", mid, "·", FAINT)
            else:
                letter = race.ERS_MODE_LETTERS.get(status["ers_mode"], "N")
                fill, fg = ERS_MODE_COLORS[letter]
                self._draw_pill("mode", mid, letter, fg, fill)
                pct = max(0.0, min(100.0, status["ers_store"] / self.settings.ers_capacity_j * 100))
                self._cell("ers", mid, f"{pct:.0f}%", BAD if pct < 20 else FG)
            tyre = race.TYRE_LETTERS.get(status["visual_tyre"])
            if tyre:
                self._draw_badge("tyre", mid, tyre, TYRE_COLORS[tyre])

        if damage is not None:
            if restricted:
                self._cell("wear", mid, "·", FAINT)
                self._cell("fw", mid, "·", FAINT)
            else:
                wear = max(damage["tyre_wear"])
                self._cell("wear", mid, f"{wear:.0f}%", wear_color(wear))
                fl, fr = damage["fw_left"], damage["fw_right"]
                if fl or fr:
                    self._cell("fw", mid, f"{fl}/{fr}", damage_color(max(fl, fr)))
                else:
                    self._cell("fw", mid, "–", FAINT)

        penalty = race.penalty_seconds(lap)
        if penalty:
            self._draw_pill("pen", mid, race.format_penalty(penalty), BRIGHT, BAD)
        if lap["unserved_dt"]:
            self._draw_pill("dt", mid, "DT", BAD, None, outline=BAD)

    def _driver_name(self, idx):
        part = _car(self.participants, idx)
        if part is None:
            return f"Car {idx + 1}"
        custom = self.settings.drivers.get(str(part["race_number"]))
        return custom or part["name"] or f"Car {idx + 1}"

    # --- drawing primitives ---

    def _cell(self, key, mid, text, color, font=None, anchor=None):
        col = self.layout.columns[key]
        anchor = anchor or (col.kind if col.kind in ("w", "e") else "center")
        x = {"w": col.x0, "e": col.x1}.get(anchor, col.center)
        self.canvas.create_text(x, mid, text=text, fill=color, font=font or self.layout.font,
                                anchor=anchor)

    def _draw_pill(self, key, mid, text, fg, fill, outline=None):
        L, col = self.layout, self.layout.columns[key]
        h = L.pill_h / 2
        self.canvas.create_rectangle(col.x0, mid - h, col.x1, mid + h, fill=fill or "",
                                     outline=outline or fill or "", width=L.line_w)
        self.canvas.create_text(col.center, mid, text=text, fill=fg, font=L.bold)

    def _draw_badge(self, key, mid, letter, color):
        L, col = self.layout, self.layout.columns[key]
        r = (L.pill_h - L.ring_w) / 2
        self.canvas.create_oval(col.center - r, mid - r, col.center + r, mid + r,
                                outline=color, width=L.ring_w)
        self.canvas.create_text(col.center, mid, text=letter, fill=color, font=L.bold)

    def _draw_segments(self, mid, segments):
        L, c = self.layout, self.canvas
        x = L.pad
        h = L.pill_h / 2
        for segment in segments:
            kind, text, color, font, fill = segment
            w = segment_width(L, segment)
            if kind == "pill":
                c.create_rectangle(x, mid - h, x + w, mid + h, fill=fill, outline=fill)
                c.create_text(x + w / 2, mid, text=text, fill=color, font=font)
            else:
                c.create_text(x, mid, text=text, fill=color, font=font, anchor="w")
            x += w


def _car(cars, idx):
    return cars[idx] if cars is not None and idx < len(cars) else None


def _print_safe(text):
    """print() to stderr that survives a legacy Windows code page. The OS
    error text is localised, so it may not be ASCII."""
    try:
        print(text, file=sys.stderr)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), file=sys.stderr)
