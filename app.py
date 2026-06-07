# app.py
# Streamlit 메인 앱
# 실행: streamlit run app.py

import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from pipeline import run_pipeline, calc_stats, check_constraints
from prompts import CATEGORY_LABELS, LEVEL_CONSTRAINTS

# ── 환경 변수 로드 ────────────────────────────────────────────────────────────
# 로컬: .env 파일 사용
# Streamlit Cloud: st.secrets 사용 (secrets.toml)
load_dotenv()

def get_api_key() -> str:
    """API 키를 환경 변수 또는 st.secrets에서 가져옵니다."""
    # Streamlit Cloud 배포 시
    if "GOOGLE_API_KEY" in st.secrets:
        return st.secrets["GOOGLE_API_KEY"]
    # 로컬 .env 파일
    key = os.getenv("GOOGLE_API_KEY", "")
    return key


# ── 뉴스 데이터 로드 ──────────────────────────────────────────────────────────
@st.cache_data
def load_news_data():
    data_path = Path(__file__).parent / "news_samples.json"
    with open(data_path, encoding="utf-8") as f:
        return json.load(f)


# ── 저장된 프롬프트 관리 (세션 기반) ─────────────────────────────────────────
# 실제 배포 시에는 JSON 파일 or DB로 영속화 가능
def init_saved_prompts():
    if "saved_prompts" not in st.session_state:
        st.session_state.saved_prompts = []

def save_prompt(name: str, system_prompt_override: dict):
    st.session_state.saved_prompts.append({
        "name": name,
        "prompts": system_prompt_override,
    })

def delete_prompt(idx: int):
    st.session_state.saved_prompts.pop(idx)


# ── 페이지 설정 ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="뉴스 파이프라인 테스터",
    page_icon="📰",
    layout="wide",
)

st.title("📰 뉴스 학습 콘텐츠 파이프라인 테스터")
st.caption("랭체인 3단계 파이프라인 · Gemini Flash · 저작권 안전 · 초/중/고급 콘텐츠 생성")

init_saved_prompts()
news_data = load_news_data()
api_key = get_api_key()

# API 키 없을 때 경고
if not api_key:
    st.error("⚠️ GOOGLE_API_KEY가 설정되지 않았습니다. .env 파일 또는 Streamlit secrets를 확인하세요.")
    st.stop()

# ── 사이드바: 기사 선택 + 옵션 ───────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 설정")

    # 카테고리 필터
    all_cats = ["전체"] + [CATEGORY_LABELS[k] for k in CATEGORY_LABELS]
    cat_label = st.selectbox("카테고리", all_cats)

    cat_code_map = {v: k for k, v in CATEGORY_LABELS.items()}
    selected_cat = cat_code_map.get(cat_label)

    # 기사 필터링
    if cat_label == "전체":
        filtered = news_data
    else:
        filtered = [n for n in news_data if n["category"] == selected_cat]

    # 기사 선택
    article_titles = [f"[{CATEGORY_LABELS[n['category']]}] {n['title'][:40]}…" if len(n['title']) > 40 else f"[{CATEGORY_LABELS[n['category']]}] {n['title']}" for n in filtered]
    article_idx = st.selectbox("기사 선택", range(len(filtered)), format_func=lambda i: article_titles[i])
    article = filtered[article_idx]

    st.divider()

    # 난이도 선택
    level_labels = {k: v["label"] for k, v in LEVEL_CONSTRAINTS.items()}
    level_code = st.radio(
        "난이도",
        list(level_labels.keys()),
        format_func=lambda k: level_labels[k],
        horizontal=True,
    )

    st.divider()

    # 원문 미리보기
    with st.expander("📄 원문 미리보기"):
        st.markdown(f"**제목:** {article['title']}")
        st.text_area("본문 (앞 500자)", article["content_clean"][:500] + "…", height=150, disabled=True)

    st.divider()

    # 실행 버튼
    run_btn = st.button("▶ 파이프라인 실행", type="primary", use_container_width=True)


# ── 메인 영역: 탭 구성 ────────────────────────────────────────────────────────
tab_result, tab_arch, tab_prompt, tab_saved = st.tabs(["📊 실행 결과", "🏗 파이프라인 구조", "🔧 프롬프트 수정", "💾 저장된 프롬프트"])


