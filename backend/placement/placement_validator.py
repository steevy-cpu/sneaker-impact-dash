"""placement_validator -- structured placement status for detected shoes.

    result = validate(detections=[...], frame_shape=(h, w))

`detections` are the preview worker's boxes: dicts with x1,y1,x2,y2 (frame
pixels), optional score/label/polygon ([[x, y], ...] in the same frame).
Returns plain dicts/lists (JSON-safe). NEVER touches UI.

Status per object, in priority order:
  YELLOW "center_bar"  the object (mask if available, else box) is within
                       margin of a restricted zone, or intersects it;
  RED    "too_close"   OVERLAPS another object (bbox IoU >= overlap_threshold,
                       i.e. stacked/piled -- measured on the real station:
                       stacked shoes ~0.46, touching-but-separate <= 0.03), or
                       is within min_gap of more unrelated neighbours than
                       allowed (allow_one_close_neighbor: mates sit together);
  GREEN  "ready"       otherwise.

Calibration note (2026-09-21, real capture frames): this crew lays pairs AND
neighbouring pairs 0-10px apart, so distance alone cannot tell a mate from a
neighbour -- that is the appearance-based pairing stage's job. Hence the
defaults: min_gap 0 (only touching counts as "close") and overlap 0.15.

Distances use mask boundaries when both objects carry polygons, else box
edges. Work is box-first (one vectorised pass); polygon refinement runs only
for pairs whose BOXES touch, on decimated polygons.

Bundles: the pipeline detects SINGLE shoes (SAM3 "shoe"), and mates are laid
side by side, so one close neighbour is treated as the mate rather than an
error. Objects carry a `group` field (None today) reserved for a later bundle
recogniser: objects sharing a group are never "too close" to each other.
"""
from dataclasses import dataclass, field, asdict

import numpy as np

from . import geometry_utils as g
from . import restricted_zones as rz


@dataclass
class PlacementConfig:
    """All thresholds in pixels at `reference_width_px`; scaled per frame."""
    min_object_gap_px: float = 0.0            # 0 = only touching counts as close
    center_bar_margin_px: float = 15.0
    overlap_threshold: float = 0.15           # bbox IoU; stacked shoes >> this
    allow_one_close_neighbor: bool = True
    hold_frames: int = 1
    reference_width_px: int = rz.REFERENCE_WIDTH_PX
    station_split_x: float = rz.DEFAULT_STATION_SPLIT_X
    restricted_zones: list = field(default_factory=lambda: list(rz.DEFAULT_RESTRICTED_ZONES))

    @classmethod
    def from_app_config(cls):
        """Build from backend.config PLACEMENT_* knobs (env-overridable)."""
        from backend import config as c
        return cls(
            min_object_gap_px=c.PLACEMENT_MIN_OBJECT_GAP_PX,
            center_bar_margin_px=c.PLACEMENT_CENTER_BAR_MARGIN_PX,
            overlap_threshold=c.PLACEMENT_OVERLAP_THRESHOLD,
            allow_one_close_neighbor=c.PLACEMENT_ALLOW_ONE_CLOSE_NEIGHBOR,
            hold_frames=c.PLACEMENT_HOLD_FRAMES,
        )


def _shape(det):
    """Polygon (N,2) for distance tests: the (decimated) mask if present,
    else the box."""
    poly = det.get("polygon")
    if poly and len(poly) >= 3:
        return g.decimate(poly), True
    return g.bbox_to_polygon(_bbox(det)), False


def _bbox(det):
    return (float(det["x1"]), float(det["y1"]), float(det["x2"]), float(det["y2"]))


def _summary(objs):
    ready = sum(1 for o in objs if o["placement_status"] == "ready")
    total = len(objs)
    return {"ready": ready, "total": total, "issues": total - ready,
            "ready_to_scan": total - ready == 0}


