"""Drift tabs: position-fix drift and satellite track deviation (see
drift.py for what each measures), added as tabs to the main window's
notebook. Data is collected by the main App continuously while connected;
these tabs only display it and record it to CSV. Plots are only redrawn
while their own tab is showing."""
from __future__ import annotations

import math
import tkinter as tk
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from tkinter import ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.patches import Circle

import nmea
import theme

matplotlib.rcParams.update({
    "figure.facecolor": theme.BG,
    "axes.facecolor": theme.SCREEN,
    "axes.edgecolor": theme.LINE,
    "axes.labelcolor": theme.FG,
    "text.color": theme.FG,
    "xtick.color": theme.FG_DIM,
    "ytick.color": theme.FG_DIM,
    "grid.color": theme.LINE,
    "axes.titlecolor": theme.FG,
    "legend.facecolor": theme.SCREEN2,
    "legend.edgecolor": theme.LINE,
    "legend.labelcolor": theme.FG,
    "font.size": 9,
    "axes.grid": True,
    "grid.linestyle": ":",
})

REFRESH_MS = 2000
MAX_PLOT_PTS = 4000


def _thin(*cols):
    n = len(cols[0])
    step = max(1, math.ceil(n / MAX_PLOT_PTS))
    return [c[::step] for c in cols] if step > 1 else list(cols)


def _dt(ts):
    return [datetime.fromtimestamp(t, timezone.utc) for t in ts]


def _time_axis(ax, span_s: float | None = None):
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6, tz=timezone.utc))
    # seconds only when the run is short, else ticks every 30 s repeat the same HH:MM
    fmt = "%H:%M:%S" if span_s is not None and span_s < 900 else "%H:%M"
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=timezone.utc))


# ---- resize-aware charts -------------------------------------------------
# Every chart uses constrained layout (labels/titles/colorbars never overlap,
# whatever the size) and its fonts shrink with the chart: the scale is the
# square root of how much smaller the canvas is than its design size `ref`,
# floored so text stays readable. Explicit sizes in the drawing code are
# relative ("small", "x-small") so they follow font.size too. Charts redraw
# shortly after a resize so the new font scale and layout apply.
BASE_FONT = 9
MIN_FONT_SCALE = 0.85


def embed(parent, fig, redraw, ref=(1000, 520)):
    """Puts `fig` in a canvas that fills `parent`; `redraw` is called
    (debounced) whenever the canvas is resized."""
    fig.set_layout_engine("constrained")
    c = FigureCanvasTkAgg(fig, master=parent)
    w = c.get_tk_widget()
    w.configure(bg=theme.BG, highlightthickness=0)
    w.pack(fill=tk.BOTH, expand=True)
    c.ref_size = ref
    job = [None]

    def on_configure(_e):
        if job[0] is not None:
            w.after_cancel(job[0])
        job[0] = w.after(150, redraw)

    w.bind("<Configure>", on_configure, add="+")
    return c


def canvas_size(canvas) -> tuple[int, int]:
    w = canvas.get_tk_widget()
    return max(1, w.winfo_width()), max(1, w.winfo_height())


def font_scale(canvas) -> float:
    (w, h), (rw, rh) = canvas_size(canvas), canvas.ref_size
    return max(MIN_FONT_SCALE, min(1.0, math.sqrt(min(w / rw, h / rh))))


@contextmanager
def scaled_draw(canvas):
    """Wrap a chart's whole rebuild in this: fonts are sized for the canvas's
    current size, and the canvas is drawn before leaving (tick labels are
    only created at draw time, so they must be drawn inside the context)."""
    with matplotlib.rc_context({"font.size": BASE_FONT * font_scale(canvas)}):
        yield
        with warnings.catch_warnings():
            # a canvas that hasn't been sized yet (first draw) is harmless; it redraws on resize
            warnings.filterwarnings("ignore", message="constrained_layout not applied")
            canvas.draw()


def autowrap(label: tk.Label, margin: int = 8) -> tk.Label:
    """Keeps a label's wraplength matched to its own width (pack it with
    fill=X) so long notes reflow instead of being clipped."""
    label.bind("<Configure>", lambda e: label.configure(wraplength=max(100, e.width - margin)), add="+")
    return label