# ── 탭 1: 실행 결과 ───────────────────────────────────────────────────────────
with tab_result:
    if run_btn:
        st.subheader(f"🗞 {article['title']}")
        st.caption(f"카테고리: {CATEGORY_LABELS[article['category']]} · 난이도: {LEVEL_CONSTRAINTS[level_code]['label']}")

        # 5단계 진행 표시
        from pipeline import (
            get_llm,
            build_filter_chain, run_filter,
            build_structured_chain, run_structured_extract,
            build_fact_chain, run_fact_extract,
            build_content_chain, run_content_gen,
            cosine_similarity,
            SIMILARITY_THRESHOLD, MAX_REGEN,
        )
        llm = get_llm(api_key)

        # ── Step 1: 필터 ──────────────────────────────────────────────────
        with st.status("① 필터 검사 중…", expanded=False) as s1:
            f_chain = build_filter_chain(llm)
            passed, reason = run_filter(f_chain, article["title"], article["content_clean"])
            if passed:
                s1.update(label=f"✅ ① 필터 통과 — {reason}", state="complete")
            else:
                s1.update(label=f"❌ ① 필터 거부 — {reason}", state="error")

        if not passed:
            st.error(f"필터 거부: {reason}\n\n콘텐츠를 생성하지 않습니다.")
            st.stop()

        # ── Step 2: 구조화 추출 ───────────────────────────────────────────
        with st.status("② 구조화 추출 중…", expanded=False) as s2:
            s_chain = build_structured_chain(llm)
            news_obj = run_structured_extract(
                s_chain, article["title"], article["category"], article["content_clean"]
            )
            s2.update(label="✅ ② 구조화 추출 완료", state="complete")

        # 출처 정보 표시 (참고용)
        with st.expander("📋 추출된 출처 정보 (언론사·작성자·날짜)"):
            meta_col1, meta_col2, meta_col3 = st.columns(3)
            meta_col1.metric("언론사", news_obj.press or "미확인")
            meta_col2.metric("작성자", news_obj.author or "미확인")
            meta_col3.metric("작성일", news_obj.published_date or "미확인")

        # ── Step 3: 사실 추출 ─────────────────────────────────────────────
        with st.status("③ 사실 추출 중…", expanded=False) as s3:
            fa_chain = build_fact_chain(llm)
            # news_obj.content만 넘김 — press/author는 차단
            facts = run_fact_extract(
                fa_chain, article["category"], news_obj.title, news_obj.content
            )
            s3.update(label="✅ ③ 사실 추출 완료", state="complete")

        with st.expander("🔍 추출된 사실 목록 (Step 3 결과)"):
            st.info(facts)

        # ── Step 4 + 5: 콘텐츠 생성 + 유사도 검증 루프 ───────────────────
        c_chain = build_content_chain(llm)
        regen_count = 0
        similarity = 0.0
        generated = ""

        for attempt in range(MAX_REGEN + 1):
            is_regen = attempt > 0
            label = f"④ 콘텐츠 생성 중{'… (재생성 #' + str(attempt) + ')' if is_regen else '…'}"

            with st.status(label, expanded=False) as s4:
                generated = run_content_gen(
                    c_chain, article["category"], facts, level_code, regen=is_regen
                )
                # Step 5: 유사도 체크
                similarity = cosine_similarity(news_obj.content, generated)
                sim_pct = round(similarity * 100, 1)

                if similarity < SIMILARITY_THRESHOLD:
                    s4.update(
                        label=f"✅ ④ 콘텐츠 생성 완료 — 유사도 {sim_pct}% (안전)",
                        state="complete",
                    )
                    break
                else:
                    s4.update(
                        label=f"⚠️ 유사도 {sim_pct}% 초과 → 재생성",
                        state="error",
                    )
                    regen_count += 1

        st.divider()

        # ── 구조화 추출 결과 (Step 2 JSON) ───────────────────────────────
        st.subheader("🗂 구조화 추출 결과 (Step 2)")
        import json as _json
        structured_dict = {
            "title":          news_obj.title,
            "category":       news_obj.category,
            "press":          news_obj.press or "",
            "author":         news_obj.author or "",
            "published_date": news_obj.published_date or "",
        }
        st.json(structured_dict)

        st.divider()

        # ── 생성된 학습 콘텐츠 ────────────────────────────────────────────
        st.subheader("📝 생성된 학습 콘텐츠")
        st.markdown(generated)
        content = generated  # 하위 통계 계산용

        # 유사도 / 재생성 요약
        sim_pct = round(similarity * 100, 1)
        sim_color = "🟢" if similarity < SIMILARITY_THRESHOLD else "🔴"
        info_parts = [f"{sim_color} 원문 유사도: **{sim_pct}%** (임계값: {int(SIMILARITY_THRESHOLD*100)}%)"]
        if regen_count:
            info_parts.append(f"🔁 재생성: {regen_count}회")
        st.caption("  ·  ".join(info_parts))

        # 통계
        stats = calc_stats(content)
        checks = check_constraints(stats, level_code)

        st.divider()
        st.subheader("📏 제약 조건 충족 여부")
        m1, m2, m3 = st.columns(3)

        def stat_display(col, label, value, ok, range_):
            color = "🟢" if ok else "🔴"
            col.metric(
                label=f"{color} {label}",
                value=value,
                delta=f"범위: {range_[0]}–{range_[1]}",
                delta_color="off",
            )

        stat_display(m1, "글자 수", stats["chars"], checks["chars_ok"], checks["chars_range"])
        stat_display(m2, "문단 수", stats["paragraphs"], checks["para_ok"], checks["para_range"])
        stat_display(m3, "문장 수", stats["sentences"], checks["sentences_ok"], checks["sentences_range"])

        # 결과 저장 (세션)
        st.session_state["last_result"] = {
            "article": article,
            "level": level_code,
            "structured": structured_dict,
            "facts": facts,
            "content": content,
            "stats": stats,
            "checks": checks,
        }

    else:
        st.info("왼쪽에서 기사와 난이도를 선택하고 **▶ 파이프라인 실행** 버튼을 눌러주세요.\n\n파이프라인 구조는 **🏗 파이프라인 구조** 탭에서 확인할 수 있습니다.")



