"""Builds docs/GPS_Satellite_Viewer_User_Guide.pdf from docs/img (see guide_screens.py)."""
import os
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, KeepTogether,
                                Table, TableStyle)

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
IMG = os.path.join(ROOT, "docs", "img")
OUT = os.path.join(ROOT, "docs", "GPS_Satellite_Viewer_User_Guide.pdf")
F = r"C:\Windows\Fonts"
pdfmetrics.registerFont(TTFont("Seg", os.path.join(F, "segoeui.ttf")))
pdfmetrics.registerFont(TTFont("Seg-B", os.path.join(F, "segoeuib.ttf")))
pdfmetrics.registerFont(TTFont("Seg-I", os.path.join(F, "segoeuii.ttf")))
pdfmetrics.registerFont(TTFont("Seg-BI", os.path.join(F, "segoeuiz.ttf")))
pdfmetrics.registerFontFamily("Seg", normal="Seg", bold="Seg-B", italic="Seg-I", boldItalic="Seg-BI")
pdfmetrics.registerFont(TTFont("Mono", os.path.join(F, "consola.ttf")))

INK, ACC, DIM = colors.HexColor("#1b2430"), colors.HexColor("#1769c2"), colors.HexColor("#5b6675")
body = ParagraphStyle("b", fontName="Seg", fontSize=10, leading=14.5, textColor=INK, spaceAfter=6)
h1 = ParagraphStyle("h1", parent=body, fontName="Seg-B", fontSize=17, leading=21, textColor=ACC, spaceBefore=4, spaceAfter=8)
h2 = ParagraphStyle("h2", parent=body, fontName="Seg-B", fontSize=12, leading=16, spaceBefore=8, spaceAfter=4)
bul = ParagraphStyle("bul", parent=body, leftIndent=14, bulletIndent=3, spaceAfter=3)
cap = ParagraphStyle("cap", parent=body, fontSize=8.5, leading=11, textColor=DIM, alignment=1)
code = ParagraphStyle("code", parent=body, fontName="Mono", fontSize=9, backColor=colors.HexColor("#eef1f5"),
                      borderPadding=5, leading=12.5, spaceBefore=4, spaceAfter=10)
title = ParagraphStyle("t", parent=body, fontName="Seg-B", fontSize=30, leading=36, textColor=ACC, alignment=1)
sub = ParagraphStyle("s", parent=body, fontSize=13, leading=18, textColor=DIM, alignment=1)
MONO = "<font name='Mono'>%s</font>"


def P(t):
    return Paragraph(t, body)


def B(items):
    return [Paragraph(i, bul, bulletText="\u2022") for i in items]


def shot(fn, caption):
    im = Image(os.path.join(IMG, fn), width=7 * inch, height=4.375 * inch)
    im.hAlign = "CENTER"
    return KeepTogether([im, Spacer(1, 3), Paragraph(caption, cap), Spacer(1, 8)])


def grid(rows, widths):
    cell = ParagraphStyle("c", parent=body, fontSize=9, leading=12, spaceAfter=0)
    t = Table([[Paragraph(c, cell) for c in r] for r in rows], colWidths=widths)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b7c0cc")),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e4e8ee")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return [t, Spacer(1, 8)]


s = []
s += [Spacer(1, 2.2 * inch), Paragraph("GPS Satellite Viewer", title), Spacer(1, 10),
      Paragraph("User Guide", sub), Spacer(1, 6),
      Paragraph("Live satellite display and receiver analysis for serial and Bluetooth NMEA GPS receivers", sub),
      Spacer(1, 3.2 * inch), Paragraph("github.com/lrizio/GPS-Satellite-Viewer", sub), PageBreak()]

