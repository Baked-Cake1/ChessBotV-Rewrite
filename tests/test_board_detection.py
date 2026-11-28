"""Board-detection model: keypoint lattice fitting and geometry.

These tests exercise the NumPy-only parts of ``board_detection.py`` - the
homography solver, the robust 9x9 lattice fit, heatmap targets, the
perspective warp and the dataset/label helpers. The torch model itself is
only built when torch is importable.
"""

import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def _load_board_detection():
    spec = importlib.util.spec_from_file_location(
        "board_detection_under_test", ROOT / "board_detection.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IDEAL = None


class BoardDetectionGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bd = _load_board_detection()

    def setUp(self):
        self.rng = np.random.default_rng(7)
        self.ideal = self.bd.IDEAL_LATTICE
        # A rotated, perspective-distorted board, like a browser zoom or a
        # slightly tilted screenshot.
        self.truth_homography = np.array(
            [
                [420.0, 60.0, 120.0],
                [40.0, 400.0, 90.0],
                [0.0004, 0.0002, 1.0],
            ]
        )
        self.truth = self.bd.apply_homography(
            self.truth_homography, self.ideal
        )

    # -- homography solver -------------------------------------------------
    def test_homography_solver_is_exact(self):
        sample = np.array([0, 4, 40, 80])
        solved = self.bd._solve_homography(
            self.ideal[sample], self.truth[sample]
        )
        self.assertIsNotNone(solved)
        projected = self.bd.apply_homography(solved, self.ideal[sample])
        np.testing.assert_allclose(projected, self.truth[sample], atol=0.01)

    def test_homography_round_trip_on_all_points(self):
        solved = self.bd._solve_homography(self.ideal, self.truth)
        self.assertIsNotNone(solved)
        projected = self.bd.apply_homography(solved, self.ideal)
        self.assertLess(float(np.abs(projected - self.truth).max()), 0.05)

    def test_homography_needs_four_points(self):
        self.assertIsNone(
            self.bd._solve_homography(self.ideal[:3], self.truth[:3])
        )

    # -- robust lattice fit -------------------------------------------------
    def _fit(self, points, visibility=None):
        return self.bd.lattice_fit(points, visibility=visibility)

    def test_clean_points_fit_exactly(self):
        fit = self._fit(self.truth)
        self.assertIsNotNone(fit)
        self.assertEqual(fit["inlier_count"], 81)
        self.assertAlmostEqual(fit["confidence"], 1.0, places=3)
        self.assertLess(float(np.abs(fit["points"] - self.truth).max()), 0.01)

    def test_outliers_are_rejected(self):
        outliers = np.array([5, 13, 27, 33, 44, 52])
        points = self.truth.copy()
        points[outliers] += self.rng.normal(0, 45, (len(outliers), 2))
        fit = self._fit(points)
        self.assertIsNotNone(fit)
        self.assertGreaterEqual(fit["inlier_count"], 70)
        # Rebuilt lattice still lands on the true board.
        self.assertLess(float(np.abs(fit["points"] - self.truth).max()), 1.5)
        self.assertGreater(fit["confidence"], 0.75)

    def test_gross_outliers_are_rejected(self):
        outliers = np.array([10, 11, 20, 21, 30, 31, 40, 41, 50, 51, 60, 61])
        points = self.truth.copy()
        points[outliers] = self.rng.uniform(0, 800, (len(outliers), 2))
        fit = self._fit(points)
        self.assertIsNotNone(fit)
        self.assertLess(float(np.abs(fit["points"] - self.truth).max()), 1.5)

    def test_missing_points_are_reconstructed(self):
        visible = np.ones(81, dtype=bool)
        visible[self.rng.choice(81, 25, replace=False)] = False
        fit = self._fit(self.truth, visibility=visible)
        self.assertIsNotNone(fit)
        # Every keypoint, including the invisible quarter, is rebuilt.
        self.assertLess(float(np.abs(fit["points"] - self.truth).max()), 0.01)

    def test_small_keypoint_noise_is_smoothed(self):
        points = self.truth + self.rng.normal(0, 1.5, self.truth.shape)
        fit = self._fit(points)
        self.assertIsNotNone(fit)
        self.assertEqual(fit["inlier_count"], 81)
        self.assertLess(float(np.abs(fit["points"] - self.truth).max()), 2.0)

    def test_garbage_collapses_confidence(self):
        garbage = self.rng.uniform(0, 800, (81, 2))
        fit = self._fit(garbage)
        if fit is not None:
            self.assertLess(fit["confidence"], 0.35)

    def test_too_few_points_returns_none(self):
        self.assertIsNone(
            self.bd.lattice_fit(np.zeros((81, 2)), visibility=np.zeros(81))
        )

    # -- lattice / square geometry -----------------------------------------
    def test_keypoint_index_round_trip(self):
        for row in range(9):
            for col in range(9):
                index = self.bd.keypoint_index(row, col)
                self.assertEqual(self.bd.keypoint_row_col(index), (row, col))
        self.assertEqual(self.bd.keypoint_index(0, 0), 0)
        self.assertEqual(self.bd.keypoint_index(8, 8), 80)
        with self.assertRaises(ValueError):
            self.bd.keypoint_index(9, 0)

    def test_corners_and_rect_follow_the_lattice(self):
        corners = self.bd.corners_from_points(self.truth)
        self.assertEqual(len(corners), 4)
        np.testing.assert_allclose(corners[0], self.truth[0], atol=1e-6)
        np.testing.assert_allclose(corners[2], self.truth[80], atol=1e-6)
        rect = self.bd.board_rect_from_points(self.truth)
        self.assertGreater(rect[2], 100)
        self.assertGreater(rect[3], 100)

    def test_square_corners_are_a_perspective_quad(self):
        quad = self.bd.square_corners(self.truth, 0, 0)
        self.assertEqual(quad.shape, (4, 2))
        # The top-left square is not axis-aligned on a rotated board.
        self.assertGreater(abs(quad[1][1] - quad[0][1]), 1.0)

    def test_warp_produces_a_square_board(self):
        image = (
            np.random.default_rng(3).random((650, 800, 3)) * 255
        ).astype(np.uint8)
        warped = self.bd.warp_board_from_points(self.truth, image, 256)
        self.assertIsNotNone(warped)
        self.assertEqual(warped.shape, (256, 256, 3))
        self.assertEqual(warped.dtype, np.uint8)

    # -- heatmaps -----------------------------------------------------------
    def test_heatmaps_round_trip(self):
        normalised = self.truth / np.array([800.0, 650.0])
        heatmaps = self.bd.gaussian_heatmaps(normalised, sigma=2.0)
        self.assertEqual(
            heatmaps.shape, (81, self.bd.KEYPOINT_HEATMAP_SIZE,
                             self.bd.KEYPOINT_HEATMAP_SIZE)
        )
        self.assertLessEqual(float(heatmaps.max()), 1.0)
        points, confidence, visibility = self.bd.heatmaps_to_points(heatmaps)
        self.assertLess(float(np.abs(points - normalised).max()), 0.01)
        self.assertTrue(bool(visibility.all()))

    def test_invisible_keypoints_get_empty_heatmaps(self):
        normalised = self.truth / np.array([800.0, 650.0])
        visibility = np.ones(81)
        visibility[:10] = 0
        heatmaps = self.bd.gaussian_heatmaps(
            normalised, visibility=visibility
        )
        self.assertEqual(float(heatmaps[:10].max()), 0.0)
        self.assertGreater(float(heatmaps[10:].max()), 0.9)
        _, _, predicted = self.bd.heatmaps_to_points(heatmaps)
        self.assertFalse(bool(predicted[:10].any()))

    # -- preprocessing / label helpers -------------------------------------
    def test_preprocess_letterboxes_and_maps_back(self):
        image = np.zeros((360, 640, 3), dtype=np.uint8)
        batch = self.bd.preprocess_screenshot(image)
        size = self.bd.KEYPOINT_INPUT_SIZE
        self.assertEqual(batch.shape, (1, 3, size, size))
        _, metadata = self.bd.prepare_sample_image(Image.new("RGB", (640, 360)))
        self.assertAlmostEqual(metadata["scale"], size / 640)
        points = np.tile([0.5, 0.5], (81, 1))
        mapped = self.bd.map_points_to_image(points, metadata)
        np.testing.assert_allclose(mapped[0], [320.0, 180.0], atol=1.0)

    def test_keypoint_labels_round_trip(self):
        folder = Path(tempfile.mkdtemp())
        try:
            sample = folder / "shot.png"
            Image.new("RGB", (64, 64)).save(sample)
            self.assertIsNone(self.bd.load_keypoint_label(sample))
            self.bd.save_keypoint_label(sample, self.truth)
            label_path = self.bd.sample_label_path(sample)
            self.assertTrue(label_path.is_file())
            payload = json.loads(label_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["points"]), 81)
            loaded = self.bd.load_keypoint_label(sample)
            self.assertIsNotNone(loaded)
            # Labels are stored as whole pixels, so compare against the
            # rounded truth rather than the continuous homography output.
            np.testing.assert_allclose(
                loaded[0], np.rint(self.truth), atol=0.01
            )
            np.testing.assert_array_equal(
                loaded[0], np.rint(loaded[0]), err_msg="labels must be integers"
            )
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)

    def test_collect_board_samples_skips_unlabelled(self):
        folder = Path(tempfile.mkdtemp())
        try:
            dataset = folder / "datasets" / "default"
            samples = (
                dataset / self.bd.BOARD_DETECTION_DIRNAME
                / self.bd.BOARD_DETECTION_SAMPLE_DIRNAME
            )
            samples.mkdir(parents=True)
            labelled = samples / "a.png"
            Image.new("RGB", (64, 64)).save(labelled)
            self.bd.save_keypoint_label(labelled, self.truth)
            Image.new("RGB", (64, 64)).save(samples / "b.png")
            (samples / "notes.txt").write_text("ignored")
            records = self.bd.collect_board_samples(dataset)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["image"].name, "a.png")
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)

    def test_checkpoint_paths_and_model_types(self):
        self.assertEqual(
            self.bd.normalise_model_type("Board detection"),
            self.bd.MODEL_TYPE_BOARD,
        )
        self.assertEqual(
            self.bd.normalise_model_type("pieces"),
            self.bd.MODEL_TYPE_PIECES,
        )
        path = self.bd.board_model_path(Path("/m") / "default.pth")
        self.assertEqual(path.name, "default_board.pth")
        self.assertEqual(
            self.bd.board_sample_dir(Path("/d") / "default").name,
            self.bd.BOARD_DETECTION_DIRNAME,
        )

    def test_annotation_draws_on_a_copy(self):
        image = np.zeros((650, 800, 3), dtype=np.uint8)
        annotated = self.bd.annotate_keypoints(image, self.truth)
        self.assertEqual(annotated.shape, image.shape)
        self.assertEqual(int(image.sum()), 0)  # original untouched
        self.assertGreater(int(annotated.sum()), 0)


