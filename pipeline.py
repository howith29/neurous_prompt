# pipeline.py
# LangChain + Gemini Flash 기반 5단계 파이프라인
#
# Step 1: Filter Chain          — 광고·편향·비속어 감지
# Step 2: Structured Extract    — Pydantic News 모델로 구조화 저장
# Step 3: Fact Extract Chain    — content에서 순수 사실만 추출
# Step 4: Content Gen Chain     — 레벨별 학습 콘텐츠 생성
# Step 5: Cosine Similarity     — TF-IDF 기반 원문 유사도 검증 + 재생성

import os
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from prompts import (
    FILTER_SYSTEM, FILTER_USER,
    STRUCTURED_EXTRACT_USER,
    FACT_EXTRACT_SYSTEM, FACT_EXTRACT_USER,
    CONTENT_GEN_SYSTEM, CONTENT_GEN_USER,
    CONTENT_REGEN_HINT,
    LEVEL_CONSTRAINTS, CATEGORY_LABELS,
)


# ── Pydantic 모델 — 구조화 추출 결과 ─────────────────────────────────────────
class NewsStructured(BaseModel):
    """Step 2 구조화 추출 결과. 출처 정보 보존용."""
    title: str = Field(description="기사 제목", default="")
    category: str = Field(description="기사 종류", examples=["정치", "생활/문화"], default="")
    content: str = Field(description="기사 내용 (원문 그대로 추출)", default="")
    press: str = Field(description="언론사 이름. 없으면 빈 문자열", examples=["조선일보"], default="")
    author: str = Field(description="작성자 이름. 없으면 빈 문자열", examples=["홍길동"], default="")
    published_date: str = Field(description="작성일. 없으면 빈 문자열", examples=["2026.01.01"], default="")


# ── 파이프라인 최종 결과 ──────────────────────────────────────────────────────
@dataclass
class PipelineResult:
    """파이프라인 전체 실행 결과"""
    # Step 1
    passed_filter: bool
    filter_reason: str
    # Step 2
    news_structured: Optional[NewsStructured]   # 출처 포함 구조화 데이터
    # Step 3
    facts: Optional[str]                        # 순수 사실 목록
    # Step 4 + 5
    content: Optional[str]                      # 최종 생성 콘텐츠
    similarity_score: float = 0.0               # 원문과의 코사인 유사도
    regen_count: int = 0                        # 재생성 횟수
    # 에러
    error: Optional[str] = None


# ── LLM 초기화 ────────────────────────────────────────────────────────────────
def get_llm(api_key: str, temperature: float = 0.3) -> ChatGoogleGenerativeAI:
    """Gemini Flash LLM 인스턴스를 반환합니다."""
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=api_key,
        temperature=temperature,
        max_output_tokens=4096,
    )


# ── Step 1: 필터 체인 ─────────────────────────────────────────────────────────
def build_filter_chain(llm):
    prompt = ChatPromptTemplate.from_messages([
        ("system", FILTER_SYSTEM),
        ("human", FILTER_USER),
    ])
    return prompt | llm | StrOutputParser()


def run_filter(chain, title: str, content: str) -> tuple[bool, str]:
    """
    Returns:
        (passed: bool, reason: str)
    """
    response = chain.invoke({"title": title, "content": content[:1500]})
    passed = "PASS" in response.upper()
    reason = "문제없음"
    for line in response.splitlines():
        if line.startswith("이유:"):
            reason = line.replace("이유:", "").strip()
            break
    return passed, reason


# ── Step 2: 구조화 추출 ───────────────────────────────────────────────────────
def build_structured_chain(llm):
    structured_llm = llm.with_structured_output(NewsStructured)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "뉴스 기사에서 메타 정보를 추출해 JSON으로 반환하세요. 알 수 없는 필드는 빈 문자열(\"\")로 채우세요. 모든 필드를 반드시 포함하세요."),
        ("human", STRUCTURED_EXTRACT_USER),
    ])
    return prompt | structured_llm


