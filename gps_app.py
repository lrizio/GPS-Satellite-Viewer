"""GPS Satellite Viewer -- live NMEA satellite display for a Bluetooth
(or any serial) GPS receiver.

Bluetooth SPP GPS receivers appear on Windows as "Standard Serial over
Bluetooth link" COM ports; the outgoing one for a paired receiver is
labelled with the receiver's own Bluetooth name in the port list here.

Shows: sky plot (azimuth/elevation, with each satellite's track while the
app runs), per-satellite C/N0 bars, the position fix + DOPs + Maidenhead
locator, a satellite table, and the raw NMEA stream (optionally recorded
to a .nmea file). See nmea.py's docstring for why this is az/el/C/N0 and
not raw broadcast-ephemeris orbit elements -- NMEA doesn't carry those.

Visual theme shared with the rest of the HamProjects Tkinter family via
theme.py (copied verbatim from MiniVNATiny).
"""
from __future__ import annotations

import json
import math
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timezone
from pathlib import Path
from tkinter import ttk

import serial
import serial.tools.list_ports

import drift
import nmea
import pcclock
import skymask
import theme

__version__ = "1.0"

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) if getattr(sys, "frozen", False) \
    else Path(__file__).parent
DATA_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_DIR = DATA_DIR / "logs"

TOP_BTN_FONT = theme.FONT_BTN_SMALL      # toolbar buttons: small, regular weight

BAUDS =["4800", "9600", "19200", "38400", "57600", "115200"]
DEMO_PORT = "DEMO -- simulated receiver"
TRACK_SAMPLE_S = 5.0          # satellite track-deviation sampling interval

SYSTEM_COLOR = {
    nmea.GPS: theme.LED_GREEN,
    nmea.GLONASS: theme.LED_RED,
    nmea.GALILEO: theme.LED_BLUE,
    nmea.BEIDOU: theme.LED_AMBER,
    nmea.QZSS: theme.PINK,
    nmea.NAVIC: "#5fd7d7",
    nmea.SBAS: theme.MUTED,
}


def snr_color(snr: float | None) -> str:
    if snr is None:
        return theme.METAL_HI
    if snr < 20:
        return theme.LED_RED
    if snr < 30:
        return theme.LED_AMBER
    if snr < 40:
        return "#c6e36b"
    return theme.LED_GREEN


# --- port discovery ----------------------------------------------------------

def _bt_device_name(mac: str) -> str | None:
    """Friendly name of a paired Bluetooth device, from the BTHPORT registry."""
    try:
        import winreg
        path = rf"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices\{mac.lower()}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            raw, _ = winreg.QueryValueEx(k, "Name")
        return bytes(raw).split(b"\0", 1)[0].decode("utf-8", "replace") or None
    except (OSError, ImportError):
        return None


def list_ports() -> list[tuple[str, str]]:
    """[(device, label)] with Bluetooth ports named after their paired device.
    Bluetooth receivers sort first -- they're what this app is for."""
    out = []
    for p in serial.tools.list_ports.comports():
        label = p.description or p.device
        is_bt = "BTHENUM" in (p.hwid or "").upper()
        if is_bt:
            m = re.search(r"&([0-9A-F]{12})_", (p.hwid or "").upper())
            mac = m.group(1) if m else None
            if mac is None or mac == "000000000000":
                label = "Bluetooth (incoming)"
            else:
                label = f"{_bt_device_name(mac) or mac} (Bluetooth)"
        out.append((p.device, f"{p.device} - {label}", is_bt, mac if is_bt else None))
    out.sort(key=lambda t: (not (t[2] and t[3] not in (None, "000000000000")),
                             int(re.sub(r"\D", "", t[0]) or 0)))
    return [(d, lbl) for d, lbl, _bt, _mac in out]


# --- data sources (threads -> queue) ------------------------------------------

