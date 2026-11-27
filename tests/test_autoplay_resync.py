"""Auto-play recovery tests: never give up on an untraceable board.

Auto-play treats the live board as the source of truth. When its changed
squares cannot be traced back to a legal move it must wait for the board
to settle and then recapture/re-import it from its FEN — never stop the
session. These tests exercise that recovery logic without pygame.
"""
import ast
import time
from pathlib import Path
import unittest

import chess

SOURCE = Path(__file__).resolve().parents[1] / "new.py"
source = ast.parse(SOURCE.read_text())

namespace = {"chess": chess, "time": time}
functions = {
    "_autoplay_resync_turn",
    "_autoplay_find_legal_move",
    "_autoplay_matching_legal_moves",
    "_autoplay_candidate_matches",
    "_autoplay_transition_matches",
    "_autoplay_board_grid_from_board",
    "_autoplay_side_name",
    # The 0/1/2-ply matcher the turn inference walks a recovered board with.
    "_advisor_move_changed_mask",
    "_advisor_find_move_sequence",
    "_advisor_find_move_sequence_cells",
    "_grid_cells",
    "_board_cells",
    "_changed_cell_indices",
    "_cells_match",
    "_transition_cells_match",
    "_placement_layout",
    "_cells_after_move",
    "_advisor_changed_cell_set",
    "_advisor_changed_cell_mask",
}
constants = {
    "AUTOPLAY_RESYNC_DELAY_SECONDS",
    "AUTOPLAY_RESYNC_RETRY_SECONDS",
    "AUTOPLAY_SCAN_RETRY_LIMIT",
    "AUTOPLAY_SYNC_RESCAN_SECONDS",
    "_GRID_CELL_CODES",
    "_WHITE_CELL_CODES",
    "_ALL_CELL_INDICES",
    "_CELL_BIT",
    "_CELL_INDICES_64",
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


def grid_of(board):
    return namespace["_autoplay_board_grid_from_board"](board)


class ResyncTurnTests(unittest.TestCase):
    """A recaptured board must keep (or correctly advance) whose turn it is."""

    def import_like_autoplay(self, live_board, side="white"):
        # Mirrors _autoplay_fen_from_visual_grid: placement only, then the
        # fresh-scan side-to-move rule.
        fen = f"{live_board.board_fen()} {'w' if side == 'white' else 'b'} - - 0 1"
        return chess.Board(fen)

    def test_unchanged_board_keeps_the_tracked_turn(self):
        tracked = chess.Board()
        tracked.push_uci("e2e4")
        imported = self.import_like_autoplay(tracked)
        self.assertEqual(imported.turn, chess.WHITE)  # fresh-scan default
        resolved = namespace["_autoplay_resync_turn"](imported, tracked)
        self.assertEqual(resolved.turn, chess.BLACK)  # tracked turn kept
        self.assertEqual(resolved.board_fen(), tracked.board_fen())

    def test_opponent_reply_advances_the_turn(self):
        tracked = chess.Board()
        tracked.push_uci("e2e4")
        live = tracked.copy()
        live.push_uci("e7e5")
        imported = self.import_like_autoplay(live)
        resolved = namespace["_autoplay_resync_turn"](imported, tracked)
        self.assertEqual(resolved.turn, chess.WHITE)
        self.assertEqual(resolved.board_fen(), live.board_fen())

    def test_two_ply_reply_advances_the_turn(self):
        tracked = chess.Board()
        tracked.push_uci("e2e4")
        live = tracked.copy()
        live.push_uci("e7e5")
        live.push_uci("g1f3")
        imported = self.import_like_autoplay(live)
        resolved = namespace["_autoplay_resync_turn"](imported, tracked)
        self.assertEqual(resolved.turn, chess.BLACK)
        self.assertEqual(resolved.board_fen(), live.board_fen())

    def test_untraceable_board_keeps_the_fresh_scan_turn(self):
        tracked = chess.Board()
        live = chess.Board()
        live.remove_piece_at(chess.E1)
        live.set_piece_at(chess.A1, chess.Piece(chess.KING, chess.WHITE))
        imported = self.import_like_autoplay(live)
        before = imported.turn
        resolved = namespace["_autoplay_resync_turn"](imported, tracked)
        self.assertEqual(resolved.turn, before)
        self.assertEqual(resolved.board_fen(), live.board_fen())

    def test_missing_previous_board_is_a_no_op(self):
        live = chess.Board()
        imported = self.import_like_autoplay(live)
        resolved = namespace["_autoplay_resync_turn"](imported, None)
        self.assertEqual(resolved.turn, imported.turn)


class RecoveryConstantsTests(unittest.TestCase):
    def test_recovery_waits_instead_of_spinning(self):
        self.assertGreater(namespace["AUTOPLAY_RESYNC_DELAY_SECONDS"], 0.0)
        self.assertGreater(
            namespace["AUTOPLAY_RESYNC_RETRY_SECONDS"],
            namespace["AUTOPLAY_RESYNC_DELAY_SECONDS"],
        )
        self.assertGreater(namespace["AUTOPLAY_SCAN_RETRY_LIMIT"], 1)

    def test_live_state_machine_has_no_give_up_on_mismatch(self):
        text = SOURCE.read_text()
        # The old dead ends must be gone for good.
        for forbidden in (
            "Auto-play stopped: the ChessBot board was changed manually",
            "Auto-play stopped: its background drag finished out of sync.",
            "Auto-play stopped before dragging: the live board no longer matches",
            "Auto-play could not resync after repeated full-board scans.",
        ):
            self.assertNotIn(forbidden, text)

    def test_resync_paths_are_wired(self):
        text = SOURCE.read_text()
        self.assertIn("def _autoplay_request_resync(", text)
        self.assertIn("def _autoplay_recoverable_failure(", text)
        self.assertIn("start_autoplay_scan(_autoplay.get(\"side\"), resume=True)", text)
        self.assertIn("_autoplay_wait_or_resync(", text)


class AskSideTests(unittest.TestCase):
    """Ask mode must raise the side question on every fresh start."""

    text = SOURCE.read_text()

    def test_fresh_starts_clear_the_retained_side(self):
        self.assertIn('"state": "choosing_window",\n        "side": ""', self.text)

    def test_restart_asks_instead_of_reusing_the_finished_side(self):
        self.assertIn("return start_autoplay_scan(None)", self.text)

    def test_scan_without_a_side_asks_in_ask_mode(self):
        self.assertIn('if not _answered_side(side) and saved_side == "ask":', self.text)
        self.assertIn("show_autoplay_side_dialog = True", self.text)

    def test_in_game_recovery_never_asks(self):
        self.assertIn("resume=True", self.text)
        self.assertIn('if not _answered_side(side) and resume:', self.text)


if __name__ == "__main__":
    unittest.main()


class TraceChangedSquaresTests(unittest.TestCase):
    """Criterion 2: diff the live board back to a legal line when possible."""

    def sequence(self, tracked, live):
        return namespace["_advisor_find_move_sequence"](
            tracked,
            namespace["_autoplay_board_grid_from_board"](live),
            max_plies=2,
        )

    def test_single_opponent_reply_is_traced(self):
        tracked = chess.Board()
        tracked.push_uci("e2e4")
        live = tracked.copy()
        live.push_uci("e7e5")
        self.assertEqual(
            [move.uci() for move in self.sequence(tracked, live)], ["e7e5"]
        )

    def test_premove_reply_is_traced_two_plies(self):
        tracked = chess.Board()
        tracked.push_uci("e2e4")
        live = tracked.copy()
        live.push_uci("e7e5")
        live.push_uci("g1f3")
        self.assertEqual(
            [move.uci() for move in self.sequence(tracked, live)],
            ["e7e5", "g1f3"],
        )

    def test_impossible_change_is_not_traced(self):
        tracked = chess.Board()
        live = chess.Board()
        live.remove_piece_at(chess.E1)
        live.set_piece_at(chess.A1, chess.Piece(chess.KING, chess.WHITE))
        self.assertIsNone(self.sequence(tracked, live))


class ResyncStateTests(unittest.TestCase):
    """The session must stay live (never 'stopped') across a recovery."""

    text = SOURCE.read_text()

    def test_request_resync_keeps_running(self):
        body = extract_function(self.text, "_autoplay_request_resync")
        self.assertIn('"running": True', body)
        self.assertIn('"state": "resync_wait"', body)
        self.assertNotIn("stop_autoplay", body)

    def test_recoverable_failure_retries_before_stopping(self):
        body = extract_function(self.text, "_autoplay_recoverable_failure")
        self.assertIn("AUTOPLAY_SCAN_RETRY_LIMIT", body)
        self.assertIn("_autoplay_request_resync", body)
        self.assertLess(
            body.index("_autoplay_request_resync"),
            body.index("stop_autoplay"),
        )

    def test_wait_or_resync_never_stops(self):
        body = extract_function(self.text, "_autoplay_wait_or_resync", indented=True)
        self.assertNotIn("stop_autoplay", body)

    def test_public_scan_has_a_resume_path(self):
        body = extract_function(self.text, "start_autoplay_scan")
        self.assertIn("resume", body)
        self.assertIn("_autoplay_resync_turn", self.text)


def extract_function(text, name, indented=False):
    """Return the source of one top-level (or 4-space nested) function."""
    lines = text.splitlines()
    prefix = "    def " if indented else "def "
    start = next(
        index for index, line in enumerate(lines) if line.startswith(prefix + name)
    )
    base = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped:
            body.append(line)
            continue
        if len(line) - len(line.lstrip()) <= base and not stripped.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)
