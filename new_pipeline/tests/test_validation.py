import unittest
from types import SimpleNamespace
from new_pipeline.backend.pipeline_core import calc_stats,check_constraints,ContentPipeline
from new_pipeline.backend.parallel_generation import ParallelGeneration
from generation_fixtures import output
from test_preprocessing import RAW,pipeline,parse_article,Chain
class ValidationTests(unittest.TestCase):
 def test_decimals(self):
  self.assertEqual(calc_stats('비율은 2.9%다. 다음이다.')['sentences'],2)
 def test_levels(self):
  for level in ('BEGINNER','INTERMEDIATE','ADVANCED'):
   self.assertTrue(all(check_constraints(calc_stats(output(level).split('\n\n',1)[1]),level).values()))
 def test_invalid_skips_quiz(self):
  class Fake:
   def run(self,**kw):return SimpleNamespace(error=None,passed_filter=True,content='제목: 제목\n\n짧다.',regen_count=2)
   def generate_quiz(self,**kw):raise AssertionError('Must not call quiz')
  result=ParallelGeneration(Fake()).run(pipeline().run(parse_article(RAW)))
  for row in result.values():
   self.assertEqual(row['status'],'validation_failed');self.assertEqual(row['quiz_result']['status'],'skipped')
 def test_regeneration_bounded(self):
  p=ContentPipeline.__new__(ContentPipeline);p.content_chain=Chain(SimpleNamespace(content='제목: 제목\n\n짧다.'))
  result=p.run('제목','원문','SOCIETY','BEGINNER',prepared=pipeline().run(parse_article(RAW)))
  self.assertEqual(p.content_chain.calls,3);self.assertEqual(result.regen_count,2)
 def test_identical_source_does_not_trigger_regeneration(self):
  p=ContentPipeline.__new__(ContentPipeline)
  generated=output('BEGINNER')
  p.content_chain=Chain(SimpleNamespace(content=generated))
  result=p.run('제목',generated,'SOCIETY','BEGINNER',prepared=pipeline().run(parse_article(RAW)))
  self.assertEqual(p.content_chain.calls,1)
  self.assertEqual(result.regen_count,0)
  self.assertFalse(hasattr(result,'similarity_score'))