def run_structured_extract(
    chain,
    title: str,
    category: str,
    content: str,
) -> NewsStructured:
    """
    뉴스 원문에서 메타 정보를 구조화해서 추출합니다.
    press / author / published_date 등 출처 정보가 여기에 보존됩니다.
    """
    cat_label = CATEGORY_LABELS.get(category, category)
    return chain.invoke({
        "title": title,
        "category": cat_label,
        "content": content[:4000],
    })


# ── Step 3: 사실 추출 체인 ───────────────────────────────────────────────────
def build_fact_chain(llm):
    prompt = ChatPromptTemplate.from_messages([
        ("system", FACT_EXTRACT_SYSTEM),
        ("human", FACT_EXTRACT_USER),
    ])
    return prompt | llm | StrOutputParser()


def run_fact_extract(chain, category: str, title: str, content: str) -> str:
    """
    구조화 추출로 얻은 content만 받아 순수 사실을 추출합니다.
    press / author 등 출처 정보는 의도적으로 넘기지 않습니다.
    """
    cat_label = CATEGORY_LABELS.get(category, category)
    return chain.invoke({
        "category": cat_label,
        "title": title,
        "content": content[:4000],
    })


# ── Step 4: 콘텐츠 생성 체인 ─────────────────────────────────────────────────
def build_content_chain(llm):
    prompt = ChatPromptTemplate.from_messages([
        ("system", CONTENT_GEN_SYSTEM),
        ("human", CONTENT_GEN_USER),
    ])
    return prompt | llm | StrOutputParser()


def run_content_gen(
    chain,
    category: str,
    facts: str,
    level: str,
    regen: bool = False,        # 재생성 여부
) -> str:
    cat_label = CATEGORY_LABELS.get(category, category)
    level_constraint = LEVEL_CONSTRAINTS[level]["description"]

    # 재생성 시 힌트를 사실 목록 앞에 추가
    facts_input = (CONTENT_REGEN_HINT + "\n\n" + facts) if regen else facts

    return chain.invoke({
        "category": cat_label,
        "facts": facts_input,
        "level_constraint": level_constraint,
    })


# ── Step 5: TF-IDF 코사인 유사도 ─────────────────────────────────────────────
def _tokenize(text: str) -> list[str]:
    """한국어 텍스트를 2-gram 음절 단위로 토크나이징합니다."""
    # 공백·특수문자 제거 후 2글자씩 슬라이딩
    clean = re.sub(r"[^\w]", "", text)
    if len(clean) < 2:
        return list(clean)
    return [clean[i:i+2] for i in range(len(clean) - 1)]


def _tfidf_vector(tokens: list[str]) -> dict[str, float]:
    """단순 TF 벡터 (단일 문서이므로 IDF 생략, TF만 사용)."""
    counts = Counter(tokens)
    total = sum(counts.values()) or 1
    return {token: count / total for token, count in counts.items()}


def cosine_similarity(text_a: str, text_b: str) -> float:
    """
    두 텍스트의 TF-IDF(TF만) 코사인 유사도를 계산합니다.
    Returns:
        0.0 ~ 1.0 (1.0 = 완전 동일)
    """
    vec_a = _tfidf_vector(_tokenize(text_a))
    vec_b = _tfidf_vector(_tokenize(text_b))

    # 공통 토큰 내적
    common = set(vec_a) & set(vec_b)
    dot = sum(vec_a[t] * vec_b[t] for t in common)

    # 각 벡터의 크기
    mag_a = math.sqrt(sum(v ** 2 for v in vec_a.values()))
    mag_b = math.sqrt(sum(v ** 2 for v in vec_b.values()))

    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ── 전체 파이프라인 실행 ──────────────────────────────────────────────────────
