"""
Star detection and profile measurement for QHY camera.

Pure NumPy — no SciPy required.  All frames are 2D uint8 NumPy arrays.
Coordinate convention: x = column, y = row (matches canvas/image convention).
"""

import numpy as np


# ---------------------------------------------------------------------------
# Star detection
# ---------------------------------------------------------------------------

def detect_stars(frame, threshold_sigma=5.0, min_separation=8, max_stars=20):
    """
    Detect point sources in a 2D uint8 array.

    Algorithm:
      1. Robust background + noise from sigma-clipped median/std.
      2. Find all pixels above threshold, sorted brightest-first.
      3. Greedy: take each unmasked peak as a star centre; mask a
         min_separation-radius region so nearby pixels are skipped.
      4. Flux-weighted centroid refinement within an 8-px aperture.
      5. FWHM estimated from the radial intensity profile.

    Returns list of dicts (sorted by SNR desc):
        x, y          – sub-pixel centroid in frame pixel coordinates
        peak          – peak pixel value (ADU)
        fwhm          – estimated FWHM in pixels
        snr           – (peak – background) / noise
    """
    img = frame.astype(np.float32)
    h, w = img.shape

    # Robust background + noise
    bg = float(np.median(img))
    rough_sigma = float(img.std())
    bg_pix = img.ravel()[np.abs(img.ravel() - bg) < 3.0 * rough_sigma]
    noise = float(bg_pix.std()) if len(bg_pix) > 50 else rough_sigma
    noise = max(noise, 1.0)

    threshold = bg + threshold_sigma * noise

    ys, xs = np.where(img > threshold)
    if len(xs) == 0:
        return []

    # Sort by brightness descending
    vals = img[ys, xs]
    order = np.argsort(-vals)
    ys, xs = ys[order], xs[order]

    used = np.zeros((h, w), dtype=bool)
    stars = []

    for cy_i, cx_i in zip(ys.tolist(), xs.tolist()):
        if used[cy_i, cx_i]:
            continue

        # Mask separation zone
        r = min_separation
        used[max(0, cy_i - r):min(h, cy_i + r + 1),
             max(0, cx_i - r):min(w, cx_i + r + 1)] = True

        # Flux-weighted centroid
        ap = 8
        y0, y1 = max(0, cy_i - ap), min(h, cy_i + ap + 1)
        x0, x1 = max(0, cx_i - ap), min(w, cx_i + ap + 1)
        sub = np.maximum(img[y0:y1, x0:x1] - bg, 0)
        total = sub.sum()
        if total <= 0:
            continue
        sy_g, sx_g = np.mgrid[y0:y1, x0:x1]
        cx_f = float((sx_g * sub).sum() / total)
        cy_f = float((sy_g * sub).sum() / total)

        peak = float(img[cy_i, cx_i])
        snr = (peak - bg) / noise
        fwhm = _fwhm_from_radial(img, cx_f, cy_f, bg)

        stars.append({
            'x':    round(cx_f, 1),
            'y':    round(cy_f, 1),
            'peak': int(peak),
            'fwhm': round(fwhm, 1),
            'snr':  round(snr, 1),
        })

        if len(stars) >= max_stars:
            break

    stars.sort(key=lambda s: -s['snr'])
    return stars


def _fwhm_from_radial(img, cx, cy, bg, radius=18):
    """
    Estimate FWHM by locating the first half-maximum crossing
    in a radially-binned (0.5 px bins) background-subtracted profile.
    """
    h, w = img.shape
    pad = int(radius) + 1
    y0, y1 = max(0, int(cy) - pad), min(h, int(cy) + pad + 1)
    x0, x1 = max(0, int(cx) - pad), min(w, int(cx) + pad + 1)
    if y1 <= y0 or x1 <= x0:
        return 3.0

    sy, sx = np.mgrid[y0:y1, x0:x1]
    r = np.sqrt((sx - cx) ** 2 + (sy - cy) ** 2).ravel()
    v = np.maximum(img[y0:y1, x0:x1].astype(np.float32) - bg, 0).ravel()

    BIN = 0.5
    bins = np.arange(0, radius + BIN, BIN)
    profile = []
    for i in range(len(bins) - 1):
        m = (r >= bins[i]) & (r < bins[i + 1])
        profile.append(float(v[m].mean()) if m.sum() > 0 else 0.0)

    if not profile or max(profile) <= 0:
        return 3.0

    peak_v = max(profile)
    half_max = peak_v / 2.0

    for i in range(1, len(profile)):
        if profile[i] < half_max:
            prev, curr = profile[i - 1], profile[i]
            frac = (prev - half_max) / max(prev - curr, 1e-9)
            r_half = bins[i - 1] + frac * BIN
            return max(1.0, round(2.0 * r_half, 1))

    # Never crossed half-max — likely saturated flat-top
    return float(2 * radius)


