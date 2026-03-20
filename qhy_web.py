#!/usr/bin/env python3
"""
QHY5-II-M Live Preview Web Server

Streams a live camera feed via browser for focus control.
Access at http://<host>:5000 after starting.

Usage:
    python qhy_web.py
    python qhy_web.py --exposure 200 --gain 20 --port 5000
"""

import ctypes
from ctypes import c_uint32, c_double, c_char_p, c_void_p, c_uint8, POINTER, byref
import numpy as np
import threading
import time
import io
import argparse

try:
    from flask import Flask, Response, jsonify, request, render_template_string
except ImportError:
    print("Flask not installed. Run: pip install flask")
    raise SystemExit(1)

try:
    from PIL import Image
except ImportError:
    print("Pillow not installed. Run: pip install pillow")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# QHY SDK setup
# ---------------------------------------------------------------------------

try:
    sdk = ctypes.CDLL("libqhyccd.so")
except OSError:
    print("Error: Could not load libqhyccd.so")
    print("Make sure the QHY SDK is installed.")
    raise SystemExit(1)

sdk.InitQHYCCDResource.restype = c_uint32
sdk.ReleaseQHYCCDResource.restype = c_uint32
sdk.ScanQHYCCD.restype = c_uint32
sdk.GetQHYCCDId.argtypes = [c_uint32, c_char_p]
sdk.GetQHYCCDId.restype = c_uint32
sdk.OpenQHYCCD.argtypes = [c_char_p]
sdk.OpenQHYCCD.restype = c_void_p
sdk.CloseQHYCCD.argtypes = [c_void_p]
sdk.CloseQHYCCD.restype = c_uint32
sdk.SetQHYCCDStreamMode.argtypes = [c_void_p, c_uint8]
sdk.SetQHYCCDStreamMode.restype = c_uint32
sdk.InitQHYCCD.argtypes = [c_void_p]
sdk.InitQHYCCD.restype = c_uint32
sdk.GetQHYCCDChipInfo.argtypes = [c_void_p, POINTER(c_double), POINTER(c_double),
                                   POINTER(c_uint32), POINTER(c_uint32),
                                   POINTER(c_double), POINTER(c_double),
                                   POINTER(c_uint32)]
sdk.GetQHYCCDChipInfo.restype = c_uint32
sdk.SetQHYCCDBinMode.argtypes = [c_void_p, c_uint32, c_uint32]
sdk.SetQHYCCDBinMode.restype = c_uint32
sdk.SetQHYCCDResolution.argtypes = [c_void_p, c_uint32, c_uint32, c_uint32, c_uint32]
sdk.SetQHYCCDResolution.restype = c_uint32
sdk.SetQHYCCDParam.argtypes = [c_void_p, c_uint32, c_double]
sdk.SetQHYCCDParam.restype = c_uint32
sdk.GetQHYCCDMemLength.argtypes = [c_void_p]
sdk.GetQHYCCDMemLength.restype = c_uint32
sdk.BeginQHYCCDLive.argtypes = [c_void_p]
sdk.BeginQHYCCDLive.restype = c_uint32
sdk.StopQHYCCDLive.argtypes = [c_void_p]
sdk.StopQHYCCDLive.restype = c_uint32
sdk.GetQHYCCDLiveFrame.argtypes = [c_void_p, POINTER(c_uint32), POINTER(c_uint32),
                                    POINTER(c_uint32), POINTER(c_uint32), POINTER(c_uint8)]
sdk.GetQHYCCDLiveFrame.restype = c_uint32
sdk.CancelQHYCCDExposingAndReadout.argtypes = [c_void_p]
sdk.CancelQHYCCDExposingAndReadout.restype = c_uint32

QHYCCD_SUCCESS = 0
CONTROL_GAIN = 6
CONTROL_EXPOSURE = 8
CONTROL_SPEED = 9
CONTROL_TRANSFERBIT = 10
CONTROL_USBTRAFFIC = 12


# ---------------------------------------------------------------------------
# Camera worker thread
# ---------------------------------------------------------------------------

