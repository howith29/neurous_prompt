# batch_runner.py
# "글 생성" 화면(진행률 / 진행 로그 / Stop·Done)의 백엔드 로직.
#
# 설계 노트 (지난 대화에서 지적했던 리스크 반영):
#   - 지난 설계안은 "Done을 눌러야 결과가 저장"되는 구조라 Stop 후 이탈 시 데이터 유실 위험이 있었음.
#   - 이번 구현은 각 task(기사 x 레벨)가 끝나는 즉시 storage에 저장한다.
#     -> Stop은 "아직 시작 안 한 task를 더 이상 실행하지 않음" 의미로만 동작.
#     -> Done은 순수 UI 네비게이션 트리거 (저장할 게 남아있지 않음).

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import storage
from .pipeline_core import ContentPipeline, split_gen_output, DEFAULT_GEMINI_MODEL
from .prompts import LEVEL_CONSTRAINTS


class Job:
    def __init__(self, job_id: str, total: int):
        self.id = job_id
        self.total = total
        self.done = 0
        self.logs: list[str] = []
        self.status = "running"  # running | stopped | done | error
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()

    def log(self, message: str):
        with self._lock:
            self.logs.append(message)
            self.done += 1

    def finish(self):
        with self._lock:
            self.status = "stopped" if self.cancel_event.is_set() else "done"

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "job_id": self.id,
                "status": self.status,
                "done": self.done,
                "total": self.total,
                "logs": list(self.logs),
            }


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def get_job(job_id: str) -> Job | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def stop_job(job_id: str) -> bool:
    job = get_job(job_id)
    if not job:
        return False
    job.cancel_event.set()
    return True


def start_job(
    api_key: str,
    articles: list[dict],
    levels: list[str],
    model: str = DEFAULT_GEMINI_MODEL,
    max_regen: int = 2,
    similarity_threshold: float = 0.5,
    max_concurrency: int = 3,
) -> str:
    if not articles:
        raise ValueError("선택된 기사가 없습니다.")
    if not levels:
        raise ValueError("최소 1개 이상의 난이도를 선택해야 합니다.")

    tasks = [(a, lvl) for a in articles for lvl in levels]
    job_id = str(uuid.uuid4())
    job = Job(job_id, total=len(tasks))
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    thread = threading.Thread(
        target=_run_job,
        args=(job, api_key, model, tasks, max_regen, similarity_threshold, max_concurrency),
        daemon=True,
    )
    thread.start()
    return job_id


def _run_job(job: Job, api_key: str, model: str, tasks: list[tuple[dict, str]],
             max_regen: int, similarity_threshold: float, max_concurrency: int):
    try:
        pipeline = ContentPipeline(api_key, model=model)  # 이 배치 작업 안에서는 체인 1회만 생성해 재사용
        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            futures = {}
            for article, level in tasks:
                if job.cancel_event.is_set():
                    break
                fut = pool.submit(_run_single_task, pipeline, article, level,
                                    max_regen, similarity_threshold)
                futures[fut] = (article, level)

            for fut in as_completed(futures):
                article, level = futures[fut]
                level_kr = LEVEL_CONSTRAINTS[level]["label"]
                try:
                    result_row = fut.result()
                    saved = storage.save_content_result(result_row)
                    job.log(_format_log(article["title"], level_kr, saved))
                except Exception as e:  # noqa: BLE001 - 배치 안 개별 실패는 로그로만 남기고 계속 진행
                    job.log(f"기사 '{article['title']}' {level_kr} 생성 중 예외: {e}")
    finally:
        job.finish()


def _format_log(article_title: str, level_kr: str, row: dict) -> str:
    if row["status"] == "success":
        return f"기사 '{article_title}' {level_kr} 생성 완료"
    if row["status"] == "similarity_failed":
        return f"기사 '{article_title}' {level_kr} 생성 중 실패 (유사도 실패, score={row['similarity_score']})"
    if row["status"] == "filter_rejected":
        return f"기사 '{article_title}' {level_kr} 생성 중 실패 (필터: {row['fail_reason']})"
    return f"기사 '{article_title}' {level_kr} 생성 중 오류 ({row['fail_reason']})"


def _run_single_task(pipeline: ContentPipeline, article: dict, level: str,
                      max_regen: int, similarity_threshold: float) -> dict:
    result = pipeline.run(
        title=article["title"], content=article["content"], category=article["category"],
        level=level, max_regen=max_regen, similarity_threshold=similarity_threshold,
    )

    if result.error:
        return {
            "source_article_id": article["id"], "source_title": article["title"],
            "gen_title": None, "content_body": None,
            "content_category": article["category"], "content_level": level,
            "status": "error", "fail_reason": result.error,
            "similarity_score": None, "regen_count": 0,
        }

    if not result.passed_filter:
        return {
            "source_article_id": article["id"], "source_title": article["title"],
            "gen_title": None, "content_body": None,
            "content_category": article["category"], "content_level": level,
            "status": "filter_rejected", "fail_reason": result.filter_reason,
            "similarity_score": None, "regen_count": 0,
        }

    gen_title, content_body = split_gen_output(result.content)
    status = "success" if result.regen_count < max_regen or result.similarity_score < similarity_threshold else "similarity_failed"
    # regen_count == max_regen 이고 마지막 유사도도 threshold 이상이면 재생성 소진 후 실패로 간주
    if result.regen_count >= max_regen and result.similarity_score >= similarity_threshold:
        status = "similarity_failed"

    return {
        "source_article_id": article["id"], "source_title": article["title"],
        "gen_title": gen_title, "content_body": content_body,
        "content_category": article["category"], "content_level": level,
        "status": status, "fail_reason": None if status == "success" else "유사도 임계값 초과",
        "similarity_score": result.similarity_score, "regen_count": result.regen_count,
    }
