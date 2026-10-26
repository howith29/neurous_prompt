from generation_fixtures import output
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from new_pipeline.run import execute
from new_pipeline.backend.parallel_generation import ParallelGeneration
from new_pipeline.backend.pipeline_core import QuizGen
from test_preprocessing import RAW, pipeline

class StoreTests(unittest.TestCase):
    def test_full_run_and_source_history(self):
        class Fake:
            def run(self,**kw):
                return SimpleNamespace(error=None,passed_filter=True,content=output(kw['level']),regen_count=0)
            def generate_quiz(self,**kw):
                if kw['level']=='ADVANCED':raise RuntimeError('quiz failure')
                return SimpleNamespace(error=None,quiz=QuizGen(question='문제',choices=['가','나','다'],answerNo=1))
        with tempfile.TemporaryDirectory() as d,contextlib.redirect_stdout(io.StringIO()):
            db=Path(d)/'result.db'
            execute([RAW],Path(d)/'first',pipeline(),ParallelGeneration(Fake()),db_path=db)
            execute([{**RAW,'Title':'수정 제목'}],Path(d)/'second',db_path=db)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM article_snapshots').fetchone()[0],2)
                self.assertEqual(conn.execute('SELECT count(*) FROM generated_contents').fetchone()[0],3)
                self.assertEqual(conn.execute('SELECT count(*) FROM generated_quizzes').fetchone()[0],3)
                self.assertEqual(conn.execute("SELECT count(*) FROM generated_quizzes WHERE status='error'").fetchone()[0],1)
                self.assertEqual(conn.execute('SELECT count(*) FROM generated_quizzes q JOIN generated_contents c USING(generation_id) JOIN article_snapshots a USING(snapshot_id)').fetchone()[0],3)
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertIn('completed_with_errors',{r[0] for r in conn.execute('SELECT status FROM pipeline_runs')})
                first=json.loads(conn.execute('SELECT content_json FROM article_snapshots WHERE news_id=? ORDER BY rowid',('123',)).fetchone()[0])
                self.assertEqual(first['title'],RAW['Title'])
