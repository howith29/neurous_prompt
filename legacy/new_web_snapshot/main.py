from pathlib import Path
# main.py
# FastAPI 엔트리포인트. 화면(Image1~5) <-> 엔드포인트 매핑은 README 참고.

import csv
import io
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import storage, batch_runner, file_parser, quiz_runner
from .pipeline_core import DEFAULT_GEMINI_MODEL
from .prompts import CATEGORY_LABELS, LEVEL_CONSTRAINTS

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

app = FastAPI(title="뉴로스 콘텐츠 생성 도구")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 로컬 1인 도구 스코프. 배포 시엔 좁혀야 함.
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_api_key() -> str:
    key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        raise HTTPException(500, "GOOGLE_API_KEY가 설정되지 않았습니다 (.env 확인)")
    return key


def get_model() -> str:
    return os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)


@app.on_event("startup")
def _startup():
    storage.init_db()


# ── 공통 메타 ─────────────────────────────────────────────────────────────────
@app.get("/api/meta")
def get_meta():
    return {
        "categories": [{"code": k, "label": v} for k, v in CATEGORY_LABELS.items()],
        "levels": [{"code": k, "label": v["label"]} for k, v in LEVEL_CONSTRAINTS.items()],
    }


# ── Source List (Image 1) ────────────────────────────────────────────────────
@app.get("/api/source")
def api_list_source(category: str | None = None):
    return storage.list_raw_news(category=category)


# ── 뉴스 기사 업로드 (Image 3) ────────────────────────────────────────────────
@app.post("/api/source/parse-file")
async def api_parse_file(file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = file_parser.parse_upload(file.filename, content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"rows": rows}


class ConfirmUploadRow(BaseModel):
    category_code: str
    title: str
    content: str
    source_url: str = ""


class ConfirmUploadBody(BaseModel):
    rows: list[ConfirmUploadRow]


@app.post("/api/source/confirm")
def api_confirm_upload(body: ConfirmUploadBody):
    if not body.rows:
        raise HTTPException(400, "선택된 기사가 없습니다.")
    invalid = [r for r in body.rows if r.category_code not in CATEGORY_LABELS]
    if invalid:
        raise HTTPException(400, f"카테고리 코드가 유효하지 않은 행이 {len(invalid)}건 있습니다.")
    items = [
        {"category": r.category_code, "title": r.title, "content": r.content,
         "source_url": r.source_url, "registered_via": "csv"}
        for r in body.rows
    ]
    saved = storage.save_raw_news_bulk(items)
    return {"saved": saved}


# ── 글 생성 (Image 4/5 초안) ──────────────────────────────────────────────────
class StartGenerationBody(BaseModel):
    article_ids: list[str]
    levels: list[str]                 # ["BEGINNER","INTERMEDIATE","ADVANCED"] 중 선택
    max_regen: int = 2
    similarity_threshold: float = 0.5
    max_concurrency: int = 3


@app.post("/api/generation/jobs")
def api_start_generation(body: StartGenerationBody):
    articles = storage.get_raw_news_by_ids(body.article_ids)
    if len(articles) != len(body.article_ids):
        raise HTTPException(400, "일부 기사를 찾을 수 없습니다.")
    if any((storage.get_preprocessing(a["id"]) or {}).get("status") != "ready" for a in articles):
        raise HTTPException(400, "파싱·적합성·분류·사실 추출을 완료한 기사만 생성할 수 있습니다.")
    invalid_levels = set(body.levels) - set(LEVEL_CONSTRAINTS.keys())
    if invalid_levels:
        raise HTTPException(400, f"유효하지 않은 난이도: {invalid_levels}")

    try:
        job_id = batch_runner.start_job(
            api_key=get_api_key(),
            model=get_model(),
            articles=articles,
            levels=body.levels,
            max_regen=body.max_regen,
            similarity_threshold=body.similarity_threshold,
            max_concurrency=body.max_concurrency,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"job_id": job_id, "total": len(articles) * len(body.levels)}


@app.get("/api/generation/jobs/{job_id}")
def api_get_job(job_id: str):
    job = batch_runner.get_job(job_id)
    if not job:
        raise HTTPException(404, "job을 찾을 수 없습니다.")
    return job.to_dict()


@app.post("/api/generation/jobs/{job_id}/stop")
def api_stop_job(job_id: str):
    ok = batch_runner.stop_job(job_id)
    if not ok:
        raise HTTPException(404, "job을 찾을 수 없습니다.")
    return {"stopped": True}


# ── Content List (Image 2) ───────────────────────────────────────────────────
@app.get("/api/content")
def api_list_content(category: str | None = None, level: str | None = None, status: str | None = None):
    return storage.list_content_results(category=category, level=level, status=status)


# ── 글 내보내기 (Image 5) ─────────────────────────────────────────────────────
class ExportBody(BaseModel):
    result_ids: list[str]


@app.post("/api/content/export")
def api_export(body: ExportBody):
    rows = storage.get_content_results_by_ids(body.result_ids)
    if not rows:
        raise HTTPException(400, "내보낼 콘텐츠를 선택하세요.")

    buf = io.StringIO()
    buf.write("\ufeff")  # BOM: 엑셀 한글 깨짐 방지 (utf-8-sig 동일 효과)
    writer = csv.DictWriter(buf, fieldnames=["title", "content_body", "content_category", "content_level"])
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "title": r["gen_title"],
            "content_body": r["content_body"],
            "content_category": r["content_category"],
            "content_level": r["content_level"],
        })
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=neurous_content.csv"},
    )