class SerialSource(threading.Thread):
    """Reads NMEA lines from a COM port. Bluetooth SPP links drop and take
    several seconds to (re)open, so this keeps retrying until stopped."""

    def __init__(self, port: str, baud: int, out: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.port, self.baud, self.q = port, baud, out
        self.stop_evt = threading.Event()

    def run(self) -> None:
        while not self.stop_evt.is_set():
            self.q.put(("status", "connecting", f"Opening {self.port}..."))
            try:
                ser = serial.Serial(self.port, self.baud, timeout=1)
            except (serial.SerialException, OSError) as e:
                msg = str(e)
                if "semaphore" in msg.lower() or "121" in msg:
                    msg = "receiver not reachable (off, asleep, out of range, or connected to another device)"
                self.q.put(("status", "error", f"{self.port}: {msg} - retrying"))
                self.stop_evt.wait(3)
                continue
            self.q.put(("status", "open", f"{self.port} open - waiting for NMEA"))
            buf = b""
            last_data = time.time()
            try:
                while not self.stop_evt.is_set():
                    chunk = ser.read(ser.in_waiting or 1)
                    if chunk:
                        last_data = time.time()     # also each line's arrival stamp (PC clock tab)
                        buf += chunk
                        while b"\n" in buf:
                            raw, buf = buf.split(b"\n", 1)
                            line = raw.decode("ascii", "replace").strip()
                            if line:
                                self.q.put(("line", line, last_data))
                        if len(buf) > 4096:        # binary junk, no newlines
                            buf = b""
                    elif time.time() - last_data > 10:
                        self.q.put(("status", "error", f"{self.port}: no data for 10 s - reopening"))
                        break
            except (serial.SerialException, OSError) as e:
                self.q.put(("status", "error", f"{self.port}: link lost ({e}) - reconnecting"))
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
            self.stop_evt.wait(2)
        self.q.put(("status", "closed", "Disconnected"))

    def stop(self) -> None:
        self.stop_evt.set()


class DemoSource(threading.Thread):
    """Synthesises a multi-constellation NMEA 4.10 stream (real sentences
    with checksums, so it exercises the same parser path as hardware).
    Runs on a clock 60x faster than real time (reported in the sentences'
    own UTC fields) so tracks visibly grow. When orbit data is loaded, the
    satellites are the real constellations propagated from their TLEs for
    the demo location, rounded to whole degrees as a real receiver does,
    so track-deviation analysis on demo data gives realistic results.
    Otherwise it falls back to a made-up sky."""

    SPEEDUP = 60.0
    LAT, LON, ALT = -37.815, 144.97, 84.3
    TALKER = {nmea.GPS: "GP", nmea.GLONASS: "GL", nmea.GALILEO: "GA", nmea.BEIDOU: "GB"}

    def __init__(self, out: queue.Queue, orbits: "drift.OrbitPredictor | None" = None) -> None:
        super().__init__(daemon=True)
        self.q = out
        self.orbits = orbits
        self._pseudo_prn: dict[str, int] = {}     # TLE name -> PRN for names that don't carry one
        self.stop_evt = threading.Event()
        rnd = random.Random(7)
        self.sats = []
        plan = [("GP", nmea.GPS, list(range(1, 33)), 11), ("GL", nmea.GLONASS, list(range(65, 89)), 7),
                ("GA", nmea.GALILEO, list(range(1, 37)), 8), ("GB", nmea.BEIDOU, list(range(1, 47)), 10),
                ("GQ", nmea.QZSS, [193, 194, 195, 199], 3)]
        for talker, system, prns, n in plan:
            for prn in rnd.sample(prns, n):
                self.sats.append({
                    "talker": talker, "system": system, "prn": prn,
                    "az0": rnd.uniform(0, 360), "daz": rnd.uniform(-0.004, 0.004),
                    "ph": rnd.uniform(0, 2 * math.pi), "w": rnd.uniform(0.00012, 0.00025),
                    "peak": rnd.uniform(35, 88) if system != nmea.QZSS else rnd.uniform(60, 85),
                    "gain": rnd.uniform(-4, 4),
                })
        self.sats.append({"talker": "GP", "system": nmea.SBAS, "prn": 50, "az0": 20, "daz": 0,
                          "ph": math.pi / 2, "w": 0.0, "peak": 32, "gain": -6})

    def _geometry(self, s, t):
        el = s["peak"] * math.sin(s["ph"] + s["w"] * t)
        az = (s["az0"] + s["daz"] * t) % 360
        return az, el

    def _orbit_sky(self, when: float, rnd) -> list:
        visible = []
        for o, az, el in self.orbits.predict(when, self.LAT, self.LON, self.ALT, set(self.TALKER)):
            if el < 3:
                continue
            prn = o.prn
            if prn is None:
                key = f"{o.system}:{o.name}"
                if key not in self._pseudo_prn:
                    n = sum(1 for k in self._pseudo_prn if k.startswith(o.system + ":"))
                    self._pseudo_prn[key] = (65 if o.system == nmea.GLONASS else 1) + n
                prn = self._pseudo_prn[key]
            s = {"talker": self.TALKER[o.system], "system": o.system, "prn": prn}
            snr = max(12, min(52, 20 + 0.33 * el + rnd.gauss(0, 1.5)))
            visible.append((s, round(az) % 360, round(el), int(snr)))
        return visible

    def run(self) -> None:
        use_orbits = bool(self.orbits and self.orbits.sats)
        self.q.put(("status", "open", "Demo source running (simulated data"
                    + (", real orbits at 60x speed)" if use_orbits else ")")))
        t0 = time.time()
        rnd = random.Random()
        while not self.stop_evt.is_set():
            t = (time.time() - t0) * self.SPEEDUP
            sim = t0 + t
            now = datetime.fromtimestamp(sim, timezone.utc)
            hms = now.strftime("%H%M%S.00")
            if use_orbits:
                visible = self._orbit_sky(sim, rnd)
            else:
                visible = []
                for s in self.sats:
                    az, el = self._geometry(s, t)
                    if el >= 3:
                        snr = max(12, min(52, 22 + 0.33 * el + s["gain"] + rnd.gauss(0, 1.2)))
                        visible.append((s, az, el, int(snr)))
            lines = []
            by_sys: dict[str, list] = {}
            for v in visible:
                by_sys.setdefault(v[0]["system"], []).append(v)
            sysids = {nmea.GPS: 1, nmea.GLONASS: 2, nmea.GALILEO: 3, nmea.BEIDOU: 4, nmea.QZSS: 5}
            for system, sysid in sysids.items():
                used = [v[0]["prn"] for v in by_sys.get(system, []) if v[2] >= 10 and v[3] >= 28][:12]
                if system == nmea.GPS:
                    used += [v[0]["prn"] for v in by_sys.get(nmea.SBAS, []) if v[3] >= 30]
                prn_fields = [str(p) for p in used[:12]] + [""] * (12 - len(used[:12]))
                lines.append(nmea.add_checksum(
                    ",".join(["GNGSA", "A", "3", *prn_fields, "1.42", "0.78", "1.19", str(sysid)])))
            groups = {}
            for s, az, el, snr in visible:
                groups.setdefault(s["talker"], []).append((s["prn"], el, az, snr))
            for talker, entries in groups.items():
                entries.sort()
                chunks = [entries[i:i + 4] for i in range(0, len(entries), 4)]
                for i, ch in enumerate(chunks, 1):
                    f = [f"{talker}GSV", str(len(chunks)), str(i), f"{len(entries):02d}"]
                    for prn, el, az, snr in ch:
                        f += [f"{prn:02d}", f"{el:02.0f}", f"{az:03.0f}", f"{snr:02d}"]
                    f.append("1")
                    lines.append(nmea.add_checksum(",".join(f)))
            n_used = sum(1 for _s, _a, el, snr in visible if el >= 10 and snr >= 28)
            lat = 3748.9000 + rnd.gauss(0, 0.0004)
            lon = 14458.2000 + rnd.gauss(0, 0.0004)
            alt = 84.3 + rnd.gauss(0, 0.4)
            lines.insert(0, nmea.add_checksum(
                f"GNGGA,{hms},{lat:09.4f},S,{lon:010.4f},E,1,{min(n_used, 99):02d},0.78,{alt:.1f},M,4.6,M,,"))
            lines.insert(1, nmea.add_checksum(
                f"GNRMC,{hms},A,{lat:09.4f},S,{lon:010.4f},E,0.02,,{now.strftime('%d%m%y')},,,A,V"))
            lines.append(nmea.add_checksum("GNTXT,01,01,02,DEMO SOURCE - SIMULATED DATA"))
            for ln in lines:
                self.q.put(("line", ln))
            self.stop_evt.wait(1.0)
        self.q.put(("status", "closed", "Demo stopped"))

    def stop(self) -> None:
        self.stop_evt.set()


# --- resizable rounded section ------------------------------------------------

class Section(tk.Canvas):
    """theme.rounded_panel() look, but resizable: redraws its outline on
    <Configure> and stretches the body to fill (rounded_panel sizes to
    content once, which doesn't suit plots that should grow with the window)."""

    def __init__(self, parent, title: str, pad: int = 12, dot_color: str | None = None) -> None:
        # tiny requested size: a Canvas otherwise asks for ~7cm each way, which
        # eats the spare space grid weights are meant to share out
        super().__init__(parent, bg=theme.BG, highlightthickness=0, bd=0, width=50, height=50)
        self.pad, self.top = pad, pad + 10
        self.body = tk.Frame(self, bg=theme.BG)
        self._win = self.create_window(pad, self.top, window=self.body, anchor="nw")
        tf = tk.Frame(self, bg=theme.BG)
        self.dot = None
        if dot_color:
            self.dot = theme.glow_dot(tf, 10, dot_color)
            self.dot.pack(side=tk.LEFT, padx=(0, 6))
        self.title_label = tk.Label(tf, text=title, bg=theme.BG, fg=theme.FG, font=theme.FONT_SECTION)
        self.title_label.pack(side=tk.LEFT)
        self.create_window(18, self.top - pad, window=tf, anchor="w")
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, e) -> None:
        self.delete("border")
        theme.rounded_rect(self, 2, self.top - self.pad, e.width - 2, e.height - 2, 14,
                           fill="", outline=theme.LINE, width=2, tags="border")
        self.tag_lower("border")
        self.itemconfigure(self._win, width=max(1, e.width - 2 * self.pad),
                           height=max(1, e.height - self.top - self.pad))