s += [Paragraph("1. Getting started", h1),
      P("GPS Satellite Viewer reads the NMEA data stream from a GPS receiver and shows what the receiver sees: "
        "where each satellite is, how strong its signal is, and how good the position fix is. Extra tabs measure the "
        "receiver itself: position drift, how closely reported satellite positions follow their predicted orbits, where "
        "the antenna has a clear view of the sky, and how far the PC clock is from GPS time."),
      Paragraph("Installing", h2),
      P("<b>Windows exe:</b> download " + MONO % "GPS-Satellite-Viewer.exe" + " from the Releases page on GitHub "
        "and run it. No Python is needed. The program creates " + MONO % "settings.json" + ", "
        + MONO % "logs\\" + " and " + MONO % "tle\\" + " next to the exe, so keep it in a folder you can write to."),
      P("<b>From source:</b> install Python 3 and run:"),
      Paragraph("pip install pyserial sgp4 matplotlib<br/>python gps_app.py", code),
      Paragraph("Trying it with no hardware", h2),
      P("Choose <b>DEMO -- simulated receiver</b> in the Port list and press <b>Connect</b>. The demo generates "
        "the real GPS, GLONASS, Galileo and BeiDou constellations over Melbourne at 60x speed, so every tab can be explored. "
        "(The PC clock tab is the one exception, because demo timing is not real.) All screenshots in this guide were taken in demo mode."),
      Paragraph("Connecting a receiver", h2),
      *B(["Plug in a USB GPS, or pair a Bluetooth receiver in Windows first. Windows then creates "
          "\u201cStandard Serial over Bluetooth link\u201d COM ports.",
          "Pick the port. Bluetooth ports are labelled with the receiver\u2019s name (for example "
          + MONO % "COM13 - iBT-GPS (Bluetooth)" + ") and the outgoing one is selected by default.",
          "Set the baud rate (9600 is typical for USB receivers; it has no effect on Bluetooth) and press <b>Connect</b>.",
          "The status next to the button changes to <b>Receiving NMEA</b> once data arrives."]),
      P("While connected the app keeps retrying every few seconds if the receiver cannot be opened, and reconnects "
        "if the link drops or goes silent for 10 seconds."),
      PageBreak()]

s += [Paragraph("2. The window", h1),
      P("Everything lives in one window. A toolbar runs along the top, a row of tab buttons sits below it, the selected "
        "tab fills the middle, and a status bar runs along the bottom."),
      *B(["<b>Toolbar:</b> the port list, the refresh button (rescan COM ports), the baud rate and the Connect/Disconnect button, "
          "followed by a link-status indicator.",
          "<b>Tab buttons:</b> click one to switch tabs. If the window is too narrow they wrap onto a second row. "
          "The app reopens on the last tab you used.",
          "<b>Status bar:</b> sentences per second, number of good and bad-checksum sentences, and per-sentence counts.",
          "<b>Resizing:</b> the window can be shrunk to 640 x 600. Charts, tables and text scale down with it. "
          "Drift data is gathered continuously while connected, whichever tab is showing, and is cleared on each new Connect."]),
      Paragraph("3. Viewing the receiver", h1),
      Paragraph("Sky view", h2),
      P("A polar plot of every satellite\u2019s azimuth and elevation, with north at the top and the horizon at the rim. "
        "Colours show the constellation (GPS, GLONASS, Galileo, BeiDou). A <b>filled</b> dot means the satellite is used in the fix; "
        "an outline means it is tracked but not used. The <b>Tracks</b> option draws each satellite\u2019s path while the app runs, "
        "and <b>Predicted +</b> overlays orbit-predicted positions as crosses. A cross with no dot on it is a satellite that should be up "
        "but is not being reported."),
      shot("01_sky.png", "Sky view: filled dots are used in the fix, crosses are predicted positions."),
      Paragraph("Signal strength", h2),
      P("A bar for each tracked satellite showing carrier-to-noise density (C/N0, dB-Hz), labelled by constellation and PRN. "
        "Solid bars are used in the fix; hatched bars are tracked but not used. Higher is better; 40 dB-Hz and above is a strong signal."),
      shot("02_signal.png", "Signal strength: C/N0 per satellite."),
      PageBreak()]

