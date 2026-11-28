"""Board-detection model integration: trainer command, GUI toggle, routing.

`board_detection.py` itself is covered by ``test_board_detection.py``. These
tests check the three places that *consume* it and are easy to break while
refactoring: board-vision.py's trainer command, cnn_trainer.py's model-type
plumbing, and new.py's experimental locator switch.
"""
import argparse
import ast
import importlib.util
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _extract(path, name):
    """AST node for one module-level function in ``path``."""
    source = ast.parse(path.read_text())
    return next(
        item
        for item in source.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )


def _exec_functions(path, names, namespace):
    """Execute selected module-level defs/assigns from ``path``."""
    source = ast.parse(path.read_text())
    wanted = set(names)
    for node in source.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"),
                namespace,
            )
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted
            for target in node.targets
        ):
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"),
                namespace,
            )
    return namespace


class TrainerCommandTests(unittest.TestCase):
    """board-vision.py must launch cnn_trainer.py in board mode."""

    @classmethod
    def setUpClass(cls):
        cls.bv = _load(ROOT / "board-vision.py", "board_vision_board_test")

    def test_checkpoint_is_the_board_suffix(self):
        path = self.bv.board_detection_checkpoint_path("mymodel")
        self.assertEqual(Path(path).name, "mymodel_board.pth")
        self.assertEqual(Path(path).parent.name, "models")

    def test_dataset_folder_lives_inside_the_model(self):
        folder = self.bv.board_detection_dataset_dir(self.bv.DATASET_DIR)
        self.assertIn("board_detection", str(folder))

    def test_command_targets_board_mode_and_suffix_output(self):
        commands = []

        class FakeCompleted:
            returncode = 0

        def fake_run(command, **kwargs):
            commands.append(command)
            return FakeCompleted()

        original_run = self.bv.subprocess.run
        original_collect = self.bv.BOARD_DETECTION.collect_board_samples
        self.bv.subprocess.run = fake_run
        self.bv.BOARD_DETECTION.collect_board_samples = (
            lambda dataset_dir: [{"image": Path("x.png"), "points": None}]
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self.bv.run_board_detection_trainer(Path(tmp) / "mymodel", "mymodel")
        finally:
            self.bv.subprocess.run = original_run
            self.bv.BOARD_DETECTION.collect_board_samples = original_collect

        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command[command.index("--model-type") + 1], "board")
        self.assertEqual(command[command.index("--model") + 1], "mymodel")
        output = command[command.index("--output") + 1]
        self.assertTrue(output.endswith("mymodel_board.pth"))
        self.assertIn("--ui", command)
        self.assertIn("--start", command)

    def test_no_samples_skips_the_trainer(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(
                self.bv.run_board_detection_trainer(Path(tmp), "nothing")
            )


class TrainerModelTypeTests(unittest.TestCase):
    """cnn_trainer.py gains a --model-type flag and routes on it."""

    def test_flag_parses_both_types(self):
        trainer = _load(ROOT / "cnn_trainer.py", "cnn_trainer_flag_test")
        self.assertEqual(trainer.parse_args([]).model_type, "pieces")
        self.assertEqual(
            trainer.parse_args(["--model-type", "board"]).model_type, "board"
        )

    def test_unknown_type_is_rejected(self):
        trainer = _load(ROOT / "cnn_trainer.py", "cnn_trainer_flag_test2")
        with self.assertRaises(SystemExit):
            trainer.parse_args(["--model-type", "segmentation"])

    def test_dispatch_prefers_board_trainer(self):
        trainer = _load(ROOT / "cnn_trainer.py", "cnn_trainer_dispatch_test")
        calls = []
        original_board = trainer.train_board_detection_model
        original_pieces = trainer.train_model
        trainer.train_board_detection_model = lambda *a, **k: calls.append("board")
        trainer.train_model = lambda *a, **k: calls.append("pieces")
        try:
            trainer.train_any_model(argparse.Namespace(model_type="board"))
            trainer.train_any_model(argparse.Namespace(model_type="pieces"))
            trainer.train_any_model(argparse.Namespace())
        finally:
            trainer.train_board_detection_model = original_board
            trainer.train_model = original_pieces
        self.assertEqual(calls, ["board", "pieces", "pieces"])

    def test_dashboard_has_a_model_type_dropdown(self):
        source = (ROOT / "cnn_trainer.py").read_text()
        self.assertIn("model_type_combo", source)
        self.assertIn("_on_model_type_selected", source)
        self.assertIn("BOARD_DETECTION.MODEL_TYPE_LABELS", source)

    def test_board_samples_map_to_the_input_letterbox(self):
        trainer = _load(ROOT / "cnn_trainer.py", "cnn_trainer_letterbox_test")
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wide.png"
            Image.new("RGB", (1000, 500)).save(path)
            points = [[0.0, 0.0]] * 81
            points[8] = [999.0, 0.0]
            truth = trainer._board_truth_in_input_space(
                {"image": path, "points": points}
            )
        # A 2:1 frame letterboxes into 512x512 with 128px bars top and bottom.
        self.assertGreater(truth[8][0], 0.99)
        self.assertAlmostEqual(truth[8][1], 0.25, places=2)


class GuiToggleTests(unittest.TestCase):
    """new.py exposes the experimental switch and routes detection through it."""

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "new.py").read_text()
        cls.namespace = {
            "os": os,
            "importlib": __import__("importlib"),
            "threading": threading,
            "np": np,
            "VISION_DEFAULT_MODEL": "default",
            "VISION_MODELS_DIR_NAME": "models",
            "_VISION_MODEL_NAME_SANITISE": re.compile(r"[^A-Za-z0-9._-]+"),
            "ENGINE_DEFAULTS": {"prefer_board_model": 0},
            "_board_detection_module": None,
            "_board_detection_error": "",
            "_board_model_cache": {
                "name": None, "path": None, "model": None, "error": ""
            },
            "_board_model_lock": threading.Lock(),
        }
        _exec_functions(
            ROOT / "new.py",
            {
                "_normalise_vision_model_name",
                "vision_models_dir",
                "vision_model_path",
                "vision_board_model_path",
                "vision_board_model_available",
                "_board_model_settings_enabled",
            },
            cls.namespace,
        )

    def _with_root(self, root):
        self.namespace["__file__"] = str(Path(root) / "new.py")

    def test_board_checkpoint_sits_beside_the_square_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._with_root(tmp)
            square = Path(self.namespace["vision_model_path"]("m1"))
            board = Path(self.namespace["vision_board_model_path"]("m1"))
        self.assertEqual(square.name, "m1.pth")
        self.assertEqual(board.name, "m1_board.pth")
        self.assertEqual(board.parent, square.parent)

    def test_availability_checks_the_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._with_root(tmp)
            models = Path(tmp) / "models"
            models.mkdir()
            self.assertFalse(self.namespace["vision_board_model_available"]("m1"))
            (models / "m1_board.pth").write_bytes(b"stub")
            self.assertTrue(self.namespace["vision_board_model_available"]("m1"))
            self.assertFalse(self.namespace["vision_board_model_available"]("other"))

    def test_switch_defaults_off_and_follows_the_global(self):
        self.namespace["prefer_board_model"] = 0
        self.assertFalse(self.namespace["_board_model_settings_enabled"]())
        self.namespace["prefer_board_model"] = 1
        self.assertTrue(self.namespace["_board_model_settings_enabled"]())
        self.namespace["prefer_board_model"] = False
        self.assertFalse(self.namespace["_board_model_settings_enabled"]())

    def test_toggle_is_a_display_gui_item_and_persists(self):
        self.assertIn("Prefer model over OpenCV (experimental)", self.source)
        self.assertIn('"prefer_board_model"', self.source)
        self.assertIn('config["prefer_board_model"]', self.source)

    def test_detection_tries_the_model_then_opencv(self):
        model_at = self.source.index("if _board_model_settings_enabled():")
        opencv_at = self.source.index(
            "corner_detection = _find_board_with_opencv_corners(image)"
        )
        self.assertLess(model_at, opencv_at)
        self.assertIn("if corner_detection is None:", self.source)

    def _run_locator(self, detection, enabled):
        namespace = {"BOARD_DETECTION_MIN_SIDE": 64, "np": np}
        namespace["detect_board_with_model"] = lambda image: detection
        namespace["_board_model_settings_enabled"] = lambda: enabled
        exec(
            compile(
                ast.Module(body=[_extract(ROOT / "new.py", "_find_board_with_model")],
                           type_ignores=[]),
                "new.py",
                "exec",
            ),
            namespace,
        )
        return namespace["_find_board_with_model"](
            np.zeros((600, 600, 3), dtype=np.uint8)
        )

    def test_model_locator_result_matches_the_opencv_contract(self):
        detection = {
            "board_rect": (40.4, 60.6, 400.0, 400.0),
            "corners": [(40, 60), (440, 60), (440, 460), (40, 460)],
            "points": [
                [40.0 + 50 * (i % 9), 60.0 + 50 * (i // 9)] for i in range(81)
            ],
            "mean_error": 0.42,
            "confidence": 0.88,
            "inlier_count": 79,
            "homography": np.eye(3),
        }
        result = self._run_locator(detection, True)
        self.assertEqual(result["method"], "board_model")
        self.assertEqual(result["board_rect"], (40, 61, 400, 400))
        self.assertEqual(result["pattern_size"], (7, 7))
        self.assertEqual(len(result["model_points"]), 81)
        self.assertAlmostEqual(result["cell_size"], 50.0)
        self.assertAlmostEqual(result["model_confidence"], 0.88)
        for field in (
            "corners",
            "grid_error",
            "max_grid_error",
            "preprocessing",
            "cell_size",
        ):
            self.assertIn(field, result)

    def test_locator_returns_none_when_the_switch_is_off(self):
        self.assertIsNone(self._run_locator({"board_rect": (0, 0, 400, 400)}, False))

    def test_locator_rejects_a_low_confidence_or_tiny_board(self):
        tiny = {
            "board_rect": (10, 10, 20, 20),
            "corners": [(10, 10), (30, 10), (30, 30), (10, 30)],
            "points": [[10.0, 10.0]] * 81,
            "mean_error": 0.0,
            "confidence": 0.99,
            "inlier_count": 81,
            "homography": np.eye(3),
        }
        self.assertIsNone(self._run_locator(tiny, True))


class CallSiteTests(unittest.TestCase):
    """Detect Board, Auto-Play and Advisor share one detection entry point."""

    def test_every_scan_worker_uses_the_shared_locator(self):
        callers = []
        current = None
        for line in (ROOT / "new.py").read_text().split("\n"):
            if line.startswith("def "):
                current = line[4:].split("(")[0]
            elif "detect_chessboard_in_image(" in line:
                callers.append(current)
        for expected in (
            "_board_detection_worker",
            "_autoplay_scan_worker",
            "_advisor_scan_worker",
        ):
            self.assertIn(expected, callers)


if __name__ == "__main__":
    unittest.main()
