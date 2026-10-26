# storage.py
# 로컬 1인 사용 도구 스코프라 SQLite + 표준 sqlite3 모듈만 사용 (ORM 없음).

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "data.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_news (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_url TEXT,
                registered_via TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content_results (
                id TEXT PRIMARY KEY,
                source_article_id TEXT NOT NULL,
                source_title TEXT NOT NULL,
                gen_title TEXT,
                content_body TEXT,
                content_category TEXT NOT NULL,
                content_level TEXT NOT NULL,
                status TEXT NOT NULL,           -- success | similarity_failed | filter_rejected | error
                fail_reason TEXT,
                similarity_score REAL,
                regen_count INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_article_id) REFERENCES raw_news(id)
            )
        """)
        # 퀴즈 문제 — 기사(글) 당 1개만 생성. content_id는 content_results.id를 참조하며,
        # 여러 난이도 결과 중 중급(INTERMEDIATE) 우선 -> 없으면 성공한 다른 레벨을 골라 그 콘텐츠로 생성한다.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quiz (
                quiz_id TEXT PRIMARY KEY,
                content_id TEXT NOT NULL,
                quiz_category TEXT NOT NULL,
                quiz_question TEXT NOT NULL,
                quiz_diff TEXT NOT NULL,        -- 생성에 쓰인 콘텐츠의 난이도 (BEGINNER|INTERMEDIATE|ADVANCED)
                created_at TEXT NOT NULL,
                FOREIGN KEY (content_id) REFERENCES content_results(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quiz_choice (
                quiz_choice_id TEXT PRIMARY KEY,
                quiz_id TEXT NOT NULL,
                choice_no INTEGER NOT NULL,     -- 1~3
                choice_text TEXT NOT NULL,
                is_correct INTEGER NOT NULL,    -- 0 | 1
                FOREIGN KEY (quiz_id) REFERENCES quiz(quiz_id)
            )
        """)




# ── Raw News (Source List) ───────────────────────────────────────────────────
def save_raw_news_bulk(items: list[dict]) -> list[dict]:
    """items: [{category, title, content, source_url}, ...]"""
    saved = []
    with get_conn() as conn:
        for it in items:
            row = {
                "id": str(uuid.uuid4()),
                "category": it["category"],
                "title": it["title"],
                "content": it["content"],
                "source_url": it.get("source_url", ""),
                "registered_via": it.get("registered_via", "csv"),
                "created_at": _now(),
            }
            conn.execute(
                "INSERT INTO raw_news (id, category, title, content, source_url, registered_via, created_at) "
                "VALUES (:id, :category, :title, :content, :source_url, :registered_via, :created_at)",
                row,
            )
            saved.append(row)
    return saved


def list_raw_news(category: str | None = None) -> list[dict]:
    with get_conn() as conn:
        q = """
            SELECT r.*,
                   EXISTS(SELECT 1 FROM content_results c WHERE c.source_article_id = r.id) AS is_generated
            FROM raw_news r
        """
        params = []
        if category:
            q += " WHERE r.category = ?"
            params.append(category)
        q += " ORDER BY r.created_at DESC"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


