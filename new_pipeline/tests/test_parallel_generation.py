from generation_fixtures import output
import threading
import unittest
from types import SimpleNamespace
from new_pipeline.backend.parallel_generation import ParallelGeneration, LEVELS

class ParallelTests(unittest.TestCase):
    def test_parallel_and_failure_isolation(self):
        barrier=threading.Barrier(3,timeout=5)
        seen=[];saved=[]
        class Fake:
            def run(self,**kwargs):
                seen.append(kwargs['prepared']['facts'])
                barrier.wait()  # Fails if branches run sequentially.
                if kwargs['level']=='INTERMEDIATE':raise ValueError('failed')
                return SimpleNamespace(error=None,passed_filter=True,content=output(kwargs['level']),regen_count=0)
        prepared={'status':'ready','content':{'news_id':'1','title':'제목','text':'원문'},'classification':{'final_category':'SOCIETY'},'facts':'공통 사실'}
        result=ParallelGeneration(Fake()).run(prepared,on_result=saved.append)
        self.assertEqual(set(result),set(LEVELS))
        self.assertEqual(seen,['공통 사실']*3)
        self.assertEqual(len(saved),3)
        self.assertEqual(result['INTERMEDIATE']['status'],'error')
        self.assertEqual(result['BEGINNER']['status'],'success')
        self.assertEqual(result['ADVANCED']['status'],'success')
    def test_not_ready_skips(self):
        self.assertEqual(ParallelGeneration(None).run({'status':'filter_hold'}),{})

    def test_quiz_uses_each_generated_text_and_preserves_failed_quiz(self):
        from new_pipeline.backend.pipeline_core import QuizGen
        received={}; saved=[];quizzes=[]
        class Fake:
            def run(self,**kwargs):
                return SimpleNamespace(error=None,passed_filter=True,content=output(kwargs['level']),regen_count=0)
            def generate_quiz(self,**kwargs):
                received[kwargs['level']]=kwargs['content_text']
                if kwargs['level']=='ADVANCED':raise RuntimeError('provider failure')
                return SimpleNamespace(error=None,quiz=QuizGen(question='문제',choices=['첫째','둘째','셋째'],answerNo=2))
        prepared={'status':'ready','content':{'news_id':'1','title':'제목','text':'원문'},'classification':{'final_category':'SOCIETY'},'facts':'공통 사실'}
        result=ParallelGeneration(Fake()).run(prepared,on_result=lambda r:saved.append(dict(r)),on_quiz=quizzes.append)
        self.assertEqual(len(saved),3);self.assertEqual(len(quizzes),3)
        for level in LEVELS:
            self.assertEqual(received[level],output(level))
            self.assertEqual(result[level]['status'],'success')
            self.assertEqual(result[level]['generation_id'],result[level]['quiz_result']['generation_id'])
        self.assertEqual(result['ADVANCED']['quiz_result']['status'],'error')
        quiz = result['BEGINNER']['quiz_result']['quiz']
        self.assertEqual(quiz['choices'][quiz['answerNo']-1], '둘째')
        self.assertTrue(all('quiz_result' not in r for r in saved))
