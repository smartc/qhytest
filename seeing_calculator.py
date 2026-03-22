#!/usr/bin/env python3
"""
Seeing Calculator Module

Self-contained seeing estimation from centroid variance and/or FWHM statistics.
Uses the single circular aperture centroid variance formula from
Tokovinin (2002), PASP 114, 1156.

Dependencies: numpy only.
"""

import collections
import math
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class SeeingConfig:
    focal_length_mm: float
    aperture_mm: float
    pixel_size_um: float
    method: str                     # "CENTROID", "FWHM", or "BOTH"
    window_frames: int = 200
    wavelength_m: float = 5.5e-7    # 550 nm
    min_valid_fraction: float = 0.7


@dataclass
class FrameResult:
    timestamp: float                # time.perf_counter() at frame receipt
    x_centroid: float               # pixels
    y_centroid: float               # pixels
    fwhm_px: Optional[float]        # pixels, None if not available
    peak_adu: int                   # peak pixel value
    snr: float                      # measured (peak - bg) / noise
    valid: bool                     # False if saturated or detection failed


@dataclass
class SeeingEstimate:
    timestamp: float
    seeing_arcsec: float            # primary output
    r0_m: Optional[float]
    sigma2_rad2: Optional[float]
    fwhm_median_arcsec: Optional[float]
    fwhm_std_arcsec: Optional[float]
    frames_in_window: int
    valid_fraction: float
    method_used: str
    quality: str                    # "OK", "LOW_VALID_FRACTION", etc.


