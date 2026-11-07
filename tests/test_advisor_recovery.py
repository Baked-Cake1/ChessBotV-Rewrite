"""Compare pruned Advisor recovery with the pre-optimization exhaustive search."""
import ast
from pathlib import Path
import random
import unittest
import chess

source = ast.parse((Path(__file__).resolve().parents[1] / 'new.py').read_text())
namespace = {'chess': chess}
functions = {'_autoplay_board_grid_from_board', '_autoplay_candidate_matches',
             '_autoplay_transition_matches', '_autoplay_matching_legal_moves',
             '_advisor_move_changed_mask', '_advisor_find_move_sequence'}
for node in source.body:
    if isinstance(node, ast.FunctionDef) and node.name in functions:
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'new.py', 'exec'), namespace)
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
            t.id == 'ADVISOR_POLL_SECONDS' for t in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'new.py', 'exec'), namespace)
exec(compile(Path(__file__).with_name('advisor_reference.py').read_text(),
             'advisor_reference.py', 'exec'), namespace)


def sequence_ucis(sequence):
    return None if sequence is None else [m.uci() for m in sequence]


class AdvisorRecoveryTests(unittest.TestCase):
    def compare(self, board, grid, max_plies=2):
        original_fen = board.fen()
        original_stack = list(board.move_stack)
        expected = namespace['exhaustive_find_move_sequence'](board, grid, max_plies)
        actual = namespace['_advisor_find_move_sequence'](board, grid, max_plies)
        self.assertEqual(sequence_ucis(actual), sequence_ucis(expected))
        self.assertEqual(board.fen(), original_fen)
        self.assertEqual(board.move_stack, original_stack)
        return actual

    def grid_after(self, board, moves):
        after = board.copy()
        for uci in moves:
            after.push_uci(uci)
        return namespace['_autoplay_board_grid_from_board'](after)

    def test_poll_interval(self):
        self.assertEqual(namespace['ADVISOR_POLL_SECONDS'], 0.18)

    def test_unchanged_single_two_ply_and_limit(self):
        board = chess.Board()
        self.assertEqual(self.compare(board, self.grid_after(board, [])), [])
        self.assertEqual(sequence_ucis(self.compare(board, self.grid_after(board, ['e2e4']))), ['e2e4'])
        grid = self.grid_after(board, ['e2e4', 'e7e5'])
        self.assertEqual(sequence_ucis(self.compare(board, grid)), ['e2e4', 'e7e5'])
        self.assertIsNone(self.compare(board, grid, max_plies=1))

    def test_special_moves_in_recovery(self):
        cases = [
            ('r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1', ['e1g1', 'e8c8']),
            ('4k3/8/8/8/3p4/8/4P3/4K3 w - - 0 1', ['e2e4', 'd4e3']),
            ('4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1', ['e5d6', 'e8f7']),
            ('4k3/P7/8/8/8/8/7p/4K3 w - - 0 1', ['a7a8n', 'h2h1q']),
            ('4k3/8/8/8/8/2n5/8/1R2K3 w - - 0 1', ['b1b3', 'c3b1']),
        ]
        for fen, moves in cases:
            with self.subTest(fen=fen, moves=moves):
                board = chess.Board(fen)
                result = self.compare(board, self.grid_after(board, moves))
                if moves == ['e2e4', 'd4e3']:
                    # e2-e3 followed by an ordinary capture has exactly
                    # the same placement: both searches must reject this
                    # ambiguity rather than guessing the en-passant line.
                    self.assertIsNone(result)
                else:
                    self.assertEqual(sequence_ucis(result), moves)

    def test_changed_mask_includes_special_move_squares(self):
        board = chess.Board('4k3/8/8/8/3pP3/8/8/4K3 b - e3 0 1')
        mask = namespace['_advisor_move_changed_mask'](board, chess.Move.from_uci('d4e3'))
        self.assertTrue(mask & chess.BB_E4)  # captured pawn, neither from nor to
        for chess960, uci in ((False, 'e1g1'), (True, 'e1h1')):
            board = chess.Board('r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1', chess960=chess960)
            move = chess.Move.from_uci(uci)
            self.assertIn(move, board.legal_moves)
            self.assertEqual(namespace['_advisor_move_changed_mask'](board, move), chess.BB_ALL)

    def test_unknown_ambiguous_and_contradictory_observations(self):
        board = chess.Board()
        self.compare(board, [['?'] * 8 for _ in range(8)])
        grid = self.grid_after(board, ['e2e4', 'e7e5'])
        grid[3][4] = '?'
        self.compare(board, grid)
        grid[0][0] = 'Q'  # impossible extra change for the observed two moves
        self.assertIsNone(self.compare(board, grid))
        # Only observe that e2 is empty; several moves/sequences can fit.
        ambiguous = [['?'] * 8 for _ in range(8)]
        ambiguous[6][4] = '.'
        self.assertIsNone(self.compare(board, ambiguous))

    def test_checkmate_banner_fallback(self):
        board = chess.Board()
        board.push_uci('f2f3'); board.push_uci('e7e5')
        grid = self.grid_after(board, ['g2g4', 'd8h4'])
        grid[0][0] = 'Q'  # a banner misclassifies an unrelated square
        self.assertEqual(sequence_ucis(self.compare(board, grid)), ['g2g4', 'd8h4'])
        board.push_uci('g2g4')
        self.assertEqual(sequence_ucis(self.compare(board, grid)), ['d8h4'])

    def test_random_positions_with_partial_observations(self):
        rng = random.Random(9387)
        board = chess.Board()
        for index in range(24):
            if board.is_game_over():
                board.reset()
            after = board.copy()
            for _ in range(2):
                if not after.is_game_over():
                    after.push(rng.choice(list(after.legal_moves)))
            grid = namespace['_autoplay_board_grid_from_board'](after)
            if index % 2:
                for _ in range(12):
                    grid[rng.randrange(8)][rng.randrange(8)] = '?'
            self.compare(board, grid)
            board.push(rng.choice(list(board.legal_moves)))

    def test_pruning_reduces_grid_construction(self):
        board = chess.Board()
        grid = self.grid_after(board, ['e2e4', 'e7e5'])
        original = namespace['_autoplay_board_grid_from_board']
        counts = []
        try:
            for name in ('exhaustive_find_move_sequence', '_advisor_find_move_sequence'):
                calls = []
                def counted(current):
                    calls.append(1)
                    return original(current)
                namespace['_autoplay_board_grid_from_board'] = counted
                result = namespace[name](board, grid)
                self.assertEqual(sequence_ucis(result), ['e2e4', 'e7e5'])
                counts.append(len(calls))
        finally:
            namespace['_autoplay_board_grid_from_board'] = original
        self.assertLess(counts[1], counts[0] // 4)


if __name__ == '__main__':
    unittest.main()