SIMILARITY_THRESHOLD = 0.5      # 이 값 이상이면 재생성 (0~1, 낮을수록 엄격)
MAX_REGEN = 2                   # 최대 재생성 횟수


def run_pipeline(
    api_key: str,
    title: str,
    content: str,
    category: str,
    level: str,
) -> PipelineResult:
    """
    5단계 파이프라인을 순서대로 실행합니다.

    Args:
        api_key:  Google Gemini API 키
        title:    뉴스 제목
        content:  뉴스 본문 (원문)
        category: 카테고리 코드 (예: "IT_SCIENCE")
        level:    난이도 코드 ("BEGINNER" | "INTERMEDIATE" | "ADVANCED")

    Returns:
        PipelineResult
    """
    try:
        llm = get_llm(api_key)

        # ── Step 1: 필터 ──────────────────────────────────────────────────
        filter_chain = build_filter_chain(llm)
        passed, reason = run_filter(filter_chain, title, content)

        if not passed:
            return PipelineResult(
                passed_filter=False,
                filter_reason=reason,
                news_structured=None,
                facts=None,
                content=None,
            )

        # ── Step 2: 구조화 추출 ───────────────────────────────────────────
        # press / author / published_date 등 출처 정보 보존
        structured_chain = build_structured_chain(llm)
        news_obj: NewsStructured = run_structured_extract(
            structured_chain, title, category, content
        )

        # ── Step 3: 사실 추출 ─────────────────────────────────────────────
        # news_obj.content(원문)만 넘겨서 출처 정보를 콘텐츠 생성에서 차단
        fact_chain = build_fact_chain(llm)
        facts = run_fact_extract(
            fact_chain, category, news_obj.title, news_obj.content
        )

        # ── Step 4 + 5: 콘텐츠 생성 + 유사도 검증 루프 ───────────────────
        content_chain = build_content_chain(llm)
        regen_count = 0
        similarity = 0.0

        for attempt in range(MAX_REGEN + 1):
            is_regen = attempt > 0
            generated = run_content_gen(
                content_chain, category, facts, level, regen=is_regen
            )

            # Step 5: 원문 vs 생성문 코사인 유사도 체크
            similarity = cosine_similarity(news_obj.content, generated)

            if similarity < SIMILARITY_THRESHOLD:
                # 통과 — 유사도가 임계값 미만이면 저작권 안전
                break
            else:
                regen_count += 1
                # MAX_REGEN 초과하면 마지막 결과를 그냥 사용
                if attempt == MAX_REGEN:
                    break

        return PipelineResult(
            passed_filter=True,
            filter_reason=reason,
            news_structured=news_obj,
            facts=facts,
            content=generated,
            similarity_score=round(similarity, 4),
            regen_count=regen_count,
        )

    except Exception as e:
        return PipelineResult(
            passed_filter=False,
            filter_reason="",
            news_structured=None,
            facts=None,
            content=None,
            error=str(e),
        )


# ── 통계 계산 유틸 ────────────────────────────────────────────────────────────
def calc_stats(text: str) -> dict:
    chars = len(text.replace(" ", "").replace("\n", ""))
    paragraphs = len([p for p in text.split("\n\n") if p.strip()])
    sentences = len([s for s in text.replace("?", ".").replace("!", ".").split(".") if s.strip()])
    return {"chars": chars, "paragraphs": paragraphs, "sentences": sentences}


def check_constraints(stats: dict, level: str) -> dict:
    c = LEVEL_CONSTRAINTS[level]
    return {
        "chars_ok":          c["chars"][0] <= stats["chars"] <= c["chars"][1],
        "para_ok":           c["paragraphs"][0] <= stats["paragraphs"] <= c["paragraphs"][1],
        "sentences_ok":      c["sentences"][0] <= stats["sentences"] <= c["sentences"][1],
        "chars_range":       c["chars"],
        "para_range":        c["paragraphs"],
        "sentences_range":   c["sentences"],
    }