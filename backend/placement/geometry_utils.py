"""Geometry helpers for placement validation. Pixel space, numpy only.

Boxes are (x1, y1, x2, y2); polygons are (N, 2) arrays in the same frame.
Designed for a live preview tick: box work is one vectorised pass over all
pairs, and polygon work (the expensive part) runs only where boxes already
touch, on polygons decimated to MAX_POLY_PTS points (SAM3 masks carry
120-220 vertices; ~2px accuracy is plenty for "do these masks touch?").
"""
import numpy as np

MAX_POLY_PTS = 48


def pairwise_box_iou_gap(boxes):
    """Vectorised (n,n) bbox IoU and edge-to-edge gap matrices."""
    b = np.asarray(boxes, dtype=np.float64).reshape(-1, 4)
    x1, y1, x2, y2 = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    ix1 = np.maximum(x1[:, None], x1[None]); iy1 = np.maximum(y1[:, None], y1[None])
    ix2 = np.minimum(x2[:, None], x2[None]); iy2 = np.minimum(y2[:, None], y2[None])
    iw = np.clip(ix2 - ix1, 0, None); ih = np.clip(iy2 - iy1, 0, None)
    inter = iw * ih
    area = (x2 - x1) * (y2 - y1)
    union = area[:, None] + area[None] - inter
    iou = np.where(union > 0, inter / np.where(union > 0, union, 1), 0.0)
    dx = np.clip(ix1 - ix2, 0, None); dy = np.clip(iy1 - iy2, 0, None)
    gap = np.hypot(dx, dy)
    np.fill_diagonal(iou, 0.0); np.fill_diagonal(gap, np.inf)
    return iou, gap


def bbox_iou(a, b):
    iou, _ = pairwise_box_iou_gap([a, b])
    return float(iou[0, 1])


def bbox_gap(a, b):
    _, gap = pairwise_box_iou_gap([a, b])
    return float(gap[0, 1])


def bbox_to_polygon(b):
    x1, y1, x2, y2 = b
    return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float64)


def bbox_centroid(b):
    return [float((b[0] + b[2]) / 2.0), float((b[1] + b[3]) / 2.0)]


def bbox_gap_to_polygon_bbox(box, poly):
    """Cheap pre-test: gap between a box and a polygon's bounding box."""
    p = np.asarray(poly, dtype=np.float64)
    pb = (p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max())
    return bbox_gap(box, pb)


def decimate(poly, max_pts=MAX_POLY_PTS):
    p = np.asarray(poly, dtype=np.float64)
    if len(p) <= max_pts:
        return p
    idx = np.linspace(0, len(p) - 1, max_pts, dtype=int)
    return p[idx]


def _points_to_segments_dist(points, poly):
    """Min distance from each point to the closed polyline `poly`: (P,) array."""
    p = np.asarray(points, dtype=np.float64)
    a = np.asarray(poly, dtype=np.float64)
    b = np.roll(a, -1, axis=0)
    ab = b - a
    ap = p[:, None, :] - a[None, :, :]
    denom = np.einsum("ij,ij->i", ab, ab)
    t = np.einsum("pij,ij->pi", ap, ab) / np.where(denom == 0, 1, denom)[None]
    t = np.clip(t, 0.0, 1.0)
    diff = ap - t[..., None] * ab[None]
    return np.sqrt(np.einsum("pij,pij->pi", diff, diff)).min(axis=1)


def point_in_polygon(pt, poly):
    """Even-odd rule."""
    x, y = float(pt[0]), float(pt[1])
    a = np.asarray(poly, dtype=np.float64)
    b = np.roll(a, -1, axis=0)
    cond = (a[:, 1] > y) != (b[:, 1] > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xs = a[:, 0] + (y - a[:, 1]) * (b[:, 0] - a[:, 0]) / (b[:, 1] - a[:, 1])
    return bool(np.count_nonzero(cond & (x < xs)) % 2)


def polygon_distance(p, q):
    """Min boundary-to-boundary distance (0 when boundaries touch/cross)."""
    return float(min(_points_to_segments_dist(p, q).min(),
                     _points_to_segments_dist(q, p).min()))


def polygons_intersect(p, q):
    """Vertex containment either way, or boundaries touching/crossing."""
    if point_in_polygon(p[0], q) or point_in_polygon(q[0], p):
        return True
    return polygon_distance(p, q) <= 0.0


def shape_distance(p, q):
    """Boundary distance between two shapes; 0 when one contains the other
    or their boundaries meet. ONE distance pass + two cheap containment checks."""
    d = polygon_distance(p, q)
    if d <= 0.0:
        return 0.0
    if point_in_polygon(p[0], q) or point_in_polygon(q[0], p):
        return 0.0
    return d
