# 뉴로스 글 관리 (Create Content Platform) — 로컬 실행용 FastAPI 버전

지금까지 설계한 IA(Login 제외) + 와이어프레임(Image1~5) 기준으로 구현한 로컬 배치 도구입니다.
`neurous_prompt` 원본 저장소의 `pipeline.py` / `prompts.py`를 리팩토링해서 그대로 재사용합니다.

## 1. 설치 & 실행

```bash
# uv 쓰신다면
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

# 또는 일반 pip
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env 열어서 GOOGLE_API_KEY 채워넣기

uvicorn legacy.backend.main:app --reload --port 8001
```

브라우저에서 `http://localhost:8001` 열면 프론트(정적 HTML) + API가 같은 서버에서 서빙됩니다.
API 스펙만 보고 싶으면 `http://localhost:8001/docs` (FastAPI 자동 Swagger).

## 2. 화면 ↔ 엔드포인트 매핑

| 화면(Image) | 컴포넌트 | 엔드포인트 |
|---|---|---|
| 1. Source List | Tab | `GET /api/source` |
| 3. 뉴스 기사 업로드 | Modal — 파일 선택 | `POST /api/source/parse-file` (파싱만, 미저장) |
| 3. 뉴스 기사 업로드 | Modal — 기사 업로드 버튼 | `POST /api/source/confirm` (선택된 row 저장) |
| 4. 글 생성 | Modal — 생성 시작 | `POST /api/generation/jobs` |
| 4. 글 생성 | Modal — 진행률/로그 폴링 | `GET /api/generation/jobs/{job_id}` (1.5초 간격 폴링) |
| 4. 글 생성 | Modal — Stop | `POST /api/generation/jobs/{job_id}/stop` |
| 2. Content List | Tab/Page | `GET /api/content?category=&level=&status=` |
| 2. Content List | Button — 퀴즈 생성 | `POST /api/quiz/generate` (선택한 콘텐츠의 원문 기사 기준, 기사 당 1개) |
| 2. Content List | 생성된 퀴즈 목록 | `GET /api/quiz` |
| 5. 글 내보내기 | Button | `POST /api/content/export` → CSV 다운로드 |

## 3. 지난 설계 대비 실제 구현에서 바뀐/확정된 것

1. **결과는 task(기사×레벨) 단위로 즉시 저장.**
   지난 설계에서 "Done을 눌러야 저장된다"는 구조는 Stop 후 이탈 시 데이터가 날아가는 리스크가 있었습니다.
   지금 구현은 배치 실행 중 각 조합이 완료되는 즉시 `content_results` 테이블에 커밋되고,
   Stop은 **"아직 시작 안 한 조합을 더 이상 실행하지 않음"** 만 의미합니다. Done은 순수 화면 전환입니다.

2. **LLM 체인은 배치(job) 1회당 1번만 생성.**
   `pipeline_core.ContentPipeline`이 생성자에서 `filter_chain`/`structured_chain`/`fact_chain`/`content_chain`을
   전부 조립해두고, 같은 job 안의 모든 task가 이 인스턴스를 재사용합니다 (원본 `pipeline.py`는 매 호출마다 재생성).

3. **재생성 횟수 / 유사도 임계값 파라미터화.**
   `MAX_REGEN`, `SIMILARITY_THRESHOLD` 상수를 `ContentPipeline.run()` 인자로 뺐습니다.
   "생성 설정" 화면 값이 그대로 요청 바디(`max_regen`, `similarity_threshold`)로 들어갑니다.

4. **동시성.**
   `batch_runner.py`가 `ThreadPoolExecutor(max_workers=max_concurrency)`로 task를 병렬 처리합니다.
   Gemini Flash 무료/저가 티어는 분당 요청 제한이 있으니, 로컬에서 대량 배치 돌리실 땐
   `max_concurrency`를 3 이하로 유지하시길 권장합니다 (요청 바디에 파라미터로 열어뒀습니다, 기본값 3).

