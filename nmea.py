"""NMEA 0183 parser and receiver-state model for the GPS Satellite Viewer.

No third-party dependencies -- feed it raw lines with GpsState.feed() and
read the decoded state back out of its attributes.

What NMEA actually carries about satellites (worth being clear about):
a receiver's NMEA stream does NOT contain the broadcast ephemeris itself
(the Keplerian orbit parameters -- sqrt(A), e, i0, OMEGA0, omega, M0,
delta-n, IDOT, the harmonic corrections, clock terms, IODE/IODC...).
Those only come out in a receiver's proprietary binary protocol
(u-blox UBX-RXM-SFRBX / AID-EPH, SiRF MID 15, etc.). What NMEA gives is
the receiver's *result* of using that ephemeris/almanac: each tracked
satellite's elevation, azimuth and C/N0 (GSV), which ones are in the fix
(GSA), plus the fix itself (GGA/RMC) and its geometry quality (DOPs).

Sentences handled: GSV, GSA, GGA, RMC, VTG, GLL, ZDA, TXT -- from any
talker (GP, GL, GA, GB/BD, GQ, GI, GN). NMEA 4.10+ extensions (GSV
signal ID, GSA system ID) are used when present.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from dataclasses import dataclass, field

# --- constellations ------------------------------------------------------

GPS, GLONASS, GALILEO, BEIDOU, QZSS, NAVIC, SBAS = (
    "GPS", "GLONASS", "Galileo", "BeiDou", "QZSS", "NavIC", "SBAS")

SYSTEM_ORDER = [GPS, GLONASS, GALILEO, BEIDOU, QZSS, NAVIC, SBAS]
SYSTEM_LETTER = {GPS: "G", GLONASS: "R", GALILEO: "E", BEIDOU: "C", QZSS: "J", NAVIC: "I", SBAS: "S"}

TALKER_SYSTEM = {"GP": GPS, "GL": GLONASS, "GA": GALILEO, "GB": BEIDOU, "BD": BEIDOU,
                  "GQ": QZSS, "QZ": QZSS, "GI": NAVIC}

# NMEA 4.10 GSA/GBS system IDs
SYSTEM_ID = {1: GPS, 2: GLONASS, 3: GALILEO, 4: BEIDOU, 5: QZSS, 6: NAVIC}

# NMEA 4.10 GSV signal IDs, per system (names only used for display)
SIGNAL_NAMES = {
    GPS: {1: "L1C/A", 2: "L1P", 3: "L1M", 4: "L2P", 5: "L2C-M", 6: "L2C-L", 7: "L5-I", 8: "L5-Q"},
    GLONASS: {1: "G1C/A", 2: "G1P", 3: "G2C/A", 4: "G2P"},
    GALILEO: {1: "E5a", 2: "E5b", 3: "E5a+b", 4: "E6-A", 5: "E6-BC", 6: "L1-A", 7: "E1-BC"},
    BEIDOU: {1: "B1I", 2: "B1Q", 3: "B1C", 4: "B1A", 5: "B2a", 6: "B2b", 7: "B2a+b", 8: "B3I",
             9: "B3Q", 10: "B3A", 11: "B2I", 12: "B2Q"},
    QZSS: {1: "L1C/A", 2: "L1C-D", 3: "L1C-P", 4: "LIS", 5: "L2C-M", 6: "L2C-L", 7: "L5-I", 8: "L5-Q",
           9: "L6D", 10: "L6E"},
    NAVIC: {1: "L5-SPS", 2: "S-SPS", 3: "L5-RS", 4: "S-RS", 5: "L1-SPS"},
}

FIX_QUALITY = {0: "No fix", 1: "GPS", 2: "DGPS", 3: "PPS", 4: "RTK fixed", 5: "RTK float",
               6: "Dead reckoning", 7: "Manual", 8: "Simulator"}
FIX_MODE = {1: "No fix", 2: "2D", 3: "3D"}


def system_for_prn(prn: int, talker_system: str | None) -> str:
    """Which constellation a PRN belongs to. A single-system talker (GP,
    GL, ...) is trusted except for the SBAS range some receivers report
    under GP; the combined GN talker without a 4.10 system ID has to fall
    back on the conventional (u-blox/SiRF/MTK) numbering ranges."""
    if talker_system == GPS or talker_system is None:
        if 33 <= prn <= 64 or 120 <= prn <= 158:
            return SBAS
        if talker_system == GPS:
            return QZSS if 193 <= prn <= 202 else GPS
    if talker_system is not None:
        return talker_system
    if 1 <= prn <= 32:
        return GPS
    if 65 <= prn <= 96:
        return GLONASS
    if 193 <= prn <= 202:
        return QZSS
    if 201 <= prn <= 263 or 401 <= prn <= 463:
        return BEIDOU
    if 301 <= prn <= 336:
        return GALILEO
    return GPS


# --- sentence-level helpers ----------------------------------------------

def checksum_ok(line: str) -> bool | None:
    """True/False if the sentence carries a *hh checksum, None if it has none."""
    star = line.rfind("*")
    if star < 0:
        return None
    body = line[1:star]
    try:
        want = int(line[star + 1:star + 3], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def add_checksum(body: str) -> str:
    """'GPGGA,...' -> '$GPGGA,...*hh' (used by the demo generator)."""
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}"


def _f(s: str) -> float | None:
    try:
        return float(s) if s != "" else None
    except ValueError:
        return None


def _i(s: str) -> int | None:
    try:
        return int(s) if s != "" else None
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def _latlon(value: str, hemi: str) -> float | None:
    """ddmm.mmmm / dddmm.mmmm + N/S/E/W -> signed decimal degrees."""
    v = _f(value)
    if v is None or hemi == "":
        return None
    deg = int(v // 100)
    dec = deg + (v - deg * 100) / 60.0
    return -dec if hemi in ("S", "W") else dec


def _hms(s: str) -> str | None:
    if len(s) < 6:
        return None
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def maidenhead(lat: float, lon: float, chars: int = 6) -> str:
    """Maidenhead grid locator (6 chars = subsquare, e.g. QF22lb)."""
    lon += 180.0
    lat += 90.0
    out = chr(ord("A") + int(lon // 20)) + chr(ord("A") + int(lat // 10))
    lon %= 20
    lat %= 10
    out += str(int(lon // 2)) + str(int(lat // 1))
    if chars >= 6:
        lon %= 2
        lat %= 1
        out += chr(ord("a") + int(lon * 12)) + chr(ord("a") + int(lat * 24))
    return out


# --- state model -----------------------------------------------------------

@dataclass
class Satellite:
    system: str
    prn: int
    elevation: float | None = None
    azimuth: float | None = None
    snr: dict[int, float | None] = field(default_factory=dict)   # signal id -> C/N0 dB-Hz
    used: bool = False
    last_seen: float = 0.0
    obs_epoch: float | None = None     # receiver UTC of the epoch this az/el was reported in
    track: list[tuple[float, float]] = field(default_factory=list)  # (az, el) history

    @property
    def key(self) -> tuple[str, int]:
        return (self.system, self.prn)

    @property
    def best_snr(self) -> float | None:
        vals = [v for v in self.snr.values() if v is not None]
        return max(vals) if vals else None

    @property
    def label(self) -> str:
        return f"{SYSTEM_LETTER.get(self.system, '?')}{self.prn:02d}"

    def signal_text(self) -> str:
        names = SIGNAL_NAMES.get(self.system, {})
        parts = []
        for sid, v in sorted(self.snr.items()):
            name = names.get(sid, "L1") if sid else "L1"
            parts.append(f"{name}:{'--' if v is None else int(v)}")
        return " ".join(parts)


@dataclass
class Fix:
    utc_time: str | None = None
    date: str | None = None
    lat: float | None = None
    lon: float | None = None
    altitude: float | None = None      # metres above MSL
    geoid_sep: float | None = None     # metres
    speed_kmh: float | None = None
    course: float | None = None
    quality: int = 0                    # GGA fix quality
    mode: int = 1                       # GSA 1/2/3
    sats_used: int | None = None        # GGA count
    pdop: float | None = None
    hdop: float | None = None
    vdop: float | None = None
    status_valid: bool = False          # RMC A/V
    dgps_age: float | None = None
    dgps_station: str | None = None
    last_update: float = 0.0

    def epoch(self) -> float | None:
        """The receiver's own UTC as a Unix timestamp. The date comes from
        RMC/ZDA; until one has arrived (GGA usually leads each epoch) the PC's
        UTC date stands in for it."""
        if not self.utc_time:
            return None
        date = self.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            dt = datetime.strptime(f"{date} {self.utc_time}", "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None


TRACK_MAX = 900          # points of az/el history kept per satellite
SAT_TIMEOUT_S = 15.0     # drop a sat if its GSV group stops reporting it


class GpsState:
    def __init__(self) -> None:
        self.fix = Fix()
        self.sats: dict[tuple[str, int], Satellite] = {}
        # GSV cycle bookkeeping: (talker, signal) -> keys currently listed
        self._gsv_groups: dict[tuple[str, int], set[tuple[str, int]]] = {}
        self._gsv_group_time: dict[tuple[str, int], float] = {}
        self._gsv_pending: dict[tuple[str, int], list] = {}
        self._used: dict[str, set[int]] = {}           # system -> PRNs in solution
        self.sentence_counts: dict[str, int] = {}
        self.good = 0
        self.bad_checksum = 0
        self.unparsed = 0
        self.messages: list[str] = []                  # $xxTXT texts
        self.last_sentence_time = 0.0

    # ---- public ----
    def feed(self, line: str, now: float | None = None) -> str | None:
        """Parse one NMEA line. Returns the sentence type ('GSV', 'GGA', ...)
        or None if the line was rejected."""
        now = time.time() if now is None else now
        line = line.strip()
        if not line.startswith("$") or len(line) < 7:
            self.unparsed += 1
            return None
        ok = checksum_ok(line)
        if ok is False:
            self.bad_checksum += 1
            return None
        star = line.rfind("*")
        body = line[1:star] if star > 0 else line[1:]
        parts = body.split(",")
        addr = parts[0]
        if addr.startswith("P"):              # proprietary ($PMTK, $PUBX, ...)
            self.sentence_counts[addr[:5]] = self.sentence_counts.get(addr[:5], 0) + 1
            self.good += 1
            return addr
        talker, stype = addr[:2], addr[2:]
        handler = getattr(self, f"_{stype.lower()}", None)
        self.sentence_counts[stype] = self.sentence_counts.get(stype, 0) + 1
        self.good += 1
        self.last_sentence_time = now
        if handler is None:
            return stype
        try:
            handler(talker, parts[1:], now)
        except (IndexError, ValueError):
            self.unparsed += 1
            return None
        return stype

    def expire(self, now: float | None = None) -> None:
        """Forget GSV groups (and so satellites) that have gone quiet."""
        now = time.time() if now is None else now
        for g, t in list(self._gsv_group_time.items()):
            if now - t > SAT_TIMEOUT_S:
                del self._gsv_group_time[g]
                self._gsv_groups.pop(g, None)
        self._rebuild_visible()

    def visible(self) -> list[Satellite]:
        return sorted(self.sats.values(), key=lambda s: (SYSTEM_ORDER.index(s.system)
                                                          if s.system in SYSTEM_ORDER else 99, s.prn))

    def counts_by_system(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for s in self.sats.values():
            v, u = out.get(s.system, (0, 0))
            out[s.system] = (v + 1, u + (1 if s.used else 0))
        return out

    # ---- sentence handlers ----
    def _gsv(self, talker: str, f: list[str], now: float) -> None:
        total, num = int(f[0]), int(f[1])
        n_fields = len(f) - 3
        signal = 0
        if n_fields % 4 == 1:               # NMEA 4.10 trailing signal ID
            signal = int(f[-1], 16) if f[-1] else 0
            f = f[:-1]
        group = (talker, signal)
        if num == 1:
            self._gsv_pending[group] = []
        pending = self._gsv_pending.setdefault(group, [])
        tsys = TALKER_SYSTEM.get(talker)
        for i in range(3, len(f) - 3, 4):
            prn = _i(f[i])
            if prn is None:
                continue
            pending.append((system_for_prn(prn, tsys), prn, _f(f[i + 1]), _f(f[i + 2]), _f(f[i + 3])))
        if num == total:
            self._commit_gsv(group, signal, self._gsv_pending.pop(group, []), now)

    def _commit_gsv(self, group, signal, entries, now) -> None:
        keys = set()
        for system, prn, el, az, snr in entries:
            key = (system, prn)
            keys.add(key)
            sat = self.sats.get(key)
            if sat is None:
                sat = Satellite(system, prn)
                self.sats[key] = sat
            if el is not None:
                sat.elevation = el
            if az is not None:
                sat.azimuth = az
            sat.snr[signal] = snr
            sat.last_seen = now
            sat.obs_epoch = self.fix.epoch()
            if sat.elevation is not None and sat.azimuth is not None:
                p = (sat.azimuth, sat.elevation)
                if not sat.track or sat.track[-1] != p:
                    sat.track.append(p)
                    if len(sat.track) > TRACK_MAX:
                        del sat.track[: len(sat.track) - TRACK_MAX]
        old = self._gsv_groups.get(group, set())
        # a sat that dropped out of this signal's list loses that signal's SNR
        for key in old - keys:
            if key in self.sats:
                self.sats[key].snr.pop(signal, None)
        self._gsv_groups[group] = keys
        self._gsv_group_time[group] = now
        self._rebuild_visible()

    def _rebuild_visible(self) -> None:
        listed = set().union(*self._gsv_groups.values()) if self._gsv_groups else set()
        for key in list(self.sats):
            if key not in listed:
                del self.sats[key]
        self._apply_used()

    def _gsa(self, talker: str, f: list[str], now: float) -> None:
        mode = _i(f[1])
        if mode is not None:
            self.fix.mode = mode
        prns = [p for p in (_i(x) for x in f[2:14]) if p is not None]
        self.fix.pdop, self.fix.hdop, self.fix.vdop = _f(f[14]), _f(f[15]), _f(f[16])
        sysid = _i(f[17]) if len(f) > 17 else None
        if sysid in SYSTEM_ID:
            systems = {SYSTEM_ID[sysid]: set(prns)}
            if SYSTEM_ID[sysid] == GPS:            # SBAS PRNs ride along with GPS
                systems = {GPS: {p for p in prns if system_for_prn(p, GPS) == GPS},
                           SBAS: {p for p in prns if system_for_prn(p, GPS) == SBAS}}
        else:
            tsys = TALKER_SYSTEM.get(talker)
            systems: dict[str, set[int]] = {}
            for p in prns:
                systems.setdefault(system_for_prn(p, tsys), set()).add(p)
            if not prns and tsys is not None:
                systems[tsys] = set()
        self._used.update(systems)
        if mode == 1:
            self._used.clear()
        self._apply_used()

    def _apply_used(self) -> None:
        for sat in self.sats.values():
            sat.used = sat.prn in self._used.get(sat.system, ())

    def _gga(self, talker, f, now) -> None:
        fx = self.fix
        fx.utc_time = _hms(f[0]) or fx.utc_time
        fx.quality = _i(f[5]) or 0
        if fx.quality:
            fx.lat = _latlon(f[1], f[2])
            fx.lon = _latlon(f[3], f[4])
            fx.altitude = _f(f[8])
            fx.geoid_sep = _f(f[10])
        fx.sats_used = _i(f[6])
        fx.hdop = _f(f[7]) if f[7] else fx.hdop
        fx.dgps_age = _f(f[12]) if len(f) > 12 else None
        fx.dgps_station = (f[13] or None) if len(f) > 13 else None
        fx.last_update = now

    def _rmc(self, talker, f, now) -> None:
        fx = self.fix
        fx.utc_time = _hms(f[0]) or fx.utc_time
        fx.status_valid = f[1] == "A"
        if fx.status_valid:
            fx.lat = _latlon(f[2], f[3])
            fx.lon = _latlon(f[4], f[5])
            kn = _f(f[6])
            fx.speed_kmh = None if kn is None else kn * 1.852
            fx.course = _f(f[7])
        d = f[8]
        if len(d) == 6:
            fx.date = f"20{d[4:6]}-{d[2:4]}-{d[0:2]}"
        fx.last_update = now

    def _vtg(self, talker, f, now) -> None:
        kmh = _f(f[6]) if len(f) > 6 else None
        if kmh is not None:
            self.fix.speed_kmh = kmh
        if _f(f[0]) is not None:
            self.fix.course = _f(f[0])

    def _gll(self, talker, f, now) -> None:
        if len(f) > 5 and f[5] == "A":
            self.fix.lat = _latlon(f[0], f[1])
            self.fix.lon = _latlon(f[2], f[3])
        self.fix.utc_time = _hms(f[4]) or self.fix.utc_time

    def _zda(self, talker, f, now) -> None:
        self.fix.utc_time = _hms(f[0]) or self.fix.utc_time
        if f[1] and f[2] and f[3]:
            self.fix.date = f"{f[3]}-{int(f[2]):02d}-{int(f[1]):02d}"

    def _txt(self, talker, f, now) -> None:
        text = f[3] if len(f) > 3 else ""
        if text:
            self.messages.append(text)
            del self.messages[:-50]
