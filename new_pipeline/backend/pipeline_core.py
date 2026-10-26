"""Generate level-specific content from persisted preprocessing results."""

import re
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

from .prompts import (
    CONTENT_GEN_SYSTEM, CONTENT_GEN_USER,
    QUIZ_SYSTEM, QUIZ_USER,
    LEVEL_CONSTRAINTS, CATEGORY_LABELS,
)

DEFAULT_MAX_REGEN = 2
# gemini-2.0-flash 는 Google 측에서 폐기(404 NOT_FOUND)되어 최신 모델로 교체.
# Preview 모델(gemini-3-flash-preview 등)은 예고 없이 바뀌거나 내려갈 수 있어 Stable 모델로 고정.
# gemini-3.5-flash: temperature 파라미터를 지원하는 최신 Stable flash 모델 중 최상위
#   (gemini-3.6-flash는 더 저렴하지만 고정 샘플링이라 temperature가 무시됨 -> 재생성 로직과 상충).
# .env 의 GEMINI_MODEL 이 설정되어 있으면 그 값이 우선 적용됨 (main.py get_model() 참고).
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"


# ── Pydantic 모델 — 구조화 추출 결과 (원본 그대로) ────────────────────────────
class NewsStructured(BaseModel):
    title: str = Field(description="기사 제목", default="")
    category: str = Field(description="기사 종류", default="")
    content: str = Field(description="기사 내용 (원문 그대로 추출)", default="")
    press: str = Field(description="언론사 이름. 없으면 빈 문자열", default="")
    author: str = Field(description="작성자 이름. 없으면 빈 문자열", default="")
    published_date: str = Field(description="작성일. 없으면 빈 문자열", default="")


# ── Step 6: 퀴즈 생성용 Pydantic 모델 ──────────────────────────────────────────
class QuizGen(BaseModel):
    question: str = Field(description="문제 문장 (1문장, 25~60자)")
    choices: list[str] = Field(description="보기 3개 (각 8~25자)")
    answerNo: int = Field(description="정답 보기 번호 (1~3)")


@dataclass
class QuizResult:
    quiz: Optional[QuizGen]
    error: Optional[str] = None


@dataclass
class PipelineResult:
    passed_filter: bool
    filter_reason: str
    news_structured: Optional[NewsStructured]
    facts: Optional[str]
    content: Optional[str]                 # "제목: ...\n\n본문" 형태의 원시 생성 출력
    regen_count: int = 0
    error: Optional[str] = None


def _extract_text(response) -> str:
    """AIMessage.content 가 str 또는 list[dict] 인 경우 모두 처리."""
    raw = response.content if hasattr(response, "content") else str(response)
    if isinstance(raw, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in raw)
    return str(raw)


def calc_stats(text: str) -> dict:
    chars = len(re.sub(r"\s", "", text))
    paragraphs = len([p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()])
    # Decimal points inside numbers are not sentence boundaries.
    sentences = len([p for p in re.split(r"(?<!\d)\.|\.(?!\d)|[?!]", text) if p.strip()])
    return {"chars": chars, "paragraphs": paragraphs, "sentences": sentences}


def check_constraints(stats: dict, level: str) -> dict:
    c = LEVEL_CONSTRAINTS[level]
    return {
        "chars_ok": c["chars"][0] <= stats["chars"] <= c["chars"][1],
        "para_ok": c["paragraphs"][0] <= stats["paragraphs"] <= c["paragraphs"][1],
        "sentences_ok": c["sentences"][0] <= stats["sentences"] <= c["sentences"][1],
    }


def split_gen_output(raw: str) -> tuple[str, str]:
    """Step4 출력 형식 "제목: ...\\n\\n본문..." 을 (title, body) 로 분리."""
    raw = (raw or "").strip()
    lines = raw.splitlines()
    if lines and lines[0].startswith("제목:"):
        title = lines[0].replace("제목:", "").strip()
        body = "\n".join(lines[1:]).strip()
        return title, body
    # 형식이 안 지켜졌으면 첫 줄을 제목으로 취급
    if lines:
        return lines[0].strip(), "\n".join(lines[1:]).strip()
    return "", raw


