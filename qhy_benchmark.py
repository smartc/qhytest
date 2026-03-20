#!/usr/bin/env python3
"""
QHY5-II-M Subframe Benchmark Script

Tests subframe capture performance at various ROI sizes.
"""

import ctypes
from ctypes import c_uint32, c_double, c_char_p, c_void_p, c_uint8, POINTER, byref
import numpy as np
import time

# Load the QHY SDK library
sdk = ctypes.CDLL("libqhyccd.so")

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
sdk.GetQHYCCDMemLength.argtypes = [c_void_p]
sdk.GetQHYCCDMemLength.restype = c_uint32
sdk.BeginQHYCCDLive.argtypes = [c_void_p]
sdk.BeginQHYCCDLive.restype = c_uint32
sdk.StopQHYCCDLive.argtypes = [c_void_p]
sdk.StopQHYCCDLive.restype = c_uint32
sdk.GetQHYCCDLiveFrame.argtypes = [c_void_p, POINTER(c_uint32), POINTER(c_uint32),
                                    POINTER(c_uint32), POINTER(c_uint32), POINTER(c_uint8)]
sdk.GetQHYCCDLiveFrame.restype = c_uint32

# QHY SDK constants
QHYCCD_SUCCESS = 0
CONTROL_GAIN = 6
CONTROL_EXPOSURE = 8
CONTROL_SPEED = 9
CONTROL_TRANSFERBIT = 10
CONTROL_USBTRAFFIC = 12


def benchmark_subframe(width, height, exposure_ms, num_frames, gain=10):
    """
    Benchmark subframe capture performance.

    Args:
        width: ROI width in pixels
        height: ROI height in pixels
        exposure_ms: Exposure time in milliseconds
        num_frames: Number of frames to capture
        gain: Camera gain

    Returns:
        Dictionary with timing statistics
    """
    handle = None

    # Calculate center offset for ROI
    full_width = 1280
    full_height = 1024
    x_offset = (full_width - width) // 2
    y_offset = (full_height - height) // 2

    try:
        # Initialize SDK
        ret = sdk.InitQHYCCDResource()
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to initialize QHY SDK: {ret}")

        # Scan for cameras
        num_cameras = sdk.ScanQHYCCD()
        if num_cameras == 0:
            raise RuntimeError("No QHY cameras found")

        # Get camera ID
        camera_id = ctypes.create_string_buffer(64)
        sdk.GetQHYCCDId(0, camera_id)

        # Open camera
        handle = sdk.OpenQHYCCD(camera_id)
        if handle is None:
            raise RuntimeError("Failed to open camera")

        # Set live mode
        sdk.SetQHYCCDStreamMode(handle, 1)

        # Initialize camera
        ret = sdk.InitQHYCCD(handle)
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to initialize camera: {ret}")

        # Set binning 1x1
        sdk.SetQHYCCDBinMode(handle, 1, 1)

        # Set ROI resolution
        ret = sdk.SetQHYCCDResolution(handle, x_offset, y_offset, width, height)
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to set resolution: {ret}")

        # Set USB traffic (lower = faster, but may cause issues)
        sdk.SetQHYCCDParam(handle, CONTROL_USBTRAFFIC, 0)

        # Set speed
        sdk.SetQHYCCDParam(handle, CONTROL_SPEED, 2)

        # Set 8-bit transfer
        sdk.SetQHYCCDParam(handle, CONTROL_TRANSFERBIT, 8)

        # Set gain
        sdk.SetQHYCCDParam(handle, CONTROL_GAIN, gain)

        # Set exposure (in microseconds)
        exposure_us = exposure_ms * 1000
        sdk.SetQHYCCDParam(handle, CONTROL_EXPOSURE, exposure_us)

        # Get required buffer size
        mem_len = sdk.GetQHYCCDMemLength(handle)

        # Allocate buffer
        img_data = (c_uint8 * mem_len)()

        # Start live mode
        ret = sdk.BeginQHYCCDLive(handle)
        if ret != QHYCCD_SUCCESS:
            raise RuntimeError(f"Failed to start live mode: {ret}")

        # Variables for frame capture
        w = c_uint32()
        h = c_uint32()
        bpp_out = c_uint32()
        channels = c_uint32()

        # Warm up - get a few frames first
        for _ in range(5):
            for _ in range(50):
                ret = sdk.GetQHYCCDLiveFrame(handle, byref(w), byref(h),
                                              byref(bpp_out), byref(channels), img_data)
                if ret == QHYCCD_SUCCESS:
                    break
                time.sleep(0.001)

        # Benchmark loop
        frame_times = []
        successful_frames = 0

        print(f"Capturing {num_frames} frames at {width}x{height}, {exposure_ms}ms exposure...")

        total_start = time.perf_counter()

        for i in range(num_frames):
            frame_start = time.perf_counter()

            # Try to get frame
            max_attempts = 100
            for attempt in range(max_attempts):
                ret = sdk.GetQHYCCDLiveFrame(handle, byref(w), byref(h),
                                              byref(bpp_out), byref(channels), img_data)
                if ret == QHYCCD_SUCCESS:
                    break
                time.sleep(0.001)

            frame_end = time.perf_counter()

            if ret == QHYCCD_SUCCESS:
                frame_times.append(frame_end - frame_start)
                successful_frames += 1

        total_end = time.perf_counter()

        # Stop live mode
        sdk.StopQHYCCDLive(handle)

        # Calculate statistics
        total_time = total_end - total_start
        if frame_times:
            avg_frame_time = np.mean(frame_times) * 1000  # ms
            min_frame_time = np.min(frame_times) * 1000
            max_frame_time = np.max(frame_times) * 1000
            fps = successful_frames / total_time
        else:
            avg_frame_time = min_frame_time = max_frame_time = fps = 0

        return {
            'width': width,
            'height': height,
            'exposure_ms': exposure_ms,
            'num_frames': num_frames,
            'successful_frames': successful_frames,
            'total_time_s': total_time,
            'avg_frame_ms': avg_frame_time,
            'min_frame_ms': min_frame_time,
            'max_frame_ms': max_frame_time,
            'fps': fps
        }

    finally:
        if handle is not None:
            sdk.StopQHYCCDLive(handle)
            sdk.CloseQHYCCD(handle)
        sdk.ReleaseQHYCCDResource()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Benchmark QHY5-II-M subframe capture")
    parser.add_argument("-W", "--width", type=int, default=128,
                        help="ROI width (default: 128)")
    parser.add_argument("-H", "--height", type=int, default=128,
                        help="ROI height (default: 128)")
    parser.add_argument("-e", "--exposure", type=float, default=5,
                        help="Exposure time in ms (default: 5)")
    parser.add_argument("-n", "--num-frames", type=int, default=100,
                        help="Number of frames to capture (default: 100)")
    parser.add_argument("-g", "--gain", type=int, default=10,
                        help="Camera gain (default: 10)")

    args = parser.parse_args()

    results = benchmark_subframe(
        width=args.width,
        height=args.height,
        exposure_ms=args.exposure,
        num_frames=args.num_frames,
        gain=args.gain
    )

    print(f"\n=== Results for {results['width']}x{results['height']} @ {results['exposure_ms']}ms ===")
    print(f"Frames captured: {results['successful_frames']}/{results['num_frames']}")
    print(f"Total time: {results['total_time_s']:.2f}s")
    print(f"Frame time: {results['avg_frame_ms']:.1f}ms avg, {results['min_frame_ms']:.1f}ms min, {results['max_frame_ms']:.1f}ms max")
    print(f"Frame rate: {results['fps']:.1f} FPS")