class BoardDetectionModelTests(unittest.TestCase):
    """The torch model builds and runs; skipped when torch is absent."""

    @classmethod
    def setUpClass(cls):
        cls.bd = _load_board_detection()
        try:
            import torch  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("torch is not installed")

    def test_model_output_shape(self):
        import torch

        model = self.bd.build_board_detection_model()
        model.eval()
        size = self.bd.KEYPOINT_INPUT_SIZE
        with torch.no_grad():
            output = model(torch.zeros(1, 3, size, size))
        self.assertEqual(
            tuple(output.shape),
            (1, self.bd.KEYPOINT_COUNT,
             self.bd.KEYPOINT_HEATMAP_SIZE, self.bd.KEYPOINT_HEATMAP_SIZE),
        )

    def test_detect_board_with_untrained_model_returns_none_or_dict(self):
        model = self.bd.build_board_detection_model()
        model.eval()
        image = (
            np.random.default_rng(5).random((480, 640, 3)) * 255
        ).astype(np.uint8)
        detection = self.bd.detect_board_with_model(image, model)
        # An untrained net produces noise, so None (no lattice) is the
        # expected outcome; if a lattice is found it must be well formed.
        if detection is not None:
            self.assertEqual(len(detection["points"]), 81)
            self.assertEqual(len(detection["corners"]), 4)