# ── 탭 2: 파이프라인 구조 ─────────────────────────────────────────────────────
with tab_arch:
    st.subheader("🏗 파이프라인 구조")
    st.caption("뉴스 원문이 학습 콘텐츠로 만들어지기까지 5단계를 거칩니다.")

    STEPS = [
        dict(
            num="Step 1", icon="🚦", title="Filter Chain",
            subtitle="광고·편향·비속어 감지",
            color="#fff3cd", border="#ffc107",
            desc=(
                "뉴스 원문을 콘텐츠로 만들기 전에 **적합성 필터**를 먼저 통과시킵니다.\n"
                "아래 기준 중 하나라도 해당하면 즉시 중단합니다.\n"
                "- 광고·홍보성 기사\n"
                "- 비속어·혐오 표현\n"
                "- 정치적 편향 (특정 세력 옹호/비방)\n"
                "- 폭력·선정성\n"
                "- 사실 없는 순수 의견글"
            ),
            why="필터가 REJECT하면 이후 Step을 아예 실행하지 않아 **API 호출을 절약**합니다. 거부 이유도 로그에 남아 디버깅이 쉽습니다.",
        ),
        dict(
            num="Step 2", icon="🗂", title="Structured Extract",
            subtitle="Pydantic 모델로 구조화 저장",
            color="#e8f4fd", border="#2196f3",
            desc=(
                "원문에서 **메타 정보**를 Pydantic 모델로 구조화해 저장합니다.\n\n"
                "| 필드 | 설명 | 용도 |\n"
                "|---|---|---|\n"
                "| `content` | 기사 본문 | Step 3·4에서 사용 |\n"
                "| `press` | 언론사 이름 | 저작권 추적용 보존 |\n"
                "| `author` | 작성자 | 저작권 추적용 보존 |\n"
                "| `published_date` | 작성일 | 저작권 추적용 보존 |"
            ),
            why="`press` / `author`는 Step 3에 **의도적으로 넘기지 않습니다.** 출처 정보가 흘러들어가면 생성 콘텐츠에 '○○기자 보도에 따르면…' 같은 문구가 섞일 수 있기 때문입니다.",
        ),
        dict(
            num="Step 3", icon="🔍", title="Fact Extract Chain",
            subtitle="원문에서 순수 사실만 추출",
            color="#f3e8fd", border="#9c27b0",
            desc=(
                "Step 2의 `news_obj.content`만 받아 **검증 가능한 사실**만 번호 목록으로 뽑습니다.\n"
                "- ✅ 날짜, 수치, 기관명, 인명, 장소, 사건\n"
                "- ❌ 기자 의견, 전망, 평가, 광고 문구, 언론사·기자 정보"
            ),
            why="콘텐츠 생성기(Step 4)에 원문을 직접 넘기면 LLM이 원문 표현을 그대로 베낄 위험이 높습니다. **사실만 넘기면** 원문 문장 구조를 참고할 수 없어 저작권 위험이 낮아집니다.",
        ),
        dict(
            num="Step 4", icon="✍️", title="Content Gen Chain",
            subtitle="레벨별 학습 콘텐츠 생성",
            color="#e8fdf3", border="#4caf50",
            desc=(
                "Step 3의 **사실 목록만** 입력으로 받아 레벨 제약에 맞게 새 문장으로 작성합니다.\n\n"
                "| 레벨 | 분량 | 문단 | 문장 |\n"
                "|---|---|---|---|\n"
                "| 초급 | 150–250자 | 1개 | 3~5문장 |\n"
                "| 중급 | 300–500자 | 2개 | 6~10문장 |\n"
                "| 고급 | 600–900자 | 3개 | 12~18문장 |\n\n"
                "재생성 시 `CONTENT_REGEN_HINT`가 프롬프트에 추가되어 다른 표현을 유도합니다."
            ),
            why="사실 추출 결과는 레벨과 무관하게 **1번만 실행**됩니다. 같은 사실로 초·중·고급을 각각 만들 때 Step 3을 3번 돌릴 필요가 없어 API 호출을 아낍니다.",
        ),
        dict(
            num="Step 5", icon="📐", title="Cosine Similarity Check",
            subtitle="TF-IDF 유사도 검증 + 재생성 루프",
            color="#fdecea", border="#f44336",
            desc=(
                "생성된 콘텐츠와 **원문의 유사도**를 TF-IDF 코사인 유사도로 계산합니다.\n\n"
                "- 유사도 **< 0.5** → ✅ 통과 (저작권 안전)\n"
                "- 유사도 **≥ 0.5** → ⚠️ Step 4 재생성 (최대 2회)\n\n"
                "**TF-IDF 선택 이유:** 외부 패키지 없이 순수 파이썬으로 구현, Streamlit Cloud 배포 시 의존성 최소화."
            ),
            why="재생성 루프가 있어도 Step 3(사실 추출)은 다시 실행하지 않습니다. **같은 facts로 Step 4만 반복**해 최소 API 호출로 저작권 안전을 확보합니다.",
        ),
    ]

    for step in STEPS:
        header_html = (
            "<div style='"
            f"border-left:4px solid {step['border']};"
            f"background:{step['color']};"
            "border-radius:8px;padding:16px 20px;margin-bottom:8px'>"
            f"<div style='font-size:13px;color:#666;font-weight:500'>{step['num']}</div>"
            f"<div style='font-size:20px;font-weight:700;margin:2px 0'>{step['icon']} {step['title']}</div>"
            f"<div style='font-size:14px;color:#444'>{step['subtitle']}</div>"
            "</div>"
        )
        st.markdown(header_html, unsafe_allow_html=True)
        col_desc, col_why = st.columns([3, 2])
        with col_desc:
            st.markdown(step["desc"])
        with col_why:
            st.info("**💡 이 단계가 필요한 이유**\n\n" + step["why"])
        if step["num"] != "Step 5":
            st.markdown(
                "<div style='text-align:center;font-size:22px;color:#aaa;margin:4px 0'>↓</div>",
                unsafe_allow_html=True,
            )

    st.divider()
    st.caption("임계값(`SIMILARITY_THRESHOLD`)과 재생성 횟수(`MAX_REGEN`)는 `pipeline.py` 상단 상수에서 조정할 수 있습니다.")