5. **콘텐츠 상태 3분류.**
   `success` / `similarity_failed`(재생성 소진 후에도 임계값 초과) / `filter_rejected`(Step1 REJECT) / `error`(예외).
   Content List 화면 체크박스는 `success` row만 기본 선택되도록 프론트에서 처리했습니다 —
   유사도 실패/필터 탈락 건은 사람이 명시적으로 체크해야 내보내기에 포함됩니다.

6. **CSV 내보내기 컬럼 오타 수정.**
   와이어프레임(Image5)에 있던 `content_body` 중복 → `content_level`로 고쳐서 구현했습니다.
   최종 스키마: `title, content_body, content_category, content_level` (UTF-8 BOM 포함, 엑셀 한글 깨짐 방지).

7. **퀴즈 생성 — 기사(글) 당 1개.**
   난이도별로 `content_results`가 최대 3개(초/중/고) 생겨도 퀴즈는 기사 당 1개만 만듭니다.
   중급(INTERMEDIATE) 성공 결과를 우선 사용하고, 없으면 성공한 다른 레벨을 사용합니다
   (`storage.pick_quiz_source_content`). 이미 해당 기사에 퀴즈가 있으면 재생성하지 않고 건너뜁니다.
   Content List 화면에서 콘텐츠를 체크하고 "퀴즈 생성" 버튼을 누르는 수동 트리거 방식이며,
   진행률 폴링 UI 없이 동기 응답으로 끝나는 단발성 액션입니다 (`quiz_runner.py`).
   `quiz` / `quiz_choice` 테이블에 저장되고, `quiz.content_id`는 `content_results.id`를 참조합니다.

## 4. 알려진 제약 (로컬 1인 도구 스코프라 의도적으로 단순화한 부분)

- **인증 없음.** Login 노드는 이번 스코프에서 제외했습니다 (필요해지면 알려주세요).
- **DB는 SQLite 파일 하나** (`backend/data.db`), ORM 없이 raw SQL. 동시 다중 사용자 쓰기에는 안 맞습니다 — 로컬 1인 사용 전제입니다.
- **배치 진행 상태는 프로세스 메모리에만 존재** (`batch_runner._JOBS` dict). 서버 재시작하면 진행 중이던 job 정보는 사라집니다.
  단, 이미 완료된 task 결과는 3번 항목대로 DB에 저장돼있어서 안전합니다.
- **직접 입력(수동 기사 등록)은 미구현.** 와이어프레임에서 회색 처리된 우선순위 외 항목이라 이번엔 CSV/XLSX 업로드만 구현했습니다.
- **카테고리 검증은 라벨 완전일치 기준.** `CATEGORY_LABELS`(경제/사회/정치/세계/생활·문화/IT·과학)와 정확히 일치해야
  매칭됩니다. 오타/공백은 업로드 프리뷰에서 "매칭 실패"로 빨간 표시되고 확정 저장에서 자동 제외됩니다.

## 5. 파일 구조

```
project/
  backend/
    main.py           # FastAPI 라우터 전체
    pipeline_core.py  # 원본 pipeline.py 리팩토링 (ContentPipeline 클래스)
    prompts.py         # 원본 그대로 (수정 없음)
    storage.py          # SQLite CRUD
    batch_runner.py     # 배치 job 오케스트레이션 (스레드+ThreadPoolExecutor+취소)
    quiz_runner.py       # 퀴즈 생성 오케스트레이션 (기사 당 1개, 중복 방지)
    file_parser.py       # CSV/XLSX 업로드 파싱 + 카테고리 검증
  frontend/
    index.html            # 바닐라 HTML/JS 단일 페이지 (디자인 최소, 기능 위주)
  requirements.txt
  .env.example
```

동작은 `python3 -m py_compile`로 문법 검증 완료했고, `TestClient` + 가짜(mock) LLM으로
업로드 → 필터 탈락 케이스 → 배치 생성 → Content List → CSV 내보내기까지 전체 플로우를 로컬에서
End-to-End로 실행 확인했습니다 (실제 Gemini 호출 부분만 목킹, 나머지 로직은 전부 실제 코드 경로).
실제 Gemini 키 넣고 돌리실 때 API 에러/레이트리밋 관련 메시지가 나오면 `max_concurrency`를 낮춰보세요.
