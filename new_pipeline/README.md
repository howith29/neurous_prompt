# 정책뉴스 전처리·병렬 생성 — CLI 및 로그

웹 서버·화면 없이 실행하고 SQLite에 결과를 저장합니다. 프로젝트 루트에서:

```sh
# 파싱만 확인 (모델 호출 없음)
.venv/bin/python -m new_pipeline.run research/policy-news-api/20261002/articles.json --parse-only --limit 2

# 파싱 → 적합성 → 분류 → 사실 추출 → 초급/중급/고급 병렬 생성
.venv/bin/python -m new_pipeline.run research/policy-news-api/20261002/articles.json --limit 3
```

JSON 배열 또는 원본 XML을 받습니다. 전체 실행은 루트 `.env`의 `GOOGLE_API_KEY`, 선택적으로 `GEMINI_MODEL`을 사용합니다. 기본 실행은 사실 추출 이후 LangChain RunnableParallel로 초급·중급·고급을 최대 3개 동시 실행합니다. `--preprocess-only`로 사실 추출까지만 실행할 수 있습니다. 자동 수집은 실행하지 않습니다.

실행마다 `new_pipeline/logs/<한국시간-실행ID>/`에 저장:
- `events.jsonl`: 기사별·단계별 시작, 완료, 판정 사유, 상위 두 분류, 오류 종류. 터미널에도 출력.
- `results.jsonl`: 콘텐츠·메타데이터·적합성·상위 두 분류·사실의 기사별 최종 결과. 각 기사 완료 즉시 기록.
- `generations.jsonl`: 난이도별 완료 즉시 저장하는 제목·본문·성공/실패·난이도 검증 결과.
- `quizzes.jsonl`: 난이도별 퀴즈 또는 오류·건너뜀 상태. `generation_id`로 생성 글과 연결.
- `summary.json`: 전처리 상태별 건수 및 생성 실패 기사 수.

`--output-dir`로 새 출력 폴더를 지정할 수 있습니다. 기존 폴더는 덮어쓰지 않습니다. `--limit`은 양수만 허용합니다. 오류 기사는 기록 후 다음 기사를 계속 처리하며 종료 코드 1을 반환합니다. REJECT/HOLD는 처리 오류와 구분합니다. 실행 도중 중단되어도 기록된 기사 결과와 로그는 남습니다.

원문의 시간대는 추측하지 않으며 실행 로그 시각은 Asia/Seoul입니다. 원문은 HTML 표시만 정리하고 모호한 캡션은 임의 삭제하지 않습니다. 카테고리 점수 전체는 저장하지 않습니다. 사람의 카테고리 검수 화면은 현재 제공하지 않습니다.

`backend/preprocessing.py`가 신규 핵심입니다. 기존 생성·저장 관련 모듈은 후속 연결용으로 남아 있으나 CLI에서는 글과 퀴즈를 생성하며 SQLite에도 완료 결과를 즉시 저장합니다. 이전 임시 웹 코드는 `legacy/new_web_snapshot/`에 보관했습니다.

검증: `.venv/bin/python -m unittest discover -s new_pipeline/tests -v`

기사 간에는 순차 처리하고, 한 기사 안에서만 세 난이도를 병렬 생성합니다. 같은 추출 사실을 공유하며 필터·분류·사실 추출을 반복하지 않습니다. 한 난이도가 실패해도 나머지는 완료·저장합니다. 전처리 `status`와 `generation_status`는 별도입니다. 난이도별 수치 기준을 함수로 검증하고 실패한 난이도만 최대 2회 재생성합니다.

각 병렬 갈래는 글 생성 → 난이도 기준 검증 → 글 즉시 저장 → 해당 글로 퀴즈 1개 생성 → 퀴즈 저장 순서입니다. 퀴즈는 `question`, `choices`(3개), `answerNo`(1~3) 형식을 유지합니다. 생성 실패·난이도 검증 탈락 글은 퀴즈를 건너뛰며, 퀴즈만 실패하면 글은 유지합니다. 글/퀴즈 연결에는 `generation_id`, 원문 연결에는 `news_id`를 사용합니다. 퀴즈의 문장·보기 길이는 기존 프롬프트 지시이며 코드 검증은 보기 개수·정답 번호입니다.


