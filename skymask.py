"""Sky obstruction map: where on the sky the antenna actually gets signal.

Accumulated over time as satellites pass overhead, in az x el bins:
  - mean C/N0 of the satellites tracked in each bin
  - tracking availability: of the satellites whose orbits (TLE/SGP4, from
    drift.TrackDeviation's latest prediction) put them in the bin, the
    fraction the receiver actually tracked (reported with a C/N0)

A blocked or reflecting part of the sky shows as low C/N0 and/or low
availability there. A healthy open-sky antenna gives C/N0 rising smoothly
with elevation and ~100% availability everywhere above a few degrees.

Satellites that are in the orbit data but that the receiver never tracks
at all this session (decommissioned, unhealthy, test satellites) would
look like obstructions everywhere they go, so by default they're left out
of availability ("only satellites tracked at least once").
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

import nmea
from drift import TLE_GROUPS, TrackDeviation

AZ_STEP = 10
EL_STEP = 5
N_AZ = 360 // AZ_STEP
N_EL = 90 // EL_STEP
MIN_PRED_EL = 5.0          # don't count satellites below this as "should be tracked"

SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _bin(az: float, el: float) -> tuple[int, int]:
    return int(az % 360 // AZ_STEP), min(N_EL - 1, max(0, int(el // EL_STEP)))


class SkyMask:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.snr_sum = [[0.0] * N_EL for _ in range(N_AZ)]
        self.snr_n = [[0] * N_EL for _ in range(N_AZ)]
        self.tracked: dict[str, Counter] = defaultdict(Counter)   # orbit name -> bin -> count
        self.missed: dict[str, Counter] = defaultdict(Counter)
        self.ever_tracked: set[str] = set()
        self.samples = 0

    def update(self, state: nmea.GpsState, td: TrackDeviation) -> None:
        """Call right after td.sample(): uses its prediction and matching."""
        self.samples += 1
        tracked_names = set()
        for key, name in td.last_matched.items():
            sat = state.sats.get(key)
            if sat is not None and sat.best_snr:
                tracked_names.add(name)
                self.tracked[name][_bin(sat.azimuth, sat.elevation)] += 1
        self.ever_tracked |= tracked_names
        for s in state.sats.values():
            snr = s.best_snr
            if snr and s.azimuth is not None and s.elevation is not None and s.elevation >= 0:
                a, e = _bin(s.azimuth, s.elevation)
                self.snr_sum[a][e] += snr
                self.snr_n[a][e] += 1
        systems = {s.system for s in state.sats.values()} & set(TLE_GROUPS)
        for o, az, el in td.last_prediction:
            if el >= MIN_PRED_EL and o.system in systems and o.name not in tracked_names:
                self.missed[o.name][_bin(az, el)] += 1

    # ---- derived grids ----
    def _counts(self, only_seen: bool):
        trk = [[0] * N_EL for _ in range(N_AZ)]
        mis = [[0] * N_EL for _ in range(N_AZ)]
        for src, grid in ((self.tracked, trk), (self.missed, mis)):
            for name, bins in src.items():
                if only_seen and name not in self.ever_tracked:
                    continue
                for (a, e), n in bins.items():
                    grid[a][e] += n
        return trk, mis

    def grids(self, only_seen: bool = True, min_n: int = 3):
        """(mean_cn0, availability%) as [az][el] lists, None where too few samples."""
        trk, mis = self._counts(only_seen)
        cn0 = [[(self.snr_sum[a][e] / self.snr_n[a][e]) if self.snr_n[a][e] >= min_n else None
                for e in range(N_EL)] for a in range(N_AZ)]
        avail = [[(100.0 * trk[a][e] / (trk[a][e] + mis[a][e])) if trk[a][e] + mis[a][e] >= min_n else None
                  for e in range(N_EL)] for a in range(N_AZ)]
        return cn0, avail

    def elevation_profile(self):
        """[(el_low, mean_cn0, n)] per elevation band, all azimuths."""
        out = []
        for e in range(N_EL):
            n = sum(self.snr_n[a][e] for a in range(N_AZ))
            if n:
                out.append((e * EL_STEP, sum(self.snr_sum[a][e] for a in range(N_AZ)) / n, n))
        return out

    def sectors(self, only_seen: bool = True, el_max: float = 30.0):
        """Per 45-deg sector, low sky (MIN_PRED_EL..el_max): (name, availability%, mean_cn0)."""
        trk, mis = self._counts(only_seen)
        out = []
        for i, name in enumerate(SECTORS):
            lo = (i * 45 - 22.5) % 360
            azs = [a for a in range(N_AZ) if ((a * AZ_STEP + AZ_STEP / 2 - lo) % 360) < 45]
            els = [e for e in range(N_EL) if MIN_PRED_EL <= e * EL_STEP < el_max]
            t = sum(trk[a][e] for a in azs for e in els)
            m = sum(mis[a][e] for a in azs for e in els)
            sn = sum(self.snr_n[a][e] for a in azs for e in els)
            ss = sum(self.snr_sum[a][e] for a in azs for e in els)
            out.append((name, (100.0 * t / (t + m)) if t + m else None, (ss / sn) if sn else None))
        return out

    def never_tracked(self) -> list[str]:
        return sorted(n for n in self.missed if n not in self.ever_tracked)

    def export_csv(self, path: Path, only_seen: bool = True) -> None:
        trk, mis = self._counts(only_seen)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["az_from", "az_to", "el_from", "el_to", "mean_cn0", "cn0_samples",
                        "tracked", "missed", "availability_pct"])
            for a in range(N_AZ):
                for e in range(N_EL):
                    n, t, m = self.snr_n[a][e], trk[a][e], mis[a][e]
                    if not (n or t or m):
                        continue
                    w.writerow([a * AZ_STEP, (a + 1) * AZ_STEP, e * EL_STEP, (e + 1) * EL_STEP,
                                f"{self.snr_sum[a][e] / n:.1f}" if n else "", n, t, m,
                                f"{100.0 * t / (t + m):.1f}" if t + m else ""])