class DriftTabs:
    def __init__(self, app, nb: ttk.Notebook) -> None:
        self.app = app
        self.nb = nb
        self.pos_tab = tk.Frame(nb, bg=theme.BG)
        self.sat_tab = tk.Frame(nb, bg=theme.BG)
        nb.add(self.pos_tab, text="Position drift")
        nb.add(self.sat_tab, text="Track deviation")
        self._build_pos()
        self._build_sat()
        nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh(), add="+")
        self._job = None
        self.refresh()

    # ---------------------------------------------------------------- helpers
    def _lbl(self, parent, text="", fg=theme.FG_DIM, font=theme.FONT_UI, **kw):
        return tk.Label(parent, text=text, bg=theme.BG, fg=fg, font=font, **kw)

    def _redraw(self) -> None:
        self.refresh(force=True)

    # ---------------------------------------------------------------- position tab
    def _build_pos(self) -> None:
        top = tk.Frame(self.pos_tab, bg=theme.BG)
        top.pack(side=tk.TOP, fill=tk.X, pady=(8, 4))
        self.pos_vals = {}
        cells = [("n", "Samples"), ("dur", "Duration"), ("cep50", "CEP50"), ("cep95", "CEP95"),
                 ("drms", "2DRMS"), ("maxh", "Max horiz"), ("sdu", "σ vertical"), ("ref", "Reference")]
        for i, (k, name) in enumerate(cells):
            f = tk.Frame(top, bg=theme.BG)
            f.grid(row=0, column=i, sticky="w", padx=(0, 22))
            self._lbl(f, name).pack(anchor="w")
            v = self._lbl(f, "--", fg=theme.LED_BLUE, font=theme.FONT_READOUT_TINY)
            v.pack(anchor="w")
            self.pos_vals[k] = v
        self.pos_vals["ref"].configure(font=("Consolas", 10, "bold"), justify="left")

        ctl = tk.Frame(self.pos_tab, bg=theme.BG)
        ctl.pack(side=tk.TOP, fill=tk.X, pady=(2, 6))
        self.pin_btn = theme.metal_button(ctl, "Pin reference", self._toggle_pin, height=26, font=theme.FONT_BTN_SMALL,
                                          width=theme.button_text_width("Use running mean", font=theme.FONT_BTN_SMALL))
        self.pin_btn.pack(side=tk.LEFT)
        theme.metal_button(ctl, "Reset", self._reset_pos, height=26, font=theme.FONT_BTN_SMALL).pack(side=tk.LEFT, padx=8)
        self.pos_rec = tk.BooleanVar(value=self.app.pos_drift.csv is not None)
        ttk.Checkbutton(ctl, text="Record CSV", variable=self.pos_rec,
                        command=lambda: self.app.set_drift_recording("pos", self.pos_rec.get())
                        ).pack(side=tk.LEFT, padx=(12, 4))
        self.pos_rec_lbl = self._lbl(ctl, "", fg=theme.LED_RED)
        self.pos_rec_lbl.pack(side=tk.LEFT)
        self._lbl(ctl, "Keep the receiver still.", font=("Segoe UI", 9, "italic")).pack(side=tk.RIGHT)

        self.pos_fig = Figure(figsize=(10, 6), dpi=100)
        self.pos_canvas = embed(self.pos_tab, self.pos_fig, self._redraw, ref=(1300, 600))

    def _toggle_pin(self) -> None:
        pd = self.app.pos_drift
        if pd.reference:
            pd.reference = None
        else:
            pd.pin_reference()
        self.refresh(force=True)

    def _reset_pos(self) -> None:
        self.app.pos_drift.reset()
        self.refresh(force=True)

    def _refresh_pos(self) -> None:
        pd = self.app.pos_drift
        s = pd.stats()
        v = self.pos_vals
        self.pin_btn.configure(text="Use running mean" if pd.reference else "Pin reference")
        f = self.app.pos_rec_path
        self.pos_rec_lbl.configure(text=f"● {f.name}" if pd.csv and f else "")
        if s is None:
            for k in v:
                v[k].configure(text="--")
            v["n"].configure(text=str(len(pd.samples)))
        else:
            dur = s["duration_s"]
            v["n"].configure(text=str(s["n"]))
            v["dur"].configure(text=f"{int(dur // 3600)}:{int(dur % 3600 // 60):02d}:{int(dur % 60):02d}")
            v["cep50"].configure(text=f"{s['cep50']:.2f} m")
            v["cep95"].configure(text=f"{s['cep95']:.2f} m")
            v["drms"].configure(text=f"{s['drms2']:.2f} m")
            v["maxh"].configure(text=f"{s['max_h']:.2f} m")
            v["sdu"].configure(text=f"{s['sd_u']:.2f} m")
            lat, lon, alt = s["ref"]
            v["ref"].configure(text=f"{'PINNED' if s['pinned'] else 'mean'}  {lat:.7f}, {lon:.7f}\n"
                                    f"{nmea.maidenhead(lat, lon)}   {alt:.1f} m")

        with scaled_draw(self.pos_canvas):
            self._draw_pos(pd, s)

    def _draw_pos(self, pd, s) -> None:
        t, e, n, u = pd.enu()
        fig = self.pos_fig
        fig.clear()
        gs = fig.add_gridspec(3, 2, width_ratios=[1, 1.35])
        fig.get_layout_engine().set(w_pad=0.06, h_pad=0.04, wspace=0.04, hspace=0.02)
        self.ax_sc = fig.add_subplot(gs[:, 0])
        self.ax_n = fig.add_subplot(gs[0, 1])
        self.ax_e = fig.add_subplot(gs[1, 1], sharex=self.ax_n)
        self.ax_u = fig.add_subplot(gs[2, 1], sharex=self.ax_n)
        ax = self.ax_sc
        ax.set_title("Horizontal scatter", loc="left")
        ax.set_xlabel("East (m)")
        ax.set_ylabel("North (m)")
        ax.set_aspect("equal", adjustable="box")
        if t:
            tt, ee, nn, uu = _thin(t, e, n, u)
            ax.scatter(ee, nn, c=range(len(ee)), cmap="viridis", s=6, alpha=0.8, linewidths=0)
            ax.plot(ee[-1:], nn[-1:], "o", ms=9, mfc="none", mec=theme.LED_RED, mew=2, label="latest")
            ax.plot([0], [0], "+", ms=14, mew=2, color=theme.FG, label="reference")
            if s:
                for r, col, name in ((s["cep50"], theme.LED_GREEN, "CEP50"), (s["cep95"], theme.LED_AMBER, "CEP95")):
                    ax.add_patch(Circle((0, 0), r, fill=False, ec=col, lw=1.5, ls="--",
                                                           label=f"{name} {r:.2f} m"))
                lim = max(s["max_h"], 0.5) * 1.15
                ax.set_xlim(-lim, lim)
                ax.set_ylim(-lim, lim)
            ax.legend(loc="upper right", fontsize="small")
            d = _dt(tt)
            for a, vals, name, col in ((self.ax_n, nn, "North (m)", theme.LED_GREEN),
                                       (self.ax_e, ee, "East (m)", theme.LED_BLUE),
                                       (self.ax_u, uu, "Up (m)", theme.LED_AMBER)):
                a.plot(d, vals, color=col, lw=1)
                a.axhline(0, color=theme.FG_DIM, lw=0.8)
                a.set_ylabel(name)
            _time_axis(self.ax_u, t[-1] - t[0])
            self.ax_u.set_xlabel("UTC")
        else:
            ax.text(0.5, 0.5, "Waiting for a valid fix...", transform=ax.transAxes, ha="center",
                    color=theme.FG_DIM)
        self.ax_n.set_title("Deviation vs time", loc="left")
        for a in (self.ax_n, self.ax_e):
            a.tick_params(labelbottom=False)

    # ---------------------------------------------------------------- satellite tab
    def _build_sat(self) -> None:
        top = tk.Frame(self.sat_tab, bg=theme.BG)
        top.pack(side=tk.TOP, fill=tk.X, pady=(8, 6))
        theme.metal_button(top, "Refresh orbits", self._refresh_orbits, height=26, font=theme.FONT_BTN_SMALL).pack(side=tk.LEFT)
        theme.metal_button(top, "Reset", self._reset_sat, height=26, font=theme.FONT_BTN_SMALL).pack(side=tk.LEFT, padx=8)
        self.sat_rec = tk.BooleanVar(value=self.app.track_dev.csv is not None)
        ttk.Checkbutton(top, text="Record CSV", variable=self.sat_rec,
                        command=lambda: self.app.set_drift_recording("sat", self.sat_rec.get())
                        ).pack(side=tk.LEFT, padx=(12, 4))
        self.sat_rec_lbl = self._lbl(top, "", fg=theme.LED_RED)
        self.sat_rec_lbl.pack(side=tk.LEFT)
        self.orbit_lbl = self._lbl(top, "", font=("Segoe UI", 9))
        self.orbit_lbl.pack(side=tk.RIGHT)
        self.orbit_lbl.configure(justify="right")

        def fit_orbit_lbl(e):
            # wrap the orbit status into the space the buttons leave, instead of clipping it
            others = sum(c.winfo_reqwidth() for c in top.winfo_children() if c is not self.orbit_lbl)
            self.orbit_lbl.configure(wraplength=max(120, e.width - others - 24))
        top.bind("<Configure>", fit_orbit_lbl, add="+")

        # footnote packed before the body so a short window squeezes the body, not this
        note = self._lbl(self.sat_tab, "Δ = reported - predicted.  NMEA reports whole degrees, so about ±0.5° is "
                                       "rounding noise.  * = GLONASS/Galileo matched to the nearest predicted "
                                       "satellite (TLE names don't carry their slot/PRN).",
                         font=("Segoe UI", 9, "italic"), justify="left", anchor="w")
        autowrap(note).pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))

        body = tk.Frame(self.sat_tab, bg=theme.BG)
        body.pack(fill=tk.BOTH, expand=True)
        left = tk.Frame(body, bg=theme.BG)
        left.pack(side=tk.LEFT, fill=tk.Y)
        cols = ("sat", "n", "del", "xt", "rms", "max", "orbit")
        self.sat_tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse", height=20)
        heads = {"sat": ("Sat", 56), "n": ("N", 56), "del": ("ΔEl°", 66), "xt": ("ΔAz·cos°", 90),
                 "rms": ("RMS°", 64), "max": ("Max°", 64), "orbit": ("Orbit (TLE)", 190)}
        for c in cols:
            self.sat_tree.heading(c, text=heads[c][0])
            self.sat_tree.column(c, width=heads[c][1], anchor="w" if c == "orbit" else "center",
                                 stretch=False)
        for system, col in _SYS_COL.items():
            self.sat_tree.tag_configure(system, foreground=col)
        self.sat_tree.tag_configure("all", foreground=theme.FG)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.sat_tree.yview)
        self.sat_tree.configure(yscrollcommand=sb.set)
        self.sat_tree.pack(side=tk.LEFT, fill=tk.Y, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        self._rebuilding = False
        self.sat_tree.bind("<<TreeviewSelect>>",
                           lambda e: None if self._rebuilding else self._refresh_sat_plots())
        # the Orbit column gives up width on a narrow window so the chart keeps room
        self.sat_tab.bind("<Configure>", lambda e: self.sat_tree.column(
            "orbit", width=max(90, min(190, round(e.width * 0.14)))), add="+")

        right = tk.Frame(body, bg=theme.BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
        self.sat_fig = Figure(figsize=(8, 6), dpi=100)
        self.sat_canvas = embed(right, self.sat_fig, self._redraw, ref=(740, 600))
        self._selected = "ALL"

    def _refresh_orbits(self) -> None:
        self.app.orbits.load_async(force=True, done=lambda: self.app.root.after(0, self.refresh))
        self.orbit_lbl.configure(text="Downloading orbit data...")

    def _reset_sat(self) -> None:
        self.app.track_dev.reset()
        self.refresh(force=True)

    def _refresh_sat(self) -> None:
        td = self.app.track_dev
        f = self.app.sat_rec_path
        self.sat_rec_lbl.configure(text=f"● {f.name}" if td.csv and f else "")
        self.orbit_lbl.configure(text=self.app.orbits.status)
        sel = self.sat_tree.selection()
        sel = sel[0] if sel else self._selected
        self._rebuilding = True
        self.sat_tree.delete(*self.sat_tree.get_children())
        tracks = sorted(td.tracks.values(), key=lambda tr: (nmea.SYSTEM_ORDER.index(tr.system), tr.prn))
        all_s = [s for tr in tracks for s in tr.samples]
        if all_s:
            n = len(all_s)
            self.sat_tree.insert("", "end", iid="ALL", tags=("all",), values=(
                "ALL", n, f"{sum(s.d_el for s in all_s) / n:+.2f}", f"{sum(s.cross for s in all_s) / n:+.2f}",
                f"{math.sqrt(sum(s.sep ** 2 for s in all_s) / n):.2f}", f"{max(s.sep for s in all_s):.2f}",
                f"{len(tracks)} satellites"))
        for tr in tracks:
            st = tr.stats()
            if not st:
                continue
            iid = f"{tr.system}:{tr.prn}"
            self.sat_tree.insert("", "end", iid=iid, tags=(tr.system,), values=(
                tr.label + ("*" if tr.matched else ""), st["n"], f"{st['mean_del']:+.2f}",
                f"{st['mean_cross']:+.2f}", f"{st['rms_sep']:.2f}", f"{st['max_sep']:.2f}",
                tr.orbit_name or ""))
        if self.sat_tree.exists(sel):
            self.sat_tree.selection_set(sel)
        self.sat_tree.update_idletasks()     # let the select event fire while guarded
        self._rebuilding = False
        self._selected = sel
        self._refresh_sat_plots()

    def _refresh_sat_plots(self) -> None:
        sel = self.sat_tree.selection()
        key = sel[0] if sel else "ALL"
        self._selected = key
        with scaled_draw(self.sat_canvas):
            self._draw_sat(key)

    def _draw_sat(self, key: str) -> None:
        td = self.app.track_dev
        fig = self.sat_fig
        fig.clear()
        if not td.tracks:
            ax = fig.add_subplot(111)
            msg = ("Waiting for a fix and satellite data..." if self.app.orbits.sats
                   else "No orbit data loaded - press Refresh orbits (needs internet)")
            ax.text(0.5, 0.5, msg, transform=ax.transAxes, ha="center", color=theme.FG_DIM, wrap=True)
            ax.set_axis_off()
            return
        if key == "ALL":
            gs = fig.add_gridspec(2, 1, height_ratios=[1.3, 1])
            fig.get_layout_engine().set(h_pad=0.08, hspace=0.06)
            ax = fig.add_subplot(gs[0])
            ax.set_title("Reported vs predicted separation, all satellites", loc="left")
            per_epoch: dict[float, list[float]] = {}
            for tr in td.tracks.values():
                if not tr.samples:
                    continue
                t, sep = _thin([s.t for s in tr.samples], [s.sep for s in tr.samples])
                ax.plot(_dt(t), sep, ".", ms=2.5, alpha=0.45, color=_SYS_COL.get(tr.system, theme.FG))
                for s in tr.samples:
                    per_epoch.setdefault(s.t, []).append(s.sep)
            et = sorted(per_epoch)
            rms = [math.sqrt(sum(v * v for v in per_epoch[k]) / len(per_epoch[k])) for k in et]
            et, rms = _thin(et, rms)
            ax.plot(_dt(et), rms, color=theme.FG, lw=1.6, label="RMS across satellites")
            ax.set_ylabel("Separation (°)")
            ax.legend(loc="upper right", fontsize="small")
            _time_axis(ax, (et[-1] - et[0]) if et else None)
            ax2 = fig.add_subplot(gs[1])
            tracks = sorted((tr for tr in td.tracks.values() if tr.samples),
                            key=lambda tr: (nmea.SYSTEM_ORDER.index(tr.system), tr.prn))
            rms = [tr.stats()["rms_sep"] for tr in tracks]
            ax2.bar(range(len(tracks)), rms, color=[_SYS_COL.get(tr.system, theme.FG) for tr in tracks])
            ax2.set_xticks(range(len(tracks)), [tr.label for tr in tracks], rotation=90, fontsize="x-small")
            ax2.set_ylabel("RMS separation (°)")
            ax2.set_title("Per-satellite RMS deviation", loc="left")
        else:
            system, prn = key.split(":")
            tr = td.tracks.get((system, int(prn)))
            if tr is None or not tr.samples:
                return
            gs = fig.add_gridspec(2, 2, width_ratios=[1.5, 1])
            fig.get_layout_engine().set(w_pad=0.06, h_pad=0.06, wspace=0.06, hspace=0.06)
            samples = list(tr.samples)
            t = [s.t for s in samples]
            t, del_, xt, sep = _thin(t, [s.d_el for s in samples], [s.cross for s in samples],
                                     [s.sep for s in samples])
            d = _dt(t)
            col = _SYS_COL.get(tr.system, theme.FG)
            ax = fig.add_subplot(gs[0, 0])
            ax.set_title(f"{tr.label}  {tr.orbit_name or ''}{'  (matched by position)' if tr.matched else ''}",
                         loc="left")
            ax.plot(d, del_, color=theme.LED_GREEN, lw=1, label="ΔEl")
            ax.plot(d, xt, color=theme.LED_BLUE, lw=1, label="ΔAz·cos(el)")
            ax.axhline(0, color=theme.FG_DIM, lw=0.8)
            ax.set_ylabel("Deviation (°)")
            ax.legend(loc="upper right", fontsize="small")
            span = t[-1] - t[0]
            _time_axis(ax, span)
            ax2 = fig.add_subplot(gs[1, 0], sharex=ax)
            ax2.plot(d, sep, color=col, lw=1)
            ax2.axhline(0.5, color=theme.FG_DIM, lw=0.8, ls="--")
            ax2.set_ylabel("Separation (°)")
            ax2.set_xlabel("UTC")
            _time_axis(ax2, span)
            pax = fig.add_subplot(gs[:, 1], projection="polar")
            pax.set_theta_zero_location("N")
            pax.set_theta_direction(-1)
            pax.set_ylim(0, 90)
            pax.set_yticks([30, 60, 90], ["60°", "30°", "0°"])
            pax.plot([math.radians(s.pred_az) for s in samples], [90 - s.pred_el for s in samples],
                     color=theme.FG, lw=1.5, alpha=0.7, label="predicted (TLE)")
            pax.plot([math.radians(s.obs_az) for s in samples], [90 - s.obs_el for s in samples],
                     ".", color=col, ms=4, label="reported (NMEA)")
            pax.plot([math.radians(samples[-1].obs_az)], [90 - samples[-1].obs_el], "o", color=col, ms=9)
            pax.set_title("Sky track", loc="left")
            pax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), fontsize="small")

    # ---------------------------------------------------------------- loop
    def refresh(self, force: bool = False) -> None:
        """Redraw whichever drift tab is showing (nothing while on another
        tab), then reschedule."""
        if self._job is not None:
            self.app.root.after_cancel(self._job)
        try:
            current = self.nb.select()
            if current == str(self.pos_tab):
                self._refresh_pos()
            elif current == str(self.sat_tab):
                self._refresh_sat()
        except tk.TclError:
            return
        self._job = self.app.root.after(REFRESH_MS, self.refresh)

    def stop(self) -> None:
        if self._job is not None:
            self.app.root.after_cancel(self._job)
            self._job = None


_SYS_COL = {
    nmea.GPS: theme.LED_GREEN, nmea.GLONASS: theme.LED_RED, nmea.GALILEO: theme.LED_BLUE,
    nmea.BEIDOU: theme.LED_AMBER, nmea.QZSS: theme.PINK, nmea.NAVIC: "#5fd7d7", nmea.SBAS: theme.MUTED,
}
