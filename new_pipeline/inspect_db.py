"""Read-only SQLite inspection. No API calls."""
import argparse,json,sqlite3
from pathlib import Path

def main():
 p=argparse.ArgumentParser(description='파이프라인 DB 읽기 전용 조회')
 p.add_argument('--db',type=Path,default=Path(__file__).parent/'data/pipeline.db')
 p.add_argument('--run',help='실행 ID. 생략하면 최신 실행')
 p.add_argument('--news-id',help='기사 원문·메타·생성글·퀴즈 상세')
 args=p.parse_args()
 conn=sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
 if args.news_id:
  for row in conn.execute('SELECT * FROM article_snapshots WHERE news_id=? ORDER BY rowid DESC',(args.news_id,)):
   r=dict(row)
   for k in ('content_json','metadata_json','preprocessing_json'):r[k]=json.loads(r[k])
   r.pop('raw_json');r['generated']=[]
   for c in conn.execute('SELECT c.*,q.result_json AS quiz_json FROM generated_contents c LEFT JOIN generated_quizzes q USING(generation_id) WHERE snapshot_id=?',(r['snapshot_id'],)):
    v=dict(c)
    for k in ('result_json','quiz_json'):
     if v[k]:v[k]=json.loads(v[k])
    r['generated'].append(v)
   print(json.dumps(r,ensure_ascii=False,indent=2))
 else:
  runs=[dict(r) for r in conn.execute('SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 5')]
  print(json.dumps({'recent_runs':runs},ensure_ascii=False,indent=2))
  run=args.run or (runs[0]['run_id'] if runs else '')
  for name,sql in [('articles','SELECT status,count(*) AS count FROM article_snapshots WHERE run_id=? GROUP BY status'),('contents','SELECT c.level,c.status,count(*) AS count FROM generated_contents c JOIN article_snapshots a USING(snapshot_id) WHERE a.run_id=? GROUP BY c.level,c.status'),('quizzes','SELECT c.level,q.status,count(*) AS count FROM generated_quizzes q JOIN generated_contents c USING(generation_id) JOIN article_snapshots a USING(snapshot_id) WHERE a.run_id=? GROUP BY c.level,q.status')]:
   print(json.dumps({name:[dict(r) for r in conn.execute(sql,(run,))]},ensure_ascii=False,indent=2))
 conn.close()
if __name__=='__main__':main()
