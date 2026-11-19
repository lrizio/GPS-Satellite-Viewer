"""Sky mask and PC clock tabs (see skymask.py and pcclock.py for what each
measures). Same pattern as drifttabs.py: data is collected by the App
continuously; these tabs only redraw while showing."""
from __future__ import annotations

import math
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import ttk

from matplotlib.colors import ListedColormap
from matplotlib.figure import Figure

import drifttabs  # noqa: F401  (imported for its shared matplotlib theme)
import pcclock
import skymask
import theme
from drifttabs import _dt, _thin, _time_axis, autowrap, canvas_size, embed, scaled_draw

REFRESH_MS = 2000
LEVEL_COL = {"good": theme.LED_GREEN, "warn": theme.LED_AMBER, "bad": theme.LED_RED}


def _lbl(parent, text="", fg=theme.FG_DIM, font=theme.FONT_UI, **kw):
    return tk.Label(parent, text=text, bg=theme.BG, fg=fg, font=font, **kw)


def _avail_col(p):
    if p is None:
        return theme.FG_DIM
    return theme.LED_GREEN if p >= 90 else theme.LED_AMBER if p >= 70 else theme.LED_RED


class SkyClockTabs:
    def __init__(self, app, nb: ttk.Notebook) -> None:
        self.app = app
        self.nb = nb
        self.mask_tab = tk.Frame(nb, bg=theme.BG)
        self.clock_tab = tk.Frame(nb, bg=theme.BG)
        nb.add(self.mask_tab, text="Sky mask")
        nb.add(self.clock_tab, text="PC clock")
        self._saved_until = 0.0
        self._build_mask()
        self._build_clock()
        nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh(), add="+")
        self._job = None
        self.refresh()

    # ------------------------------------------------------------ sky mask
    def _build_mask(self) -> None:
        ctl = tk.Frame(self.mask_tab, bg=theme.BG)
        ctl.pack(side=tk.TOP, fill=tk.X, pady=(8, 6))
        theme.metal_button(ctl, "Reset", self._reset_mask, height=26, font=theme.FONT_BTN_SMALL).pack(side=tk.LEFT)
        theme.metal_button(ctl, "Export CSV", self._export_mask, height=26, font=theme.FONT_BTN_SMALL).pack(side=tk.LEFT, padx=8)
        self.only_seen = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Availability: only satellites tracked at least once",
                        variable=self.only_seen, command=self.refresh).pack(side=tk.LEFT, padx=(12, 0))

        body = tk.Frame(self.mask_tab, bg=theme.BG)
        body.pack(fill=tk.BOTH, expand=True)
        left = tk.Frame(body, bg=theme.BG)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(4, 12))
        _lbl(left, "LOW SKY (5-30°) BY DIRECTION", fg=theme.FG, font=theme.FONT_SECTION).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(4, 6))
        for c, h in enumerate(("Sector", "Tracked", "C/N0")):
            _lbl(left, h).grid(row=1, column=c, sticky="w", padx=(0, 14))
        self.sector_cells = []
        # own font object so the sector table can shrink with the window (see _scale_mask_table)
        self.sector_font = tkfont.Font(font=theme.FONT_READOUT_TINY)
        self._sector_size = self.sector_font.cget("size")
        for i, name in enumerate(skymask.SECTORS):
            _lbl(left, name, fg=theme.FG, font=self.sector_font).grid(row=2 + i, column=0, sticky="w")
            a = _lbl(left, "--", font=self.sector_font)
            a.grid(row=2 + i, column=1, sticky="w", padx=(0, 14))
            s = _lbl(left, "--", fg=theme.LED_BLUE, font=self.sector_font)
            s.grid(row=2 + i, column=2, sticky="w")
            self.sector_cells.append((a, s))
        self.mask_info = _lbl(left, "", font=("Segoe UI", 9), wraplength=250, justify="left")
        self.mask_info.grid(row=10, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.mask_note = _lbl(left, "", font=("Segoe UI", 9), wraplength=250, justify="left")
        self.mask_note.grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.mask_tab.bind("<Configure>", lambda e: self._scale_mask_table(e.width), add="+")

        self.mask_fig = Figure(figsize=(10, 5), dpi=100)
        self.mask_canvas = embed(body, self.mask_fig, self.refresh, ref=(1000, 560))

    def _scale_mask_table(self, tab_width: int) -> None:
        """Shrinks the sector table's readout font (and the note's wrap width)
        in proportion to the tab width, so the charts keep their share."""
        k = max(0.72, min(1.0, tab_width / 1300))
        self.sector_font.configure(size=max(10, round(self._sector_size * k)))
        self.mask_note.configure(wraplength=round(250 * k))
        self.mask_info.configure(wraplength=round(250 * k))

    def _reset_mask(self) -> None:
        self.app.sky_mask.reset()
        self.refresh()

    def _export_mask(self) -> None:
        from gps_app import LOG_DIR
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / datetime.now().strftime("sky_mask_%Y%m%d_%H%M%S.csv")
        self.app.sky_mask.export_csv(path, self.only_seen.get())
        self.mask_info.configure(text=f"Saved to logs: {path.name}", fg=theme.LED_GREEN)
        self._saved_until = time.time() + 10

    def _refresh_mask(self) -> None:
        sm = self.app.sky_mask
        only = self.only_seen.get()
        never = sm.never_tracked()
        if time.time() > self._saved_until:
            self.mask_info.configure(
                text=f"{sm.samples} samples (one per 5 s)"
                     + ("" if self.app.orbits.sats else "   |  no orbit data: availability unavailable"),
                fg=theme.FG_DIM)

        worst = None
        empty = []
        for (a_lbl, s_lbl), (name, avail, cn0) in zip(self.sector_cells, sm.sectors(only)):
            a_lbl.configure(text="--" if avail is None else f"{avail:.0f}%", fg=_avail_col(avail))
            s_lbl.configure(text="--" if cn0 is None else f"{cn0:.0f}")
            if avail is not None and (worst is None or avail < worst[1]):
                worst = (name, avail)
            if avail is None and cn0 is None:
                empty.append(name)
        note = []
        # GPS/GLONASS inclinations (55-65 deg) leave the low sky toward the nearer pole empty
        fx = self.app.state.fix
        pole = "S" if fx.lat is not None and fx.lat < 0 else "N"
        if sm.samples >= 360 and pole in empty and self.app.orbits.sats:
            note.append(f"{pole}: no satellites pass low in the {pole} from this latitude - the gap in "
                        f"GNSS orbits toward the pole, not an obstruction.")
        if worst and worst[1] < 85:
            note.append(f"Weakest low-sky direction: {worst[0]} ({worst[1]:.0f}% of predicted "
                        f"satellites tracked). Look for buildings, walls or metal that way.")
        if never:
            note.append(f"Never tracked this session ({len(never)}), "
                        + ("excluded" if only else "counted as missed") + ": "
                        + ", ".join(n.split("(")[0].strip() for n in never[:8]) + ("..." if len(never) > 8 else ""))
        if sm.samples < 720:
            note.append("Builds up as satellites pass: give it an hour or more for a full picture.")
        self.mask_note.configure(text="\n\n".join(note))

        cn0, avail = sm.grids(only)
        with scaled_draw(self.mask_canvas):
            self._draw_mask(sm, cn0, avail)

    def _draw_mask(self, sm, cn0, avail) -> None:
        fig = self.mask_fig
        fig.clear()
        w, h = canvas_size(self.mask_canvas)
        # three charts side by side when there's room; otherwise the two sky
        # plots on top and the elevation profile underneath
        wide = w >= 1.45 * h or w >= 900
        if wide:
            gs = fig.add_gridspec(1, 3, width_ratios=[1, 1, 0.8])
            slots = (gs[0, 0], gs[0, 1], gs[0, 2])
        else:
            gs = fig.add_gridspec(2, 2, height_ratios=[1.5, 1])
            slots = (gs[0, 0], gs[0, 1], gs[1, :])
        fig.get_layout_engine().set(w_pad=0.08, h_pad=0.08, wspace=0.08, hspace=0.06)
        theta = [math.radians(a * skymask.AZ_STEP) for a in range(skymask.N_AZ + 1)]
        r = [90 - e * skymask.EL_STEP for e in range(skymask.N_EL + 1)]
        cmap_bad = ListedColormap(["#ff7a6b", "#ffbf5f", "#c6e36b", "#57e39a"])
        for i, (grid, title, cmap, vmin, vmax, unit) in enumerate((
                (cn0, "Mean C/N0", "viridis", 15, 50, "dB-Hz"),
                (avail, "Tracked vs predicted", cmap_bad, 0, 100, "%"))):
            ax = fig.add_subplot(slots[i], projection="polar")
            ax.set_theta_zero_location("N")
            ax.set_theta_direction(-1)
            ax.set_facecolor(theme.SCREEN)
            z = [[(grid[a][e] if grid[a][e] is not None else float("nan")) for a in range(skymask.N_AZ)]
                 for e in range(skymask.N_EL)]
            m = ax.pcolormesh(theta, r, z, cmap=cmap, vmin=vmin, vmax=vmax, shading="flat")
            ax.set_ylim(0, 90)
            ax.set_yticks([30, 60, 90], ["60°", "30°", "0°"], fontsize="x-small")
            ax.set_xticks([math.radians(d) for d in range(0, 360, 45)], skymask.SECTORS, fontsize="small")
            ax.set_title(title, pad=10)
            # colourbar as an inset just under the (square) polar plot, so it stays
            # attached to the circle however tall the cell is
            cax = ax.inset_axes([0.05, -0.27 if w < 760 else -0.2, 0.9, 0.035])
            cb = fig.colorbar(m, cax=cax, orientation="horizontal")
            cb.set_label(unit, fontsize="small")
            cb.ax.tick_params(labelsize="x-small")
        ax = fig.add_subplot(slots[2])
        prof = sm.elevation_profile()
        if prof:
            ax.bar([p[0] + skymask.EL_STEP / 2 for p in prof], [p[1] for p in prof],
                   width=skymask.EL_STEP * 0.85, color=theme.LED_BLUE)
            ax.set_ylim(0, 55)
        ax.set_xlim(0, 90)
        ax.set_xlabel("Elevation (°)")
        ax.set_ylabel("Mean C/N0 (dB-Hz)")
        ax.set_title("C/N0 vs elevation", loc="left")
        ax.text(0.02, 0.97, "open sky: rises smoothly\nwith elevation", transform=ax.transAxes,
                va="top", fontsize="small", color=theme.FG_DIM)

    # ------------------------------------------------------------ PC clock
    def _build_clock(self) -> None:
        top = tk.Frame(self.clock_tab, bg=theme.BG)
        top.pack(side=tk.TOP, fill=tk.X, pady=(10, 4))
        left = tk.Frame(top, bg=theme.BG)
        left.pack(side=tk.LEFT, padx=(4, 40))
        _lbl(left, "PC clock offset (best estimate, last 60 s)").pack(anchor="w")
        self.clk_big = _lbl(left, "--", fg=theme.LED_BLUE, font=theme.FONT_READOUT)
        self.clk_big.pack(anchor="w")
        self.clk_dir = _lbl(left, "", fg=theme.FG, font=theme.FONT_LABEL)
        self.clk_dir.pack(anchor="w")
        self.clk_verdict = _lbl(left, "", font=theme.FONT_UI_BOLD)
        self.clk_verdict.pack(anchor="w", pady=(4, 0))

        grid = tk.Frame(top, bg=theme.BG)
        grid.pack(side=tk.LEFT, anchor="n", pady=(4, 0))
        self.clk_vals = {}
        for i, (k, name) in enumerate((("latest", "Latest"), ("median", "Median (60 s)"),
                                       ("jitter", "Jitter (60 s)"), ("best_all", "Best, whole run"),
                                       ("n", "Epochs"), ("span", "Running"))):
            _lbl(grid, name).grid(row=(i // 3) * 2, column=i % 3, sticky="w", padx=(0, 28))
            v = _lbl(grid, "--", fg=theme.LED_BLUE, font=theme.FONT_READOUT_TINY)
            v.grid(row=(i // 3) * 2 + 1, column=i % 3, sticky="w", padx=(0, 28), pady=(0, 8))
            self.clk_vals[k] = v
        theme.metal_button(top, "Reset", self._reset_clock, height=26, font=theme.FONT_BTN_SMALL).pack(side=tk.RIGHT, anchor="n")

        note = _lbl(self.clock_tab,
             "Measured from when each fix's first NMEA sentence arrives. Delivery latency (receiver output + "
             "Bluetooth/serial, typically 0.05-0.3 s) is included, so the true PC error is slightly smaller "
             "than shown. FT8/FT4 want the PC within about ±0.5 s. To correct it: Windows Settings > "
             "Time & language > Date & time > Sync now, or run a time-sync tool.",
             font=("Segoe UI", 9, "italic"), justify="left", anchor="w")
        autowrap(note).pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        self.clk_fig = Figure(figsize=(10, 4), dpi=100)
        self.clk_canvas = embed(self.clock_tab, self.clk_fig, self.refresh, ref=(1300, 480))

    def _reset_clock(self) -> None:
        self.app.clock.reset()
        self.refresh()

    def _refresh_clock(self) -> None:
        clk = self.app.clock
        demo = self.app.source_is_demo()
        s = clk.stats()
        v = self.clk_vals
        if demo or s is None:
            self.clk_big.configure(text="--", fg=theme.FG_DIM)
            self.clk_dir.configure(text="Not available with the DEMO source (its clock runs 60x)" if demo
                                   else "Waiting for a valid GPS time...")
            self.clk_verdict.configure(text="")
            for k in v:
                v[k].configure(text="--")
        else:
            best = s["best"]
            level, text = pcclock.verdict(best)
            self.clk_big.configure(text=pcclock.fmt_offset(best), fg=LEVEL_COL[level])
            self.clk_dir.configure(text=pcclock.ahead_behind(best))
            self.clk_verdict.configure(text=text, fg=LEVEL_COL[level])
            v["latest"].configure(text=pcclock.fmt_offset(s["latest"]))
            v["median"].configure(text=pcclock.fmt_offset(s["median"]))
            v["jitter"].configure(text=f"{s['jitter'] * 1000:.0f} ms")
            v["best_all"].configure(text=pcclock.fmt_offset(s["best_all"]))
            v["n"].configure(text=str(s["n"]))
            span = s["span_s"]
            v["span"].configure(text=f"{int(span // 3600)}:{int(span % 3600 // 60):02d}:{int(span % 60):02d}")

        with scaled_draw(self.clk_canvas):
            self._draw_clock(clk, s, demo)

    def _draw_clock(self, clk, s, demo) -> None:
        fig = self.clk_fig
        fig.clear()
        ax = fig.add_subplot(111)
        ax.set_title("Arrival offset per fix, and best estimate (lowest over trailing 60 s)", loc="left")
        ax.axhspan(-0.5, 0.5, color=theme.LED_GREEN, alpha=0.07)
        for y in (-1, -0.5, 0.5, 1):
            ax.axhline(y, color=theme.LED_AMBER if abs(y) == 0.5 else theme.LED_RED, lw=0.8, ls="--", alpha=0.6)
        ax.axhline(0, color=theme.FG_DIM, lw=0.8)
        if s is not None and not demo:
            pts = list(clk.points)
            t, o = _thin([p[0] for p in pts], [p[1] for p in pts])
            ax.plot(_dt(t), o, ".", ms=3, color=theme.LED_BLUE, alpha=0.5, label="per-fix arrival offset")
            rm = clk.rolling_min()
            rt, ro = _thin([p[0] for p in rm], [p[1] for p in rm])
            ax.plot(_dt(rt), ro, color=theme.FG, lw=1.8, label="best estimate")
            lo, hi = min(min(o), -0.6), max(max(o), 0.6)
            ax.set_ylim(lo - 0.1, hi + 0.1)
            _time_axis(ax, s["span_s"])
            ax.legend(loc="upper right", fontsize="small")
        else:
            ax.set_ylim(-1.2, 1.2)
        ax.set_ylabel("PC - GPS (s)")
        ax.set_xlabel("UTC")

    # ------------------------------------------------------------ loop
    def refresh(self) -> None:
        if self._job is not None:
            self.app.root.after_cancel(self._job)
        try:
            current = self.nb.select()
            if current == str(self.mask_tab):
                self._refresh_mask()
            elif current == str(self.clock_tab):
                self._refresh_clock()
        except tk.TclError:
            return
        self._job = self.app.root.after(REFRESH_MS, self.refresh)

    def stop(self) -> None:
        if self._job is not None:
            self.app.root.after_cancel(self._job)
            self._job = None

