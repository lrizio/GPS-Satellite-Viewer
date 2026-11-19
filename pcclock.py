"""PC clock offset against GPS time, from NMEA arrival times.

Each NMEA epoch's time (RMC/GGA/GLL/ZDA) is the UTC instant of that fix.
The PC timestamps each line the moment the serial read returns it, so for
the FIRST sentence of each epoch:

    arrival_pc - epoch_gps = PC clock error + delivery latency

Latency is always positive (the receiver emits a few tens of ms after the
epoch, then UART/Bluetooth delivery) and jitters, so the lowest value over
a window is the best estimate: it's the PC clock error plus the minimum
latency, typically 0.05-0.3 s over Bluetooth. That makes this good for
"is the PC within FT8/FT4 tolerance" (needs < ~1 s, ideally < 0.5 s), not
for millisecond work -- that needs a PPS line.
"""
from __future__ import annotations

import statistics
from collections import deque
from datetime import datetime, timezone

TIME_SENTENCES = {"RMC": 0, "GGA": 0, "GLL": 4, "ZDA": 0}   # index of the hhmmss field


def _parse_hms(s: str) -> float | None:
    """'hhmmss.sss' -> seconds of day."""
    if len(s) < 6:
        return None
    try:
        return int(s[0:2]) * 3600 + int(s[2:4]) * 60 + float(s[4:])
    except ValueError:
        return None


class ClockOffset:
    MAX = 86400
    WINDOW_S = 60.0

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.points: deque[tuple[float, float]] = deque(maxlen=self.MAX)   # (pc_time, offset_s)
        self._date: str | None = None        # YYYY-MM-DD from RMC/ZDA
        self._last_epoch: float | None = None

    def feed(self, line: str, rx_time: float, time_valid: bool) -> None:
        if not line.startswith("$") or len(line) < 7:
            return
        star = line.rfind("*")
        f = line[1:star if star > 0 else None].split(",")
        stype = f[0][2:]
        if stype not in TIME_SENTENCES:
            return
        body = f[1:]
        try:
            if stype == "RMC" and len(body[8]) == 6:
                d = body[8]
                self._date = f"20{d[4:6]}-{d[2:4]}-{d[0:2]}"
            elif stype == "ZDA" and body[1] and body[2] and body[3]:
                self._date = f"{body[3]}-{int(body[2]):02d}-{int(body[1]):02d}"
            if stype == "RMC" and body[1] != "A":
                return
            if stype == "GLL" and (len(body) < 6 or body[5] != "A"):
                return
            sod = _parse_hms(body[TIME_SENTENCES[stype]])
        except (IndexError, ValueError):
            return
        if sod is None or not time_valid:
            return
        date = self._date or datetime.fromtimestamp(rx_time, timezone.utc).strftime("%Y-%m-%d")
        epoch = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + sod
        if epoch == self._last_epoch:
            return                        # not the first sentence of this epoch
        self._last_epoch = epoch
        off = rx_time - epoch
        off = (off + 43200) % 86400 - 43200   # midnight roll-over with a stale date
        self.points.append((rx_time, off))

    def stats(self) -> dict | None:
        if not self.points:
            return None
        now = self.points[-1][0]
        win = [o for t, o in self.points if now - t <= self.WINDOW_S]
        allv = [o for _t, o in self.points]
        return {"n": len(self.points), "latest": self.points[-1][1], "best": min(win),
                "median": statistics.median(win),
                "jitter": statistics.pstdev(win) if len(win) > 1 else 0.0,
                "best_all": min(allv), "span_s": now - self.points[0][0],
                "window_n": len(win)}

    def rolling_min(self, window_s: float | None = None):
        """(t, min offset over the trailing window) for plotting the estimate."""
        w = window_s or self.WINDOW_S
        out, q = [], deque()          # monotonic: offsets increasing front to back
        for t, o in self.points:
            while q and q[-1][1] >= o:
                q.pop()
            q.append((t, o))
            while t - q[0][0] > w:
                q.popleft()
            out.append((t, q[0][1]))
        return out


def verdict(offset_s: float) -> tuple[str, str]:
    """(level, text) for FT8/FT4 use; level is good/warn/bad."""
    a = abs(offset_s)
    if a < 0.5:
        return "good", "Good for FT8/FT4"
    if a < 1.0:
        return "warn", "Marginal for FT8/FT4 - decodes may suffer, worth correcting"
    return "bad", "Too far off for reliable FT8/FT4 - correct the PC clock"


def fmt_offset(offset_s: float) -> str:
    return f"{offset_s * 1000:+.0f} ms" if abs(offset_s) < 10 else f"{offset_s:+.2f} s"


def ahead_behind(offset_s: float) -> str:
    return "PC ahead of GPS" if offset_s > 0 else "PC behind GPS"

