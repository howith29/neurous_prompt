# file_parser.py
# Image3 "뉴스 기사 업로드" 화면 로직: 파일 파싱 -> 프리뷰(카테고리 매칭 검증) -> 확정 저장은 storage.py 로.

import io

import pandas as pd

from .prompts import CATEGORY_LABELS

# 업로드 파일 한글 컬럼 -> 내부 필드명
COLUMN_MAP = {"카테고리": "category_label", "제목": "title", "본문": "content", "링크": "source_url"}
REQUIRED_COLUMNS = set(COLUMN_MAP.keys())
LABEL_TO_CODE = {v: k for k, v in CATEGORY_LABELS.items()}


def _normalize_label(label: str) -> str:
    """가운뎃점(·) 표기를 슬래시(/)나 공백으로 잘못 쓰는 실수가 흔해서 비교 전 정규화."""
    return label.strip().replace("/", "·").replace(" ", "")


NORMALIZED_LABEL_TO_CODE = {_normalize_label(v): k for k, v in CATEGORY_LABELS.items()}


# 국내에서 엑셀로 "CSV 저장"하면 기본 인코딩이 UTF-8이 아닌 경우가 흔해서,
# UTF-8 -> UTF-8-SIG(BOM) -> CP949(EUC-KR) 순으로 순차 시도한다.
CSV_ENCODINGS = ["utf-8", "utf-8-sig", "cp949"]


def _read_csv_with_encoding_fallback(file_bytes: bytes) -> pd.DataFrame:
    last_error = None
    for enc in CSV_ENCODINGS:
        try:
            text = file_bytes.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError) as e:
            last_error = e
            text = None
    if text is None:
        raise ValueError(
            f"CSV 인코딩을 인식하지 못했습니다 (시도: {', '.join(CSV_ENCODINGS)}). "
            f"파일을 UTF-8로 다시 저장 후 업로드해주세요. ({last_error})"
        )
    return _parse_text_with_header_detection(text)


def _parse_text_with_header_detection(text: str) -> pd.DataFrame:
    """
    "표 1" 같은 캡션/제목 줄이 실제 헤더(카테고리,제목,본문,링크) 위에 껴있는 경우를 대비해,
    필수 컬럼명이 모두 들어있는 첫 줄을 헤더로 잡아 그 줄부터 다시 파싱한다.
    엑셀에서 내보낸 파일에 흔한 패턴이라 자동으로 스킵해준다.
    """
    lines = text.splitlines()
    header_idx = 0
    for i, line in enumerate(lines[:10]):  # 상단 10줄 안에서만 탐색 (그 이상은 진짜 데이터 손상으로 간주)
        if all(col in line for col in REQUIRED_COLUMNS):
            header_idx = i
            break
    else:
        # 못 찾으면 원래대로 첫 줄을 헤더로 시도 (에러 메시지는 아래 missing 체크에서 나감)
        header_idx = 0

    df = pd.read_csv(io.StringIO(text), skiprows=header_idx)
    # 엑셀에서 흔히 붙는 trailing comma로 생기는 완전 빈 컬럼(Unnamed: N) 제거
    df = df.loc[:, ~df.columns.str.startswith("Unnamed:")]
    return df


def _read_excel_with_sheet_detection(file_bytes: bytes) -> pd.DataFrame:
    """여러 시트로 구성된 엑셀(예: 안내용 'Info' 시트 + 실제 데이터 'News' 시트)을 대비해,
    필수 컬럼(카테고리/제목/본문/링크)이 모두 있는 첫 번째 시트를 찾아 사용한다.
    (기본 pd.read_excel은 항상 첫 시트만 읽어서, 안내 시트가 앞에 있으면 컬럼 누락 에러가 났었음)"""
    sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None)
    for df in sheets.values():
        if REQUIRED_COLUMNS <= set(df.columns):
            return df
    # 못 찾으면 첫 시트를 그대로 반환 (기존처럼 아래 missing-컬럼 에러 메시지로 안내)
    return next(iter(sheets.values()))


def parse_upload(filename: str, file_bytes: bytes) -> list[dict]:
    """
    반환: [{row_idx, category_label, category_code, category_valid, title, content, source_url}, ...]
    category_valid=False 인 row는 프론트에서 빨간 표시 + 확정 저장 시 차단 대상.
    """
    if filename.lower().endswith(".csv"):
        df = _read_csv_with_encoding_fallback(file_bytes)
    elif filename.lower().endswith((".xlsx", ".xls")):
        df = _read_excel_with_sheet_detection(file_bytes)
    else:
        raise ValueError("CSV 또는 XLSX 파일만 지원합니다.")

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"필수 컬럼 누락: {', '.join(missing)} (필요 컬럼: 카테고리/제목/본문/링크)")

    df = df.rename(columns=COLUMN_MAP)
    df = df.fillna("")  # 빈 셀(NaN)이 str(NaN) -> "nan" 문자열로 새어나가는 것 방지
    rows = []
    for i, r in df.iterrows():
        label = str(r.get("category_label", "")).strip()
        code = LABEL_TO_CODE.get(label) or NORMALIZED_LABEL_TO_CODE.get(_normalize_label(label))
        rows.append({
            "row_idx": int(i),
            "category_label": label,
            "category_code": code,
            "category_valid": code is not None,
            "title": str(r.get("title", "")).strip(),
            "content": str(r.get("content", "")).strip(),
            "source_url": str(r.get("source_url", "")).strip(),
        })
    return rows
