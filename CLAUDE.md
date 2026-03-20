# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python scripts for controlling QHY5-II-M astronomy camera via the QHY SDK.

## Environment

- Python 3.10 virtual environment in `venv/`
- Activate with: `source venv/bin/activate`
- Requires QHY SDK installed system-wide (libqhyccd.so)
- System fxload required for firmware loading: `sudo apt install fxload`

## Fresh Install (Pi3/Pi4/ARM64)

```bash
sudo ./install_qhy.sh
# Unplug and replug camera
python3 -m venv venv
source venv/bin/activate
pip install numpy astropy pillow
python qhy_capture.py
```

## Dependencies

```bash
pip install numpy astropy pillow
```

## Running

```bash
# Basic test capture (100ms exposure, saves FITS)
python qhy_capture.py

# Custom exposure and gain
python qhy_capture.py -e 50 -g 20

# Save as PNG instead of FITS
python qhy_capture.py --png
```

## QHY SDK Notes

- QHY5-II-M must use live/video mode (stream mode 1) - single-frame mode fails
- Camera re-enumerates after firmware load: 1618:0920 → 1618:0921
- Exposure is set in microseconds internally (multiply ms by 1000)
- QHY5-II-M specs: 1280x1024 pixels, 5.2um pixel size, monochrome, 8-bit
- Set USB traffic parameter (CONTROL_USBTRAFFIC=12) before capture
- GetQHYCCDLiveFrame may need multiple attempts as camera warms up
