from generation_fixtures import output
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from new_pipeline.run import execute, read_articles
from test_preprocessing import RAW, pipeline

class RunnerTests(unittest.TestCase):
    def test_logs_and_top_two(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            out=Path(d)/'run'
            counts=execute([RAW],out,pipeline())
            self.assertEqual(counts,{'ready':1})
            events=[json.loads(s) for s in (out/'events.jsonl').read_text().splitlines()]
            self.assertEqual({e['stage'] for e in events},{'run','parse','filter','classification','facts','article'})
            self.assertNotIn('"scores"',(out/'results.jsonl').read_text())
            with self.assertRaises(FileExistsError):execute([RAW],out)
    def test_input_and_continue(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            source=Path(d)/'a.xml';source.write_text('<response><header><resultCode>98</resultCode></header></response>')
            with self.assertRaises(ValueError):read_articles(source)
            counts=execute([{},RAW],Path(d)/'out')
            self.assertEqual(counts,{'parse_hold':1,'parsed':1})

    def test_generation_and_quiz_files_link(self):
        from types import SimpleNamespace
        from new_pipeline.backend.parallel_generation import ParallelGeneration
        from new_pipeline.backend.pipeline_core import QuizGen
        class Fake:
            def run(self,**kwargs):
                return SimpleNamespace(error=None,passed_filter=True,content=output(kwargs['level']),regen_count=0)
            def generate_quiz(self,**kwargs):
                return SimpleNamespace(error=None,quiz=QuizGen(question='문제',choices=['가','나','다'],answerNo=1))
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            out=Path(d)/'run'
            execute([RAW],out,pipeline(),ParallelGeneration(Fake()))
            articles=[json.loads(s) for s in (out/'generations.jsonl').read_text().splitlines()]
            quizzes=[json.loads(s) for s in (out/'quizzes.jsonl').read_text().splitlines()]
            self.assertEqual(len(articles),3);self.assertEqual(len(quizzes),3)
            self.assertEqual({r['generation_id'] for r in articles},{r['generation_id'] for r in quizzes})
            final=json.loads((out/'results.jsonl').read_text())
            self.assertEqual(final['quiz_status'],'completed')