class SeeingCalculator:

    def __init__(self, config: SeeingConfig):
        self.config = config

        # Precompute plate scales
        focal_length_m = config.focal_length_mm / 1000.0
        pixel_size_m = config.pixel_size_um / 1e6
        self.plate_scale_rad_px = pixel_size_m / focal_length_m
        self.plate_scale_arcsec_px = self.plate_scale_rad_px * 206265.0

        self._buffer = collections.deque(maxlen=config.window_frames)
        self._history = collections.deque(maxlen=120)
        self._frame_count = 0

    def add_frame(self, result: FrameResult) -> Optional[SeeingEstimate]:
        self._buffer.append(result)
        self._frame_count += 1

        if self._frame_count % 50 != 0:
            return None

        # Gather valid frames from the buffer
        all_frames = list(self._buffer)
        valid_frames = [f for f in all_frames if f.valid]
        total = len(all_frames)
        valid_fraction = len(valid_frames) / total if total > 0 else 0.0

        if len(valid_frames) < 10:
            return None

        method = self.config.method
        seeing_arcsec = None
        r0_m = None
        sigma2_rad2 = None
        fwhm_median_arcsec = None
        fwhm_std_arcsec = None

        if method in ("CENTROID", "BOTH"):
            seeing_arcsec, r0_m, sigma2_rad2 = self._calc_centroid_seeing(
                valid_frames)

        if method in ("FWHM", "BOTH"):
            fwhm_median_arcsec, fwhm_std_arcsec = self._calc_fwhm_seeing(
                valid_frames)

        if method == "FWHM":
            seeing_arcsec = fwhm_median_arcsec
        elif method == "BOTH" and seeing_arcsec is None:
            seeing_arcsec = fwhm_median_arcsec

        method_used = method
        quality = self._quality_flag(
            all_frames, valid_fraction, fwhm_std_arcsec, seeing_arcsec)

        estimate = SeeingEstimate(
            timestamp=time.time(),
            seeing_arcsec=seeing_arcsec,
            r0_m=r0_m,
            sigma2_rad2=sigma2_rad2,
            fwhm_median_arcsec=fwhm_median_arcsec,
            fwhm_std_arcsec=fwhm_std_arcsec,
            frames_in_window=len(all_frames),
            valid_fraction=valid_fraction,
            method_used=method_used,
            quality=quality,
        )

        self._history.append(estimate)
        return estimate

    def _calc_centroid_seeing(self, valid_frames):
        """Returns (seeing_arcsec, r0_m, sigma2_rad2)."""
        x = np.array([f.x_centroid for f in valid_frames])
        y = np.array([f.y_centroid for f in valid_frames])

        # Convert to radians
        x_rad = x * self.plate_scale_rad_px
        y_rad = y * self.plate_scale_rad_px

        # Detrend each axis with linear fit to remove Polaris drift
        n = len(x_rad)
        t = np.arange(n, dtype=np.float64)
        x_coeffs = np.polyfit(t, x_rad, 1)
        y_coeffs = np.polyfit(t, y_rad, 1)
        x_detrended = x_rad - np.polyval(x_coeffs, t)
        y_detrended = y_rad - np.polyval(y_coeffs, t)

        # Centroid variance (mean of x and y variances)
        sigma2_measured = (np.var(x_detrended) + np.var(y_detrended)) / 2.0

        # Subtract noise-induced centroid jitter bias.
        # For a Gaussian PSF, noise-induced centroid error per axis is
        # approximately sigma_psf / SNR (in pixels), where sigma_psf =
        # FWHM / 2.35.  Use the actual measured SNR from each frame.
        fwhm_vals = [f.fwhm_px for f in valid_frames if f.fwhm_px]
        snr_vals = [f.snr for f in valid_frames if f.snr > 0]
        if fwhm_vals and snr_vals:
            med_fwhm_px = float(np.median(fwhm_vals))
            med_snr = max(float(np.median(snr_vals)), 1.0)
            sigma_psf_px = med_fwhm_px / 2.35
            noise_var_px = (sigma_psf_px / med_snr) ** 2
            noise_var_rad = noise_var_px * self.plate_scale_rad_px ** 2
            sigma2 = max(sigma2_measured - noise_var_rad, 1e-20)
        else:
            sigma2 = sigma2_measured

        # Guard: if variance is effectively zero, seeing is indeterminate
        if sigma2 < 1e-20:
            return (None, None, sigma2)

        # Tokovinin (2002) single circular aperture formula
        aperture_m = self.config.aperture_mm / 1000.0
        wavelength = self.config.wavelength_m
        r0 = aperture_m * (
            0.179 * wavelength**2 / (sigma2 * aperture_m**2)
        ) ** (3.0 / 5.0)

        # Convert r0 to seeing FWHM
        seeing_arcsec = 0.98 * wavelength / r0 * 206265.0

        return (seeing_arcsec, r0, sigma2)

    def _calc_fwhm_seeing(self, valid_frames):
        """Returns (fwhm_median_arcsec, fwhm_std_arcsec)."""
        fwhm_vals = np.array(
            [f.fwhm_px for f in valid_frames if f.fwhm_px is not None],
            dtype=np.float64,
        )

        if len(fwhm_vals) < 5:
            return (None, None)

        # At coarse plate scales the measured FWHM is dominated by the
        # pixel/optics PSF rather than atmosphere.  Subtract a minimum
        # instrumental FWHM floor (~1.3 px Nyquist limit) in quadrature.
        fwhm_floor_px = 1.3
        fwhm_atm_px = np.sqrt(np.maximum(
            fwhm_vals ** 2 - fwhm_floor_px ** 2, 0.0))

        # Convert to arcsec
        fwhm_arcsec = fwhm_atm_px * self.plate_scale_arcsec_px

        # Filter out frames where the atmospheric contribution was zero
        fwhm_arcsec = fwhm_arcsec[fwhm_arcsec > 0]
        if len(fwhm_arcsec) < 5:
            return (None, None)

        # Reject outliers beyond 3 sigma
        median = np.median(fwhm_arcsec)
        std = np.std(fwhm_arcsec)
        if std > 0:
            mask = np.abs(fwhm_arcsec - median) < 3.0 * std
            fwhm_arcsec = fwhm_arcsec[mask]

        if len(fwhm_arcsec) < 3:
            return (None, None)

        return (float(np.median(fwhm_arcsec)), float(np.std(fwhm_arcsec)))

    def _quality_flag(self, all_frames, valid_fraction, fwhm_std, seeing):
        """Returns quality string, evaluated in priority order."""
        # SATURATED: >10% of frames have peak_adu > 240
        saturated_count = sum(1 for f in all_frames if f.peak_adu > 240)
        if saturated_count > 0.1 * len(all_frames):
            return "SATURATED"

        if valid_fraction < self.config.min_valid_fraction:
            return "LOW_VALID_FRACTION"

        if (fwhm_std is not None and seeing is not None
                and fwhm_std > 0.5 * seeing):
            return "HIGH_SCATTER"

        return "OK"

    def get_latest(self) -> Optional[SeeingEstimate]:
        if not self._history:
            return None
        return self._history[-1]

    def get_history(self, n: int = 30) -> list:
        history = list(self._history)
        return history[-n:]

    def to_dict(self, estimate: SeeingEstimate) -> dict:
        def _round(v):
            if v is None:
                return None
            return round(v, 3)

        return {
            'timestamp': _round(estimate.timestamp),
            'seeing_arcsec': _round(estimate.seeing_arcsec),
            'r0_m': _round(estimate.r0_m),
            'sigma2_rad2': _round(estimate.sigma2_rad2),
            'fwhm_median_arcsec': _round(estimate.fwhm_median_arcsec),
            'fwhm_std_arcsec': _round(estimate.fwhm_std_arcsec),
            'frames_in_window': estimate.frames_in_window,
            'valid_fraction': _round(estimate.valid_fraction),
            'method_used': estimate.method_used,
            'quality': estimate.quality,
        }


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import random

    print("=== Seeing Calculator Test Harness ===\n")

    config = SeeingConfig(
        focal_length_mm=130.0,
        aperture_mm=30.0,
        pixel_size_um=5.2,
        method="BOTH",
        window_frames=200,
    )
    calc = SeeingCalculator(config)

    print(f"Plate scale: {calc.plate_scale_arcsec_px:.2f} arcsec/px")
    print(f"Plate scale: {calc.plate_scale_rad_px:.6e} rad/px\n")

    # Target seeing = 2 arcsec → derive r0
    target_seeing = 2.0  # arcsec
    wavelength = config.wavelength_m
    # seeing = 0.98 * lambda / r0 * 206265  →  r0 = 0.98 * lambda / (seeing/206265)
    target_r0 = 0.98 * wavelength / (target_seeing / 206265.0)
    print(f"Target seeing: {target_seeing:.1f} arcsec")
    print(f"Target r0: {target_r0 * 1000:.1f} mm\n")

    # Derive expected centroid sigma in pixels
    # sigma2 = 0.179 * lambda^2 / (D^(1/3) * r0^(5/3))
    # From r0 = D * (0.179 * lambda^2 / (sigma2 * D^2))^(3/5)
    # Solving for sigma2: sigma2 = 0.179 * lambda^2 / (D^2 * (r0/D)^(5/3))
    aperture_m = config.aperture_mm / 1000.0
    sigma2_rad = 0.179 * wavelength**2 / (aperture_m**2 * (target_r0 / aperture_m) ** (5.0 / 3.0))
    sigma_rad = math.sqrt(sigma2_rad)
    sigma_px = sigma_rad / calc.plate_scale_rad_px
    print(f"Expected centroid sigma: {sigma_px:.3f} px ({sigma_rad * 206265:.3f} arcsec)\n")

    np.random.seed(42)
    random.seed(42)
    base_x, base_y = 128.0, 128.0
    estimates = []

    for i in range(500):
        drift = 0.005 * i  # slow linear drift
        cx = base_x + drift + np.random.normal(0, sigma_px)
        cy = base_y + drift * 0.3 + np.random.normal(0, sigma_px)
        fwhm = max(0.5, np.random.normal(1.5, 0.3))
        peak = random.randint(100, 200)

        fr = FrameResult(
            timestamp=time.perf_counter(),
            x_centroid=cx,
            y_centroid=cy,
            fwhm_px=fwhm,
            peak_adu=peak,
            snr=15.0,
            valid=True,
        )

        est = calc.add_frame(fr)
        if est is not None:
            estimates.append(est)
            d = calc.to_dict(est)
            print(f"Frame {i+1:3d}: seeing={d['seeing_arcsec']:.3f}\" "
                  f"r0={d['r0_m']:.4f}m "
                  f"FWHM_med={d['fwhm_median_arcsec']:.2f}\" "
                  f"quality={d['quality']} "
                  f"valid_frac={d['valid_fraction']:.2f}")

    print(f"\nTotal estimates produced: {len(estimates)}")
    print(f"History length: {len(calc.get_history())}")

    if estimates:
        final = estimates[-1]
        print(f"\nFinal centroid seeing: {final.seeing_arcsec:.3f} arcsec")
        if 1.0 <= final.seeing_arcsec <= 4.0:
            print("PASS - seeing in plausible range [1.0, 4.0] arcsec")
        else:
            print(f"FAIL - seeing {final.seeing_arcsec:.3f} outside "
                  "plausible range [1.0, 4.0] arcsec")
    else:
        print("FAIL - no estimates produced")
