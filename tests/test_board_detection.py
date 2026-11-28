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
            np.testing.assert_allclose(loaded[0], self.truth, atol=0.01)
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
