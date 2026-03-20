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

    def __init__(self, exposure_ms=100, gain=10):
        self.exposure_ms = exposure_ms
        self.gain = gain
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._latest_jpeg = None
        self._pending_params = None   # (exposure_ms, gain)
        self._fps = 0.0
        self._error = None
        self._auto_stretch = True
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="camera")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def set_params(self, exposure_ms=None, gain=None, auto_stretch=None):
        with self._lock:
            if auto_stretch is not None:
                self._auto_stretch = auto_stretch
            if exposure_ms is not None or gain is not None:
                new_exp = exposure_ms if exposure_ms is not None else self.exposure_ms
                new_gain = gain if gain is not None else self.gain
                self._pending_params = (new_exp, new_gain)

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
                'error': self._error,
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

            sdk.SetQHYCCDBinMode(handle, 1, 1)
            sdk.SetQHYCCDResolution(handle, 0, 0, imagew.value, imageh.value)
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

            print(f"Camera ready: {imagew.value}x{imageh.value}, "
                  f"exposure={self.exposure_ms}ms, gain={self.gain}")

            fps_frames = 0
            fps_t = time.time()

            while not self._stop_event.is_set():
                # Apply any queued parameter change
                with self._lock:
                    pending = self._pending_params
                    self._pending_params = None

                if pending:
                    exp_ms, gain = pending
                    sdk.StopQHYCCDLive(handle)
                    sdk.SetQHYCCDParam(handle, CONTROL_GAIN, gain)
                    sdk.SetQHYCCDParam(handle, CONTROL_EXPOSURE, exp_ms * 1000)
                    with self._lock:
                        self.exposure_ms = exp_ms
                        self.gain = gain
                    sdk.BeginQHYCCDLive(handle)

                ret = sdk.GetQHYCCDLiveFrame(
                    handle, byref(w), byref(h), byref(bpp_out), byref(channels), img_data
                )
                if ret == QHYCCD_SUCCESS:
                    arr = np.ctypeslib.as_array(img_data)
                    arr = arr[:w.value * h.value].reshape((h.value, w.value)).copy()

                    with self._lock:
                        auto_stretch = self._auto_stretch

                    jpeg = self._encode_jpeg(arr, auto_stretch)

                    with self._lock:
                        self._latest_jpeg = jpeg

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
    <img id="stream-img" src="/stream" alt="Camera stream">
    <div id="crosshair"></div>
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
      })
      .catch(() => {});
  }
  setInterval(pollStats, 1000);
  pollStats();

  // Load initial params from server
  fetch('/api/params')
    .then(r => r.json())
    .then(d => {
      expSlider.value = d.exposure_ms;
      expNum.value    = d.exposure_ms;
      gainSlider.value = d.gain;
      gainNum.value    = d.gain;
      chkStretch.checked = d.auto_stretch;
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
        while True:
            frame = camera.get_jpeg()
            if frame is not None and frame is not last:
                last = frame
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                time.sleep(0.02)

    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/api/params', methods=['GET', 'POST'])
def params():
    if request.method == 'POST':
        data = request.get_json(force=True)
        camera.set_params(
            exposure_ms=data.get('exposure_ms'),
            gain=data.get('gain'),
            auto_stretch=data.get('auto_stretch'),
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
