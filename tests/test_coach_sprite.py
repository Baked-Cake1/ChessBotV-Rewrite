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

    def test_best_sprite_stays_inside_destination_rect(self):
        surface = pygame.Surface((160, 180), pygame.SRCALPHA)
        rect = pygame.Rect(20, 20, 120, 132)
        draw_coach(surface, rect, 'best', time_ms=400, mouth_open=0.8)
        self.assertTrue(rect.contains(surface.get_bounding_rect()))


if __name__ == '__main__':
    unittest.main()
