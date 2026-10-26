from pathlib import Path
# import_and_generate.py
# 뉴스 원본 파일(CSV/XLSX) 업로드 -> 초급/중급/고급 3개 레벨 전부 생성 -> 기사 당 퀴즈 1개 생성
# -> 콘텐츠 CSV(레벨을 컬럼으로 피벗) + 퀴즈 CSV 로 내보내는 일회성 배치 스크립트.
#
# 웹 UI(Image1~5)를 거치지 않고 대량 원본 파일을 한 번에 처리할 때 사용한다.
# 기존 배치 로직(batch_runner/quiz_runner)을 그대로 재사용하고, 결과만 CSV로 다르게 내보낸다.
#
# 사용법:
#   python -m backend.import_and_generate "Neurous News Raw.xlsx"
#   python -m backend.import_and_generate "Neurous News Raw.xlsx" --out-dir exports

import argparse
import csv
import os
import time

from dotenv import load_dotenv

from . import storage, batch_runner, file_parser, quiz_runner
from .pipeline_core import DEFAULT_GEMINI_MODEL
from .prompts import LEVEL_CONSTRAINTS, CATEGORY_LABELS

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

LEVELS = ["BEGINNER", "INTERMEDIATE", "ADVANCED"]


def get_api_key() -> str:
    key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        raise SystemExit("GOOGLE_API_KEY가 .env에 설정되어 있지 않습니다.")
    return key


def get_model() -> str:
    return os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)


def import_rows(path: str) -> list[dict]:
    with open(path, "rb") as f:
        file_bytes = f.read()
    rows = file_parser.parse_upload(os.path.basename(path), file_bytes)
    invalid = [r for r in rows if not r["category_valid"]]
    if invalid:
        print(f"경고: 카테고리 매칭 실패로 제외된 행 {len(invalid)}건")
        for r in invalid[:10]:
            print(f"  - row {r['row_idx']}: '{r['category_label']}' ({r['title'][:30]})")

    valid_rows = [r for r in rows if r["category_valid"]]
    items = [
        {"category": r["category_code"], "title": r["title"], "content": r["content"],
         "source_url": r["source_url"], "registered_via": "xlsx"}
        for r in valid_rows
    ]
    saved = storage.save_raw_news_bulk(items)
    print(f"원본 기사 저장 완료: {len(saved)}건")
    return saved


def run_generation(articles: list[dict], max_regen: int, similarity_threshold: float, max_concurrency: int):
    job_id = batch_runner.start_job(
        api_key=get_api_key(), model=get_model(), articles=articles, levels=LEVELS,
        max_regen=max_regen, similarity_threshold=similarity_threshold, max_concurrency=max_concurrency,
    )
    total = len(articles) * len(LEVELS)
    print(f"글 생성 시작 (기사 {len(articles)}건 x 3레벨 = {total}건)...")
    job = batch_runner.get_job(job_id)
    while job.status == "running":
        print(f"\r  진행률 {job.done}/{job.total}", end="", flush=True)
        time.sleep(2)
        job = batch_runner.get_job(job_id)
    print(f"\r  진행률 {job.done}/{job.total} (상태: {job.status})")
    for line in job.logs:
        print("  -", line)
    return job


def run_quiz(article_ids: list[str], max_concurrency: int):
    content_rows = storage.get_content_results_by_article_ids(article_ids)
    print(f"퀴즈 생성 시작 (기사 {len(article_ids)}건, 중급 우선 -> 없으면 성공한 다른 레벨)...")
    results = quiz_runner.generate_quizzes(
        api_key=get_api_key(), model=get_model(),
        content_result_ids=[r["id"] for r in content_rows],
        max_concurrency=max_concurrency,
    )
    for r in results:
        print("  -", r)
    return results