if __name__ == "__main__":
    unittest.main()


class IdealLatticeOrientationTests(unittest.TestCase):
    """The ideal lattice must be (x, y) = (col/8, row/8), never transposed.

    A transposed lattice still fits *some* homography, so every geometric
    test can pass while the four corners come out rotated by 90 degrees.
    These tests pin the orientation down directly.
    """

    @classmethod
    def setUpClass(cls):
        cls.bd = _load_board_detection()

    def test_corners_match_their_row_and_column(self):
        for index, expected in (
            (0, (0.0, 0.0)),      # top-left
            (8, (1.0, 0.0)),      # top-right
            (72, (0.0, 1.0)),     # bottom-left
            (80, (1.0, 1.0)),     # bottom-right
            (40, (0.5, 0.5)),     # centre
        ):
            row, col = self.bd.keypoint_row_col(index)
            self.assertAlmostEqual(
                self.bd.IDEAL_LATTICE[index][0], col / 8.0, places=6
            )
            self.assertAlmostEqual(
                self.bd.IDEAL_LATTICE[index][1], row / 8.0, places=6
            )
            np.testing.assert_allclose(
                self.bd.IDEAL_LATTICE[index], expected, atol=1e-9
            )

    def test_fit_keeps_a_perspective_board_upright(self):
        corners = np.array(
            [[60.0, 50.0], [440.0, 50.0], [470.0, 360.0], [40.0, 370.0]]
        )
        homography = self.bd._solve_homography(
            np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]),
            corners,
        )
        points = self.bd.apply_homography(
            homography, self.bd.IDEAL_LATTICE
        )
        fit = self.bd.lattice_fit(points)
        self.assertIsNotNone(fit)
        np.testing.assert_allclose(fit["points"][0], corners[0], atol=0.5)
        np.testing.assert_allclose(fit["points"][8], corners[1], atol=0.5)
        np.testing.assert_allclose(fit["points"][80], corners[2], atol=0.5)
        np.testing.assert_allclose(fit["points"][72], corners[3], atol=0.5)

    def test_top_right_is_not_the_bottom_left(self):
        """Guard the exact failure mode: index 8 must not sit below index 0."""
        self.assertLess(
            self.bd.IDEAL_LATTICE[8][1],
            self.bd.IDEAL_LATTICE[72][1],
        )
        self.assertGreater(
            self.bd.IDEAL_LATTICE[8][0],
            self.bd.IDEAL_LATTICE[72][0],
        )


