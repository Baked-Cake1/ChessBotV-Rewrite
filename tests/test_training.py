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
                 tempfile=tempfile, time=time, TRAINING_DEPTH=40)
for node in source.body:
    if isinstance(node, ast.ClassDef) and node.name in ('TrainingStore', 'TrainingSession'):
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
        self.engine.info = self.info(40)
        self.session.tick(self.board, self.engine)
        self.assertEqual(self.session.db.execute('SELECT count(*) FROM frontier WHERE done=1').fetchone()[0], 1)
        self.assertEqual(self.session.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 40)
        self.session.stop(self.board, self.engine)
        self.session.start('auto', self.board, self.engine)
        self.assertEqual(self.session.db.execute('SELECT count(*) FROM frontier WHERE done=1').fetchone()[0], 1)
    def test_stale_info_and_corrupt_file(self):
        info = self.info(40)
        info['fen'] = 'wrong'
        self.assertFalse(self.store.save(self.board, info))
        self.store.path.write_text('{broken')
        broken = namespace['TrainingStore'](self.temp.name)
        self.assertIsNotNone(broken.error)
        with self.assertRaises(RuntimeError):
            broken.save(self.board, self.info(40))


if __name__ == '__main__':
    unittest.main()
