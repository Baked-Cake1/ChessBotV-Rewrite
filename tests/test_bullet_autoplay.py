"""Bullet Auto-Play preset tests without importing the pygame application."""
import ast
from pathlib import Path
import unittest

import chess

SOURCE = Path(__file__).resolve().parents[1] / "new.py"
source = ast.parse(SOURCE.read_text())

namespace = {"chess": chess}
functions = {
    "_autoplay_bullet_enabled",
    "_autoplay_bullet_depth",
    "_autoplay_poll_seconds",
    "get_autoplay_depth",
    "autoplay_depth_wait_label",
}
constants = {
    "AUTOPLAY_DEPTH_MIN",
    "AUTOPLAY_DEPTH_MAX",
    "AUTOPLAY_DEFAULT_DEPTH",
    "AUTOPLAY_POLL_SECONDS",
    "BULLET_AUTOPLAY_DEPTH",
    "BULLET_AUTOPLAY_MOVETIME_MS",
    "BULLET_AUTOPLAY_MOVETIME_SECONDS",
    "BULLET_AUTOPLAY_DRAG_SECONDS",
    "BULLET_AUTOPLAY_DRAG_MIN_SECONDS",
    "BULLET_AUTOPLAY_DRAG_MAX_SECONDS",
    "BULLET_AUTOPLAY_APPROACH_MIN_SECONDS",
    "BULLET_AUTOPLAY_APPROACH_MAX_SECONDS",
    "BULLET_AUTOPLAY_VERIFY_RETRY_SECONDS",
    "BULLET_AUTOPLAY_FOCUS_SECONDS",
    "ENGINE_DEFAULTS",
}
for node in source.body:
    if isinstance(node, ast.FunctionDef) and node.name in functions:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "new.py", "exec"),
             namespace)
    elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants
            for target in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), "new.py", "exec"),
             namespace)


class BulletAutoplayTests(unittest.TestCase):
    def setUp(self):
        namespace["config"] = {"depth": 25, "autoplay_depth": 20}
        namespace["ANALYSIS_DEPTH"] = 25
        namespace["autoplay_depth"] = 20

    def enable(self, on=True):
        namespace["config"]["autoplay_bullet_mode"] = 1 if on else 0

    def test_default_is_off_and_saved(self):
        self.assertEqual(namespace["ENGINE_DEFAULTS"]["autoplay_bullet_mode"], 0)
        self.assertFalse(namespace["_autoplay_bullet_enabled"]())

    def test_bullet_limits_are_three_seconds_and_depth_eighteen(self):
        self.assertEqual(namespace["BULLET_AUTOPLAY_DEPTH"], 18)
        self.assertEqual(namespace["BULLET_AUTOPLAY_MOVETIME_MS"], 3000)
        self.assertEqual(namespace["BULLET_AUTOPLAY_MOVETIME_SECONDS"], 3.0)

    def test_bullet_depth_is_eighteen(self):
        self.enable()
        self.assertEqual(namespace["_autoplay_bullet_depth"](), 18)
        self.assertEqual(namespace["get_autoplay_depth"](), 18)

    def test_bullet_depth_stays_capped_by_depth_limit(self):
        self.enable()
        namespace["ANALYSIS_DEPTH"] = 12
        self.assertEqual(namespace["_autoplay_bullet_depth"](), 12)
        self.assertEqual(namespace["get_autoplay_depth"](), 12)

    def test_saved_depth_returns_when_bullet_is_off(self):
        self.enable()
        self.assertEqual(namespace["get_autoplay_depth"](), 18)
        self.enable(False)
        self.assertEqual(namespace["get_autoplay_depth"](), 20)
        namespace["config"]["autoplay_depth"] = 33
        namespace["autoplay_depth"] = 33
        self.assertEqual(namespace["get_autoplay_depth"](), 25)

    def test_bullet_polls_without_delay(self):
        self.assertAlmostEqual(namespace["_autoplay_poll_seconds"](), 0.05)
        self.enable()
        self.assertEqual(namespace["_autoplay_poll_seconds"](), 0.0)

    def test_bullet_drag_timings_are_shorter_than_normal(self):
        self.assertLessEqual(
            namespace["BULLET_AUTOPLAY_DRAG_MAX_SECONDS"],
            namespace["BULLET_AUTOPLAY_DRAG_SECONDS"] * 2,
        )
        self.assertLess(
            namespace["BULLET_AUTOPLAY_DRAG_SECONDS"], 0.44  # normal nominal
        )
        self.assertLess(
            namespace["BULLET_AUTOPLAY_APPROACH_MAX_SECONDS"],
            namespace["BULLET_AUTOPLAY_DRAG_SECONDS"],
        )
        self.assertLess(namespace["BULLET_AUTOPLAY_FOCUS_SECONDS"], 0.05)
        self.assertLess(namespace["BULLET_AUTOPLAY_VERIFY_RETRY_SECONDS"], 1.2)

    def test_wait_label_names_the_bullet_limits(self):
        self.assertEqual(namespace["autoplay_depth_wait_label"](), "depth 20")
        self.enable()
        self.assertEqual(
            namespace["autoplay_depth_wait_label"](), "Bullet depth 18 or 3s"
        )

    def test_gui_settings_expose_the_toggle(self):
        self.assertIn(
            '("autoplay_bullet_mode", "Bullet Auto-Play", "toggle"',
            SOURCE.read_text(),
        )


if __name__ == "__main__":
    unittest.main()