# ---------------------------------------------------------------------------
# Star measurement (called every frame for the selected star)
# ---------------------------------------------------------------------------

def measure_star(frame, cx, cy, aperture=None):
    """
    Detailed measurement of a star at sub-pixel position (cx, cy).

    aperture defaults to max(10, min(25, fwhm * 2.5)) computed internally.

    Returns dict:
        x, y              – refined sub-pixel centroid (flux-weighted)
        fwhm              – FWHM in pixels
        peak              – peak ADU
        background        – local sky background ADU
        snr               – (peak – background) / sky_noise
        saturation_frac   – fraction of aperture pixels >= 250
        radial_profile    – list of [r_centre, normalised_brightness]
        histogram         – {'bins': [...], 'counts': [...]}
    Returns None if the position is too close to the frame edge.
    """
    img = frame.astype(np.float32)
    h, w = img.shape
    cx, cy = float(cx), float(cy)

    # Flux-weighted centroid refinement (8-px aperture around input position)
    _ap = 8
    _y0, _y1 = max(0, int(cy) - _ap), min(h, int(cy) + _ap + 1)
    _x0, _x1 = max(0, int(cx) - _ap), min(w, int(cx) + _ap + 1)
    bg_rough = float(np.median(img))
    _sub = np.maximum(img[_y0:_y1, _x0:_x1] - bg_rough, 0)
    _total = _sub.sum()
    if _total > 0:
        _sy, _sx = np.mgrid[_y0:_y1, _x0:_x1]
        cx = float((_sx * _sub).sum() / _total)
        cy = float((_sy * _sub).sum() / _total)

    # Quick FWHM estimate for adaptive aperture
    fwhm = _fwhm_from_radial(img, cx, cy, bg_rough)

    if aperture is None:
        aperture = max(10, min(25, int(fwhm * 2.5)))

    r_in  = aperture + 3
    r_out = aperture + 12
    pad   = r_out + 2

    y0, y1 = max(0, int(cy) - pad), min(h, int(cy) + pad + 1)
    x0, x1 = max(0, int(cx) - pad), min(w, int(cx) + pad + 1)
    if y1 <= y0 or x1 <= x0:
        return None

    sy, sx = np.mgrid[y0:y1, x0:x1]
    r_all = np.sqrt((sx - cx) ** 2 + (sy - cy) ** 2)

    # Annulus background
    ann = (r_all >= r_in) & (r_all < r_out)
    if ann.sum() > 20:
        ann_pix = img[sy[ann], sx[ann]]
        bg    = float(np.median(ann_pix))
        noise = max(float(ann_pix.std()), 1.0)
    else:
        flat = img.ravel()
        bg    = float(np.median(flat))
        noise = max(float(flat.std()), 1.0)

    # Aperture photometry
    ap_mask = r_all < aperture
    if ap_mask.sum() == 0:
        return None

    ap_pix = frame[sy[ap_mask], sx[ap_mask]]   # original uint8 values
    peak   = int(np.max(ap_pix))
    sat_frac = float(np.sum(ap_pix >= 250)) / len(ap_pix)
    snr = (peak - bg) / noise

    # Radial profile — binned, bg-subtracted, normalised to peak
    BIN     = 0.5
    bins    = np.arange(0, aperture + BIN, BIN)
    r_flat  = r_all.ravel()
    v_flat  = np.maximum(img[sy, sx].ravel() - bg, 0)
    profile = []
    for i in range(len(bins) - 1):
        m = (r_flat >= bins[i]) & (r_flat < bins[i + 1])
        val = float(v_flat[m].mean()) if m.sum() > 0 else 0.0
        profile.append([round(float(bins[i] + BIN / 2), 2), round(val, 2)])

    peak_p = max((p[1] for p in profile), default=1.0)
    if peak_p > 0:
        profile = [[p[0], round(p[1] / peak_p, 4)] for p in profile]

    # Pixel histogram (aperture pixels, 32 bins 0-255)
    hist_c, hist_e = np.histogram(ap_pix, bins=32, range=(0, 256))
    histogram = {
        'bins':   [int(e) for e in hist_e[:-1]],
        'counts': [int(c) for c in hist_c],
    }

    return {
        'x':               round(cx, 2),
        'y':               round(cy, 2),
        'fwhm':            round(fwhm, 2),
        'peak':            peak,
        'background':      round(bg, 1),
        'snr':             round(snr, 1),
        'saturation_frac': round(sat_frac, 3),
        'radial_profile':  profile,
        'histogram':       histogram,
    }