def validate(detections, frame_shape, config=None, previous=None):
    """Validate one frame of detections.

    previous: the last result dict, for missed-detection hold -- when this
    frame has NO detections but `previous` had objects and hasn't been held
    longer than config.hold_frames, the previous result is returned again
    with stale=True / stale_frames+1 so the overlay doesn't blink out.
    """
    cfg = config or PlacementConfig()
    h, w = int(frame_shape[0]), int(frame_shape[1])
    px = w / float(cfg.reference_width_px)          # threshold scale for this frame
    gap_px = cfg.min_object_gap_px * px
    zones = rz.resolve_zones(cfg.restricted_zones, (h, w), cfg.center_bar_margin_px, px)

    dets = list(detections or [])
    if not dets and previous and previous.get("objects"):
        stale = int(previous.get("stale_frames", 0)) + 1
        if stale <= cfg.hold_frames:
            held = dict(previous)
            held["stale"] = True
            held["stale_frames"] = stale
            return held

    shapes = [_shape(d) for d in dets]
    boxes = [_bbox(d) for d in dets]
    n = len(dets)

    # ---- pairwise geometry: one vectorised box pass; masks only where boxes touch
    if n:
        iou, dist = g.pairwise_box_iou_gap(boxes)
        dist = dist.astype(np.float64)
        refine_within = gap_px + 1.0            # boxes closer than this may still have separated masks
        for i in range(n):
            for j in range(i + 1, n):
                if dist[i, j] <= refine_within and (shapes[i][1] or shapes[j][1]) \
                        and iou[i, j] < cfg.overlap_threshold:
                    d = g.shape_distance(shapes[i][0], shapes[j][0])
                    dist[i, j] = dist[j, i] = d
    else:
        iou = np.zeros((0, 0)); dist = np.zeros((0, 0))

    objects = []
    for i, d in enumerate(dets):
        b = boxes[i]
        c = g.bbox_centroid(b)
        group = d.get("group")
        warnings = []
        status = "ready"

        # 1. restricted zones (YELLOW) -- box-vs-zone-bbox pre-test skips the
        #    polygon work for every object nowhere near a zone.
        for z in zones:
            if g.bbox_gap_to_polygon_bbox(b, z["polygon"]) > z["margin_px"]:
                continue
            zd = g.shape_distance(shapes[i][0], z["polygon"])
            if zd <= z["margin_px"]:
                status = "center_bar"
                warnings.append(z["label"])
                break

        # 2. neighbours (RED) -- unrelated = not in the same declared group
        close, overlap = [], []
        for j in range(n):
            if j == i or (group is not None and dets[j].get("group") == group):
                continue
            if iou[i, j] >= cfg.overlap_threshold or dist[i, j] <= 0.0:
                overlap.append(j)
            elif dist[i, j] <= gap_px:
                close.append(j)
        allowed = 1 if cfg.allow_one_close_neighbor else 0
        if status == "ready" and (overlap or len(close) > allowed):
            status = "too_close"
            warnings.append("TOO CLOSE")

        nearest = None
        nearest_d = None
        if n > 1:
            j = int(np.argmin(dist[i]))
            nearest, nearest_d = j, float(dist[i, j])

        objects.append({
            "object_id": i,
            "station": rz.station_for(c[0], w, cfg.station_split_x),
            "centroid": [round(c[0], 1), round(c[1], 1)],
            "bbox": [int(v) for v in b],
            "width": int(b[2] - b[0]),
            "height": int(b[3] - b[1]),
            "mask_available": bool(shapes[i][1]),
            "nearest_object": nearest,
            "distance_to_nearest_object": None if nearest_d is None else round(nearest_d, 1),
            "group": group,
            "placement_status": status,
            "placement_warning": warnings[0] if warnings else None,
            "warnings": warnings,
        })

    by_station = {"A": [o for o in objects if o["station"] == "A"],
                  "B": [o for o in objects if o["station"] == "B"]}
    stations = {k: _summary(v) for k, v in by_station.items()}
    table = _summary(objects)
    return {
        "objects": objects,
        "stations": stations,
        "table": table,
        "table_ready": table["ready_to_scan"],
        "station_a_ready": stations["A"]["ready_to_scan"],
        "station_b_ready": stations["B"]["ready_to_scan"],
        "zones": [{"name": z["name"], "label": z["label"],
                   "polygon": z["polygon"].astype(int).tolist(),
                   "margin_px": round(z["margin_px"], 1)} for z in zones],
        "station_split_x": int(cfg.station_split_x * w),
        "frame_shape": [h, w],
        "thresholds_px": {"min_object_gap": round(gap_px, 1),
                          "center_bar_margin": round(cfg.center_bar_margin_px * px, 1),
                          "overlap_iou": cfg.overlap_threshold},
        "stale": False,
        "stale_frames": 0,
    }
