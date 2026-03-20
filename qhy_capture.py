#!/usr/bin/env python3
"""
QHY5-II-M Camera Test Script

Captures a test image from the QHY5-II-M camera using the QHY SDK.
Requires the QHY SDK to be installed on the system.
"""

import ctypes
from ctypes import c_uint32, c_double, c_char_p, c_void_p, c_uint8, POINTER, byref
import numpy as np
from datetime import datetime

# Load the QHY SDK library
try:
    sdk = ctypes.CDLL("libqhyccd.so")
except OSError:
    print("Error: Could not load libqhyccd.so")
    print("Make sure the QHY SDK is installed. On Linux:")
    print("  1. Download SDK from https://www.qhyccd.com/download/")
    print("  2. Install the SDK and udev rules")
    raise SystemExit(1)

# SDK function signatures
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
sdk.ExpQHYCCDSingleFrame.argtypes = [c_void_p]
sdk.ExpQHYCCDSingleFrame.restype = c_uint32
sdk.GetQHYCCDSingleFrame.argtypes = [c_void_p, POINTER(c_uint32), POINTER(c_uint32),
                                      POINTER(c_uint32), POINTER(c_uint32), POINTER(c_uint8)]
sdk.GetQHYCCDSingleFrame.restype = c_uint32
sdk.GetQHYCCDMemLength.argtypes = [c_void_p]
sdk.GetQHYCCDMemLength.restype = c_uint32
sdk.CancelQHYCCDExposingAndReadout.argtypes = [c_void_p]
sdk.CancelQHYCCDExposingAndReadout.restype = c_uint32
sdk.BeginQHYCCDLive.argtypes = [c_void_p]
sdk.BeginQHYCCDLive.restype = c_uint32
sdk.StopQHYCCDLive.argtypes = [c_void_p]
sdk.StopQHYCCDLive.restype = c_uint32
sdk.GetQHYCCDLiveFrame.argtypes = [c_void_p, POINTER(c_uint32), POINTER(c_uint32),
                                    POINTER(c_uint32), POINTER(c_uint32), POINTER(c_uint8)]
sdk.GetQHYCCDLiveFrame.restype = c_uint32

# QHY SDK constants
QHYCCD_SUCCESS = 0
CONTROL_BRIGHTNESS = 0
CONTROL_CONTRAST = 1
CONTROL_WBR = 2
CONTROL_WBB = 3
CONTROL_WBG = 4
CONTROL_GAMMA = 5
CONTROL_GAIN = 6
CONTROL_OFFSET = 7
CONTROL_EXPOSURE = 8
CONTROL_SPEED = 9
CONTROL_TRANSFERBIT = 10
CONTROL_CHANNELS = 11
CONTROL_USBTRAFFIC = 12
CONTROL_CURTEMP = 14
CONTROL_CURPWM = 15
CONTROL_MANULPWM = 16
CONTROL_COOLER = 18