# ── 탭 2: 프롬프트 수정 ───────────────────────────────────────────────────────
with tab_prompt:
    st.subheader("🔧 프롬프트 수정")
    st.caption("각 단계의 프롬프트를 수정하고 저장할 수 있습니다. 수정 후 실행하면 즉시 반영됩니다.")

    # prompts.py 의 기본값을 세션에 초기화
    from prompts import FILTER_SYSTEM, FACT_EXTRACT_SYSTEM, CONTENT_GEN_SYSTEM
    if "custom_filter_system" not in st.session_state:
        st.session_state.custom_filter_system = FILTER_SYSTEM
    if "custom_fact_system" not in st.session_state:
        st.session_state.custom_fact_system = FACT_EXTRACT_SYSTEM
    if "custom_content_system" not in st.session_state:
        st.session_state.custom_content_system = CONTENT_GEN_SYSTEM

    p1, p2, p3 = st.tabs(["① 필터 프롬프트", "② 사실 추출 프롬프트", "③ 콘텐츠 생성 프롬프트"])

    with p1:
        st.session_state.custom_filter_system = st.text_area(
            "필터 시스템 프롬프트",
            value=st.session_state.custom_filter_system,
            height=350,
            key="filter_sys_input",
        )

    with p2:
        st.session_state.custom_fact_system = st.text_area(
            "사실 추출 시스템 프롬프트",
            value=st.session_state.custom_fact_system,
            height=350,
            key="fact_sys_input",
        )

    with p3:
        st.session_state.custom_content_system = st.text_area(
            "콘텐츠 생성 시스템 프롬프트",
            value=st.session_state.custom_content_system,
            height=350,
            key="content_sys_input",
        )

    st.divider()

    # 저장
    save_col1, save_col2 = st.columns([3, 1])
    with save_col1:
        save_name = st.text_input("저장 이름 (예: v2_더_엄격한_필터)", key="prompt_save_name")
    with save_col2:
        st.write("")  # 정렬용
        if st.button("💾 저장", use_container_width=True):
            if save_name.strip():
                save_prompt(save_name.strip(), {
                    "filter": st.session_state.custom_filter_system,
                    "fact":   st.session_state.custom_fact_system,
                    "content": st.session_state.custom_content_system,
                })
                st.success(f"'{save_name}' 저장 완료!")
            else:
                st.warning("저장 이름을 입력해주세요.")

    if st.button("🔄 기본값으로 초기화"):
        st.session_state.custom_filter_system = FILTER_SYSTEM
        st.session_state.custom_fact_system = FACT_EXTRACT_SYSTEM
        st.session_state.custom_content_system = CONTENT_GEN_SYSTEM
        st.rerun()