s += [Paragraph("Position fix", h2),
      P("The receiver\u2019s solution in large readouts: UTC, latitude, longitude, Maidenhead locator, altitude, geoid separation, "
        "speed and course, fix type (2D/3D and system), satellites used and in view, and the PDOP, HDOP and VDOP dilution-of-precision figures. "
        "Lower DOP is better."),
      shot("03_position.png", "Position fix."),
      PageBreak(),
      Paragraph("Satellites", h2),
      P("A table of every satellite in view: system, PRN, elevation, azimuth, C/N0, whether it is used in the fix, and a per-signal C/N0 "
        "list (such as L1C/A and E5a) for dual-frequency receivers reporting NMEA 4.10 signal IDs."),
      shot("04_satellites.png", "Satellite table with per-signal C/N0."),
      PageBreak(),
      Paragraph("NMEA stream", h2),
      P("The raw sentences as they arrive, colour-coded by talker. <b>Pause</b> freezes the display. "
        "<b>Record</b> saves the raw stream to " + MONO % "logs\\gps_YYYYMMDD_HHMMSS.nmea" + "."),
      shot("05_nmea.png", "Raw NMEA stream."),
      Paragraph("Supported systems", h2),
      P("GPS, GLONASS, Galileo, BeiDou, QZSS, NavIC and SBAS, from single-system talkers (GP, GL, GA, GB, GQ) or combined GN, "
        "with or without NMEA 4.10 system and signal IDs."),
      PageBreak()]

s += [Paragraph("4. Measuring the receiver", h1),
      P("The four analysis tabs gather data continuously while connected. Keep the receiver and antenna still while measuring."),
      Paragraph("Position drift", h2),
      P("Every valid fix is plotted as north/east/up metres from the mean position (or a pinned reference). Shown are a horizontal "
        "scatter with CEP50 and CEP95 circles, north, east and up against time, and the statistics: CEP50, CEP95, 2DRMS, maximum "
        "horizontal error and vertical sigma."),
      *B(["<b>Pin reference</b> / <b>Use running mean</b>: measure against a known survey position instead of the running average.",
          "<b>Reset</b> clears the collected data. <b>Record CSV</b> writes " + MONO % "logs\\position_drift_*.csv"
          + ", starting with everything collected so far and then appending live."]),
      shot("06_drift.png", "Position drift: scatter with CEP circles and N/E/U against time."),
      PageBreak(),
      Paragraph("Track deviation", h2),
      P("Every 5 seconds each satellite\u2019s reported azimuth/elevation is compared with its orbit-predicted position. "
        "Predictions come from Celestrak TLEs propagated with SGP4 for your own fix position. The tab shows the \u0394El, "
        "\u0394Az\u00b7cos(el) and angular separation per satellite, the observed track against the predicted one, and a per-satellite RMS summary."),
      *B(["GPS and BeiDou are matched to their TLEs by PRN. GLONASS and Galileo TLE names do not carry the slot number, so those satellites "
          "(marked <b>*</b>) are matched to the nearest predicted satellite of the same constellation.",
          "NMEA reports whole degrees, so about 0.4\u00b0 RMS of rounding noise is the floor. Consistent offsets above that are real deviation.",
          "Orbit data is cached in " + MONO % "tle\\" + " and refreshed when more than 12 hours old, or with <b>Refresh orbits</b>. "
          "An internet connection is needed for the first download."]),
      shot("07_track.png", "Track deviation: per-satellite table, separation over time, and RMS by satellite."),
      PageBreak(),
      Paragraph("Sky mask", h2),
      P("Shows where on the sky the antenna actually gets signal, built up as satellites pass (10\u00b0 x 5\u00b0 cells). "
        "Two polar maps show mean C/N0 per cell, and the fraction of orbit-predicted satellites that the receiver actually tracked. "
        "A C/N0 against elevation chart (an open sky rises smoothly) and a low-sky table by compass direction point out the weakest direction. "
        "Satellites never tracked all session, such as retired ones still in the orbit data, are excluded from availability by default. "
        "<b>Export CSV</b> saves the cells to " + MONO % "logs\\sky_mask_*.csv" + ". Give it an hour or more for a full picture."),
      shot("08_mask.png", "Sky mask: mean C/N0, tracked vs predicted, C/N0 against elevation, and the low-sky table."),
      PageBreak(),
      Paragraph("PC clock", h2),
      P("Estimates the PC clock\u2019s offset from GPS time, using the arrival time of each fix\u2019s first NMEA sentence (stamped at the serial read). "
        "The lowest offset over the last 60 seconds is the estimate: PC error plus the minimum delivery latency "
        "(typically 0.05 to 0.3 s over Bluetooth). For FT8/FT4, within \u00b10.5 s is good, \u00b11 s is marginal, and beyond that the clock should be corrected. "
        "It is not available with the DEMO source, whose clock runs 60x."),
      shot("09_clock.png", "PC clock tab (blank in demo mode; it needs a real receiver)."),
      PageBreak()]

