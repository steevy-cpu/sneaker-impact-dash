"""
Live Placement Validation (2026-09-21).

Pure geometry over the detections the capture page already gets from the
shared SAM3 preview worker: is every shoe clear of the centre support bar,
and clear of its unrelated neighbours? Returns structured data only -- the
capture page draws the green / red / yellow overlay, this package never
touches UI.

    detections -> restricted-zone test -> neighbour-distance test
               -> placement status -> (UI overlay, drawn by capture.js)

Modules:
  geometry_utils        bbox/polygon distance + intersection helpers (numpy)
  restricted_zones      configurable table geometry (centre bar, station split)
  placement_validator   validate(detections, frame_shape) -> result dict
"""
from .placement_validator import PlacementConfig, validate  # noqa: F401