## SQLite 저장

기본 DB: `new_pipeline/data/pipeline.db`. `--db /경로/파일.db`로 변경할 수 있습니다.
별도 DB 서버나 패키지 설치가 필요하지 않습니다. 기존 JSONL 로그·결과도 유지합니다.

| 테이블 | 저장 내용 | 연결 |
|---|---|---|
| pipeline_runs | 실행 시각·상태·요약·로그 경로 | run_id |
| article_snapshots | API 원본, 콘텐츠, 메타데이터, 필터·분류·사실 | run_id, news_id, snapshot_id |
| generated_contents | 난이도별 글과 생성 결과 | snapshot_id, generation_id |
| generated_quizzes | 문제·보기·정답 번호와 성공/오류/건너뜀 | generation_id |

같은 기사를 다시 실행해도 과거 결과를 덮어쓰지 않고 실행별 스냅샷으로 보존합니다. 따라서 news_id는 여러 실행에서 반복될 수 있습니다. 글과 퀴즈는 생성 당시의 원문 스냅샷으로 연결됩니다. 기사 전처리 완료, 각 난이도 글 완료, 각 퀴즈 완료 시 각각 커밋합니다. 병렬 저장은 잠금과 SQLite WAL로 처리합니다. 강제 프로세스 종료 시 실행 상태가 running으로 남을 수 있지만 이미 커밋된 결과는 유지됩니다.

DB에는 이번 연결 이후 실행한 결과가 저장됩니다. 이전 JSONL 기록은 자동 이관하지 않습니다.

## DB 확인 (읽기 전용)

프로젝트 루트에서 실행:

```sh
.venv/bin/python -m new_pipeline.inspect_db
.venv/bin/python -m new_pipeline.inspect_db --news-id 148973100
```

첫 명령은 최근 실행 목록과 최신 실행의 단계별 건수, 두 번째는 기사별 원문·메타·판정·생성글·퀴즈를 출력합니다. `--run 실행ID`로 특정 실행을 집계할 수 있습니다. SQLite에서 직접 확인하려면:

```sh
sqlite3 -readonly new_pipeline/data/pipeline.db
```

```sql
.headers on
.mode column
SELECT run_id, started_at, status, summary_json FROM pipeline_runs ORDER BY started_at DESC;
SELECT news_id, status, json_extract(metadata_json, '$.parsed_at') AS parsed_at FROM article_snapshots;
SELECT level, status, COUNT(*) FROM generated_contents GROUP BY level, status;
.quit
```

`parsed_at`은 실제 파싱 시각(UTC)입니다. 원문 `modified_at`과 별개이며 기존 기록에 소급 생성하지 않습니다. 파싱 결과는 LLM 호출 전에 DB에 먼저 커밋됩니다.

## 글 생성 후 검증

각 난이도는 생성 → 검증 → (실패 시 최대 2회 재생성) → 글 저장 → 통과한 글만 퀴즈 생성 순서입니다.
제목 제외 본문의 공백 제외 글자 수·빈 줄 기준 문단 수·문장 수·마침표 종료를 검사합니다. 숫자 사이 소수점은 문장 경계에서 제외합니다. 검증 값은 generated_contents.result_json의 validation에 저장하고 로그에도 남깁니다. 실패 시 validation_failed 및 quiz skipped로 기록합니다.
유사도 검사는 사용하지 않습니다. 표현·문장 구조는 생성 프롬프트의 자체 점검으로 안내합니다. 사실 정확성, 배경 설명 비중, 어휘·문장 구조의 난이도 및 읽기 시간 측정은 아직 별도 검증하지 않습니다.
