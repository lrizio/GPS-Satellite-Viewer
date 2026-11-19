"""Captures light-theme screenshots of every tab (DEMO source) for the user guide.

Usage:  python scripts/guide_screens.py [outdir] [warmup_seconds]
Overrides the palette in theme before the app is built, never saves settings.
"""
import ctypes, os, sys, time, tkinter as tk
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PIL import ImageGrab
import theme

# --- light "daylight" palette (set before gps_app reads theme.*) -------------
P = dict(METAL_HI="#d4dae2", METAL_LO="#e4e8ee", METAL_EDGE="#ffffff", SCREEN="#ffffff", SCREEN2="#f3f5f8",
         TXT="#1b2430", MUTED="#5b6675", LED_GREEN="#1f9d55", LED_AMBER="#c77700", LED_BLUE="#1769c2",
         LED_RED="#d03a2b", PINK="#8f3fc4", GRN="#2e8b3d", BTN_RED="#c2402f", BG="#f4f6f9", LINE="#b7c0cc")
for k, v in P.items():
    setattr(theme, k, v)
theme.BG_PANEL, theme.BG_HEADER = theme.SCREEN2, theme.METAL_LO
theme.FG, theme.FG_DIM, theme.ACCENT = theme.TXT, theme.MUTED, theme.LED_BLUE
theme.GOOD, theme.WARN, theme.BAD = theme.LED_GREEN, theme.LED_AMBER, theme.BTN_RED

import gps_app
gps_app.App._save_settings = lambda self: None

out = sys.argv[1] if len(sys.argv) > 1 else "docs/img"
warm = float(sys.argv[2]) if len(sys.argv) > 2 else 100
os.makedirs(out, exist_ok=True)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass
root = tk.Tk()
app = gps_app.App(root)
root.geometry("1280x800+40+30")
root.attributes("-topmost", True)
app.port_var.set(gps_app.DEMO_PORT)
app.toggle_connect()

def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        root.update(); time.sleep(0.02)

pump(warm)
names = ["sky", "signal", "position", "satellites", "nmea", "drift", "track", "mask", "clock"]
for i, n in enumerate(names):
    app.nb.select(i); pump(2.5)
    x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
    ImageGrab.grab((x, y, x + w, y + h)).save(os.path.join(out, f"{i + 1:02d}_{n}.png"))
    print("saved", n)
app.disconnect(); root.destroy()
