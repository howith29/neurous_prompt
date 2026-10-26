"""CLI preprocessing runner. SQLite results and structured logs; no web server."""
import argparse
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import os
from pathlib import Path
import sys
import uuid
import threading
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from .backend.preprocessing import parse_article, PreprocessingPipeline

ROOT = Path(__file__).resolve().parents[1]

def read_articles(path):
    text=Path(path).read_text(encoding='utf-8-sig')
    if Path(path).suffix.lower()=='.xml':
        root=ET.fromstring(text)
        for element in root.iter(): element.tag=element.tag.split('}')[-1]
        code=root.findtext('.//resultCode')
        if code is not None and code!='0': raise ValueError('API error response; not article data')
        rows=[{c.tag:''.join(c.itertext()) for c in row} for row in root.iter('NewsItem')]
    else:
        data=json.loads(text); rows=data if isinstance(data,list) else data.get('articles') if isinstance(data,dict) else None
    if not isinstance(rows,list) or not rows or any(not isinstance(r,dict) for r in rows):
        raise ValueError('Expected a non-empty NewsItem array')
    return rows

def execute(rows, output, pipeline=None, generator=None, db_path=None):
    from .backend.result_store import ResultStore
    # Library calls default to a local DB alongside their logs; CLI passes the shared DB.
    if Path(output).exists(): raise FileExistsError(output)
    store=ResultStore(db_path or Path(output).parent / 'pipeline.db')
    run_id=store.start_run(output)
    try:
        counts=_execute(rows,output,pipeline,generator,store,run_id)
    except BaseException:
        store.finish_run(run_id,'interrupted',{})
        raise
    failed=any(counts.get(k) for k in ('error','generation_failed_articles','quiz_failed_branches'))
    store.finish_run(run_id,'completed_with_errors' if failed else 'completed',counts)
    return counts


def _execute(rows, output, pipeline, generator, store, run_id):
    output=Path(output); output.mkdir(parents=True,exist_ok=False)
    counts=Counter()
    write_lock=threading.Lock()
    with (output/'events.jsonl').open('w',encoding='utf-8') as log, (output/'results.jsonl').open('w',encoding='utf-8') as results, (output/'generations.jsonl').open('w',encoding='utf-8') as generations, (output/'quizzes.jsonl').open('w',encoding='utf-8') as quizzes:
        def save_generation(row):
            with write_lock:
                store.save_generation(snapshot_id,row)
                generations.write(json.dumps(row,ensure_ascii=False)+'\n'); generations.flush()
        def save_quiz(row):
            with write_lock:
                store.save_quiz(row)
                quizzes.write(json.dumps(row,ensure_ascii=False)+'\n'); quizzes.flush()
        def emit(**event):
            event={'time':datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),**event}
            line=json.dumps(event,ensure_ascii=False)
            with write_lock:
                log.write(line+'\n');log.flush();print(line,flush=True)
        emit(stage='run',status='started',articles=len(rows),mode='generate' if generator else 'preprocess' if pipeline else 'parse-only')
        for index,raw in enumerate(rows,1):
            def event(**fields):emit(index=index,news_id=str(raw.get('NewsItemId') or ''),**fields)
            event(stage='parse',status='started')
            try:
                result=parse_article(raw)
                event(stage='parse',status=result['status'],errors=result['errors'])
                store.save_article(run_id,index,raw,result)
                if pipeline and result['status']=='parsed':result=pipeline.run(result,on_event=event)
            except Exception as exc:
                result={'status':'error','error_stage':'parse','news_id':str(raw.get('NewsItemId') or ''),'errors':[type(exc).__name__]}
                event(stage='parse',status='error',error_type=type(exc).__name__)
            snapshot_id=store.save_article(run_id,index,raw,result)
            if generator and result['status']=='ready':
                result['generation']=generator.run(result,on_event=event,on_result=save_generation,on_quiz=save_quiz)
                success=sum(r['status']=='success' for r in result['generation'].values())
                result['generation_status']='completed' if success==3 else 'partial_failure' if success else 'failed'
                if success<3: counts['generation_failed_articles']+=1
                quiz_success=sum(r['quiz_result']['status']=='success' for r in result['generation'].values())
                result['quiz_status']='completed' if quiz_success==3 else 'partial_failure' if quiz_success else 'failed'
                counts['quiz_failed_branches']+=sum(r['quiz_result']['status']=='error' for r in result['generation'].values())
            store.save_article(run_id,index,raw,result)
            results.write(json.dumps(result,ensure_ascii=False)+'\n');results.flush()
            counts[result['status']]+=1
            event(stage='article',status=result['status'])
        emit(stage='run',status='completed',counts=dict(counts))
    (output/'summary.json').write_text(json.dumps(dict(counts),ensure_ascii=False,indent=2),encoding='utf-8')
    return dict(counts)

def main():
    parser=argparse.ArgumentParser(description='정책뉴스 파싱·적합성·분류·사실 추출 (로그 출력)')
    parser.add_argument('input',type=Path,help='정책뉴스 API JSON/XML 파일')
    parser.add_argument('--parse-only',action='store_true',help='LLM 호출 없이 파싱만 확인')
    parser.add_argument('--preprocess-only',action='store_true',help='사실 추출까지만 실행')
    parser.add_argument('--limit',type=int,help='앞에서부터 처리할 기사 수')
    parser.add_argument('--db',type=Path,default=ROOT/'new_pipeline'/'data'/'pipeline.db',help='SQLite 결과 저장 경로')
    parser.add_argument('--output-dir',type=Path,help='새 실행 결과 폴더 (이미 존재하면 중단)')
    args=parser.parse_args()
    try:
        rows=read_articles(args.input)
        if args.limit is not None:
            if args.limit<1:raise ValueError('limit must be positive')
            rows=rows[:args.limit]
        pipeline=None
        generator=None
        if not args.parse_only:
            load_dotenv(ROOT/'.env')
            key=os.getenv('GOOGLE_API_KEY')
            if not key:raise ValueError('GOOGLE_API_KEY is missing')
            from .backend.pipeline_core import ContentPipeline, DEFAULT_GEMINI_MODEL
            from .backend.parallel_generation import ParallelGeneration
            content_pipeline=ContentPipeline(key,model=os.getenv('GEMINI_MODEL',DEFAULT_GEMINI_MODEL))
            pipeline=PreprocessingPipeline(content_pipeline.llm)
            if not args.preprocess_only: generator=ParallelGeneration(content_pipeline)
        run_id=datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
        output=args.output_dir or ROOT/'new_pipeline'/'logs'/run_id
        counts=execute(rows,output,pipeline,generator,db_path=args.db)
        print(f'결과 폴더: {output}')
        return 1 if counts.get('error') or counts.get('generation_failed_articles') or counts.get('quiz_failed_branches') else 0
    except Exception as exc:
        # Do not print provider exceptions or URLs which may contain credentials.
        print(f'실행 실패: {type(exc).__name__}. 입력 파일·키 설정·출력 폴더를 확인하세요.',file=sys.stderr)
        return 1

if __name__=='__main__':raise SystemExit(main())
