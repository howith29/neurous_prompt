

## Neurous 콘텐츠 파이프라인

정책뉴스를 파싱하고 적합성·주제·사실을 추출한 뒤 초급·중급·고급 글과 퀴즈를 생성합니다. 신규 방식은 웹 UI 없이 SQLite와 로그에 저장합니다.

- [사용 방법](new_pipeline/README.md)
- [콘텐츠 피드백 반영](new_pipeline/docs/content-feedback-20261026.md)
- [분리된 레거시 앱](legacy/README.md)

Python 3.12 이상에서 `pip install -r requirements.txt`로 설치합니다. 루트 `.env`에 `GOOGLE_API_KEY`, 선택적으로 `GEMINI_MODEL`을 설정합니다. API 수집은 `DATA_GO_KR_SERVICE_KEY`가 필요합니다. 키·DB·수집 및 생성 데이터는 포함하지 않습니다.

실행: `python -m new_pipeline.run <JSON/XML 입력 파일>`

테스트: `python -m unittest discover -s new_pipeline/tests -v`