# ── 퀴즈 생성 (Content List 화면 버튼) ────────────────────────────────────────
class QuizGenerateBody(BaseModel):
    content_result_ids: list[str]


@app.post("/api/quiz/generate")
def api_generate_quiz(body: QuizGenerateBody):
    if not body.content_result_ids:
        raise HTTPException(400, "퀴즈를 생성할 콘텐츠를 선택하세요.")
    results = quiz_runner.generate_quizzes(
        api_key=get_api_key(),
        model=get_model(),
        content_result_ids=body.content_result_ids,
    )
    return {"results": results}


@app.get("/api/quiz")
def api_list_quiz():
    return storage.list_quizzes()


# Preprocessing-only endpoints: generation is an explicit subsequent action.
class PolicyImportBody(BaseModel):
    articles: list[dict]

@app.post("/api/preprocessing/parse")
def parse_policy_news(body: PolicyImportBody):
    from .preprocessing import parse_article
    return {"results": [storage.save_preprocessing(parse_article(raw)) for raw in body.articles]}

class PreprocessBody(BaseModel):
    article_ids: list[str]
    retry_errors: bool = False

@app.post("/api/preprocessing/run")
def run_preprocessing(body: PreprocessBody):
    from .preprocessing import PreprocessingPipeline
    from .pipeline_core import ContentPipeline
    parsed=[]
    for aid in dict.fromkeys(body.article_ids):
        result=storage.get_preprocessing(aid)
        if result is None: raise HTTPException(400, "정책뉴스 파싱 결과가 없는 기사입니다")
        parsed.append(result)
    pipeline=PreprocessingPipeline(ContentPipeline(get_api_key(),model=get_model()).llm)
    results=[]
    for result in parsed:
        if result['status']=='error' and body.retry_errors:
            result={k:v for k,v in result.items() if k in ('content','metadata')}
            result.update(status='parsed',errors=[])
        if result['status']=='parsed': result=pipeline.run(result)
        results.append(storage.save_preprocessing(result))
    return {"results":results}

@app.get("/api/preprocessing")
def list_preprocessing():
    return storage.list_preprocessing()

class CategoryReviewBody(BaseModel):
    category: str

@app.patch("/api/preprocessing/{news_id}/category")
def review_category(news_id: str, body: CategoryReviewBody):
    if body.category not in CATEGORY_LABELS: raise HTTPException(400,"유효하지 않은 카테고리")
    try: return storage.review_category(news_id,body.category)
    except ValueError as exc: raise HTTPException(400,str(exc))

# ── 프론트엔드 정적 파일 서빙 (대충 버전 — 별도 프론트 서버 없이 이걸로 로컬 실행) ──
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