class ContentPipeline:
    """
    LLM(Gemini) 체인을 1회 생성해 배치 작업 내내 재사용하는 실행기.
    기존 pipeline.py 의 build_*_chain()들을 생성자에서 한 번만 조립한다.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_GEMINI_MODEL, temperature: float = 0.3):
        self.llm = ChatGoogleGenerativeAI(
            model=model, google_api_key=api_key,
            temperature=temperature, max_output_tokens=8192, timeout=90, max_retries=2,
        )
        self.content_chain = (
            ChatPromptTemplate.from_messages([("system", CONTENT_GEN_SYSTEM), ("human", CONTENT_GEN_USER)])
            | self.llm
        )
        self.quiz_chain = (
            ChatPromptTemplate.from_messages([("system", QUIZ_SYSTEM), ("human", QUIZ_USER)])
            | self.llm.with_structured_output(QuizGen)
        )

    def run(
        self,
        title: str,
        content: str,
        category: str,
        level: str,
        max_regen: int = DEFAULT_MAX_REGEN,
        prepared: dict | None = None,
    ) -> PipelineResult:
        cat_label = CATEGORY_LABELS.get(category, category)
        try:
            if not prepared or prepared.get("status") != "ready":
                return PipelineResult(False, "전처리 완료가 필요합니다", None, None, None)
            news_obj = NewsStructured(title=title, category=cat_label, content=content)
            reason = prepared["filter"]["reason"]
            facts = prepared["facts"]

            # Generate from shared facts, then validate level constraints.
            level_constraint = LEVEL_CONSTRAINTS[level]["description"]
            regen_count = 0
            generated = ""
            correction = ""
            for attempt in range(max_regen + 1):
                is_regen = attempt > 0
                facts_input = (correction + "\n\n" + facts) if is_regen else facts
                raw_gen = self.content_chain.invoke({
                    "category": cat_label, "facts": facts_input, "level_constraint": level_constraint,
                })
                generated = _extract_text(raw_gen)
                gen_title, body = split_gen_output(generated)
                stats = calc_stats(body)
                checks = check_constraints(stats, level)
                valid = bool(gen_title and body) and all(checks.values()) and body.rstrip().endswith('.')
                if valid:
                    break
                correction = "[재작성] 이전 출력의 본문 측정값: " + str(stats) + ". 난이도별 분량·문단·문장 수 및 마침표 종료를 맞추세요."
                regen_count = min(attempt + 1, max_regen)
                if attempt == max_regen:
                    break

            return PipelineResult(
                passed_filter=True, filter_reason=reason, news_structured=news_obj,
                facts=facts, content=generated,
                regen_count=regen_count,
            )
        except Exception as e:
            return PipelineResult(False, "", None, None, None, error=str(e))

    def generate_quiz(self, category: str, level: str, content_text: str) -> QuizResult:
        """생성된 학습 콘텐츠(content_text) 1건으로 객관식 퀴즈 1문항을 만든다.
        글(기사) 당 호출은 1회만 하도록 상위 오케스트레이션(quiz_runner)에서 제어한다."""
        cat_label = CATEGORY_LABELS.get(category, category)
        level_label = LEVEL_CONSTRAINTS[level]["label"]
        try:
            quiz: QuizGen = self.quiz_chain.invoke({
                "level": level_label, "category": cat_label, "content": content_text,
            })
            if len(quiz.choices) != 3:
                raise ValueError(f"보기 개수가 3개가 아닙니다: {len(quiz.choices)}개")
            if quiz.answerNo not in (1, 2, 3):
                raise ValueError(f"answerNo가 1~3 범위를 벗어났습니다: {quiz.answerNo}")
            return QuizResult(quiz=quiz)
        except Exception as e:
            return QuizResult(quiz=None, error=str(e))
