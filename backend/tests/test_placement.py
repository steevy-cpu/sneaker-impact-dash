"""Placement-validation tests (stdlib unittest -- the dash venv has no pytest).

Run:  venv/bin/python3 -m unittest backend.tests.test_placement -v

Frame is 1280x720 (the preview size). The centre bar zone defaults to
x = 0.603..0.628 -> px 772..804; the station split is x = 788.
"""
import unittest

from backend.placement import PlacementConfig, validate

FRAME = (720, 1280)
# Explicit thresholds so the geometry cases are unambiguous; the station
# defaults (gap 0 / overlap 0.15) are exercised in the "real layout" tests.
CFG = PlacementConfig(min_object_gap_px=12, center_bar_margin_px=15,
                      overlap_threshold=0.05, allow_one_close_neighbor=True,
                      hold_frames=1)
DEFAULT = PlacementConfig()


def box(x1, y1, x2, y2, **kw):
    d = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "score": 0.9, "label": "shoe"}
    d.update(kw)
    return d


def statuses(res):
    return [o["placement_status"] for o in res["objects"]]


class PlacementTests(unittest.TestCase):

    def test_01_one_valid_shoe(self):
        res = validate([box(100, 100, 300, 200)], FRAME, CFG)
        self.assertEqual(statuses(res), ["ready"])
        self.assertEqual(res["table"], {"ready": 1, "total": 1, "issues": 0, "ready_to_scan": True})
        self.assertTrue(res["table_ready"])
        self.assertEqual(res["objects"][0]["station"], "A")

    def test_02_multiple_correctly_separated(self):
        dets = [box(50, 50, 250, 150), box(50, 250, 250, 350), box(300, 50, 500, 150)]
        res = validate(dets, FRAME, CFG)
        self.assertEqual(statuses(res), ["ready"] * 3)
        self.assertEqual(res["table"]["ready"], 3)

    def test_03_two_objects_too_close(self):
        # A mate pair is ALLOWED one close neighbour; add a third shoe crowding
        # both -> the crowded ones go red. Gaps of 5px < 12px threshold.
        dets = [box(100, 100, 300, 200), box(100, 205, 300, 305), box(100, 310, 300, 410)]
        res = validate(dets, FRAME, CFG)
        # middle shoe has two close neighbours -> too_close; outer ones have one each -> ready
        self.assertEqual(statuses(res), ["ready", "too_close", "ready"])
        self.assertIn("TOO CLOSE", res["objects"][1]["warnings"])
        # with pairs not allowed, every close shoe is flagged
        strict = PlacementConfig(**{**CFG.__dict__, "allow_one_close_neighbor": False})
        self.assertEqual(statuses(validate(dets[:2], FRAME, strict)), ["too_close", "too_close"])

    def test_04_two_overlapping_objects(self):
        dets = [box(100, 100, 300, 200), box(250, 150, 450, 250)]   # IoU well over 0.05
        res = validate(dets, FRAME, CFG)
        self.assertEqual(statuses(res), ["too_close", "too_close"])
        self.assertEqual(res["objects"][0]["distance_to_nearest_object"], 0.0)

    def test_05_shoe_inside_center_bar(self):
        res = validate([box(760, 300, 820, 420)], FRAME, CFG)      # straddles x 772..804
        self.assertEqual(statuses(res), ["center_bar"])
        self.assertEqual(res["objects"][0]["placement_warning"], "CENTER BAR")

    def test_06_shoe_near_center_bar_margin(self):
        # zone left edge at 772px; margin 15px -> anything within 757..772 is yellow
        near = validate([box(600, 300, 762, 420)], FRAME, CFG)     # 10px from the bar
        clear = validate([box(600, 300, 740, 420)], FRAME, CFG)    # 32px from the bar
        self.assertEqual(statuses(near), ["center_bar"])
        self.assertEqual(statuses(clear), ["ready"])

    def test_07_valid_shoes_both_stations(self):
        dets = [box(100, 100, 300, 200), box(900, 100, 1100, 200)]
        res = validate(dets, FRAME, CFG)
        self.assertEqual([o["station"] for o in res["objects"]], ["A", "B"])
        self.assertTrue(res["station_a_ready"] and res["station_b_ready"] and res["table_ready"])
        self.assertEqual(res["stations"]["A"]["total"], 1)
        self.assertEqual(res["stations"]["B"]["total"], 1)

    def test_08_station_a_invalid_b_still_valid(self):
        dets = [box(100, 100, 300, 200), box(250, 150, 450, 250),   # overlap in A
                box(900, 100, 1100, 200), box(900, 300, 1100, 400)] # clean in B
        res = validate(dets, FRAME, CFG)
        self.assertFalse(res["station_a_ready"])
        self.assertTrue(res["station_b_ready"])
        self.assertFalse(res["table_ready"])
        self.assertEqual(res["stations"]["A"]["issues"], 2)
        self.assertEqual(res["stations"]["B"], {"ready": 2, "total": 2, "issues": 0, "ready_to_scan": True})

    def test_09_no_detected_objects(self):
        res = validate([], FRAME, CFG)
        self.assertEqual(res["objects"], [])
        self.assertEqual(res["table"], {"ready": 0, "total": 0, "issues": 0, "ready_to_scan": True})
        self.assertFalse(res["stale"])

    def test_10_temporary_missed_detection(self):
        first = validate([box(100, 100, 300, 200)], FRAME, CFG)
        held = validate([], FRAME, CFG, previous=first)            # one empty frame -> held
        self.assertTrue(held["stale"])
        self.assertEqual(held["stale_frames"], 1)
        self.assertEqual(len(held["objects"]), 1)
        gone = validate([], FRAME, CFG, previous=held)             # second empty -> really empty
        self.assertEqual(gone["objects"], [])
        self.assertFalse(gone["stale"])

    # --- extras that guard real-world behaviour ---------------------------

    def test_polygon_distance_refines_box_gap(self):
        # Boxes overlap (IoU tiny, < threshold) but the diagonal masks don't touch:
        # mask-boundary distance keeps both shoes ready.
        a = box(100, 100, 300, 300, polygon=[[100, 100], [140, 100], [300, 260], [300, 300], [260, 300], [100, 140]])
        b = box(280, 280, 480, 480, polygon=[[480, 480], [440, 480], [280, 320], [280, 280], [320, 280], [480, 440]])
        res = validate([a, b], FRAME, CFG)
        self.assertTrue(all(o["mask_available"] for o in res["objects"]))
        self.assertEqual(statuses(res), ["ready", "ready"])

    def test_group_members_never_too_close(self):
        dets = [box(100, 100, 300, 200, group=7), box(250, 150, 450, 250, group=7)]
        res = validate(dets, FRAME, CFG)
        self.assertEqual(statuses(res), ["ready", "ready"])

    def test_thresholds_scale_with_frame_width(self):
        # Same layout at 2x resolution: a 20px gap at 2560 wide == 10px at 1280 -> too close
        dets = [box(200, 200, 600, 400), box(200, 420, 600, 620), box(200, 640, 600, 840)]
        res = validate(dets, (1440, 2560), CFG)
        self.assertEqual(statuses(res), ["ready", "too_close", "ready"])
        self.assertEqual(res["thresholds_px"]["min_object_gap"], 24.0)


    def test_real_layout_touching_pairs_are_ready(self):
        # The crew's actual layout: rows of pairs 0-4px apart (measured on
        # TBL-20260909-0129). With station defaults nothing here is an error.
        dets = [box(272, 101, 328, 263), box(283, 269, 343, 429),   # a pair, 6px apart
                box(346, 116, 406, 273), box(353, 280, 407, 439),   # next pair, 3px from the first
                box(410, 122, 461, 267), box(421, 278, 473, 426)]
        res = validate(dets, FRAME, DEFAULT)
        self.assertEqual(statuses(res), ["ready"] * 6)
        self.assertTrue(res["table_ready"])

    def test_real_layout_stacked_pair_is_too_close(self):
        # Stacked shoes: bbox IoU ~0.46 on the real frame >> 0.15 default.
        dets = [box(544, 257, 602, 402), box(560, 300, 620, 440), box(100, 100, 160, 240)]
        res = validate(dets, FRAME, DEFAULT)
        self.assertEqual(statuses(res), ["too_close", "too_close", "ready"])

    def test_validator_is_fast_on_a_crowded_frame(self):
        import time
        import numpy as np
        rng = np.random.default_rng(0)
        dets = []
        for _ in range(40):
            x, y = int(rng.integers(0, 1180)), int(rng.integers(0, 600))
            t = np.linspace(0, 2 * np.pi, 160, endpoint=False)
            poly = [[float(x + 50 + 50 * np.cos(a)), float(y + 60 + 60 * np.sin(a))] for a in t]
            dets.append(box(x, y, x + 100, y + 120, polygon=poly))
        t0 = time.perf_counter()
        for _ in range(5): validate(dets, FRAME, DEFAULT)
        ms = (time.perf_counter() - t0) / 5 * 1000
        self.assertLess(ms, 150, f"validate took {ms:.0f} ms on 40 masked objects")


if __name__ == "__main__":
    unittest.main()