def export_content_csv(article_ids: list[str], out_path: str):
    """레벨(초급/중급/고급)을 row가 아니라 column으로 피벗해서 기사 당 1행으로 내보낸다."""
    content_rows = storage.get_content_results_by_article_ids(article_ids)
    by_article: dict[str, dict[str, list[dict]]] = {}
    for r in content_rows:
        by_article.setdefault(r["source_article_id"], {}).setdefault(r["content_level"], []).append(r)
    raw_map = {a["id"]: a for a in storage.get_raw_news_by_ids(article_ids)}

    fieldnames = ["source_title", "content_category", "source_url"]
    for lvl in LEVELS:
        label = LEVEL_CONSTRAINTS[lvl]["label"]
        fieldnames += [f"{label}_제목", f"{label}_본문", f"{label}_상태"]

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:  # BOM: 엑셀 한글 깨짐 방지
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for aid in article_ids:
            raw = raw_map.get(aid, {})
            row = {
                "source_title": raw.get("title", ""),
                "content_category": CATEGORY_LABELS.get(raw.get("category", ""), raw.get("category", "")),
                "source_url": raw.get("source_url", ""),
            }
            for lvl in LEVELS:
                label = LEVEL_CONSTRAINTS[lvl]["label"]
                candidates = sorted(by_article.get(aid, {}).get(lvl, []), key=lambda r: r["created_at"])
                latest = candidates[-1] if candidates else None
                row[f"{label}_제목"] = latest["gen_title"] if latest else ""
                row[f"{label}_본문"] = latest["content_body"] if latest else ""
                row[f"{label}_상태"] = latest["status"] if latest else "미생성"
            writer.writerow(row)
    print(f"콘텐츠 CSV 저장: {out_path}")


def export_quiz_csv(article_ids: list[str], out_path: str):
    quizzes = [q for q in storage.list_quizzes() if q["source_article_id"] in set(article_ids)]
    fieldnames = ["source_title", "quiz_category", "quiz_diff", "quiz_question",
                  "choice_1", "choice_2", "choice_3", "answer_no"]

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for q in quizzes:
            choices = sorted(q["choices"], key=lambda c: c["choice_no"])
            row = {
                "source_title": q["source_title"],
                "quiz_category": CATEGORY_LABELS.get(q["quiz_category"], q["quiz_category"]),
                "quiz_diff": LEVEL_CONSTRAINTS.get(q["quiz_diff"], {}).get("label", q["quiz_diff"]),
                "quiz_question": q["quiz_question"],
                "answer_no": next((c["choice_no"] for c in choices if c["is_correct"]), ""),
            }
            for c in choices:
                row[f"choice_{c['choice_no']}"] = c["choice_text"]
            writer.writerow(row)
    print(f"퀴즈 CSV 저장: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="뉴스 원본 업로드 -> 3레벨 생성 -> 퀴즈 생성 -> CSV 내보내기")
    parser.add_argument("input_path", help="CSV/XLSX 원본 뉴스 파일 경로")
    parser.add_argument("--out-dir", default=".", help="CSV 출력 디렉토리 (기본: 현재 디렉토리)")
    parser.add_argument("--max-regen", type=int, default=2)
    parser.add_argument("--similarity-threshold", type=float, default=0.5)
    parser.add_argument("--max-concurrency", type=int, default=3)
    args = parser.parse_args()

    storage.init_db()
    saved_articles = import_rows(args.input_path)
    if not saved_articles:
        raise SystemExit("저장된 기사가 없습니다 (카테고리 매칭 실패 등으로 전부 제외됨).")
    article_ids = [a["id"] for a in saved_articles]

    run_generation(saved_articles, args.max_regen, args.similarity_threshold, args.max_concurrency)
    run_quiz(article_ids, args.max_concurrency)

    os.makedirs(args.out_dir, exist_ok=True)
    export_content_csv(article_ids, os.path.join(args.out_dir, "neurous_content.csv"))
    export_quiz_csv(article_ids, os.path.join(args.out_dir, "neurous_quiz.csv"))


if __name__ == "__main__":
    main()