# --- plots ---------------------------------------------------------------------

class SkyPlot(tk.Canvas):
    def __init__(self, parent) -> None:
        super().__init__(parent, bg=theme.BG, highlightthickness=0, bd=0, width=50, height=50)
        self.sats: list[nmea.Satellite] = []
        self.predicted: list[tuple[str, str, float, float]] = []   # (system, label, az, el) from orbit data
        self.show_tracks = True
        self.bind("<Configure>", lambda e: self.redraw())

    def set_sats(self, sats, predicted=None) -> None:
        self.sats = sats
        self.predicted = predicted or []
        self.redraw()

    def _xy(self, cx, cy, r, az, el):
        rr = r * (90 - max(0.0, min(90.0, el))) / 90
        a = math.radians(az)
        return cx + rr * math.sin(a), cy - rr * math.cos(a)

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50 or h < 50:
            return
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - 26
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=theme.SCREEN, outline=theme.FG_DIM, width=1.5)
        for el in (30, 60):
            rr = r * (90 - el) / 90
            self.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=theme.LINE, dash=(3, 3))
            self.create_text(cx + 4, cy - rr - 1, text=f"{el}°", fill=theme.FG_DIM, anchor="sw",
                             font=("Segoe UI", 8))
        for az in range(0, 360, 30):
            x, y = self._xy(cx, cy, r, az, 0)
            self.create_line(cx, cy, x, y, fill=theme.LINE, dash=(1, 4) if az % 90 else ())
        for az, lbl in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
            a = math.radians(az)
            self.create_text(cx + (r + 14) * math.sin(a), cy - (r + 14) * math.cos(a), text=lbl,
                             fill=theme.FG if lbl == "N" else theme.FG_DIM, font=theme.FONT_UI_BOLD)
        # tracks under the dots
        if self.show_tracks:
            for s in self.sats:
                if len(s.track) >= 2:
                    pts = []
                    for az, el in s.track:
                        pts.extend(self._xy(cx, cy, r, az, el))
                    self.create_line(*pts, fill=SYSTEM_COLOR.get(s.system, theme.FG_DIM), width=1,
                                     stipple="gray50", smooth=False)
        # orbit-predicted positions: small crosses, so a satellite that should be
        # up but isn't being reported stands out as a cross with no dot on it
        seen = {s.label for s in self.sats}
        for system, label, az, el in self.predicted:
            if el < 0:
                continue
            x, y = self._xy(cx, cy, r, az, el)
            col = SYSTEM_COLOR.get(system, theme.FG_DIM)
            self.create_line(x - 6, y, x + 7, y, fill=col, width=2)
            self.create_line(x, y - 6, x, y + 7, fill=col, width=2)
            if label not in seen:
                self.create_text(x + 8, y - 8, text=label, anchor="sw", fill=col, font=("Segoe UI", 7))
        rad = max(9, min(15, r / 22))
        for s in self.sats:
            if s.azimuth is None or s.elevation is None:
                continue
            x, y = self._xy(cx, cy, r, s.azimuth, s.elevation)
            col = SYSTEM_COLOR.get(s.system, theme.FG)
            if s.used:
                self.create_oval(x - rad - 3, y - rad - 3, x + rad + 3, y + rad + 3, fill=col,
                                 outline="", stipple="gray25")
                self.create_oval(x - rad, y - rad, x + rad, y + rad, fill=col, outline=theme.FG, width=1)
                fg = theme.METAL_EDGE
            else:
                self.create_oval(x - rad, y - rad, x + rad, y + rad, fill=theme.SCREEN2, outline=col, width=2)
                fg = col
            self.create_text(x, y, text=s.label, fill=fg, font=("Segoe UI", 7 if rad < 12 else 8, "bold"))
        self.create_text(4, 4, text="filled = used in fix", anchor="nw", fill=theme.FG_DIM,
                         font=("Segoe UI", 9, "italic"))
        if not self.sats:
            self.create_text(cx, cy, text="No satellites in view", fill=theme.FG_DIM, font=theme.FONT_LABEL)


