"""Drift measurement for the GPS Satellite Viewer.

Two independent measurements, both fed from the live NMEA state:

PositionDrift
    The receiver sits still; its reported fix wanders. Every valid GGA
    fix is converted to local north/east/up metres relative to the mean
    of all samples so far (or a pinned reference), and the usual accuracy
    statistics are computed: CEP50/CEP95 (median / 95th-percentile
    horizontal error), 2DRMS, vertical sigma, max horizontal error.

TrackDeviation
    Each satellite's reported azimuth/elevation (GSV) is compared with
    where its published orbit says it should be: Celestrak TLEs run
    through SGP4 for the observer's own fix position. Records dEl, dAz,
    the cross-track component dAz*cos(el) and the total angular separation
    on the sky.
    - GPS and BeiDou TLE names carry the PRN, so they map exactly.
    - GLONASS and Galileo TLE names don't carry the NMEA slot/PRN, so
      those satellites are matched to the nearest predicted satellite of
      the same constellation (then held sticky). Deviations for matched
      satellites are flagged: nearest-match can hide a gross error.
    Expect about +/-0.5 deg of noise: NMEA GSV reports whole degrees, and
    that rounding dominates. TLE/SGP4 error for GNSS orbits is a few km at
    20,000+ km range, i.e. hundredths of a degree.
"""
from __future__ import annotations

import csv
import math
import re
import socket
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import nmea

# --- name lookups ------------------------------------------------------------------
# This LAN's router answers a name's IPv4 (A) query but can time out on the IPv6
# (AAAA) one, and Windows then fails the whole lookup (getaddrinfo 11001/11004),
# which made the CelesTrak download fail now and then.  Retry such a failure as
# IPv4-only; .local names go IPv4 first.  (Same fix as the Shack Dashboard's net.py.)
_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4_safe(host, port, family=0, *args, **kwargs):
    if family not in (0, socket.AF_UNSPEC):
        return _getaddrinfo(host, port, family, *args, **kwargs)
    if isinstance(host, str) and host.lower().rstrip(".").endswith(".local"):
        try:
            return _getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)
        except socket.gaierror:
            pass
    try:
        return _getaddrinfo(host, port, family, *args, **kwargs)
    except socket.gaierror:
        return _getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)


if not getattr(socket.getaddrinfo, "_ipv4_safe", False):
    _getaddrinfo_ipv4_safe._ipv4_safe = True
    socket.getaddrinfo = _getaddrinfo_ipv4_safe

# --- geodesy -------------------------------------------------------------------

WGS84_A = 6378.137            # km
WGS84_E2 = 6.69437999014e-3
EARTH_R_M = 6371008.8          # mean radius, for local metre offsets


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> tuple[float, float, float]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    h = alt_m / 1000.0
    return ((n + h) * math.cos(lat) * math.cos(lon),
            (n + h) * math.cos(lat) * math.sin(lon),
            (n * (1 - WGS84_E2) + h) * math.sin(lat))


def gmst_rad(jd_ut1: float) -> float:
    """Greenwich mean sidereal time (IAU 1982), radians."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    g = (280.46061837 + 360.98564736629 * (jd_ut1 - 2451545.0)
         + 0.000387933 * t * t - t ** 3 / 38710000.0)
    return math.radians(g % 360.0)


def look_angles(sat_teme_km, jd: float, obs_ecef, lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Azimuth/elevation (deg) of a TEME position seen from an ECEF observer.
    TEME->ECEF by GMST rotation only (polar motion ignored: arcseconds)."""
    g = gmst_rad(jd)
    x, y, z = sat_teme_km
    xe = math.cos(g) * x + math.sin(g) * y
    ye = -math.sin(g) * x + math.cos(g) * y
    dx, dy, dz = xe - obs_ecef[0], ye - obs_ecef[1], z - obs_ecef[2]
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    e = -math.sin(lon) * dx + math.cos(lon) * dy
    n = -math.sin(lat) * math.cos(lon) * dx - math.sin(lat) * math.sin(lon) * dy + math.cos(lat) * dz
    u = math.cos(lat) * math.cos(lon) * dx + math.cos(lat) * math.sin(lon) * dy + math.sin(lat) * dz
    az = math.degrees(math.atan2(e, n)) % 360.0
    el = math.degrees(math.atan2(u, math.hypot(e, n)))
    return az, el


