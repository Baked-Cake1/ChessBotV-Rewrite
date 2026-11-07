"""Training core tests without importing the Windows/SDL application."""
import ast
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import chess

source = ast.parse((Path(__file__).resolve().parents[1] / 'new.py').read_text())
namespace = dict(chess=chess, Path=Path, json=json, os=os, sqlite3=sqlite3,
                 tempfile=tempfile, time=time)
for node in source.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TRAINING_DEPTH"
            for target in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), "new.py", "exec"), namespace)
for node in source.body:
    if isinstance(node, ast.ClassDef) and node.name in ('TrainingStore', 'TrainingSession', 'EngineThread'):
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'new.py', 'exec'), namespace)


class Engine:
    engine_active = True
    training_depth = None
    def __init__(self):
        self.lock = threading.Lock()
        self.info = {}
    def recalculate_current(self):
        pass
    def update_position(self, board):
        self.info = {'fen': board.fen(), 'depth': 0, 'pv': []}


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = namespace['TrainingStore'](self.temp.name)
        namespace['training_store'] = self.store
        self.session = namespace['TrainingSession']()
        self.board = chess.Board()
        self.engine = Engine()
    def tearDown(self):
        if self.session.db:
            self.session.db.close()
        self.temp.cleanup()
    def info(self, depth):
        return dict(fen=self.board.fen(), depth=depth,
                    pv=[next(iter(self.board.legal_moves))], score=23, mate=None)
    def test_save_merge_and_no_downgrade(self):
        self.assertTrue(self.store.save(self.board, self.info(40)))
        self.store.save(self.board, self.info(12))
        self.assertEqual(self.store.lookup(self.board)['depth'], 40)
        self.board.push_uci('e2e4')
        self.store.save(self.board, self.info(15))
        reopened = namespace['TrainingStore'](self.temp.name)
        self.assertEqual(len(reopened.records), 2)
        self.assertEqual(reopened.lookup(self.board)['side_to_move'], 'black')
    def test_manual_wait_skip_and_cached(self):
        self.session.start('manual', self.board, self.engine)
        self.session.tick(self.board, self.engine)
        self.assertTrue(self.session.blocked())
        self.engine.info = self.info(18)
        self.session.skip(self.board, self.engine)
        self.assertFalse(self.session.blocked())
        self.assertEqual(self.store.lookup(self.board)['depth'], 18)
        self.store.save(self.board, self.info(40))
        self.session.fen = None
        self.session.tick(self.board, self.engine)
        self.assertIn('already done', self.session.status)
    def test_auto_frontier_and_resume(self):
        self.session.start('auto', self.board, self.engine)
        self.assertEqual(self.session.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 20)
        self.session.tick(self.board, self.engine)
        self.assertEqual(self.board.turn, chess.BLACK)
        self.engine.info = self.info(35)
        self.session.tick(self.board, self.engine)
        self.assertEqual(self.session.db.execute('SELECT count(*) FROM frontier WHERE done=1').fetchone()[0], 1)
        self.assertEqual(self.session.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 40)
        self.session.stop(self.board, self.engine)
        self.session.start('auto', self.board, self.engine)
        self.assertEqual(self.session.db.execute('SELECT count(*) FROM frontier WHERE done=1').fetchone()[0], 1)
    def test_training_target_and_completion_boundary(self):
        self.assertEqual(namespace['TRAINING_DEPTH'], 35)
        self.session.start('manual', self.board, self.engine)
        self.assertEqual(self.engine.training_depth, 35)
        self.engine.info = self.info(34)
        self.session.tick(self.board, self.engine)
        self.assertTrue(self.session.blocked())
        self.assertIn('34/35', self.session.status)
        self.engine.info = self.info(35)
        self.session.tick(self.board, self.engine)
        self.assertFalse(self.session.blocked())
        self.assertEqual(self.store.lookup(self.board)['depth'], 35)
        self.assertIn('Saved depth 35', self.session.status)
        self.session.fen = None
        self.session.tick(self.board, self.engine)
        self.assertIn('already done', self.session.status)

    def test_stale_info_and_corrupt_file(self):
        info = self.info(40)
        info['fen'] = 'wrong'
        self.assertFalse(self.store.save(self.board, info))
        self.store.path.write_text('{broken')
        broken = namespace['TrainingStore'](self.temp.name)
        self.assertIsNotNone(broken.error)
        with self.assertRaises(RuntimeError):
            broken.save(self.board, self.info(40))