# ── 탭 3: 저장된 프롬프트 ────────────────────────────────────────────────────
with tab_saved:
    st.subheader("💾 저장된 프롬프트")

    if not st.session_state.saved_prompts:
        st.info("저장된 프롬프트가 없습니다. '프롬프트 수정' 탭에서 수정 후 저장해보세요.")
    else:
        for i, p in enumerate(st.session_state.saved_prompts):
            with st.expander(f"📌 {p['name']}"):
                st.text_area("필터 프롬프트", p["prompts"]["filter"], height=100, disabled=True, key=f"sp_f_{i}")
                st.text_area("사실 추출 프롬프트", p["prompts"]["fact"], height=100, disabled=True, key=f"sp_fa_{i}")
                st.text_area("콘텐츠 생성 프롬프트", p["prompts"]["content"], height=100, disabled=True, key=f"sp_c_{i}")

                col_load, col_del = st.columns(2)
                with col_load:
                    if st.button("📂 불러오기", key=f"load_{i}"):
                        st.session_state.custom_filter_system = p["prompts"]["filter"]
                        st.session_state.custom_fact_system = p["prompts"]["fact"]
                        st.session_state.custom_content_system = p["prompts"]["content"]
                        st.success("불러왔습니다. '프롬프트 수정' 탭에서 확인하세요.")
                with col_del:
                    if st.button("🗑 삭제", key=f"del_{i}"):
                        delete_prompt(i)
                        st.rerun()