def wrap180(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


def sky_separation(az1, el1, az2, el2) -> float:
    """Great-circle angle between two sky directions, degrees."""
    a1, e1, a2, e2 = map(math.radians, (az1, el1, az2, el2))
    c = math.sin(e1) * math.sin(e2) + math.cos(e1) * math.cos(e2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


# --- position drift ----------------------------------------------------------------

@dataclass
class PosSample:
    t: float
    lat: float
    lon: float
    alt: float | None
    hdop: float | None
    sats: int | None
    quality: int


class PositionDrift:
    MAX = 86400                   # 24 h at 1 Hz

    def __init__(self) -> None:
        self.samples: deque[PosSample] = deque(maxlen=self.MAX)
        self.reference: tuple[float, float, float] | None = None   # pinned lat, lon, alt
        self._last_key = None
        self.csv = None

    def reset(self) -> None:
        self.samples.clear()
        self.reference = None
        self._last_key = None

    def add_from_fix(self, fx: nmea.Fix, now: float) -> bool:
        if not fx.quality or fx.lat is None or fx.lon is None:
            return False
        key = (fx.utc_time, fx.lat, fx.lon)
        if key == self._last_key:         # GGA and RMC both land here each epoch
            return False
        self._last_key = key
        s = PosSample(now, fx.lat, fx.lon, fx.altitude, fx.hdop, fx.sats_used, fx.quality)
        self.samples.append(s)
        if self.csv:
            self.csv.writerow(self._row(s))
        return True

    def ref(self) -> tuple[float, float, float] | None:
        if self.reference:
            return self.reference
        if not self.samples:
            return None
        n = len(self.samples)
        alts = [s.alt for s in self.samples if s.alt is not None]
        return (sum(s.lat for s in self.samples) / n, sum(s.lon for s in self.samples) / n,
                sum(alts) / len(alts) if alts else 0.0)

    def pin_reference(self) -> None:
        self.reference = self.ref()

    def enu(self) -> tuple[list[float], list[float], list[float], list[float]]:
        """(t, east_m, north_m, up_m) relative to ref()."""
        r = self.ref()
        if r is None:
            return [], [], [], []
        lat0, lon0, alt0 = r
        k_n = math.radians(1) * EARTH_R_M
        k_e = k_n * math.cos(math.radians(lat0))
        t = [s.t for s in self.samples]
        e = [(s.lon - lon0) * k_e for s in self.samples]
        n = [(s.lat - lat0) * k_n for s in self.samples]
        u = [((s.alt if s.alt is not None else alt0) - alt0) for s in self.samples]
        return t, e, n, u

    def stats(self) -> dict | None:
        t, e, n, u = self.enu()
        if len(t) < 2:
            return None
        h = sorted(math.hypot(a, b) for a, b in zip(e, n))

        def pct(p):
            i = p * (len(h) - 1)
            lo = int(i)
            return h[lo] + (h[min(lo + 1, len(h) - 1)] - h[lo]) * (i - lo)

        def sd(v):
            m = sum(v) / len(v)
            return math.sqrt(sum((x - m) ** 2 for x in v) / len(v))

        r = self.ref()
        return {"n": len(t), "duration_s": t[-1] - t[0], "ref": r, "pinned": self.reference is not None,
                "cep50": pct(0.5), "cep95": pct(0.95), "drms2": 2 * math.hypot(sd(e), sd(n)),
                "sd_e": sd(e), "sd_n": sd(n), "sd_u": sd(u), "max_h": h[-1],
                "span_u": max(u) - min(u)}

    # CSV
    HEADER = ["utc", "unix_time", "lat", "lon", "alt_m", "hdop", "sats_used", "fix_quality"]

    def _row(self, s: PosSample):
        return [datetime.fromtimestamp(s.t, timezone.utc).isoformat(timespec="seconds"), f"{s.t:.1f}",
                f"{s.lat:.8f}", f"{s.lon:.8f}", "" if s.alt is None else f"{s.alt:.2f}",
                "" if s.hdop is None else s.hdop, "" if s.sats is None else s.sats, s.quality]

    def start_csv(self, path: Path) -> None:
        f = open(path, "w", newline="", encoding="utf-8")
        self.csv = csv.writer(f)
        self._csv_file = f
        self.csv.writerow(self.HEADER)
        for s in self.samples:
            self.csv.writerow(self._row(s))
        f.flush()

    def stop_csv(self) -> None:
        if self.csv:
            self._csv_file.close()
        self.csv = None

    def flush(self) -> None:
        if self.csv:
            self._csv_file.flush()


# --- orbit prediction ----------------------------------------------------------------

TLE_GROUPS = {nmea.GPS: "gps-ops", nmea.GLONASS: "glo-ops", nmea.GALILEO: "galileo", nmea.BEIDOU: "beidou"}
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={}&FORMAT=tle"
TLE_MAX_AGE_S = 12 * 3600


@dataclass
class OrbitSat:
    system: str
    prn: int | None          # None where the TLE name doesn't carry it
    name: str
    satrec: object


def _prn_from_name(system: str, name: str) -> int | None:
    if system == nmea.GPS:
        m = re.search(r"\(PRN\s*(\d+)\)", name)
    elif system == nmea.BEIDOU:
        m = re.search(r"\(C(\d+)\)", name)
    else:
        m = None
    return int(m.group(1)) if m else None


class OrbitPredictor:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.sats: list[OrbitSat] = []
        self.status = "Orbit data not loaded"
        self.loaded_at: float | None = None      # file mtime of the oldest group used
        self._lock = threading.Lock()
        self.loading = False

    def load_async(self, force: bool = False, done=None) -> None:
        if self.loading:
            return
        self.loading = True

        def run():
            try:
                self.load(force)
            finally:
                self.loading = False
                if done:
                    done()
        threading.Thread(target=run, daemon=True).start()

    def load(self, force: bool = False) -> None:
        from sgp4.api import Satrec
        self.cache_dir.mkdir(exist_ok=True)
        sats, notes, oldest = [], [], None
        for system, group in TLE_GROUPS.items():
            path = self.cache_dir / f"{group}.tle"
            age = time.time() - path.stat().st_mtime if path.exists() else None
            if force or age is None or age > TLE_MAX_AGE_S:
                self.status = f"Downloading {system} orbit data..."
                try:
                    req = urllib.request.Request(TLE_URL.format(group),
                                                 headers={"User-Agent": "GPS-Satellite-Viewer"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        text = r.read().decode("ascii", "replace")
                    if "1 " not in text:
                        raise ValueError(text.strip()[:80])
                    path.write_text(text)
                except Exception as e:           # keep going on the cached copy
                    notes.append(f"{system}: download failed ({e})")
            if not path.exists():
                continue
            oldest = min(oldest or 1e18, path.stat().st_mtime)
            lines = [ln.rstrip() for ln in path.read_text().splitlines() if ln.strip()]
            for i in range(0, len(lines) - 2, 3):
                name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
                if not (l1.startswith("1 ") and l2.startswith("2 ")):
                    continue
                sats.append(OrbitSat(system, _prn_from_name(system, name), name, Satrec.twoline2rv(l1, l2)))
        with self._lock:
            self.sats = sats
            self.loaded_at = oldest
        by = {}
        for s in sats:
            by[s.system] = by.get(s.system, 0) + 1
        age_h = "" if oldest is None else f", {(time.time() - oldest) / 3600:.1f} h old"
        self.status = ("Orbits: " + "  ".join(f"{k} {v}" for k, v in by.items()) + age_h
                       + ("   |  " + "; ".join(notes) if notes else "")) if sats else \
            ("No orbit data. " + "; ".join(notes))

    def predict(self, when: float, lat: float, lon: float, alt_m: float,
                systems: set[str] | None = None) -> list[tuple[OrbitSat, float, float]]:
        """[(orbit_sat, az, el)] for every loaded satellite (above or below horizon)."""
        from sgp4.api import jday
        dt = datetime.fromtimestamp(when, timezone.utc)
        jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second + dt.microsecond / 1e6)
        obs = geodetic_to_ecef(lat, lon, alt_m)
        out = []
        with self._lock:
            sats = list(self.sats)
        for s in sats:
            if systems is not None and s.system not in systems:
                continue
            err, r, _v = s.satrec.sgp4(jd, fr)
            if err:
                continue
            az, el = look_angles(r, jd + fr, obs, lat, lon)
            out.append((s, az, el))
        return out


# --- satellite track deviation ------------------------------------------------------------

@dataclass
class DevSample:
    t: float
    obs_az: float
    obs_el: float
    pred_az: float
    pred_el: float
    d_el: float          # observed - predicted
    d_az: float          # wrapped, observed - predicted
    cross: float         # d_az * cos(el): true angular azimuth error
    sep: float           # great-circle separation
    snr: float | None


class SatTrack:
    MAX = 20000

    def __init__(self, system: str, prn: int) -> None:
        self.system, self.prn = system, prn
        self.orbit_name: str | None = None
        self.matched = False              # True = nearest-match, not PRN-identified
        self.samples: deque[DevSample] = deque(maxlen=self.MAX)

    @property
    def label(self) -> str:
        return f"{nmea.SYSTEM_LETTER.get(self.system, '?')}{self.prn:02d}"

    def stats(self) -> dict | None:
        if not self.samples:
            return None
        n = len(self.samples)
        return {"n": n,
                "mean_del": sum(s.d_el for s in self.samples) / n,
                "mean_cross": sum(s.cross for s in self.samples) / n,
                "rms_sep": math.sqrt(sum(s.sep ** 2 for s in self.samples) / n),
                "max_sep": max(s.sep for s in self.samples),
                "last_sep": self.samples[-1].sep}


class TrackDeviation:
    MATCH_MAX_DEG = 6.0           # nearest-match acceptance for unnamed TLEs

    def __init__(self, predictor: OrbitPredictor) -> None:
        self.pred = predictor
        self.tracks: dict[tuple[str, int], SatTrack] = {}
        self._sticky: dict[tuple[str, int], str] = {}      # obs key -> TLE name
        self.last_prediction: list[tuple[OrbitSat, float, float]] = []
        self.last_matched: dict[tuple[str, int], str] = {}    # obs key -> TLE name, latest sample
        self.csv = None

    def reset(self) -> None:
        self.tracks.clear()
        self._sticky.clear()
        self.last_matched = {}

    def sample(self, state: nmea.GpsState, now: float) -> int:
        fx = state.fix
        if not self.pred.sats or fx.lat is None or fx.lon is None or not (fx.quality or fx.status_valid):
            self.last_matched, self.last_prediction = {}, []
            return 0
        obs = [s for s in state.sats.values()
               if s.azimuth is not None and s.elevation is not None and s.system in TLE_GROUPS]
        # Predict for the epoch the az/el were actually reported in. Receivers
        # usually send GSV after GGA, so at GGA time the satellite list is
        # still the previous epoch's; using `now` would add a lag bias.
        epochs = [s.obs_epoch for s in obs if s.obs_epoch is not None]
        if epochs:
            when = max(set(epochs), key=epochs.count)
            obs = [s for s in obs if s.obs_epoch in (when, None)]
        else:
            when = now
        preds = self.pred.predict(when, fx.lat, fx.lon, fx.altitude or 0.0)
        self.last_prediction = preds
        by_prn = {(o.system, o.prn): (o, az, el) for o, az, el in preds if o.prn is not None}
        by_name = {o.name: (o, az, el) for o, az, el in preds}
        matched: dict[tuple[str, int], tuple] = {}
        pending = []
        for s in obs:
            hit = by_prn.get((s.system, s.prn))
            if hit:
                matched[s.key] = (hit, False)
                continue
            name = self._sticky.get(s.key)
            if name in by_name:
                o, az, el = by_name[name]
                if sky_separation(s.azimuth, s.elevation, az, el) <= self.MATCH_MAX_DEG:
                    matched[s.key] = (by_name[name], True)
                    continue
                del self._sticky[s.key]
            pending.append(s)
        # greedy nearest-match for the rest, one TLE per observed satellite
        taken = {v[0][0].name for v in matched.values()}
        cands = []
        for s in pending:
            for o, az, el in preds:
                if o.system == s.system and o.prn is None and o.name not in taken and el > -5:
                    cands.append((sky_separation(s.azimuth, s.elevation, az, el), s, (o, az, el)))
        cands.sort(key=lambda c: c[0])
        done = set()
        for sep, s, hit in cands:
            if sep > self.MATCH_MAX_DEG:
                break
            if s.key in done or hit[0].name in taken:
                continue
            matched[s.key] = (hit, True)
            self._sticky[s.key] = hit[0].name
            done.add(s.key)
            taken.add(hit[0].name)

        self.last_matched = {k: v[0][0].name for k, v in matched.items()}
        n = 0
        for s in obs:
            if s.key not in matched:
                continue
            (o, paz, pel), is_match = matched[s.key]
            tr = self.tracks.get(s.key)
            if tr is None:
                tr = self.tracks[s.key] = SatTrack(s.system, s.prn)
            tr.orbit_name, tr.matched = o.name, is_match
            d_az = wrap180(s.azimuth - paz)
            ds = DevSample(when, s.azimuth, s.elevation, paz, pel, s.elevation - pel, d_az,
                           d_az * math.cos(math.radians(s.elevation)),
                           sky_separation(s.azimuth, s.elevation, paz, pel), s.best_snr)
            tr.samples.append(ds)
            if self.csv:
                self.csv.writerow(self._row(tr, ds))
            n += 1
        return n

    def predicted_for(self, key) -> tuple[float, float] | None:
        tr = self.tracks.get(key)
        if tr and tr.samples:
            s = tr.samples[-1]
            return s.pred_az, s.pred_el
        return None

    # CSV
    HEADER = ["utc", "unix_time", "system", "prn", "orbit_name", "matched_by_position",
              "obs_az", "obs_el", "pred_az", "pred_el", "d_el", "d_az", "cross_track", "separation", "cn0"]

    def _row(self, tr: SatTrack, s: DevSample):
        return [datetime.fromtimestamp(s.t, timezone.utc).isoformat(timespec="seconds"), f"{s.t:.1f}",
                tr.system, tr.prn, tr.orbit_name, int(tr.matched), f"{s.obs_az:.0f}", f"{s.obs_el:.0f}",
                f"{s.pred_az:.3f}", f"{s.pred_el:.3f}", f"{s.d_el:.3f}", f"{s.d_az:.3f}",
                f"{s.cross:.3f}", f"{s.sep:.3f}", "" if s.snr is None else f"{s.snr:.0f}"]

    def start_csv(self, path: Path) -> None:
        f = open(path, "w", newline="", encoding="utf-8")
        self._csv_file = f
        self.csv = csv.writer(f)
        self.csv.writerow(self.HEADER)
        rows = [(s.t, tr, s) for tr in self.tracks.values() for s in tr.samples]
        for _t, tr, s in sorted(rows, key=lambda r: r[0]):
            self.csv.writerow(self._row(tr, s))
        f.flush()

    def stop_csv(self) -> None:
        if self.csv:
            self._csv_file.close()
        self.csv = None

    def flush(self) -> None:
        if self.csv:
            self._csv_file.flush()