class PixelAccuracyTests(unittest.TestCase):
    """Keypoints must land on the pixel they came from, at any resolution.

    The heatmap encoder places a point at ``p * heatmap_size``; the decoder
    used to add half a cell, and inference used to treat 0..1 predictions as
    raw width/height instead of undoing the letterbox. Both are small in
    normalised space and large once scaled to a real screenshot, so these
    tests check the round trip in *pixels* at several aspect ratios.
    """

    @classmethod
    def setUpClass(cls):
        cls.bd = _load_board_detection()
        cls.image = np.zeros((480, 640, 3), dtype=np.uint8)

    def _true_pixels(self, width, height):
        """A perspective board that fits inside a width x height screenshot."""
        corners = np.array(
            [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], dtype=np.float64
        )
        target = np.array(
            [[120.0, 60.0], [520.0, 75.0], [500.0, 430.0], [100.0, 410.0]]
        )
        shrink = min(width / 640.0, height / 480.0)
        homography = self.bd._solve_homography(corners, target * shrink)
        return self.bd.apply_homography(homography, self.bd.IDEAL_LATTICE)

    def _round_trip(self, width, height):
        truth = self._true_pixels(width, height)
        size = self.bd.KEYPOINT_INPUT_SIZE
        scale = min(size / width, size / height)
        new_w = int(round(width * scale))
        new_h = int(round(height * scale))
        offset_x = (size - new_w) // 2
        offset_y = (size - new_h) // 2
        normalised = np.stack(
            [
                (truth[:, 0] * scale + offset_x) / size,
                (truth[:, 1] * scale + offset_y) / size,
            ],
            axis=1,
        )
        heatmaps = self.bd.gaussian_heatmaps(normalised)
        predicted, _confidence, _visible = self.bd.heatmaps_to_points(heatmaps)
        metadata = {
            "scale": scale,
            "offset": (offset_x, offset_y),
            "size": (size, size),
            "original_size": (width, height),
        }
        return truth, self.bd.map_points_to_image(predicted, metadata)

    def test_decode_round_trip_is_sub_pixel(self):
        for width, height in (
            (640, 480),
            (1280, 720),
            (1366, 768),
            (1920, 1080),
            (1000, 1000),
        ):
            with self.subTest(size=(width, height)):
                truth, decoded = self._round_trip(width, height)
                error = np.linalg.norm(decoded - truth, axis=1).mean()
                self.assertLess(error, 0.5, f"{width}x{height}: {error:.2f}px")

    def test_no_systematic_half_cell_bias(self):
        """A half-cell offset shows up as a constant shift, not random noise."""
        truth, decoded = self._round_trip(640, 480)
        bias = np.mean(decoded - truth, axis=0)
        np.testing.assert_allclose(bias, [0.0, 0.0], atol=0.2)

    def test_half_cell_bias_would_be_visible(self):
        """Guard the exact regression: (x + 0.5) must not come back."""
        source = (ROOT / "board_detection.py").read_text()
        self.assertNotIn("(x + 0.5) / float(width)", source)
        self.assertIn("x / float(width)", source)

    def test_letterbox_metadata_is_returned(self):
        batch, metadata = self.bd.preprocess_screenshot(
            self.image, with_metadata=True
        )
        self.assertEqual(batch.shape[0], 1)
        self.assertEqual(metadata["original_size"], (640, 480))
        self.assertAlmostEqual(metadata["scale"], 512 / 640)
        # Wide screenshot: padded top and bottom, flush left/right.
        self.assertEqual(metadata["offset"][0], 0)
        self.assertGreater(metadata["offset"][1], 0)

    def test_preprocess_still_returns_a_bare_batch_by_default(self):
        batch = self.bd.preprocess_screenshot(self.image)
        self.assertEqual(batch.ndim, 4)

    def test_non_square_mapping_is_not_stretched(self):
        """The old width/height mapping distorted 16:9 boards vertically.

        The image centre happens to agree under both mappings, so the probe
        has to sit off-centre: ``y = offset / size`` is the very top of the
        real content and must map back to row 0, not row 157.
        """
        height, width = 720, 1280
        scale = 512 / 1280
        new_h = int(round(height * scale))
        offset_y = (512 - new_h) // 2
        metadata = {
            "scale": scale,
            "offset": (0, offset_y),
            "size": (512, 512),
            "original_size": (width, height),
        }
        top = np.tile([[0.25, offset_y / 512.0]], (81, 1))
        bottom = np.tile([[0.25, (offset_y + new_h) / 512.0]], (81, 1))
        correct_top = self.bd.map_points_to_image(top, metadata)[0]
        correct_bottom = self.bd.map_points_to_image(bottom, metadata)[0]
        # Letterboxed content spans the full screenshot height.
        np.testing.assert_allclose(correct_top, [320.0, 0.0], atol=0.5)
        np.testing.assert_allclose(correct_bottom, [320.0, 720.0], atol=0.5)
        self.assertAlmostEqual(correct_bottom[1] - correct_top[1], 720.0, places=1)
        # The naive mapping is what the letterbox-free code produced.
        naive_top = (top * np.array([width, height]))[0]
        naive_bottom = (bottom * np.array([width, height]))[0]
        self.assertGreater(abs(naive_top[1] - correct_top[1]), 100.0)
        self.assertLess(naive_bottom[1] - naive_top[1], 500.0)

    def test_snap_points_rounds_and_clamps(self):
        points = np.array([[10.4, 20.6], [-3.2, 999.9]])
        snapped = self.bd.snap_points_to_pixels(points, (100, 200))
        np.testing.assert_array_equal(snapped, [[10.0, 21.0], [0.0, 199.0]])
        np.testing.assert_array_equal(snapped, np.rint(snapped))

    def test_snap_points_without_bounds(self):
        points = np.array([[10.4, 20.6], [-3.2, 999.9]])
        snapped = self.bd.snap_points_to_pixels(points)
        np.testing.assert_array_equal(snapped, [[10.0, 21.0], [-3.0, 1000.0]])

    def test_detection_reports_integer_geometry(self):
        """`detect_board_with_model` snaps its lattice to whole pixels."""
        try:
            import torch
        except ImportError:
            self.skipTest("torch is not installed")

        class FakeModel:
            def __init__(self, heatmaps):
                self._heatmaps = torch.from_numpy(heatmaps)

            def parameters(self):
                return iter(())

            def __call__(self, _tensor):
                return self._heatmaps[None, ...]

        truth = self._true_pixels(640, 480)
        size = self.bd.KEYPOINT_INPUT_SIZE
        scale = min(size / 640, size / 480)
        offset_y = (size - int(round(480 * scale))) // 2
        normalised = np.stack(
            [
                truth[:, 0] * scale / size,
                (truth[:, 1] * scale + offset_y) / size,
            ],
            axis=1,
        )
        heatmaps = self.bd.gaussian_heatmaps(normalised)
        model = FakeModel(heatmaps)

        detection = self.bd.detect_board_with_model(self.image, model)
        self.assertIsNotNone(detection)
        points = np.asarray(detection["points"])
        np.testing.assert_array_equal(points, np.rint(points))
        self.assertEqual(len(detection["corners"]), 4)
        for corner in detection["corners"]:
            self.assertEqual(float(corner[0]), round(float(corner[0])))
            self.assertEqual(float(corner[1]), round(float(corner[1])))
        # The continuous fit is still available for callers that want it.
        continuous = np.asarray(detection["continuous_points"])
        self.assertFalse(np.array_equal(continuous, points))
        error = np.linalg.norm(points - truth, axis=1).mean()
        self.assertLess(error, 1.0)
