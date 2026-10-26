# quiz_runner.py
# "퀴즈 생성" 버튼(Content List 화면)의 백엔드 로직.
#
# 설계 노트
#   - 퀴즈는 기사(글) 당 1개만 생성한다. 초급/중급/고급 결과가 여러 개 있어도 무관하게
#     중급(INTERMEDIATE) 성공 결과를 우선 사용하고, 없으면 성공한 다른 레벨을 사용한다
#     (storage.pick_quiz_source_content).
#   - 이미 해당 기사에 퀴즈가 생성돼 있으면 재생성하지 않고 건너뛴다 (중복 방지).
#   - 배치 콘텐츠 생성(batch_runner)과 달리 진행률 폴링 UI 없이 버튼 클릭 -> 동기 응답으로
#     끝나는 단발성 액션이라 Job 상태를 따로 두지 않는다. 다건 선택 시에는 스레드풀로만 병렬화한다.

from concurrent.futures import ThreadPoolExecutor, as_completed

from . import storage
from .pipeline_core import ContentPipeline, DEFAULT_GEMINI_MODEL


def generate_quizzes(
    api_key: str,
    content_result_ids: list[str],
    model: str = DEFAULT_GEMINI_MODEL,
    max_concurrency: int = 3,
) -> list[dict]:
    """선택된 content_results id들에서 기사(source_article_id)를 뽑아 기사 당 퀴즈 1개씩 생성한다.

    반환: [{source_article_id, status, quiz_id?, reason?}, ...]
    status: created | skipped_exists | no_content | error
    """
    content_rows = storage.get_content_results_by_ids(content_result_ids)
    article_ids = sorted({r["source_article_id"] for r in content_rows})
    if not article_ids:
        return []

    results: list[dict] = []
    to_generate: list[dict] = []  # 실제로 LLM 호출이 필요한 content_results row들

    for article_id in article_ids:
        existing = storage.get_quiz_by_source_article(article_id)
        if existing:
            results.append({
                "source_article_id": article_id,
                "status": "skipped_exists",
                "quiz_id": existing["quiz_id"],
            })
            continue

        source_content = storage.pick_quiz_source_content(article_id)
        if not source_content:
            results.append({
                "source_article_id": article_id,
                "status": "no_content",
                "reason": "퀴즈를 만들 수 있는 성공한 생성 콘텐츠가 없습니다.",
            })
            continue

        to_generate.append(source_content)

    if not to_generate:
        return results

    pipeline = ContentPipeline(api_key, model=model)  # 이 호출 안에서는 체인 1회만 생성해 재사용
    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        futures = {pool.submit(_generate_one, pipeline, row): row["source_article_id"] for row in to_generate}
        for fut in as_completed(futures):
            article_id = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001 - 개별 실패는 결과 목록에만 남기고 계속 진행
                results.append({"source_article_id": article_id, "status": "error", "reason": str(e)})

    return results


def _generate_one(pipeline: ContentPipeline, content_row: dict) -> dict:
    article_id = content_row["source_article_id"]
    content_text = f"제목: {content_row['gen_title']}\n\n{content_row['content_body']}"

    quiz_result = pipeline.generate_quiz(
        category=content_row["content_category"],
        level=content_row["content_level"],
        content_text=content_text,
    )
    if quiz_result.error:
        return {"source_article_id": article_id, "status": "error", "reason": quiz_result.error}

    quiz = quiz_result.quiz
    choices = [
        {"choice_no": i + 1, "choice_text": text, "is_correct": (i + 1) == quiz.answerNo}
        for i, text in enumerate(quiz.choices)
    ]
    saved = storage.save_quiz(
        content_id=content_row["id"],
        quiz_category=content_row["content_category"],
        quiz_question=quiz.question,
        quiz_diff=content_row["content_level"],
        choices=choices,
    )
    return {"source_article_id": article_id, "status": "created", "quiz_id": saved["quiz_id"]}
