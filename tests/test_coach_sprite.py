"""Headless checks for Coach Nolan's procedural Best-move sprite."""
import ast
import math
import os
from pathlib import Path
import unittest

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
import pygame

source = ast.parse((Path(__file__).resolve().parents[1] / 'new.py').read_text())
namespace = dict(pygame=pygame, math=math, GAME_REVIEW_RATINGS={'best': {}})
for node in source.body:
    if isinstance(node, ast.FunctionDef) and node.name == '_draw_game_review_coach_character':
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'new.py', 'exec'), namespace)
draw_coach = namespace['_draw_game_review_coach_character']


class CoachSpriteTests(unittest.TestCase):
    def test_best_medal_is_below_viewer_left_fist(self):
        for size in ((120, 132), (240, 264), (60, 66)):
            for time_ms in (0, 400, 1000, 2000):
                with self.subTest(size=size, time_ms=time_ms):
                    surface = pygame.Surface(size, pygame.SRCALPHA)
                    draw_coach(surface, surface.get_rect(), 'best', time_ms=time_ms)
                    def gold_pixels(left, right):
                        count = 0
                        for x in range(round(left * size[0] / 120), round(right * size[0] / 120)):
                            for y in range(round(108 * size[1] / 132), round(124 * size[1] / 132)):
                                r, g, b, a = surface.get_at((x, y))
                                count += a > 150 and r > 185 and g > 140 and b < 135
                        return count
                    self.assertGreater(gold_pixels(10, 30), 0)
                    self.assertEqual(gold_pixels(92, 112), 0)

    def test_best_mouth_is_one_connected_smile_at_rest_and_while_speaking(self):
        heights = []
        for openness in (0.0, 0.08, 0.16, 0.5, 1.0):
            with self.subTest(mouth_open=openness):
                surface = pygame.Surface((360, 396), pygame.SRCALPHA)
                draw_coach(surface, surface.get_rect(), 'best',
                           time_ms=0, mouth_open=openness)
                # At time zero head_y is zero; isolate the mouth, away
                # from the nose, cheeks and hands. Exact fill colours
                # are checked with tolerance for smoothscale rounding.
                dark = {(x, y) for x in range(145, 205) for y in range(220, 260)
                        if surface.get_at((x, y)).r < 170 and surface.get_at((x, y)).g < 110 and surface.get_at((x, y)).b < 100}
                self.assertTrue(dark)
                heights.append(max(y for x, y in dark) - min(y for x, y in dark) + 1)
                teeth = {(x, y) for x in range(145, 205) for y in range(220, 260)
                         if all(abs(a-b) <= 4 for a, b in zip(surface.get_at((x, y))[:3], (246, 238, 220)))}
                remaining = dark | teeth
                pending = [remaining.pop()]
                while pending:
                    x, y = pending.pop()
                    for point in ((x+dx, y+dy) for dx in range(-2, 3) for dy in range(-2, 3)):
                        if point in remaining:
                            remaining.remove(point)
                            pending.append(point)
                self.assertFalse(remaining, 'Mouth must not contain detached lip arcs')
                if openness == 0.0:
                    self.assertFalse(teeth, 'Closed smile must not show floating teeth')
                for x, y in teeth:
                    self.assertGreater(y, min(dy for dx, dy in dark))
                    self.assertGreater(x, min(dx for dx, dy in dark))
                    self.assertLess(x, max(dx for dx, dy in dark))
                    self.assertTrue(any((x, below) in dark for below in range(y+1, 260)))
        self.assertLessEqual(heights[0], 10, 'Resting smile should be a thin curve')
        self.assertEqual(heights, sorted(heights))
        self.assertGreater(heights[-1], heights[0])

    def test_best_sprite_stays_inside_destination_rect(self):
        surface = pygame.Surface((160, 180), pygame.SRCALPHA)
        rect = pygame.Rect(20, 20, 120, 132)
        draw_coach(surface, rect, 'best', time_ms=400, mouth_open=0.8)
        self.assertTrue(rect.contains(surface.get_bounding_rect()))


if __name__ == '__main__':
    unittest.main()
