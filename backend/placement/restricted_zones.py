"""Configurable table geometry for placement validation.

The physical table is 8 ft x 4 ft, split into STATION A (camera-left) and
STATION B (camera-right) by a centre support bar. Until camera calibration
exists, geometry is expressed as FRACTIONS of the frame (0-1) so the same
config works for the 1280px preview frame, 1080p and 4K captures. Replace
`frac_polygon` with calibrated table coordinates + a homography later
without touching the validator: `resolve_zones()` is the only place that
turns config into pixels.

    restricted_zones = [
        {"name": "center_support_bar",
         "frac_polygon": [[x, y], ...],   # fractions of frame width/height
         "margin_px": 15},                # keep-out distance at REFERENCE_WIDTH_PX
    ]
"""
import numpy as np

# Station camera, 2026-09-21 (measured on TBL-20260918-0102, 1920x1080): the
# vertical support rail spans x = 0.603..0.628 of the frame, full height.
# Re-measure if the camera is ever moved (same rule as SEGMENT_ROI).
DEFAULT_RESTRICTED_ZONES = [
    {
        "name": "center_support_bar",
        "label": "CENTER BAR",
        "frac_polygon": [[0.603, 0.0], [0.628, 0.0], [0.628, 1.0], [0.603, 1.0]],
        "margin_px": None,          # None -> PlacementConfig.center_bar_margin_px
    },
]

# Station split: objects whose centroid is left of this fraction are STATION A,
# right of it STATION B. Defaults to the bar's centre line.
DEFAULT_STATION_SPLIT_X = 0.6155

# Pixel thresholds are specified at this frame width and scaled linearly to
# whatever frame is validated, so "15px" means the same physical gap on a
# 1280px preview and a 1920px capture.
REFERENCE_WIDTH_PX = 1280


def resolve_zones(zones, frame_shape, default_margin_px, px_scale):
    """Turn fractional zone config into pixel polygons for this frame.
    Returns [{"name", "label", "polygon": (N,2) float32, "margin_px": float}]."""
    h, w = frame_shape[0], frame_shape[1]
    out = []
    for z in zones:
        poly = np.array([[fx * w, fy * h] for fx, fy in z["frac_polygon"]],
                        dtype=np.float32)
        margin = z.get("margin_px")
        margin = default_margin_px if margin is None else margin
        out.append({"name": z["name"], "label": z.get("label", z["name"]),
                    "polygon": poly, "margin_px": float(margin) * px_scale})
    return out


def station_for(centroid_x, frame_width, split_x_frac):
    return "A" if centroid_x < split_x_frac * frame_width else "B"