class CameraWorker:
    """Captures frames continuously in a background thread."""

    SENSOR_W = 1280
    SENSOR_H = 1024

    def __init__(self, exposure_ms=100, gain=10):
        self.exposure_ms = exposure_ms
        self.gain = gain
        self.roi_size = 0           # 0 = full frame; else 128/256/512
        self.roi_cx = self.SENSOR_W // 2
        self.roi_cy = self.SENSOR_H // 2
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._latest_jpeg = None
        self._pending_params = None   # dict of changed params
        self._fps = 0.0
        self._error = None
        self._auto_stretch = True
        self._histogram = None   # list of 256 counts
        self._hist_stats = None  # (min_adu, median_adu, max_adu)
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="camera")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    @staticmethod
    def _roi_xywh(roi_size, roi_cx, roi_cy, sensor_w, sensor_h):
        """Return (x, y, w, h) clamped to sensor bounds."""
        if roi_size == 0:
            return 0, 0, sensor_w, sensor_h
        w = h = roi_size
        x = max(0, min(roi_cx - w // 2, sensor_w - w))
        y = max(0, min(roi_cy - h // 2, sensor_h - h))
        return x, y, w, h

    def set_params(self, exposure_ms=None, gain=None, auto_stretch=None,
                   roi_size=None, roi_cx=None, roi_cy=None):
        with self._lock:
            if auto_stretch is not None:
                self._auto_stretch = auto_stretch
            needs_restart = (exposure_ms is not None or gain is not None
                             or roi_size is not None or roi_cx is not None
                             or roi_cy is not None)
            if needs_restart:
                pending = self._pending_params or {}
                if exposure_ms is not None:
                    pending['exposure_ms'] = exposure_ms
                if gain is not None:
                    pending['gain'] = gain
                if roi_size is not None:
                    pending['roi_size'] = roi_size
                if roi_cx is not None:
                    pending['roi_cx'] = roi_cx
                if roi_cy is not None:
                    pending['roi_cy'] = roi_cy
                self._pending_params = pending

    def get_jpeg(self):
        with self._lock:
            return self._latest_jpeg

    def get_stats(self):
        with self._lock:
            return {
                'fps': round(self._fps, 1),
                'exposure_ms': self.exposure_ms,
                'gain': self.gain,
                'auto_stretch': self._auto_stretch,
                'roi_size': self.roi_size,
                'roi_cx': self.roi_cx,
                'roi_cy': self.roi_cy,
                'sensor_w': self.SENSOR_W,
                'sensor_h': self.SENSOR_H,
                'error': self._error,
            }

    def get_histogram(self):
        with self._lock:
            return {
                'histogram': self._histogram,
                'stats': self._hist_stats,
            }

    def _encode_jpeg(self, arr, auto_stretch):
        if auto_stretch:
            lo, hi = np.percentile(arr, [0.5, 99.5])
            if hi > lo:
                arr = np.clip(
                    (arr.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255
                ).astype(np.uint8)
        img = Image.fromarray(arr)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        return buf.getvalue()

    def _run(self):
        handle = None
        try:
            ret = sdk.InitQHYCCDResource()
            if ret != QHYCCD_SUCCESS:
                raise RuntimeError(f"InitQHYCCDResource failed: {ret}")

            num_cameras = sdk.ScanQHYCCD()
            if num_cameras == 0:
                raise RuntimeError("No QHY cameras found. Check USB connection.")

            camera_id = ctypes.create_string_buffer(64)
            ret = sdk.GetQHYCCDId(0, camera_id)
            if ret != QHYCCD_SUCCESS:
                raise RuntimeError(f"GetQHYCCDId failed: {ret}")

            handle = sdk.OpenQHYCCD(camera_id)
            if handle is None:
                raise RuntimeError("OpenQHYCCD returned null handle")

            ret = sdk.SetQHYCCDStreamMode(handle, 1)  # 1 = live mode
            if ret != QHYCCD_SUCCESS:
                raise RuntimeError(f"SetQHYCCDStreamMode failed: {ret}")

            ret = sdk.InitQHYCCD(handle)
            if ret != QHYCCD_SUCCESS:
                raise RuntimeError(f"InitQHYCCD failed: {ret}")

            chipw, chiph = c_double(), c_double()
            imagew, imageh = c_uint32(), c_uint32()
            pixelw, pixelh = c_double(), c_double()
            bpp = c_uint32()
            sdk.GetQHYCCDChipInfo(handle, byref(chipw), byref(chiph),
                                   byref(imagew), byref(imageh),
                                   byref(pixelw), byref(pixelh), byref(bpp))

            sensor_w = imagew.value
            sensor_h = imageh.value

            sdk.SetQHYCCDBinMode(handle, 1, 1)
            rx, ry, rw, rh = self._roi_xywh(
                self.roi_size, self.roi_cx, self.roi_cy, sensor_w, sensor_h)
            sdk.SetQHYCCDResolution(handle, rx, ry, rw, rh)
            sdk.SetQHYCCDParam(handle, CONTROL_USBTRAFFIC, 30)
            sdk.SetQHYCCDParam(handle, CONTROL_SPEED, 0)
            sdk.SetQHYCCDParam(handle, CONTROL_TRANSFERBIT, 8)
            sdk.SetQHYCCDParam(handle, CONTROL_GAIN, self.gain)
            sdk.SetQHYCCDParam(handle, CONTROL_EXPOSURE, self.exposure_ms * 1000)

            mem_len = sdk.GetQHYCCDMemLength(handle)
            img_data = (c_uint8 * mem_len)()
            w, h, bpp_out, channels = c_uint32(), c_uint32(), c_uint32(), c_uint32()

            ret = sdk.BeginQHYCCDLive(handle)
            if ret != QHYCCD_SUCCESS:
                raise RuntimeError(f"BeginQHYCCDLive failed: {ret}")

            roi_label = f"{rw}x{rh}" if self.roi_size else "full"
            print(f"Camera ready: {sensor_w}x{sensor_h}, ROI={roi_label}, "
                  f"exposure={self.exposure_ms}ms, gain={self.gain}")

            fps_frames = 0
            fps_t = time.time()

            while not self._stop_event.is_set():
                # Apply any queued parameter changes
                with self._lock:
                    pending = self._pending_params
                    self._pending_params = None

                if pending:
                    sdk.StopQHYCCDLive(handle)
                    with self._lock:
                        if 'exposure_ms' in pending:
                            self.exposure_ms = pending['exposure_ms']
                        if 'gain' in pending:
                            self.gain = pending['gain']
                        if 'roi_size' in pending:
                            self.roi_size = pending['roi_size']
                        if 'roi_cx' in pending:
                            self.roi_cx = pending['roi_cx']
                        if 'roi_cy' in pending:
                            self.roi_cy = pending['roi_cy']
                        exp_ms = self.exposure_ms
                        gain = self.gain
                        roi_size = self.roi_size
                        roi_cx = self.roi_cx
                        roi_cy = self.roi_cy
                    rx, ry, rw, rh = self._roi_xywh(
                        roi_size, roi_cx, roi_cy, sensor_w, sensor_h)
                    sdk.SetQHYCCDResolution(handle, rx, ry, rw, rh)
                    sdk.SetQHYCCDParam(handle, CONTROL_GAIN, gain)
                    sdk.SetQHYCCDParam(handle, CONTROL_EXPOSURE, exp_ms * 1000)
                    sdk.BeginQHYCCDLive(handle)
                    roi_label = f"{rw}x{rh}" if roi_size else "full"
                    print(f"Params updated: ROI={roi_label} @({rx},{ry}), "
                          f"exp={exp_ms}ms, gain={gain}")

                ret = sdk.GetQHYCCDLiveFrame(
                    handle, byref(w), byref(h), byref(bpp_out), byref(channels), img_data
                )
                if ret == QHYCCD_SUCCESS:
                    arr = np.ctypeslib.as_array(img_data)
                    arr = arr[:w.value * h.value].reshape((h.value, w.value)).copy()

                    with self._lock:
                        auto_stretch = self._auto_stretch

                    jpeg = self._encode_jpeg(arr, auto_stretch)

                    # Compute histogram on raw 8-bit pixel values
                    hist, _ = np.histogram(arr, bins=256, range=(0, 255))
                    hist_stats = (
                        int(arr.min()),
                        int(np.median(arr)),
                        int(arr.max()),
                    )

                    with self._lock:
                        self._latest_jpeg = jpeg
                        self._histogram = hist.tolist()
                        self._hist_stats = hist_stats

                    fps_frames += 1
                    now = time.time()
                    if now - fps_t >= 1.0:
                        with self._lock:
                            self._fps = fps_frames / (now - fps_t)
                        fps_frames = 0
                        fps_t = now
                else:
                    time.sleep(0.01)

        except Exception as e:
            print(f"Camera error: {e}")
            with self._lock:
                self._error = str(e)
        finally:
            if handle:
                sdk.StopQHYCCDLive(handle)
                sdk.CancelQHYCCDExposingAndReadout(handle)
                sdk.CloseQHYCCD(handle)
            sdk.ReleaseQHYCCDResource()
            print("Camera released")


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
camera = None  # set in main()

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QHY5 Live Preview</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d0d0d;
    color: #e0e0e0;
    font-family: 'Courier New', monospace;
    font-size: 14px;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }
  #header {
    background: #1a1a2e;
    padding: 8px 16px;
    display: flex;
    align-items: center;
    gap: 24px;
    border-bottom: 1px solid #333;
    flex-shrink: 0;
  }
  #header h1 { font-size: 16px; color: #7eb8f7; letter-spacing: 1px; }
  #stats { display: flex; gap: 16px; font-size: 12px; color: #aaa; }
  .stat-val { color: #7eb8f7; font-weight: bold; }
  #error-banner {
    display: none;
    background: #5c1a1a;
    border: 1px solid #c0392b;
    color: #e74c3c;
    padding: 8px 16px;
    font-size: 13px;
  }
  #main {
    display: flex;
    flex: 1;
    overflow: hidden;
  }
  #preview-wrap {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    background: #000;
    position: relative;
  }
  #stream-img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    display: block;
  }
  #crosshair {
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    pointer-events: none;
    display: none;
  }
  #crosshair::before, #crosshair::after {
    content: '';
    position: absolute;
    background: rgba(255, 80, 80, 0.7);
  }
  #crosshair::before { width: 1px; height: 40px; top: -20px; left: 0; }
  #crosshair::after  { width: 40px; height: 1px; left: -20px; top: 0; }
  #controls {
    width: 220px;
    background: #111;
    border-left: 1px solid #333;
    padding: 16px 12px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    overflow-y: auto;
    flex-shrink: 0;
  }
  .ctrl-group label {
    display: block;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #888;
    margin-bottom: 6px;
  }
  .row { display: flex; gap: 6px; align-items: center; }
  input[type=range] {
    flex: 1;
    -webkit-appearance: none;
    appearance: none;
    height: 4px;
    background: #333;
    border-radius: 2px;
    outline: none;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 14px; height: 14px;
    border-radius: 50%;
    background: #7eb8f7;
    cursor: pointer;
  }
  input[type=number] {
    width: 64px;
    background: #1e1e1e;
    border: 1px solid #444;
    color: #e0e0e0;
    padding: 3px 5px;
    border-radius: 3px;
    font-family: inherit;
    font-size: 13px;
    text-align: right;
  }
  input[type=number]:focus { outline: none; border-color: #7eb8f7; }
  .toggle-row { display: flex; align-items: center; gap: 8px; cursor: pointer; }
  .toggle-row input[type=checkbox] { width: 16px; height: 16px; cursor: pointer; accent-color: #7eb8f7; }
  .toggle-row span { font-size: 13px; }
  .divider { border: none; border-top: 1px solid #2a2a2a; }
  .hint { font-size: 11px; color: #555; line-height: 1.5; }
  .btn-group { display: flex; gap: 4px; flex-wrap: wrap; }
  .roi-btn {
    flex: 1;
    min-width: 44px;
    padding: 5px 4px;
    background: #1e1e1e;
    border: 1px solid #444;
    color: #aaa;
    border-radius: 3px;
    font-family: inherit;
    font-size: 11px;
    cursor: pointer;
    text-align: center;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }
  .roi-btn:hover { border-color: #7eb8f7; color: #e0e0e0; }
  .roi-btn.active { background: #1c3a5e; border-color: #7eb8f7; color: #7eb8f7; }
  .center-row { display: flex; gap: 4px; align-items: center; margin-top: 8px; font-size: 11px; color: #888; }
  .center-row input[type=number] { width: 56px; font-size: 11px; }
  #roi-overlay {
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    cursor: crosshair; z-index: 2;
  }
  #roi-overlay.idle { cursor: crosshair; }
  .roi-pending { display: none; margin-top: 10px; }
  .roi-pending.visible { display: block; }
  .pending-info { font-size: 11px; color: #aaa; margin-bottom: 6px; }
  .pending-info span { color: #7eb8f7; }
  .apply-row { display: flex; gap: 5px; }
  .btn-apply {
    flex: 1; padding: 6px 4px;
    background: #1c3a5e; border: 1px solid #7eb8f7; color: #7eb8f7;
    border-radius: 3px; font-family: inherit; font-size: 12px; cursor: pointer;
  }
  .btn-apply:hover { background: #2a5080; }
  .btn-cancel-pending {
    flex: 1; padding: 6px 4px;
    background: #1e1e1e; border: 1px solid #444; color: #888;
    border-radius: 3px; font-family: inherit; font-size: 12px; cursor: pointer;
  }
  .btn-cancel-pending:hover { border-color: #aaa; color: #ccc; }
  .btn-clear-roi {
    display: none; width: 100%; margin-top: 6px; padding: 6px 4px;
    background: #2a1515; border: 1px solid #7a2020; color: #c05050;
    border-radius: 3px; font-family: inherit; font-size: 12px; cursor: pointer;
  }
  .btn-clear-roi.visible { display: block; }
  .btn-clear-roi:hover { background: #3a2020; border-color: #d46060; color: #e07070; }
</style>
</head>
<body>

<div id="header">
  <h1>QHY5 Live Preview</h1>
  <div id="stats">
    <span>FPS: <span class="stat-val" id="s-fps">--</span></span>
    <span>Exp: <span class="stat-val" id="s-exp">--</span> ms</span>
    <span>Gain: <span class="stat-val" id="s-gain">--</span></span>
  </div>
</div>
<div id="error-banner"></div>

<div id="main">
  <div id="preview-wrap">
    <img id="stream-img" src="/stream" alt="Camera stream" style="pointer-events:none;">
    <div id="crosshair"></div>
    <canvas id="roi-overlay"></canvas>
  </div>

  <div id="controls">

    <div class="ctrl-group">
      <label>Exposure (ms)</label>
      <div class="row">
        <input type="range" id="exp-slider" min="1" max="5000" step="1" value="100">
        <input type="number" id="exp-num" min="1" max="5000" value="100">
      </div>
      <div style="margin-top:4px; font-size:11px; color:#555;">1 – 5000 ms</div>
    </div>

    <div class="ctrl-group">
      <label>Gain</label>
      <div class="row">
        <input type="range" id="gain-slider" min="0" max="100" step="1" value="10">
        <input type="number" id="gain-num" min="0" max="100" value="10">
      </div>
    </div>

    <hr class="divider">

    <div class="ctrl-group">
      <label>Display</label>
      <label class="toggle-row">
        <input type="checkbox" id="chk-stretch" checked>
        <span>Auto-stretch</span>
      </label>
      <div style="margin-top:10px">
      <label class="toggle-row">
        <input type="checkbox" id="chk-crosshair">
        <span>Crosshair</span>
      </label>
      </div>
    </div>

    <hr class="divider">

    <div class="ctrl-group">
      <label>Region of Interest</label>
      <div class="btn-group">
        <button class="roi-btn" data-size="128">128²</button>
        <button class="roi-btn" data-size="256">256²</button>
        <button class="roi-btn" data-size="512">512²</button>
        <button class="roi-btn active" data-size="0">Full</button>
      </div>
      <div class="center-row">
        <span>Center X:</span>
        <input type="number" id="roi-cx" min="0" max="1280" value="640">
        <span>Y:</span>
        <input type="number" id="roi-cy" min="0" max="1024" value="512">
      </div>
      <div style="margin-top:5px;font-size:10px;color:#555;">Click image to position ROI</div>
      <div class="roi-pending" id="roi-pending">
        <div class="pending-info">→ <span id="pending-coords">--</span> px</div>
        <div class="apply-row">
          <button class="btn-apply" id="btn-apply-roi">Apply</button>
          <button class="btn-cancel-pending" id="btn-cancel-roi">Cancel</button>
        </div>
      </div>
      <button class="btn-clear-roi" id="btn-clear-roi">✕ Clear ROI</button>
    </div>

    <hr class="divider">

    <div class="ctrl-group">
      <label>Histogram (ADU)</label>
      <canvas id="hist-canvas" width="196" height="80"
              style="width:100%;background:#0a0a0a;border:1px solid #2a2a2a;border-radius:2px;display:block;"></canvas>
      <div style="margin-top:5px;font-size:11px;line-height:1.8;color:#888;">
        Min: <span class="stat-val" id="h-min">--</span>
        &nbsp;Med: <span class="stat-val" id="h-med">--</span>
        &nbsp;Max: <span class="stat-val" id="h-max">--</span>
      </div>
    </div>

    <hr class="divider">

    <div class="hint">
      <b>Focus tips:</b><br>
      • Start with short exposure (10–50 ms) and high gain<br>
      • Auto-stretch reveals faint stars<br>
      • Crosshair marks frame center<br>
      • Increase exposure once focused
    </div>

  </div>
</div>

<script>
(function () {
  const expSlider = document.getElementById('exp-slider');
  const expNum    = document.getElementById('exp-num');
  const gainSlider = document.getElementById('gain-slider');
  const gainNum    = document.getElementById('gain-num');
  const chkStretch = document.getElementById('chk-stretch');
  const chkCross   = document.getElementById('chk-crosshair');
  const crosshair  = document.getElementById('crosshair');
  const errorBanner = document.getElementById('error-banner');

  let sendTimer = null;
  function scheduleSend() {
    clearTimeout(sendTimer);
    sendTimer = setTimeout(sendParams, 300);
  }

  function sendParams() {
    fetch('/api/params', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        exposure_ms: parseInt(expNum.value),
        gain: parseInt(gainNum.value),
        auto_stretch: chkStretch.checked,
      })
    });
  }

  // Sync slider ↔ number input
  expSlider.addEventListener('input', () => { expNum.value = expSlider.value; scheduleSend(); });
  expNum.addEventListener('change', () => {
    expSlider.value = Math.min(5000, Math.max(1, expNum.value));
    expNum.value = expSlider.value;
    scheduleSend();
  });
  gainSlider.addEventListener('input', () => { gainNum.value = gainSlider.value; scheduleSend(); });
  gainNum.addEventListener('change', () => {
    gainSlider.value = Math.min(100, Math.max(0, gainNum.value));
    gainNum.value = gainSlider.value;
    scheduleSend();
  });

  chkStretch.addEventListener('change', sendParams);

  chkCross.addEventListener('change', () => {
    crosshair.style.display = chkCross.checked ? 'block' : 'none';
  });

  // Poll stats
  function pollStats() {
    fetch('/api/params')
      .then(r => r.json())
      .then(d => {
        document.getElementById('s-fps').textContent  = d.fps ?? '--';
        document.getElementById('s-exp').textContent  = d.exposure_ms ?? '--';
        document.getElementById('s-gain').textContent = d.gain ?? '--';
        if (d.error) {
          errorBanner.textContent = 'Camera error: ' + d.error;
          errorBanner.style.display = 'block';
        }
        // Keep camState in sync so overlay coordinates are always correct
        if (d.roi_size  !== undefined) camState.roi_size  = d.roi_size;
        if (d.roi_cx    !== undefined) camState.roi_cx    = d.roi_cx;
        if (d.roi_cy    !== undefined) camState.roi_cy    = d.roi_cy;
        if (d.sensor_w  !== undefined) camState.sensor_w  = d.sensor_w;
        if (d.sensor_h  !== undefined) camState.sensor_h  = d.sensor_h;
        if (d.roi_size  > 0)           lastSubRoiSize      = d.roi_size;
        updateClearBtn();
        drawRoiOverlay();
      })
      .catch(() => {});
  }
  setInterval(pollStats, 1000);
  pollStats();

  // Histogram
  const histCanvas = document.getElementById('hist-canvas');
  const histCtx = histCanvas.getContext('2d');

  function drawHistogram(data) {
    const W = histCanvas.width, H = histCanvas.height;
    histCtx.clearRect(0, 0, W, H);
    const max = Math.max(...data);
    if (max === 0) return;
    const barW = W / data.length;
    histCtx.fillStyle = '#4a90d9';
    for (let i = 0; i < data.length; i++) {
      const barH = (data[i] / max) * H;
      histCtx.fillRect(i * barW, H - barH, barW, barH);
    }
  }

  function pollHistogram() {
    fetch('/api/histogram')
      .then(r => r.json())
      .then(d => {
        if (d.histogram) drawHistogram(d.histogram);
        if (d.stats) {
          document.getElementById('h-min').textContent = d.stats[0];
          document.getElementById('h-med').textContent = d.stats[1];
          document.getElementById('h-max').textContent = d.stats[2];
        }
      })
      .catch(() => {});
  }
  setInterval(pollHistogram, 500);
  pollHistogram();

  // --------------- ROI state ---------------
  const roiBtns    = document.querySelectorAll('.roi-btn');
  const roiCxInput = document.getElementById('roi-cx');
  const roiCyInput = document.getElementById('roi-cy');
  const roiCanvas  = document.getElementById('roi-overlay');
  const roiCtx     = roiCanvas.getContext('2d');

  // Mirrors of server state, updated from pollStats
  const camState = { roi_size: 0, roi_cx: 640, roi_cy: 512, sensor_w: 1280, sensor_h: 1024 };

  // Last non-zero roi_size, used as preview size when full-frame is active
  let lastSubRoiSize = 256;

  // Pending click: {cx, cy} in sensor coords, or null
  let pendingCenter = null;

  // Ghost: the last active sub-ROI shown as a dashed outline when in full-frame mode
  let ghostRoi = null;

  // ---- helpers ----

  function roiXYWH(size, cx, cy, sw, sh) {
    if (size === 0) return { x: 0, y: 0, w: sw, h: sh };
    const x = Math.max(0, Math.min(cx - size / 2, sw - size));
    const y = Math.max(0, Math.min(cy - size / 2, sh - size));
    return { x, y, w: size, h: size };
  }

  // Returns the position/size of the rendered image content within the canvas.
  // The <img> uses object-fit:contain but the element itself is already
  // sized to fit (max-w/h:100% in a flex-center container), so
  // getBoundingClientRect gives the content area directly.
  function getImgRenderInfo() {
    const img = document.getElementById('stream-img');
    const iRect = img.getBoundingClientRect();
    const cRect = roiCanvas.getBoundingClientRect();
    const offX = iRect.left - cRect.left;
    const offY = iRect.top  - cRect.top;
    const renderW = iRect.width;
    const renderH = iRect.height;
    const curRoi = roiXYWH(camState.roi_size, camState.roi_cx, camState.roi_cy,
                            camState.sensor_w, camState.sensor_h);
    return { offX, offY, renderW, renderH, curRoi };
  }

  // Canvas pixel → sensor coordinate (null if outside the image).
  function canvasToSensor(cx, cy) {
    const { offX, offY, renderW, renderH, curRoi } = getImgRenderInfo();
    const nx = (cx - offX) / renderW;
    const ny = (cy - offY) / renderH;
    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return null;
    return {
      cx: Math.round(curRoi.x + nx * curRoi.w),
      cy: Math.round(curRoi.y + ny * curRoi.h),
    };
  }

  function drawRoiOverlay() {
    roiCtx.clearRect(0, 0, roiCanvas.width, roiCanvas.height);

    if (pendingCenter) {
      const { offX, offY, renderW, renderH, curRoi } = getImgRenderInfo();
      const { sensor_w, sensor_h } = camState;
      const previewSize = camState.roi_size > 0 ? camState.roi_size : lastSubRoiSize;
      const proposed = roiXYWH(previewSize, pendingCenter.cx, pendingCenter.cy, sensor_w, sensor_h);

      const toCanvas = (sx, sy) => ({
        x: offX + ((sx - curRoi.x) / curRoi.w) * renderW,
        y: offY + ((sy - curRoi.y) / curRoi.h) * renderH,
      });

      const tl = toCanvas(proposed.x, proposed.y);
      const br = toCanvas(proposed.x + proposed.w, proposed.y + proposed.h);
      const bw = br.x - tl.x;
      const bh = br.y - tl.y;

      roiCtx.save();
      roiCtx.beginPath();
      roiCtx.rect(0, 0, roiCanvas.width, roiCanvas.height);
      roiCtx.rect(tl.x, tl.y, bw, bh);
      roiCtx.fillStyle = 'rgba(0,0,0,0.58)';
      roiCtx.fill('evenodd');

      roiCtx.strokeStyle = '#7eb8f7';
      roiCtx.lineWidth = 1.5;
      roiCtx.strokeRect(tl.x, tl.y, bw, bh);

      const cc = toCanvas(pendingCenter.cx, pendingCenter.cy);
      roiCtx.strokeStyle = 'rgba(126,184,247,0.9)';
      roiCtx.lineWidth = 1;
      roiCtx.beginPath();
      roiCtx.moveTo(cc.x - 10, cc.y); roiCtx.lineTo(cc.x + 10, cc.y);
      roiCtx.moveTo(cc.x, cc.y - 10); roiCtx.lineTo(cc.x, cc.y + 10);
      roiCtx.stroke();
      roiCtx.restore();
      return;
    }

    // Ghost: dashed outline of last sub-ROI, shown only in full-frame mode
    if (ghostRoi && camState.roi_size === 0) {
      const { offX, offY, renderW, renderH, curRoi } = getImgRenderInfo();
      const g = roiXYWH(ghostRoi.size, ghostRoi.cx, ghostRoi.cy, camState.sensor_w, camState.sensor_h);
      const toCanvas = (sx, sy) => ({
        x: offX + ((sx - curRoi.x) / curRoi.w) * renderW,
        y: offY + ((sy - curRoi.y) / curRoi.h) * renderH,
      });
      const tl = toCanvas(g.x, g.y);
      const br = toCanvas(g.x + g.w, g.y + g.h);
      roiCtx.save();
      roiCtx.strokeStyle = 'rgba(126,184,247,0.45)';
      roiCtx.lineWidth = 1;
      roiCtx.setLineDash([4, 4]);
      roiCtx.strokeRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);
      roiCtx.font = '10px monospace';
      roiCtx.fillStyle = 'rgba(126,184,247,0.55)';
      roiCtx.fillText(`${ghostRoi.size}²`, tl.x + 3, tl.y + 11);
      roiCtx.restore();
    }
  }

  function setPendingCenter(sensorCx, sensorCy) {
    pendingCenter = { cx: sensorCx, cy: sensorCy };
    roiCxInput.value = sensorCx;
    roiCyInput.value = sensorCy;
    document.getElementById('pending-coords').textContent = `${sensorCx}, ${sensorCy}`;
    document.getElementById('roi-pending').classList.add('visible');
    drawRoiOverlay();
  }

  function cancelPending() {
    pendingCenter = null;
    document.getElementById('roi-pending').classList.remove('visible');
    drawRoiOverlay();  // may draw ghost if applicable
  }

  function applyPending() {
    if (!pendingCenter) return;
    const applySize = camState.roi_size > 0 ? camState.roi_size : lastSubRoiSize;
    if (applySize !== camState.roi_size) setActiveRoiBtn(applySize);
    camState.roi_size = applySize;
    ghostRoi = null;  // entering sub-ROI mode, ghost no longer needed
    sendRoi(applySize, pendingCenter.cx, pendingCenter.cy);
    cancelPending();
    updateClearBtn();
  }

  // ---- button / input wiring ----

  function setActiveRoiBtn(size) {
    roiBtns.forEach(b => b.classList.toggle('active', parseInt(b.dataset.size) === size));
  }

  function updateClearBtn() {
    document.getElementById('btn-clear-roi').classList.toggle('visible', camState.roi_size > 0);
  }

  function sendRoi(size, cx, cy) {
    fetch('/api/params', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ roi_size: size, roi_cx: cx, roi_cy: cy }),
    });
  }

  roiBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const size = parseInt(btn.dataset.size);
      if (size === 0) {
        // Full: apply immediately, save ghost so the old ROI stays visible
        if (camState.roi_size > 0) {
          ghostRoi = { size: camState.roi_size, cx: camState.roi_cx, cy: camState.roi_cy };
        }
        sendRoi(0, camState.roi_cx, camState.roi_cy);
        camState.roi_size = 0;
        cancelPending();  // clears pending, redraws ghost
        setActiveRoiBtn(0);
        updateClearBtn();
      } else {
        // Sub-ROI: update preview size only — don't send to camera yet
        camState.roi_size = size;
        lastSubRoiSize = size;
        setActiveRoiBtn(size);
        if (pendingCenter) {
          drawRoiOverlay();
        } else {
          // Auto-start pending with the current center so Apply becomes available
          setPendingCenter(
            parseInt(roiCxInput.value) || camState.roi_cx,
            parseInt(roiCyInput.value) || camState.roi_cy,
          );
        }
      }
    });
  });

  roiCxInput.addEventListener('change', () => {
    if (pendingCenter) setPendingCenter(parseInt(roiCxInput.value), pendingCenter.cy);
    else sendRoi(camState.roi_size, parseInt(roiCxInput.value), parseInt(roiCyInput.value));
  });
  roiCyInput.addEventListener('change', () => {
    if (pendingCenter) setPendingCenter(pendingCenter.cx, parseInt(roiCyInput.value));
    else sendRoi(camState.roi_size, parseInt(roiCxInput.value), parseInt(roiCyInput.value));
  });

  document.getElementById('btn-apply-roi').addEventListener('click', applyPending);
  document.getElementById('btn-cancel-roi').addEventListener('click', cancelPending);
  document.getElementById('btn-clear-roi').addEventListener('click', () => {
    if (camState.roi_size > 0) {
      ghostRoi = { size: camState.roi_size, cx: camState.roi_cx, cy: camState.roi_cy };
    }
    sendRoi(0, camState.roi_cx, camState.roi_cy);
    camState.roi_size = 0;
    cancelPending();
    setActiveRoiBtn(0);
    updateClearBtn();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') cancelPending(); });

  // ---- canvas click ----

  roiCanvas.addEventListener('click', e => {
    const rect = roiCanvas.getBoundingClientRect();
    const sensor = canvasToSensor(e.clientX - rect.left, e.clientY - rect.top);
    if (!sensor) return;
    setPendingCenter(sensor.cx, sensor.cy);
  });

  // ---- resize: keep canvas pixel dims in sync with layout dims ----

  function resizeCanvas() {
    const wrap = document.getElementById('preview-wrap');
    roiCanvas.width  = wrap.clientWidth;
    roiCanvas.height = wrap.clientHeight;
    drawRoiOverlay();
  }
  window.addEventListener('resize', resizeCanvas);
  resizeCanvas();

  // Load initial params from server
  fetch('/api/params')
    .then(r => r.json())
    .then(d => {
      expSlider.value = d.exposure_ms;
      expNum.value    = d.exposure_ms;
      gainSlider.value = d.gain;
      gainNum.value    = d.gain;
      chkStretch.checked = d.auto_stretch;
      if (d.roi_size  !== undefined) { camState.roi_size = d.roi_size; setActiveRoiBtn(d.roi_size); }
      if (d.roi_cx    !== undefined) { camState.roi_cx   = d.roi_cx;   roiCxInput.value = d.roi_cx; }
      if (d.roi_cy    !== undefined) { camState.roi_cy   = d.roi_cy;   roiCyInput.value = d.roi_cy; }
      if (d.sensor_w  !== undefined) { camState.sensor_w = d.sensor_w; roiCxInput.max   = d.sensor_w; }
      if (d.sensor_h  !== undefined) { camState.sensor_h = d.sensor_h; roiCyInput.max   = d.sensor_h; }
      if (d.roi_size  > 0) lastSubRoiSize = d.roi_size;
      updateClearBtn();
    });
})();
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/stream')
def stream():
    def generate():
        last = None
        target_interval = 0.1  # 10 fps
        next_send = time.time()
        while True:
            now = time.time()
            wait = next_send - now
            if wait > 0:
                time.sleep(wait)
            frame = camera.get_jpeg()
            if frame is not None and frame is not last:
                last = frame
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                next_send = time.time() + target_interval
            else:
                time.sleep(0.02)

    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/api/histogram')
def histogram():
    return jsonify(camera.get_histogram())


@app.route('/api/params', methods=['GET', 'POST'])
def params():
    if request.method == 'POST':
        data = request.get_json(force=True)
        camera.set_params(
            exposure_ms=data.get('exposure_ms'),
            gain=data.get('gain'),
            auto_stretch=data.get('auto_stretch'),
            roi_size=data.get('roi_size'),
            roi_cx=data.get('roi_cx'),
            roi_cy=data.get('roi_cy'),
        )
        return jsonify({'ok': True})
    return jsonify(camera.get_stats())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global camera

    parser = argparse.ArgumentParser(description="QHY5-II-M live preview web server")
    parser.add_argument('-e', '--exposure', type=float, default=100,
                        help='Initial exposure in ms (default: 100)')
    parser.add_argument('-g', '--gain', type=int, default=10,
                        help='Initial gain 0-100 (default: 10)')
    parser.add_argument('-p', '--port', type=int, default=5000,
                        help='HTTP port (default: 5000)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Bind address (default: 0.0.0.0)')
    args = parser.parse_args()

    camera = CameraWorker(exposure_ms=args.exposure, gain=args.gain)
    camera.start()

    print(f"Starting web server on http://{args.host}:{args.port}")
    print("Open the URL in your browser to see the live feed.")
    print("Press Ctrl+C to stop.")

    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        camera.stop()


if __name__ == '__main__':
    main()