s += [Paragraph("5. Reference", h1),
      Paragraph("Files the program writes", h2),
      *grid([["Location", "Contents"],
             ["settings.json", "Port, baud rate, window position and size, option toggles, last tab."],
             ["logs\\gps_*.nmea", "Raw NMEA recording from the NMEA stream tab."],
             ["logs\\position_drift_*.csv", "Position drift recording."],
             ["logs\\track_deviation_*.csv", "Track deviation recording."],
             ["logs\\sky_mask_*.csv", "Sky mask cell export."],
             ["tle\\", "Cached Celestrak orbit data."]], [2.3 * inch, 4.7 * inch]),
      Paragraph("Troubleshooting", h2),
      *grid([["Symptom", "What to check"],
             ["\u201cReceiver not reachable\u201d (Windows error 121)",
              "Windows could not open the Bluetooth link: the receiver is off, asleep, out of range, or already connected to another "
              "device such as a phone. The app retries every few seconds."],
             ["Connected but no data", "Wrong COM port or baud rate. After 10 s of silence the app reopens the port."],
             ["Many \u201cbad checksum\u201d sentences", "Wrong baud rate, or a poor link. Check the status bar counters."],
             ["No predicted + crosses, empty Track deviation",
              "Orbit data has not been downloaded yet. It needs an internet connection and a position fix; try Refresh orbits."],
             ["Sky mask looks sparse", "Satellites fill in as they pass. Run for an hour or more."],
             ["Program will not start from source", "Install the dependencies: pip install pyserial sgp4 matplotlib."]],
            [2.3 * inch, 4.7 * inch]),
      Paragraph("About ephemeris", h2),
      P("NMEA does not carry the broadcast ephemeris (the Keplerian orbit and clock parameters). Only a receiver\u2019s proprietary binary "
        "protocol exposes those. NMEA provides what the receiver computed from them: each satellite\u2019s elevation, azimuth and C/N0 (GSV), "
        "which satellites are in the solution (GSA), and the fix itself (GGA and RMC). That is why the Track deviation tab "
        "compares against separately downloaded orbit data."),
      ]


def foot(c, d):
    c.saveState()
    c.setFont("Seg", 8)
    c.setFillColor(DIM)
    if d.page > 1:
        c.drawString(0.75 * inch, 0.5 * inch, "GPS Satellite Viewer \u2013 User Guide")
        c.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, "Page %d" % d.page)
    c.restoreState()


doc = SimpleDocTemplate(OUT, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.7 * inch,
                        bottomMargin=0.8 * inch, title="GPS Satellite Viewer User Guide", author="lrizio")
doc.build(s, onFirstPage=foot, onLaterPages=foot)
print("wrote", OUT)
