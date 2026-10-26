import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from new_pipeline.backend.preprocessing import parse_article, PreprocessingPipeline, Scores, FilterResult, numbered_facts
from new_pipeline.backend import storage

RAW=dict(NewsItemId='123',Title='제목',DataContents='<p>첫 문장 &amp; 내용.</p><script>bad()</script><p>둘째 문장.</p>',ContentsType='H',ModifyDate='10/02/2026 17:47:23',OriginalUrl='https://www.korea.kr/example',KoglType='1')
class Chain:
    def __init__(self,output): self.output=output;self.calls=0
    def invoke(self,args):self.calls+=1;return self.output

def pipeline(decision='PASS'):
    p=PreprocessingPipeline.__new__(PreprocessingPipeline)
    p.filter_chain=Chain(FilterResult(decision=decision,content_kind='ARTICLE',reason='사유',reason_codes=[] if decision=='PASS' else ['OPINION'],evidence=[] if decision=='PASS' else ['첫 문장 & 내용.']))
    p.classification_chain=Chain(Scores(**dict(zip(['IT_SCIENCE','LIFE_CULTURE','POLITICS','SOCIETY','ECONOMY','WORLD'],[5,10,90,20,40,80]))))
    p.fact_chain=Chain(SimpleNamespace(content='1. 첫 사실.\n2. 둘째 사실.\n3. 셋째 사실.'))
    return p

class Tests(unittest.TestCase):
    def test_parse(self):
        r=parse_article(RAW);self.assertEqual(r['status'],'parsed');self.assertEqual(r['content']['text'],'첫 문장 & 내용.\n\n둘째 문장.');self.assertEqual(r['metadata']['news_id'],'123')
    def test_plain_and_missing(self):
        r=parse_article({**RAW,'DataContents':'1 < 2','ContentsType':'T'});self.assertEqual(r['content']['text'],'1 < 2')
        r=parse_article({**RAW,'ModifyDate':'bad','KoglType':''});self.assertEqual(r['status'],'parse_hold')
        p=pipeline();p.run(r);self.assertEqual(p.filter_chain.calls,0)
    def test_filter_stops(self):
        for decision in ('REJECT','HOLD'):
            p=pipeline(decision);r=p.run(parse_article(RAW));self.assertNotEqual(r['status'],'ready');self.assertEqual(p.classification_chain.calls,0)
    def test_top_two(self):
        r=pipeline().run(parse_article(RAW));self.assertEqual(r['status'],'ready');c=r['classification'];self.assertEqual(c['primary']['category'],'POLITICS');self.assertEqual(c['secondary']['category'],'WORLD');self.assertNotIn('scores',c)
    def test_insufficient_and_invalid_facts(self):
        p=pipeline();p.fact_chain.output.content='FACTS_INSUFFICIENT';self.assertEqual(p.run(parse_article(RAW))['status'],'facts_insufficient')
        with self.assertRaises(ValueError):numbered_facts('1. 하나')
    def test_generation_reuses_prepared_facts(self):
        from new_pipeline.backend.pipeline_core import ContentPipeline
        p=ContentPipeline.__new__(ContentPipeline)
        from generation_fixtures import output
        p.content_chain=Chain(SimpleNamespace(content=output('BEGINNER')))
        prepared=pipeline().run(parse_article(RAW))
        for level in ('BEGINNER','INTERMEDIATE','ADVANCED'):
            p.content_chain.output.content=output(level)
            result=p.run('제목','원문','IT_SCIENCE',level,prepared=prepared)
            self.assertTrue(result.passed_filter)
            self.assertEqual(result.facts,prepared['facts'])
        self.assertEqual(p.content_chain.calls,3)
        blocked=p.run('제목','원문','IT_SCIENCE','BEGINNER')
        self.assertFalse(blocked.passed_filter)

    def test_storage_review(self):
        old=storage.DB_PATH
        with tempfile.TemporaryDirectory() as d:
            try:
                storage.DB_PATH=Path(d)/'test.db';storage.init_db();storage.init_preprocessing_db()
                result=pipeline().run(parse_article(RAW));saved=storage.save_preprocessing(result)
                storage.save_preprocessing(result);self.assertEqual(len(storage.list_preprocessing()),1)
                storage.review_category('123','SOCIETY');r=storage.get_preprocessing(saved['source_article_id']);self.assertEqual(r['classification']['final_category'],'SOCIETY');self.assertEqual(r['classification']['primary']['category'],'POLITICS')
            finally:storage.DB_PATH=old
if __name__=='__main__':unittest.main()