def get_raw_news_by_ids(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT * FROM raw_news WHERE id IN ({placeholders})", ids).fetchall()
        return [dict(r) for r in rows]


# ── Content Results (Content List) ───────────────────────────────────────────
def save_content_result(row: dict) -> dict:
    """row: source_article_id, source_title, gen_title, content_body, content_category,
             content_level, status, fail_reason, similarity_score, regen_count"""
    row = dict(row)
    row["id"] = str(uuid.uuid4())
    row["created_at"] = _now()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO content_results
                (id, source_article_id, source_title, gen_title, content_body, content_category,
                 content_level, status, fail_reason, similarity_score, regen_count, created_at)
            VALUES
                (:id, :source_article_id, :source_title, :gen_title, :content_body, :content_category,
                 :content_level, :status, :fail_reason, :similarity_score, :regen_count, :created_at)
        """, row)
    return row


def list_content_results(category: str | None = None, level: str | None = None,
                          status: str | None = None) -> list[dict]:
    with get_conn() as conn:
        q = "SELECT * FROM content_results WHERE 1=1"
        params = []
        if category:
            q += " AND content_category = ?"
            params.append(category)
        if level:
            q += " AND content_level = ?"
            params.append(level)
        if status:
            q += " AND status = ?"
            params.append(status)
        q += " ORDER BY created_at DESC"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


def get_content_results_by_ids(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT * FROM content_results WHERE id IN ({placeholders})", ids).fetchall()
        return [dict(r) for r in rows]


def get_content_results_by_article_ids(article_ids: list[str]) -> list[dict]:
    """여러 기사(raw_news.id)에 대해 생성된 content_results 전체(레벨 무관)를 조회."""
    if not article_ids:
        return []
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in article_ids)
        rows = conn.execute(
            f"SELECT * FROM content_results WHERE source_article_id IN ({placeholders})", article_ids
        ).fetchall()
        return [dict(r) for r in rows]


# ── Quiz ──────────────────────────────────────────────────────────────────────
def pick_quiz_source_content(source_article_id: str) -> dict | None:
    """기사 1건에 대해 퀴즈 생성에 쓸 content_results 1건을 고른다.
    중급(INTERMEDIATE) 성공 결과 우선, 없으면 성공한 다른 레벨 중 먼저 생성된 것."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM content_results WHERE source_article_id = ? AND status = 'success' "
            "ORDER BY CASE WHEN content_level = 'INTERMEDIATE' THEN 0 ELSE 1 END, created_at ASC",
            (source_article_id,),
        ).fetchall()
        return dict(rows[0]) if rows else None


def get_quiz_by_source_article(source_article_id: str) -> dict | None:
    """해당 기사에 이미 생성된 퀴즈가 있는지 확인 (글 당 퀴즈 1개 제약)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT q.* FROM quiz q JOIN content_results c ON q.content_id = c.id "
            "WHERE c.source_article_id = ? LIMIT 1",
            (source_article_id,),
        ).fetchone()
        return dict(row) if row else None


def save_quiz(content_id: str, quiz_category: str, quiz_question: str, quiz_diff: str,
              choices: list[dict]) -> dict:
    """choices: [{choice_no, choice_text, is_correct}, ...] (3개)"""
    quiz_id = str(uuid.uuid4())
    created_at = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO quiz (quiz_id, content_id, quiz_category, quiz_question, quiz_diff, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (quiz_id, content_id, quiz_category, quiz_question, quiz_diff, created_at),
        )
        choice_rows = []
        for c in choices:
            row = {
                "quiz_choice_id": str(uuid.uuid4()),
                "quiz_id": quiz_id,
                "choice_no": c["choice_no"],
                "choice_text": c["choice_text"],
                "is_correct": 1 if c["is_correct"] else 0,
            }
            conn.execute(
                "INSERT INTO quiz_choice (quiz_choice_id, quiz_id, choice_no, choice_text, is_correct) "
                "VALUES (:quiz_choice_id, :quiz_id, :choice_no, :choice_text, :is_correct)",
                row,
            )
            choice_rows.append(row)
    return {
        "quiz_id": quiz_id, "content_id": content_id, "quiz_category": quiz_category,
        "quiz_question": quiz_question, "quiz_diff": quiz_diff, "created_at": created_at,
        "choices": choice_rows,
    }


def list_quizzes() -> list[dict]:
    """생성된 퀴즈 전체를 원문/생성 콘텐츠 정보 + 보기와 함께 조회."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT q.quiz_id, q.content_id, q.quiz_category, q.quiz_question, q.quiz_diff, q.created_at,
                   c.source_article_id, c.source_title, c.gen_title
            FROM quiz q
            JOIN content_results c ON q.content_id = c.id
            ORDER BY q.created_at DESC
        """).fetchall()
        quizzes = [dict(r) for r in rows]
        if not quizzes:
            return []
        ids = [q["quiz_id"] for q in quizzes]
        placeholders = ",".join("?" for _ in ids)
        choice_rows = conn.execute(
            f"SELECT * FROM quiz_choice WHERE quiz_id IN ({placeholders}) ORDER BY choice_no ASC",
            ids,
        ).fetchall()
        by_quiz: dict[str, list[dict]] = {}
        for cr in choice_rows:
            by_quiz.setdefault(cr["quiz_id"], []).append(dict(cr))
        for q in quizzes:
            q["choices"] = by_quiz.get(q["quiz_id"], [])
        return quizzes
