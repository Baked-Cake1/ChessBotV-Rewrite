"""Multiple-CNN-model layout: datasets/<model>/ plus models/<model>.pth.

These tests never import the heavy GUI/trainer stacks. board-vision.py is
importable on its own; cnn_trainer.py and new.py are read with ``ast`` and
only the small model-layout helpers are executed.
"""
import ast
import importlib.util
import os
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_board_vision():
    spec = importlib.util.spec_from_file_location(
        "board_vision_under_test", ROOT / "board-vision.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_helpers(path, names, namespace):
    """Execute selected module-level assigns/functions from ``path``."""
    source = ast.parse(path.read_text())
    for node in source.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"),
                namespace,
            )
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names
            for target in node.targets
        ):
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"),
                namespace,
            )
    return namespace


class BoardVisionModelLayoutTests(unittest.TestCase):
    def setUp(self):
        self.bv = _load_board_vision()
        self.tmp = Path(tempfile.mkdtemp())
        self.bv.BASE_DIR = self.tmp
        self.bv.DATASET_DIR = self.tmp / "datasets"
        self.bv.MODELS_DIR = self.tmp / "models"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _legacy_dataset(self):
        (self.bv.DATASET_DIR / "training" / "white_king").mkdir(parents=True)
        (self.bv.DATASET_DIR / "validation" / "empty").mkdir(parents=True)
        (self.bv.DATASET_DIR / "training" / "white_king" / "a.png").write_bytes(b"a")
        (self.bv.DATASET_DIR / "validation" / "empty" / "b.png").write_bytes(b"b")
        (self.bv.DATASET_DIR / "metadata.csv").write_text("label\n")

    def test_legacy_split_folders_move_into_default(self):
        self._legacy_dataset()
        default_dir = self.bv.prepare_model_layout(self.bv.DATASET_DIR)
        self.assertEqual(default_dir, self.bv.DATASET_DIR / "default")
        self.assertTrue(
            (default_dir / "training" / "white_king" / "a.png").is_file()
        )
        self.assertTrue(
            (default_dir / "validation" / "empty" / "b.png").is_file()
        )
        self.assertTrue((default_dir / "metadata.csv").is_file())
        self.assertFalse((self.bv.DATASET_DIR / "training").exists())
        self.assertFalse((self.bv.DATASET_DIR / "validation").exists())
        # The class-folder placeholders every model needs exist.
        for split in ("training", "validation"):
            for class_dir in (default_dir / split).iterdir():
                self.assertTrue(class_dir.is_dir())

    def test_migration_is_idempotent_and_keeps_new_images(self):
        self._legacy_dataset()
        self.bv.prepare_model_layout(self.bv.DATASET_DIR)
        added = (
            self.bv.DATASET_DIR / "default" / "training" / "white_king" / "c.png"
        )
        added.write_bytes(b"c")
        self.assertFalse(self.bv._migrate_legacy_layout(self.bv.DATASET_DIR))
        self.assertTrue(added.is_file())

    def test_root_checkpoint_becomes_models_default(self):
        legacy = self.tmp / "chessbotv_cnn.pth"
        legacy.write_bytes(b"weights")
        moved = self.bv.prepare_model_layout(self.bv.DATASET_DIR)
        self.assertTrue(moved.is_dir())
        self.assertTrue((self.tmp / "models" / "default.pth").is_file())
        self.assertFalse(legacy.exists())
        self.assertIn("default", self.bv.list_model_names(self.bv.DATASET_DIR))

    def test_model_names_cover_datasets_and_checkpoints(self):
        self.bv.prepare_model_layout(self.bv.DATASET_DIR)
        (self.bv.MODELS_DIR / "lichess.pth").write_bytes(b"w")
        names = self.bv.list_model_names(self.bv.DATASET_DIR)
        self.assertIn("default", names)
        self.assertIn("lichess", names)
        self.assertNotIn("training", names)
        self.assertNotIn("validation", names)

    def test_model_paths_and_sanitising(self):
        self.assertEqual(self.bv.sanitise_model_name(" My Model #2 "), "My_Model_2")
        self.assertEqual(self.bv.sanitise_model_name(""), "default")
        self.assertEqual(
            self.bv.model_dataset_dir("lichess", self.bv.DATASET_DIR),
            self.bv.DATASET_DIR / "lichess",
        )
        self.assertEqual(
            self.bv.model_output_path("lichess"), self.tmp / "models" / "lichess.pth"
        )

    def test_chooser_defaults_numbers_and_new_names(self):
        self.bv.prepare_model_layout(self.bv.DATASET_DIR)
        (self.bv.MODELS_DIR / "lichess.pth").write_bytes(b"w")
        names = self.bv.list_model_names(self.bv.DATASET_DIR)
        self.assertEqual(names, ["default", "lichess"])

        answers = iter(["", "2", "blitz set", "1"])
        picked = [
            self.bv.choose_model_for_run(
                names, input_fn=lambda _prompt: next(answers)
            )
            for _ in range(4)
        ]
        self.assertEqual(picked, ["default", "lichess", "blitz_set", "default"])

    def test_chooser_survives_end_of_input(self):
        def raise_eof(_prompt):
            raise EOFError

        self.assertEqual(
            self.bv.choose_model_for_run([], input_fn=raise_eof), "default"
        )

    def test_trainer_command_targets_the_chosen_model(self):
        self.bv.prepare_model_layout(self.bv.DATASET_DIR)
        captured = {}

        def fake_run(command, cwd=None, check=False):
            captured["command"] = command

            class Result:
                returncode = 0

            return Result()

        original = self.bv.subprocess.run
        self.bv.subprocess.run = fake_run
        try:
            ok = self.bv.run_cnn_trainer(
                [{"label": "white_king", "split": "training"}],
                self.bv.DATASET_DIR / "lichess",
                "lichess",
            )
        finally:
            self.bv.subprocess.run = original
        self.assertTrue(ok)
        command = captured["command"]
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "lichess")
        self.assertEqual(
            command[command.index("--output") + 1],
            str(self.bv.model_output_path("lichess")),
        )
        self.assertTrue(
            command[command.index("--dataset-dir") + 1].endswith("datasets/lichess")
        )


class TrainerModelArgsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        namespace = {"re": re, "Path": Path, "BASE_DIR": self.tmp}
        self.ct = _exec_helpers(
            ROOT / "cnn_trainer.py",
            {
                "DATASET_DIR",
                "MODELS_DIR",
                "DEFAULT_MODEL_NAME",
                "LEGACY_MODEL_FILENAME",
                "_MODEL_NAME_SANITISE",
                "sanitise_model_name",
                "model_dataset_dir",
                "model_output_path",
                "models_dir",
                "list_model_names",
                "resolve_model_name",
            },
            namespace,
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_paths_follow_the_model_name(self):
        self.assertEqual(self.ct["model_dataset_dir"]("x"), self.tmp / "datasets" / "x")
        self.assertEqual(self.ct["model_output_path"]("x"), self.tmp / "models" / "x.pth")
        self.assertTrue((self.tmp / "models").is_dir())

    def test_resolution_order(self):
        resolve = self.ct["resolve_model_name"]

        class Args:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        self.assertEqual(resolve(Args(model="Lichess #2")), "Lichess_2")
        self.assertEqual(
            resolve(Args(model=None, output=self.tmp / "models" / "foo.pth")), "foo"
        )
        self.assertEqual(
            resolve(Args(model=None, output=None, dataset_dir=self.tmp / "datasets" / "bar")),
            "bar",
        )
        self.assertEqual(
            resolve(Args(model=None, output=None, dataset_dir=Path("/tmp/elsewhere"))),
            "default",
        )

    def test_names_include_dataset_folders_and_checkpoints(self):
        (self.tmp / "datasets" / "alpha" / "training").mkdir(parents=True)
        (self.tmp / "models").mkdir(parents=True)
        (self.tmp / "models" / "beta.pth").write_bytes(b"w")
        self.assertEqual(self.ct["list_model_names"](), ["alpha", "beta"])


class ChessBotVModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        namespace = {
            "re": re,
            "os": os,
            "Path": Path,
            "__file__": str(self.tmp / "new.py"),
            "CNN_MODEL_FILENAME": "chessbotv_cnn.pth",
            "CNN_MODEL_ENV_VAR": "CHESSBOTV_CNN_MODEL",
            "config": {},
        }
        self.cbm = _exec_helpers(
            ROOT / "new.py",
            {
                "VISION_MODELS_DIR_NAME",
                "VISION_DEFAULT_MODEL",
                "VISION_LEGACY_MODEL_FILENAME",
                "_VISION_MODEL_NAME_SANITISE",
                "vision_model_name",
                "_normalise_vision_model_name",
                "vision_models_dir",
                "vision_model_path",
                "vision_model_names",
                "_find_cnn_model_path_for",
            },
            namespace,
        )
        (self.tmp / "models").mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("CHESSBOTV_CNN_MODEL", None)

    def test_selected_model_checkpoint_is_preferred(self):
        (self.tmp / "models" / "lichess.pth").write_bytes(b"w")
        (self.tmp / "models" / "default.pth").write_bytes(b"w")
        find = self.cbm["_find_cnn_model_path_for"]
        self.cbm["vision_model_name"] = "lichess"
        self.assertEqual(
            find(str(self.tmp)), str(self.tmp / "models" / "lichess.pth")
        )
        self.cbm["vision_model_name"] = "default"
        self.assertEqual(
            find(str(self.tmp)), str(self.tmp / "models" / "default.pth")
        )

    def test_missing_model_reports_the_selected_name(self):
        self.cbm["vision_model_name"] = "ghost"
        with self.assertRaises(FileNotFoundError) as caught:
            self.cbm["_find_cnn_model_path_for"](str(self.tmp), use_package=False)
        self.assertIn("models/ghost.pth", str(caught.exception))

    def test_legacy_root_checkpoint_still_loads_for_package_mode(self):
        (self.tmp / "chessbotv_cnn.pth").write_bytes(b"w")
        self.cbm["vision_model_name"] = "default"
        find = self.cbm["_find_cnn_model_path_for"]
        self.assertEqual(
            find(str(self.tmp), use_package=True),
            str(self.tmp / "chessbotv_cnn.pth"),
        )
        # ... but the built-in recogniser mode never silently picks it.
        with self.assertRaises(FileNotFoundError):
            find(str(self.tmp), use_package=False)

    def test_model_names_and_paths(self):
        (self.tmp / "models" / "beta.pth").write_bytes(b"w")
        self.cbm["vision_model_name"] = "gamma"
        names = self.cbm["vision_model_names"]()
        self.assertIn("beta", names)
        self.assertIn("gamma", names)
        self.assertIn("default", names)
        self.assertEqual(
            self.cbm["vision_model_path"]("My Set"),
            str(self.tmp / "models" / "My_Set.pth"),
        )
        self.assertEqual(self.cbm["_normalise_vision_model_name"](""), "default")


if __name__ == "__main__":
    unittest.main()
