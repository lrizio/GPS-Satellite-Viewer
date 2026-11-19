# GPS Satellite Viewer

Live satellite display for a Bluetooth (or any serial) NMEA GPS receiver.

```
python gps_app.py
```

Requires `pyserial`, plus `sgp4` and `matplotlib` for the drift tabs (`pip install pyserial sgp4 matplotlib`). Tkinter ships with Python.

**User guide:** [docs/GPS_Satellite_Viewer_User_Guide.pdf](docs/GPS_Satellite_Viewer_User_Guide.pdf)

## Standalone exe (Windows)
```
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name "GPS Satellite Viewer" --icon icon.ico --add-data "icon.ico;." gps_app.py
```
The exe is written to `dist\`. When frozen, `settings.json`, `logs\` and `tle\` are kept next to the exe.

## What it shows
- **Sky view**: satellite azimuth/elevation, coloured by constellation (filled = used in the fix), with each satellite's track drawn while the app runs
- **Signal strength**: C/N0 (dB-Hz) per satellite (hatched = tracked but not used)
- **Position fix**: UTC, lat/lon, Maidenhead locator, altitude, geoid separation, speed/course, fix type, PDOP/HDOP/VDOP
- **Satellite table**: including per-signal C/N0 for dual-frequency receivers (NMEA 4.10 signal IDs)
- **Raw NMEA stream**: can be recorded to `logs\gps_YYYYMMDD_HHMMSS.nmea`

Supports GPS, GLONASS, Galileo, BeiDou, QZSS, NavIC and SBAS, from single-system talkers (GP/GL/GA/GB/GQ) or combined GN, with or without NMEA 4.10 system/signal IDs.

## Tabs
Everything is in one window, one tab per display: **Sky view**, **Signal strength**, **Position fix**, **Satellites**, **NMEA stream**, **Position drift**, **Track deviation**, **Sky mask** and **PC clock**. The tabs are 3D metal buttons that wrap onto extra rows when the window is too narrow to fit them in one. The toolbar and status bar are shared across tabs, and the app reopens on the last tab used.

**Resizing**: the window can be made as small as 640 x 600. Charts, tables, fonts and the port selector scale with the window, and chart text shrinks (to a floor) rather than overlapping. Buttons and the status bar use small, regular-weight text (`FONT_BTN_SMALL` in `theme.py`).

Drift data is collected continuously while connected, whichever tab is showing. It's cleared on each new Connect, so demo and real sessions never mix.

**Position drift**: every valid fix as north/east/up metres from the mean position (or a pinned reference). Shows a horizontal scatter with CEP50/CEP95 circles, N/E/U against time, and CEP50, CEP95, 2DRMS, max horizontal error and vertical sigma. Keep the receiver still while measuring.

**Track deviation**: every 5 s, each satellite's reported az/el is compared with its orbit-predicted az/el. Predictions come from Celestrak TLEs propagated with SGP4 for your own fix position, at the receiver epoch the az/el was reported in. Shows ΔEl, ΔAz·cos(el) and angular separation per satellite, the observed track overlaid on the predicted one, and a per-satellite RMS summary.
- GPS and BeiDou map to their TLEs by PRN. GLONASS and Galileo TLE names don't carry the slot/PRN, so those satellites (marked `*`) are matched to the nearest predicted satellite of the same constellation.
- NMEA reports whole degrees, so about 0.4° RMS of rounding noise is the floor. Consistent offsets above that are real deviation. TLE error itself is about 0.01°.
- Orbit data is cached in `tle\` and refreshed when over 12 h old, or with **Refresh orbits**.

The main sky view also shows the orbit-predicted positions as `+` crosses (the **Predicted +** toggle). A labelled cross with no satellite dot on it is a satellite that should be up but isn't being reported.

Both measurements can be recorded to `logs\position_drift_*.csv` and `logs\track_deviation_*.csv`. A recording starts with everything collected so far, then appends live.

**Sky mask**: where on the sky the antenna actually gets signal, built up as satellites pass (10° × 5° cells). Two polar maps show the mean C/N0 per cell, and the fraction of orbit-predicted satellites in each cell that the receiver actually tracked. Also shows C/N0 against elevation (an open sky rises smoothly) and a low-sky (5-30°) table by compass direction, pointing at the weakest direction. Satellites the receiver never tracks all session (e.g. retired or unhealthy ones still in the orbit data) are excluded from availability by default. **Export CSV** saves the cells to `logs\sky_mask_*.csv`. Give it an hour or more.

**PC clock**: the PC clock's offset from GPS time, from when each fix's first NMEA sentence arrives (stamped at the serial read). The lowest offset over the last 60 s is the estimate: PC error plus minimum delivery latency (typically 0.05-0.3 s over Bluetooth). Rated for FT8/FT4: within ±0.5 s is good, ±1 s marginal, beyond that correct the clock. Not available with the DEMO source.

With orbit data loaded, the DEMO source generates the real constellations for Melbourne at 60x speed, so both analyses can be tried without hardware.

## Connecting a Bluetooth receiver
Pair it in Windows first. Windows then creates "Standard Serial over Bluetooth link" COM ports. The app labels the outgoing port with the receiver's Bluetooth name (e.g. `COM13 - iBT-GPS (Bluetooth)`) and selects it by default. The baud setting has no effect on a Bluetooth SPP link.

"Receiver not reachable" (Windows error 121, semaphore timeout) means Windows couldn't open the RFCOMM link. The receiver is off, asleep, out of range, or already connected to another device (e.g. a phone). While connected, the app keeps retrying every few seconds. It also reconnects if the link drops or goes silent for 10 s.

Pick **DEMO -- simulated receiver** to see the app working with no hardware.

## About "ephemeris"
NMEA does not carry the broadcast ephemeris itself, i.e. the Keplerian orbit and clock parameters. Only a receiver's proprietary binary protocol exposes those (u-blox UBX-RXM-SFRBX, SiRF MID 15, ...). NMEA provides what the receiver computed from them: each satellite's elevation, azimuth and C/N0 (GSV), which satellites are in the solution (GSA), and the fix (GGA/RMC).

## Files
- `nmea.py`: dependency-free NMEA parser and receiver-state model
- `gps_app.py`: Tkinter GUI, serial and demo sources
- `drift.py`: position-drift statistics, TLE/SGP4 orbit prediction, track-deviation measurement
- `drifttabs.py`: the Position drift and Track deviation tabs (matplotlib)
- `skymask.py`: sky obstruction map accumulation
- `pcclock.py`: PC clock offset from NMEA arrival times
- `skyclocktabs.py`: the Sky mask and PC clock tabs
- `theme.py`: shared HamProjects Tkinter theme (copied from MiniVNATiny), including the 3D metal buttons
- `scripts/make_icon.py`: generates `icon.ico`
- `scripts/guide_screens.py`, `scripts/make_guide.py`: regenerate the user guide screenshots (light theme, DEMO source) and PDF