def capture_image(exposure_ms=100, gain=10, save_fits=True):
    """
    Capture a single image from the QHY5-II-M camera.

    Args:
        exposure_ms: Exposure time in milliseconds
        gain: Camera gain (0-100 typical range)
        save_fits: If True, save as FITS file; otherwise save as PNG

    Returns:
        numpy array containing the image data
    """
    handle = None

    try:
        # Initialize SDK
        ret = sdk.InitQHYCCDResource()
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to initialize QHY SDK: {ret}")
        print("SDK initialized")

        # Scan for cameras
        num_cameras = sdk.ScanQHYCCD()
        if num_cameras == 0:
            raise RuntimeError("No QHY cameras found")
        print(f"Found {num_cameras} camera(s)")

        # Get camera ID
        camera_id = ctypes.create_string_buffer(64)
        ret = sdk.GetQHYCCDId(0, camera_id)
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to get camera ID: {ret}")
        print(f"Camera ID: {camera_id.value.decode()}")

        # Open camera
        handle = sdk.OpenQHYCCD(camera_id)
        if handle is None:
            raise RuntimeError("Failed to open camera")
        print("Camera opened")

        # Set live/video mode (0 = single frame, 1 = live/video mode)
        # QHY5-II-M works better in live mode
        ret = sdk.SetQHYCCDStreamMode(handle, 1)
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to set stream mode: {ret}")
        print("Stream mode: live")

        # Initialize camera
        ret = sdk.InitQHYCCD(handle)
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to initialize camera: {ret}")
        print("Camera initialized")

        # Get chip info
        chipw = c_double()
        chiph = c_double()
        imagew = c_uint32()
        imageh = c_uint32()
        pixelw = c_double()
        pixelh = c_double()
        bpp = c_uint32()

        ret = sdk.GetQHYCCDChipInfo(handle, byref(chipw), byref(chiph),
                                     byref(imagew), byref(imageh),
                                     byref(pixelw), byref(pixelh), byref(bpp))
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to get chip info: {ret}")

        print(f"Chip size: {chipw.value:.2f} x {chiph.value:.2f} mm")
        print(f"Image size: {imagew.value} x {imageh.value} pixels")
        print(f"Pixel size: {pixelw.value:.2f} x {pixelh.value:.2f} um")
        print(f"Bits per pixel: {bpp.value}")

        # Set binning 1x1
        ret = sdk.SetQHYCCDBinMode(handle, 1, 1)
        if ret != QHYCCD_SUCCESS:
            print(f"Warning: Failed to set bin mode: {ret}")

        # Set resolution (full frame)
        ret = sdk.SetQHYCCDResolution(handle, 0, 0, imagew.value, imageh.value)
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to set resolution: {ret}")

        # Set USB traffic (important for QHY5-II)
        ret = sdk.SetQHYCCDParam(handle, CONTROL_USBTRAFFIC, 30)
        if ret != QHYCCD_SUCCESS:
            print(f"Warning: Failed to set USB traffic: {ret}")
        else:
            print("USB traffic set to 30")

        # Set speed
        ret = sdk.SetQHYCCDParam(handle, CONTROL_SPEED, 0)
        if ret != QHYCCD_SUCCESS:
            print(f"Warning: Failed to set speed: {ret}")

        # Set 8-bit transfer
        ret = sdk.SetQHYCCDParam(handle, CONTROL_TRANSFERBIT, 8)
        if ret != QHYCCD_SUCCESS:
            print(f"Warning: Failed to set transfer bit: {ret}")

        # Set gain first (before exposure for QHY5-II)
        ret = sdk.SetQHYCCDParam(handle, CONTROL_GAIN, gain)
        if ret != QHYCCD_SUCCESS:
            print(f"Warning: Failed to set gain: {ret}")
        else:
            print(f"Gain set to {gain}")

        # Set exposure (in microseconds)
        exposure_us = exposure_ms * 1000
        ret = sdk.SetQHYCCDParam(handle, CONTROL_EXPOSURE, exposure_us)
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to set exposure: {ret}")
        print(f"Exposure set to {exposure_ms} ms")

        # Get required buffer size
        mem_len = sdk.GetQHYCCDMemLength(handle)
        print(f"Buffer size: {mem_len} bytes")

        # Allocate buffer
        img_data = (c_uint8 * mem_len)()

        # Start live mode
        print("Starting live capture...")
        ret = sdk.BeginQHYCCDLive(handle)
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to start live mode: {ret}")

        # Get a frame (may need a few attempts as camera warms up)
        import time
        w = c_uint32()
        h = c_uint32()
        bpp_out = c_uint32()
        channels = c_uint32()

        max_attempts = 50
        for attempt in range(max_attempts):
            ret = sdk.GetQHYCCDLiveFrame(handle, byref(w), byref(h),
                                          byref(bpp_out), byref(channels), img_data)
            if ret == QHYCCD_SUCCESS:
                break
            time.sleep(0.1)
        else:
            sdk.StopQHYCCDLive(handle)
            raise RuntimeError(f"Failed to get frame after {max_attempts} attempts")

        # Stop live mode
        sdk.StopQHYCCDLive(handle)

        print(f"Image captured: {w.value} x {h.value}, {bpp_out.value} bpp, {channels.value} channels")

        # Convert to numpy array
        img_array = np.ctypeslib.as_array(img_data)
        img_array = img_array[:w.value * h.value].reshape((h.value, w.value))

        # Save the image
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if save_fits:
            try:
                from astropy.io import fits
                filename = f"qhy5_test_{timestamp}.fits"
                hdu = fits.PrimaryHDU(img_array)
                hdu.header['EXPTIME'] = exposure_ms / 1000.0
                hdu.header['GAIN'] = gain
                hdu.header['CAMERA'] = camera_id.value.decode()
                hdu.header['DATE-OBS'] = datetime.now().isoformat()
                hdu.writeto(filename, overwrite=True)
                print(f"Saved FITS: {filename}")
            except ImportError:
                print("astropy not installed, falling back to PNG")
                save_fits = False

        if not save_fits:
            from PIL import Image
            filename = f"qhy5_test_{timestamp}.png"
            img = Image.fromarray(img_array)
            img.save(filename)
            print(f"Saved PNG: {filename}")

        return img_array

    finally:
        # Cleanup
        if handle is not None:
            sdk.CancelQHYCCDExposingAndReadout(handle)
            sdk.CloseQHYCCD(handle)
            print("Camera closed")
        sdk.ReleaseQHYCCDResource()
        print("SDK released")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Capture test image from QHY5-II-M")
    parser.add_argument("-e", "--exposure", type=float, default=100,
                        help="Exposure time in milliseconds (default: 100)")
    parser.add_argument("-g", "--gain", type=int, default=10,
                        help="Camera gain (default: 10)")
    parser.add_argument("--png", action="store_true",
                        help="Save as PNG instead of FITS")

    args = parser.parse_args()

    capture_image(
        exposure_ms=args.exposure,
        gain=args.gain,
        save_fits=not args.png
    )