class SnrChart(tk.Canvas):
    MAX_SNR = 55

    def __init__(self, parent) -> None:
        super().__init__(parent, bg=theme.BG, highlightthickness=0, bd=0, width=50, height=50)
        self.sats: list[nmea.Satellite] = []
        self.bind("<Configure>", lambda e: self.redraw())

    def set_sats(self, sats) -> None:
        self.sats = sats
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50 or h < 40:
            return
        left, right, top, bottom = 34, 6, 18, 34
        ph = h - top - bottom
        self.create_rectangle(left, top, w - right, h - bottom, fill=theme.SCREEN, outline=theme.LINE)
        for v in (10, 20, 30, 40, 50):
            y = h - bottom - ph * v / self.MAX_SNR
            self.create_line(left, y, w - right, y, fill=theme.LINE, dash=(2, 4))
            self.create_text(left - 5, y, text=str(v), anchor="e", fill=theme.FG_DIM, font=("Segoe UI", 8))
        self.create_text(4, 2, text="dB-Hz", anchor="nw", fill=theme.FG_DIM, font=("Segoe UI", 7))
        sats = self.sats
        if not sats:
            self.create_text((left + w - right) / 2, top + ph / 2, text="No signal data",
                             fill=theme.FG_DIM, font=theme.FONT_LABEL)
            return
        n = len(sats)
        slot = (w - left - right) / n
        bw = max(3, min(26, slot * 0.72))
        for i, s in enumerate(sats):
            xc = left + slot * (i + 0.5)
            snr = s.best_snr
            col = SYSTEM_COLOR.get(s.system, theme.FG)
            if snr:
                y = h - bottom - ph * min(snr, self.MAX_SNR) / self.MAX_SNR
                kw = {} if s.used else {"stipple": "gray50"}
                self.create_rectangle(xc - bw / 2, y, xc + bw / 2, h - bottom, fill=snr_color(snr),
                                      outline="", **kw)
                self.create_rectangle(xc - bw / 2, y, xc + bw / 2, y + 3, fill=col, outline="")
                if slot >= 20:
                    self.create_text(xc, y - 2, text=str(int(snr)), anchor="s", fill=theme.FG,
                                     font=("Segoe UI", 7))
            # narrow bars (lots of satellites): system letter only where the system
            # changes, PRN only on every other bar, so labels never run together
            new_sys = i == 0 or sats[i - 1].system != s.system
            if slot >= 16 or new_sys:
                self.create_text(xc, h - bottom + 4, text=nmea.SYSTEM_LETTER.get(s.system, "?"), anchor="n",
                                 fill=col, font=("Segoe UI", 8, "bold"))
            if slot >= 16 or (slot >= 8 and i % 2 == 0):
                self.create_text(xc, h - bottom + 17, text=str(s.prn), anchor="n",
                                 fill=theme.FG if s.used else theme.FG_DIM,
                                 font=("Segoe UI", 7 if slot < 22 else 8))