class EngineDepthTests(unittest.TestCase):
    def setUp(self):
        self.engine = namespace['EngineThread'].__new__(namespace['EngineThread'])
        self.engine.lock = threading.Lock()
        self.engine.search_token = 1
        self.engine.board = chess.Board()
        self.engine._reset_info(token=1)

    def parse(self, line, token=1):
        self.engine._parse_info_line(line, True, token=token)

    def test_secondary_depth_cannot_lower_or_raise_primary(self):
        self.parse('info depth 38 multipv 1 score cp 25 pv e2e4')
        self.parse('info depth 35 multipv 2 score cp 12 pv d2d4')
        self.assertEqual(self.engine.info['depth'], 38)
        self.parse('info depth 40 multipv 2 score cp 12 pv d2d4')
        self.assertEqual(self.engine.info['depth'], 38)
        self.assertEqual(self.engine.info['pv'][0].uci(), 'e2e4')
        self.assertEqual(self.engine.info['lines'][2]['depth'], 40)
        self.parse('info depth 36 multipv 2 score cp 1 pv c2c4')
        self.assertEqual(self.engine.info['lines'][2]['depth'], 40)

    def test_primary_result_never_downgrades_and_updates_atomically(self):
        self.parse('info depth 38 score cp 25 pv e2e4')
        self.parse('info depth 35 score cp 2 pv d2d4')
        self.assertEqual(self.engine.info['depth'], 38)
        self.assertEqual(self.engine.info['score'], 25)
        self.assertEqual(self.engine.info['pv'][0].uci(), 'e2e4')
        self.parse('info depth 40 score cp 30 pv d2d4')
        self.assertEqual(self.engine.info['depth'], 40)
        self.assertEqual(self.engine.info['score'], 30)
        self.assertEqual(self.engine.info['pv'][0].uci(), 'd2d4')
        self.assertEqual(self.engine.info['lines'][1]['depth'], 40)

    def test_progress_and_bound_messages_do_not_certify_old_pv(self):
        self.parse('info depth 38 score cp 25 pv e2e4')
        self.parse('info depth 40 currmove d2d4 nps 1500')
        self.parse('info depth 40 score cp 100 lowerbound pv d2d4')
        self.parse('info depth 40 score cp -50 upperbound pv d2d4')
        self.assertEqual(self.engine.info['depth'], 38)
        self.assertEqual(self.engine.info['score'], 25)
        self.assertEqual(self.engine.info['nps'], 1500)

    def test_cached_result_is_preserved_during_shallow_research(self):
        self.engine.info.update(depth=38, score=25, pv=[chess.Move.from_uci('e2e4')])
        self.parse('info depth 1 score cp 5 pv d2d4')
        self.assertEqual(self.engine.info['depth'], 38)
        self.assertEqual(self.engine.info['pv'][0].uci(), 'e2e4')
        self.parse('info depth 39 score cp 35 pv d2d4')
        self.assertEqual(self.engine.info['depth'], 39)

    def test_stale_tokens_ignored_and_new_position_can_reset_depth(self):
        self.parse('info depth 38 score cp 25 pv e2e4')
        self.parse('info depth 40 score cp 1 pv d2d4', token=0)
        self.assertEqual(self.engine.info['depth'], 38)
        self.engine.search_token = 2
        self.engine.board.push_uci('e2e4')
        self.engine._reset_info(token=2)
        self.parse('info depth 1 score cp 0 pv e7e5', token=2)
        self.assertEqual(self.engine.info['depth'], 1)

    def test_manual_does_not_finish_on_secondary_or_progress_depth_35(self):
        with tempfile.TemporaryDirectory() as directory:
            store = namespace['TrainingStore'](directory)
            namespace['training_store'] = store
            session = namespace['TrainingSession']()
            session.mode = 'manual'
            session.tick(self.engine.board, self.engine)
            self.parse('info depth 34 multipv 1 score cp 25 pv e2e4')
            self.parse('info depth 35 multipv 2 score cp 10 pv d2d4')
            self.parse('info depth 35 currmove e2e4')
            session.tick(self.engine.board, self.engine)
            self.assertTrue(session.blocked())
            self.assertFalse(store.records)
            session.skip(self.engine.board, self.engine)
            self.assertEqual(store.lookup(self.engine.board)['depth'], 34)
            session.ready = False
            self.parse('info depth 35 multipv 1 score cp 28 pv e2e4')
            session.tick(self.engine.board, self.engine)
            self.assertFalse(session.blocked())
            self.assertEqual(store.lookup(self.engine.board)['depth'], 35)


if __name__ == '__main__':
    unittest.main()
