"""Thread-safe SQLite persistence for CLI runs; snapshots preserve source revisions."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

DEFAULT_DB = Path(__file__).resolve().parents[1] / 'data' / 'pipeline.db'

def encode(value):
    return json.dumps(value, ensure_ascii=False)

def now():
    return datetime.now(timezone.utc).isoformat()

class ResultStore:
    def __init__(self, path=DEFAULT_DB):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL, output_dir TEXT NOT NULL, summary_json TEXT
                );
                CREATE TABLE IF NOT EXISTS article_snapshots (
                    snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
                    item_index INTEGER NOT NULL, news_id TEXT, status TEXT NOT NULL,
                    raw_json TEXT NOT NULL, content_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    preprocessing_json TEXT NOT NULL, UNIQUE(run_id,item_index)
                );
                CREATE INDEX IF NOT EXISTS snapshot_news ON article_snapshots(news_id);
                CREATE TABLE IF NOT EXISTS generated_contents (
                    generation_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES article_snapshots(snapshot_id),
                    level TEXT NOT NULL, status TEXT NOT NULL, title TEXT, body TEXT,
                    result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(snapshot_id,level)
                );
                CREATE TABLE IF NOT EXISTS generated_quizzes (
                    generation_id TEXT PRIMARY KEY REFERENCES generated_contents(generation_id),
                    status TEXT NOT NULL, question TEXT, choices_json TEXT, answer_no INTEGER,
                    result_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=30)
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db: yield db
        finally: db.close()

    def start_run(self, output):
        run_id=str(uuid.uuid4())
        with self.connection() as db:
            db.execute('INSERT INTO pipeline_runs(run_id,started_at,status,output_dir) VALUES (?,?,?,?)',
                       (run_id,now(),'running',str(Path(output).resolve())))
        return run_id

    def finish_run(self, run_id, status, counts):
        with self.connection() as db:
            db.execute('UPDATE pipeline_runs SET status=?,finished_at=?,summary_json=? WHERE run_id=?',
                       (status,now(),encode(counts),run_id))

    def save_article(self, run_id, index, raw, result):
        snapshot_id=f'{run_id}:{index}'
        content=result.get('content',{})
        with self.connection() as db:
            db.execute('''INSERT INTO article_snapshots VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,item_index) DO UPDATE SET status=excluded.status,
                content_json=excluded.content_json,metadata_json=excluded.metadata_json,
                preprocessing_json=excluded.preprocessing_json''',
                (snapshot_id,run_id,index,content.get('news_id') or raw.get('NewsItemId') or None,
                 result['status'],encode(raw),encode(content),encode(result.get('metadata',{})),
                 encode({k:v for k,v in result.items() if k not in ('content','metadata','generation')})))
        return snapshot_id

    def save_generation(self, snapshot_id, row):
        with self.connection() as db:
            db.execute('INSERT INTO generated_contents VALUES (?,?,?,?,?,?,?,?)',
                       (row['generation_id'],snapshot_id,row['level'],row['status'],row.get('title'),row.get('text'),encode(row),now()))

    def save_quiz(self, row):
        quiz=row.get('quiz',{})
        with self.connection() as db:
            db.execute('INSERT INTO generated_quizzes VALUES (?,?,?,?,?,?,?)',
                       (row['generation_id'],row['status'],quiz.get('question'),encode(quiz.get('choices',[])),quiz.get('answerNo'),encode(row),now()))
