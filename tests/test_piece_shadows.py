"""Headless shadow rendering and cache regression tests."""
import ast
import math
import os
from pathlib import Path
import unittest

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
import chess
import pygame

source = ast.parse((Path(__file__).resolve().parents[1] / 'new.py').read_text())
namespace = dict(chess=chess, pygame=pygame, math=math, SQUARE_SIZE=80,
                 _piece_shadow_cache={})
for node in source.body:
    if isinstance(node, ast.FunctionDef) and node.name == '_draw_piece_ground_shadow':
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'new.py', 'exec'), namespace)


class PieceShadowTests(unittest.TestCase):
    def setUp(self):
        namespace['_piece_shadow_cache'].clear()
        namespace['SQUARE_SIZE'] = 80

    def draw(self, piece=chess.KING, **kwargs):
        surface = pygame.Surface((240, 240), pygame.SRCALPHA)
        namespace['_draw_piece_ground_shadow'](
            surface, (120, 120), chess.Piece(piece, chess.WHITE), **kwargs)
        return surface

    def test_smooth_symmetric_falloff_and_transparent_border(self):
        self.draw()
        shadow = next(iter(namespace['_piece_shadow_cache'].values()))
        row = [shadow.get_at((x, shadow.get_height() // 2)).a
               for x in range(shadow.get_width())]
        self.assertEqual(row, list(reversed(row)))
        self.assertGreater(len(set(row)), 20)  # not four stepped oval bands
        self.assertEqual(row[:2], [0, 0])
        self.assertEqual(row[-2:], [0, 0])
        self.assertEqual(row[:len(row)//2], sorted(row[:len(row)//2]))
        for x in range(shadow.get_width()):
            self.assertEqual(shadow.get_at((x, 0)).a, 0)
            self.assertEqual(shadow.get_at((x, shadow.get_height()-1)).a, 0)

    def test_lift_spreads_and_softens_shadow(self):
        resting = self.draw()
        raised = self.draw(lift=1.0)
        self.assertGreater(raised.get_bounding_rect().width, resting.get_bounding_rect().width)
        self.assertGreater(raised.get_bounding_rect().height, resting.get_bounding_rect().height)
        peak = lambda image: max(image.get_at((x, y)).a
                                 for x in range(80, 165) for y in range(95, 145))
        self.assertLess(peak(raised), peak(resting))

    def test_opacity_reuses_cached_geometry_and_zero_is_noop(self):
        faint = self.draw(alpha=40)
        original = next(iter(namespace['_piece_shadow_cache'].values()))
        strong = self.draw(alpha=150)
        self.assertEqual(len(namespace['_piece_shadow_cache']), 1)
        self.assertIs(next(iter(namespace['_piece_shadow_cache'].values())), original)
        self.assertGreater(strong.get_at((120, 120)).a, faint.get_at((120, 120)).a)
        self.assertEqual(self.draw(alpha=0).get_bounding_rect().size, (0, 0))

    def test_piece_and_board_sizes_and_bounded_lift_cache(self):
        pawn = self.draw(piece=chess.PAWN)
        king = self.draw(piece=chess.KING)
        self.assertLess(pawn.get_bounding_rect().width, king.get_bounding_rect().width)
        namespace['_piece_shadow_cache'].clear()
        for frame in range(101):
            self.draw(lift=frame / 100)
        self.assertLessEqual(len(namespace['_piece_shadow_cache']), 9)
        self.draw(lift=-2)
        self.draw(lift=2)
        self.assertLessEqual(len(namespace['_piece_shadow_cache']), 9)
        for size in (40, 120):
            namespace['SQUARE_SIZE'] = size
            self.assertGreater(self.draw().get_bounding_rect().width, 0)


if __name__ == '__main__':
    unittest.main()