# --- main window ---------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(f"GPS Satellite Viewer v{__version__}")
        theme.configure(root)
        ico = APP_DIR / "icon.ico"
        self.icon_path = str(ico) if ico.exists() else None
        if self.icon_path:
            try:
                root.iconbitmap(self.icon_path)
            except tk.TclError:
                pass
        self.settings = self._load_settings()
        root.geometry(self.settings.get("geometry", "1400x900"))
        root.minsize(640, 600)      # tab buttons wrap to extra rows below ~1100 px

        self.q: queue.Queue = queue.Queue()
        self.source: threading.Thread | None = None
        self.state = nmea.GpsState()
        self.dirty = True
        self.log_paused = tk.BooleanVar(value=False)
        self.show_tracks = tk.BooleanVar(value=self.settings.get("tracks", True))
        self.recording = tk.BooleanVar(value=False)
        self.rec_file = None
        self.line_times: list[float] = []
        self.show_pred = tk.BooleanVar(value=self.settings.get("predicted", True))

        # drift measurement: collected continuously while connected, shown in the drift tabs
        self.pos_drift = drift.PositionDrift()
        self.orbits = drift.OrbitPredictor(DATA_DIR / "tle")
        self.track_dev = drift.TrackDeviation(self.orbits)
        self.sky_mask = skymask.SkyMask()
        self.clock = pcclock.ClockOffset()
        self.pos_rec_path: Path | None = None
        self.sat_rec_path: Path | None = None
        self._last_track_sample = 0.0
        self.orbits.load_async()

        self._style_tree()
        self._build_toolbar()
        self._build_statusbar()     # before the body, so pack never squeezes it out
        self._build_body()
        import drifttabs                  # after the Live tab, so the drift tabs come after it
        self.drift_tabs = drifttabs.DriftTabs(self, self.nb)
        import skyclocktabs
        self.skyclock_tabs = skyclocktabs.SkyClockTabs(self, self.nb)
        self._build_tab_buttons()
        try:
            self.nb.select(int(self.settings.get("tab", 0)))
        except (tk.TclError, ValueError):
            pass
        self.refresh_ports()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_job = root.after(50, self._poll)
        self._tick_job = root.after(250, self._render_tick)

    # ---- settings ----
    def _load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        self.settings.update(port=self.port_var.get().split(" - ")[0], baud=self.baud_var.get(),
                             geometry=self.root.geometry(), tracks=self.show_tracks.get(),
                             tab=self.nb.index("current"),
                             predicted=self.show_pred.get())
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except OSError:
            pass

    # ---- layout ----
    def _style_tree(self) -> None:
        st = ttk.Style(self.root)
        st.configure("Treeview", background=theme.SCREEN, fieldbackground=theme.SCREEN,
                     foreground=theme.FG, rowheight=tkfont.Font(font=("Consolas", 10)).metrics("linespace") + 4,
                     font=("Consolas", 10), borderwidth=0)
        st.configure("Treeview.Heading", background=theme.METAL_LO, foreground=theme.FG,
                     font=theme.FONT_UI_BOLD, relief="flat")
        st.map("Treeview", background=[("selected", theme.METAL_HI)], foreground=[("selected", theme.FG)])
        st.map("Treeview.Heading", background=[("active", theme.METAL_HI)])
        st.configure("Vertical.TScrollbar", background=theme.METAL_LO, troughcolor=theme.BG,
                     bordercolor=theme.BG, arrowcolor=theme.FG_DIM)
        st.configure("TCheckbutton", background=theme.BG, foreground=theme.FG_DIM, font=theme.FONT_UI)
        st.map("TCheckbutton", background=[("active", theme.BG)])

    def _build_toolbar(self) -> None:
        bar = theme.metal_bar(self.root)
        bar.pack(side=tk.TOP, fill=tk.X, padx=14, pady=(12, 4))
        tk.Label(bar, text="Port", bg=theme.BG, fg=theme.FG_DIM, font=theme.FONT_UI).pack(side=tk.LEFT)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(bar, textvariable=self.port_var, state="readonly", width=44,
                                       font=theme.FONT_UI)
        self.port_combo.pack(side=tk.LEFT, padx=(6, 4))
        theme.metal_button(bar, "⟳", self.refresh_ports, height=26, width=30,
                           font=TOP_BTN_FONT).pack(side=tk.LEFT, padx=(0, 12))
        tk.Label(bar, text="Baud", bg=theme.BG, fg=theme.FG_DIM, font=theme.FONT_UI).pack(side=tk.LEFT)
        self.baud_var = tk.StringVar(value=self.settings.get("baud", "9600"))
        ttk.Combobox(bar, textvariable=self.baud_var, values=BAUDS, state="readonly", width=8,
                     font=theme.FONT_UI).pack(side=tk.LEFT, padx=(6, 12))
        self.connect_btn = theme.metal_button(bar, "Connect", self.toggle_connect, height=26,
                                              width=theme.button_text_width("Disconnect", font=TOP_BTN_FONT),
                                              font=TOP_BTN_FONT,
                                              bg=theme.ACCENT, fg=theme.METAL_EDGE)
        self.connect_btn.pack(side=tk.LEFT)
        self.link_dot = theme.glow_dot(bar, 14, theme.METAL_HI)
        self.link_dot.pack(side=tk.LEFT, padx=(16, 6))
        self.link_label = tk.Label(bar, text="Not connected", bg=theme.BG, fg=theme.FG_DIM, font=theme.FONT_UI)
        self.link_label.pack(side=tk.LEFT)
        self._toolbar = bar
        bar.bind("<Configure>", lambda e: self._fit_port_combo(e.width), add="+")

    def _fit_port_combo(self, bar_width: int) -> None:
        """The port dropdown takes whatever width the rest of the toolbar
        leaves (12-44 characters), so the toolbar fits narrow windows."""
        combo = self.port_combo
        others = self._toolbar.winfo_reqwidth() - combo.winfo_reqwidth()
        char_px = max(1, combo.winfo_reqwidth() / max(1, int(combo.cget("width"))))
        chars = max(12, min(44, int((bar_width - others - 8) / char_px)))
        if chars != int(combo.cget("width")):
            combo.configure(width=chars)

    def _build_tab_buttons(self) -> None:
        """One raised button per notebook tab; the current tab's button is lit
        in the accent colour, like Connect."""
        for i, tab_id in enumerate(self.nb.tabs()):
            btn = theme.metal_button(self.tab_bar, self.nb.tab(tab_id, "text"),
                                     lambda i=i: self.nb.select(i), height=28,
                                     font=("Segoe UI", 9))
            self.tab_buttons.append(btn)
        self._tab_bar_width = 0
        self.tab_bar.bind("<Configure>", lambda e: self._layout_tab_buttons(e.width))
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._update_tab_buttons(), add="+")
        self._update_tab_buttons()
        self._layout_tab_buttons(self.root.winfo_width() - 20)

    def _layout_tab_buttons(self, width: int) -> None:
        """Flows the tab buttons left to right, wrapping onto extra rows when
        the window is too narrow for one, and sizes the bar to fit them."""
        if width <= 1 or width == self._tab_bar_width:
            return
        self._tab_bar_width = width
        gap = 4
        x = y = row_h = 0
        for btn in self.tab_buttons:
            bw, bh = btn.winfo_reqwidth(), btn.winfo_reqheight()
            if x > 0 and x + bw > width:
                x, y = 0, y + row_h + gap
                row_h = 0
            btn.place(x=x, y=y)
            x += bw + gap
            row_h = max(row_h, bh)
        self.tab_bar.configure(height=y + row_h)

    def _update_tab_buttons(self) -> None:
        current = self.nb.index("current")
        for i, btn in enumerate(self.tab_buttons):
            if i == current:
                btn.configure(bg=theme.ACCENT, fg=theme.METAL_EDGE)
            else:
                btn.configure(bg=theme.METAL_HI, fg=theme.FG)

    def _build_body(self) -> None:
        # one window, one notebook: a tab per live panel, then the drift tabs
        # The notebook's own tab strip is hidden; a row of raised 3D buttons
        # above it (built in _build_tab_buttons, once every tab exists) does
        # the switching instead.
        st = ttk.Style(self.root)
        st.configure("Buttons.TNotebook", background=theme.BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.layout("Buttons.TNotebook.Tab", [])
        self.tab_bar = tk.Frame(self.root, bg=theme.BG)
        self.tab_bar.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(6, 0))
        self.tab_buttons: list[theme.RoundedButton] = []
        self.nb = ttk.Notebook(self.root, style="Buttons.TNotebook")
        self.nb.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=4)

        def tab(text: str, title: str, **kw) -> Section:
            """A notebook tab holding one full-size Section panel."""
            page = tk.Frame(self.nb, bg=theme.BG, pady=6)
            self.nb.add(page, text=text)
            sec = Section(page, title, **kw)
            sec.pack(fill=tk.BOTH, expand=True)
            return sec

        sky_sec = tab("Sky view", "SKY VIEW")
        legend = tk.Frame(sky_sec.body, bg=theme.BG)
        legend.pack(side=tk.BOTTOM, fill=tk.X)
        self.sky = SkyPlot(sky_sec.body)
        self.sky.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        # right-hand items packed first so a narrow window squeezes the legend, not these
        ttk.Checkbutton(legend, text="Tracks", variable=self.show_tracks,
                        command=self._toggle_tracks).pack(side=tk.RIGHT)
        ttk.Checkbutton(legend, text="Predicted +", variable=self.show_pred,
                        command=lambda: setattr(self, "dirty", True)).pack(side=tk.RIGHT, padx=(0, 8))
        # one entry per constellation, shown only while it has satellites in view
        self.legend_items: dict[str, tuple[tk.Frame, tk.Label]] = {}
        self._legend_shown: tuple[str, ...] = ()
        for system in nmea.SYSTEM_ORDER:
            f = tk.Frame(legend, bg=theme.BG)
            theme.glow_dot(f, 12, SYSTEM_COLOR[system]).pack(side=tk.LEFT, padx=(0, 4))
            lbl = tk.Label(f, text=system, bg=theme.BG, fg=theme.FG, font=("Segoe UI", 9))
            lbl.pack(side=tk.LEFT)
            self.legend_items[system] = (f, lbl)
        self.sky.show_tracks = self.show_tracks.get()

        snr_sec = tab("Signal strength", "SIGNAL STRENGTH  (C/N0 per satellite)")
        self.snr = SnrChart(snr_sec.body)
        self.snr.pack(fill=tk.BOTH, expand=True)

        fix_sec = tab("Position fix", "POSITION FIX", dot_color=theme.METAL_HI)
        self.fix_dot = fix_sec.dot
        self._build_fix(fix_sec.body)

        sat_sec = tab("Satellites", "SATELLITES")
        self._build_table(sat_sec.body)

        log_sec = tab("NMEA stream", "NMEA STREAM")
        self._build_log(log_sec.body)

    def _build_fix(self, parent) -> None:
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(3, weight=1)
        self.fix_vals: dict[str, tk.Label] = {}

        # sized for a whole tab to itself
        def row(r, c, key, label, font=theme.FONT_READOUT_SMALL, color=theme.LED_BLUE, span=1):
            tk.Label(parent, text=label, bg=theme.BG, fg=theme.FG_DIM, font=theme.FONT_LABEL).grid(
                row=r, column=c, sticky="w", padx=(0, 16), pady=6)
            v = tk.Label(parent, text="--", bg=theme.BG, fg=color, font=font, anchor="w")
            v.grid(row=r, column=c + 1, columnspan=span, sticky="w", pady=6, padx=(0, 30))
            self.fix_vals[key] = v

        row(0, 0, "utc", "UTC", span=3)
        big = theme.FONT_READOUT
        row(1, 0, "lat", "Latitude", font=big, span=3)
        row(2, 0, "lon", "Longitude", font=big, span=3)
        row(3, 0, "grid", "Locator", color=theme.PINK)
        row(3, 2, "alt", "Altitude")
        row(4, 0, "fix", "Fix", color=theme.FG)
        row(4, 2, "speed", "Speed")
        row(5, 0, "sats", "Sats used", color=theme.FG)
        row(5, 2, "course", "Course")
        row(6, 0, "pdop", "PDOP")
        row(6, 2, "hdop", "HDOP")
        row(7, 0, "vdop", "VDOP")
        row(7, 2, "geoid", "Geoid sep")

    def _build_table(self, parent) -> None:
        cols = ("sys", "prn", "el", "az", "snr", "used", "sig")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="none")
        heads = {"sys": ("System", 96), "prn": ("PRN", 56), "el": ("Elev°", 70), "az": ("Azim°", 70),
                 "snr": ("C/N0", 62), "used": ("Fix", 48), "sig": ("Signals", 150)}
        for c in cols:
            txt, wid = heads[c]
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=wid, anchor="w" if c in ("sys", "sig") else "center",
                             stretch=(c == "sig"))
        for system, col in SYSTEM_COLOR.items():
            self.tree.tag_configure(system, foreground=col)
        self.tree.tag_configure("unused", foreground=theme.FG_DIM)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_log(self, parent) -> None:
        ctl = tk.Frame(parent, bg=theme.BG)
        ctl.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
        ttk.Checkbutton(ctl, text="Pause", variable=self.log_paused).pack(side=tk.LEFT)
        ttk.Checkbutton(ctl, text="Record to logs\\*.nmea", variable=self.recording,
                        command=self._toggle_record).pack(side=tk.LEFT, padx=12)
        self.rec_label = tk.Label(ctl, text="", bg=theme.BG, fg=theme.LED_RED, font=theme.FONT_UI)
        self.rec_label.pack(side=tk.LEFT)
        self.log = tk.Text(parent, bg=theme.SCREEN, fg=theme.FG_DIM, font=("Consolas", 9), height=5,
                           wrap="none", bd=0, highlightthickness=0, insertbackground=theme.FG)
        self.log.pack(fill=tk.BOTH, expand=True)
        for tag, col in (("GSV", theme.LED_BLUE), ("GSA", theme.LED_AMBER), ("GGA", theme.LED_GREEN),
                         ("RMC", theme.LED_GREEN), ("TXT", theme.PINK), ("bad", theme.LED_RED)):
            self.log.tag_configure(tag, foreground=col)
        self.log.configure(state="disabled")

    def _build_statusbar(self) -> None:
        self.status = tk.Label(self.root, text="", bg=theme.BG, fg=theme.FG_DIM, font=("Segoe UI", 8),
                               anchor="w")
        self.status.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 6))

    # ---- actions ----
    def refresh_ports(self) -> None:
        ports = list_ports()
        labels = [lbl for _d, lbl in ports] + [DEMO_PORT]
        self.port_combo["values"] = labels
        want = self.settings.get("port")
        pick = next((l for l in labels if want and l.split(" - ")[0] == want), None)
        if pick is None:        # a paired device named like a GPS, else any named BT port
            pick = (next((l for l in labels if "(Bluetooth)" in l and "GPS" in l.upper()), None)
                    or next((l for l in labels if "(Bluetooth)" in l), labels[0]))
        self.port_var.set(pick)

    def toggle_connect(self) -> None:
        if self.source is not None:
            self.disconnect()
            return
        sel = self.port_var.get()
        self.state = nmea.GpsState()
        self.pos_drift.reset()          # never mix sessions (e.g. demo data then real data)
        self.track_dev.reset()
        self.sky_mask.reset()
        self.clock.reset()
        self.dirty = True
        if sel == DEMO_PORT:
            self.source = DemoSource(self.q, self.orbits)
        else:
            self.source = SerialSource(sel.split(" - ")[0], int(self.baud_var.get()), self.q)
        self.source.start()
        self.connect_btn.configure(text="Disconnect", bg=theme.BTN_RED, fg=theme.FG)
        self._save_settings()

    def disconnect(self) -> None:
        if self.source is not None:
            self.source.stop()
            self.source = None
        self.connect_btn.configure(text="Connect", bg=theme.ACCENT, fg=theme.METAL_EDGE)
        self._set_link("closed", "Not connected")

    def _set_link(self, kind: str, text: str) -> None:
        col = {"open": theme.LED_AMBER, "data": theme.LED_GREEN, "error": theme.LED_RED,
               "connecting": theme.LED_AMBER}.get(kind, theme.METAL_HI)
        theme.recolor_glow_dot(self.link_dot, col)
        self.link_label.configure(text=text, fg=theme.FG if kind in ("data", "open") else theme.FG_DIM)
        self.link_kind = kind

    def _toggle_tracks(self) -> None:
        self.sky.show_tracks = self.show_tracks.get()
        self.sky.redraw()

    def _toggle_record(self) -> None:
        if self.recording.get():
            LOG_DIR.mkdir(exist_ok=True)
            path = LOG_DIR / datetime.now().strftime("gps_%Y%m%d_%H%M%S.nmea")
            self.rec_file = open(path, "a", encoding="ascii", errors="replace", newline="\r\n")
            self.rec_label.configure(text=f"● {path.name}")
        else:
            if self.rec_file:
                self.rec_file.close()
            self.rec_file = None
            self.rec_label.configure(text="")

    # ---- data pump ----
    def _poll(self) -> None:
        log_lines = []
        n = 0
        try:
            while n < 2000:
                item = self.q.get_nowait()
                n += 1
                if item[0] == "status":
                    if self.source is not None or item[1] == "closed":
                        self._set_link(item[1], item[2])
                    continue
                if self.source is None:
                    continue
                line = item[1]
                kind = self.state.feed(line)
                self.dirty = True
                if len(item) > 2:           # serial source: arrival-stamped (demo's clock is fake)
                    fx = self.state.fix
                    self.clock.feed(line, item[2], bool(fx.quality or fx.status_valid))
                if kind in ("GGA", "RMC"):
                    now = self.state.fix.epoch() or time.time()   # receiver's UTC, PC clock as fallback
                    self.pos_drift.add_from_fix(self.state.fix, now)
                    if kind == "GGA" and abs(now - self._last_track_sample) >= TRACK_SAMPLE_S:
                        self._last_track_sample = now
                        self.track_dev.sample(self.state, now)
                        self.sky_mask.update(self.state, self.track_dev)
                self.line_times.append(time.time())
                if self.rec_file:
                    self.rec_file.write(line + "\n")
                if not self.log_paused.get():
                    log_lines.append((line, kind if kind in ("GSV", "GSA", "GGA", "RMC", "TXT")
                                      else ("bad" if kind is None else "")))
        except queue.Empty:
            pass
        if log_lines and self.source is not None:
            if getattr(self, "link_kind", "") != "data" and self.state.good:
                self._set_link("data", "Receiving NMEA")
            self._append_log(log_lines)
        self._poll_job = self.root.after(50, self._poll)

    def _append_log(self, lines) -> None:
        self.log.configure(state="normal")
        for text, tag in lines:
            self.log.insert("end", text + "\n", tag)
        excess = int(self.log.index("end-1c").split(".")[0]) - 400
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _render_tick(self) -> None:
        now = time.time()
        self.state.expire(now)
        if self.source is not None and getattr(self, "link_kind", "") == "data" \
                and now - self.state.last_sentence_time > 5:
            self._set_link("open", "Link open but no NMEA for 5 s")
        if self.dirty:
            self.dirty = False
            self._render()
        self.line_times = [t for t in self.line_times if now - t < 5]
        self.pos_drift.flush()
        self.track_dev.flush()
        st = self.state
        counts = "  ".join(f"{k}:{v}" for k, v in sorted(st.sentence_counts.items()))
        rate = len(self.line_times) / 5
        msg = st.messages[-1] if st.messages else ""
        self.status.configure(text=f"{rate:.1f} sentences/s   ok {st.good}   bad checksum {st.bad_checksum}"
                                   f"   unparsed {st.unparsed}     {counts}"
                                   + (f"     receiver says: {msg}" if msg else ""))
        self._tick_job = self.root.after(250, self._render_tick)

    def _render(self) -> None:
        st = self.state
        sats = st.visible()
        pred = []
        if self.show_pred.get() and self.track_dev.last_prediction:
            systems = {s.system for s in sats}
            by_name = {tr.orbit_name: tr.label for tr in self.track_dev.tracks.values()}
            for o, az, el in self.track_dev.last_prediction:
                if o.system in systems and el > 0:
                    label = by_name.get(o.name) or (f"{nmea.SYSTEM_LETTER[o.system]}{o.prn:02d}" if o.prn
                                                    else o.name.split("(")[-1].rstrip(") "))
                    pred.append((o.system, label, az, el))
        self.sky.set_sats(sats, pred)
        self.snr.set_sats(sats)

        fx = st.fix
        has_fix = fx.quality > 0 or fx.status_valid
        v = self.fix_vals
        v["utc"].configure(text=f"{fx.date or ''} {fx.utc_time or '--'}".strip())
        if fx.lat is not None and fx.lon is not None and has_fix:
            v["lat"].configure(text=f"{abs(fx.lat):.6f}° {'S' if fx.lat < 0 else 'N'}")
            v["lon"].configure(text=f"{abs(fx.lon):.6f}° {'W' if fx.lon < 0 else 'E'}")
            v["grid"].configure(text=nmea.maidenhead(fx.lat, fx.lon))
        else:
            for k in ("lat", "lon", "grid"):
                v[k].configure(text="--")
        v["alt"].configure(text=f"{fx.altitude:.1f} m" if fx.altitude is not None and has_fix else "--")
        v["geoid"].configure(text=f"{fx.geoid_sep:+.1f} m" if fx.geoid_sep is not None else "--")
        v["speed"].configure(text=f"{fx.speed_kmh:.1f} km/h" if fx.speed_kmh is not None else "--")
        v["course"].configure(text=f"{fx.course:.0f}°" if fx.course is not None else "--")
        mode = nmea.FIX_MODE.get(fx.mode, "?")
        qual = nmea.FIX_QUALITY.get(fx.quality, str(fx.quality))
        v["fix"].configure(text=f"{mode} · {qual}" if has_fix else "No fix",
                           fg=theme.LED_GREEN if has_fix and fx.mode == 3 else
                           (theme.LED_AMBER if has_fix else theme.LED_RED))
        used = sum(1 for s in sats if s.used)
        v["sats"].configure(text=f"{fx.sats_used if fx.sats_used is not None else used} / {len(sats)} in view")
        for k in ("pdop", "hdop", "vdop"):
            val = getattr(fx, k)
            v[k].configure(text=f"{val:.2f}" if val is not None else "--",
                           fg=theme.LED_BLUE if val is None or val < 2 else
                           (theme.LED_AMBER if val < 5 else theme.LED_RED))
        if self.fix_dot is not None:
            theme.recolor_glow_dot(self.fix_dot, theme.LED_GREEN if has_fix and fx.mode == 3 else
                                   (theme.LED_AMBER if has_fix else theme.LED_RED if sats else theme.METAL_HI))
        by = st.counts_by_system()
        shown = tuple(s for s in nmea.SYSTEM_ORDER if s in by)
        if shown != self._legend_shown:
            for f, _lbl in self.legend_items.values():
                f.pack_forget()
            for system in shown:
                self.legend_items[system][0].pack(side=tk.LEFT, padx=(0, 9))
            self._legend_shown = shown
        for system in shown:
            n, u = by[system]
            self.legend_items[system][1].configure(text=f"{system} {u}/{n}")

        # table -- rebuild in place, preserving scroll
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        for s in sats:
            snr = s.best_snr
            self.tree.insert("", "end", values=(
                s.system, s.prn,
                "--" if s.elevation is None else f"{s.elevation:.0f}",
                "--" if s.azimuth is None else f"{s.azimuth:.0f}",
                "--" if snr is None else f"{snr:.0f}",
                "●" if s.used else "", s.signal_text()),
                tags=(s.system if s.used else "unused",))
        self.tree.yview_moveto(top)

    def source_is_demo(self) -> bool:
        return isinstance(self.source, DemoSource)

    def set_drift_recording(self, which: str, on: bool) -> None:
        obj = self.pos_drift if which == "pos" else self.track_dev
        if on:
            LOG_DIR.mkdir(exist_ok=True)
            stem = "position_drift" if which == "pos" else "track_deviation"
            path = LOG_DIR / datetime.now().strftime(f"{stem}_%Y%m%d_%H%M%S.csv")
            obj.start_csv(path)
        else:
            obj.stop_csv()
            path = None
        if which == "pos":
            self.pos_rec_path = path
        else:
            self.sat_rec_path = path

    def on_close(self) -> None:
        self.drift_tabs.stop()
        self.skyclock_tabs.stop()
        self.pos_drift.stop_csv()
        self.track_dev.stop_csv()
        self._save_settings()
        for job in (self._poll_job, self._tick_job):
            self.root.after_cancel(job)
        if self.source is not None:
            self.source.stop()
        if self.rec_file:
            self.rec_file.close()
        self.root.destroy()


def main() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
