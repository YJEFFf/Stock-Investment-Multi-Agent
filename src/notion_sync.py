"""매매일지·프로젝트 소개 페이지를 노션에 자동으로 채운다.

포트폴리오 피스로서의 노션 워크스페이스 — 사람이 나중에 수동으로 채울 필드는
없다(사용자 확정, 2026-08-09). logs/trade_journal.jsonl이 유일한 원천이고, 이
모듈은 그걸 노션이 읽기 좋은 형태로 그대로 옮기기만 한다.

kis.py와 같은 관례: 네트워크·429(rate limit)만 재시도하고, 그 외 4xx(스키마
오류, 권한 없음 등)는 재시도해도 같은 응답이 나오므로 즉시 실패로 처리한다.
"""

import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from src import collectors, translate
from src.schemas import PortfolioState

load_dotenv()

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
REQUEST_TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

GITHUB_URL = "https://github.com/YJEFFf/Stock-Investment-Multi-Agent"

# pipeline.py의 DEFAULT_LOG_PATH/DEFAULT_TRADE_JOURNAL_LOG_PATH와 같은 경로 문자열을
# 여기서 다시 상수로 든다 — pipeline.py를 임포트하지 않기 위해서다(순환 임포트는
# 없지만, notion_sync는 "노션에 뭘 쓰나"가 고치는 이유고 pipeline은 "판단 로직"이
# 고치는 이유라 굳이 묶지 않는다. llm.py의 DEFAULT_LLM_CALL_LOG_PATH와 같은 패턴).
DEFAULT_PIPELINE_LOG_PATH = Path("logs/pipeline.jsonl")
DEFAULT_TRADE_JOURNAL_LOG_PATH = Path("logs/trade_journal.jsonl")
DEFAULT_DAILY_REPORT_STATE_PATH = Path("logs/notion_daily_report_state.json")

# CLAUDE.md/PLAN.md와 다른 파일로 관리하는 이유: 코드가 아니라 노션 표시용
# 텍스트라 손보는 이유가 다르다(문서 내용이 아니라 노션 블록 포맷을 고치게 됨).
DEFAULT_SYNC_STATE_PATH = Path("logs/notion_sync_state.json")

REASON_LABELS = {
    "stop_loss": "손절",
    "take_profit_trail": "익절",
    "llm_discretionary": "LLM재량매도",
    "price_data_unavailable": "시세조회실패",
    "gap_too_large": "갭초과",
    "balance_unavailable": "잔고조회실패",
    "quantity_zero": "수량0",
    "order_rejected": "주문거부",
}


def _display_name(ticker: str) -> str:
    """노션에는 종목코드 대신 종목명을 보여준다(사용자 요청, 2026-08-12).
    pipeline.display_name과 로직이 같지만 여기서 따로 두는 이유는
    DEFAULT_TRADE_JOURNAL_LOG_PATH 등과 같다 — 노션 표시용 텍스트를 고치는 이유와
    판단 로직을 고치는 이유가 다르니 pipeline을 임포트하지 않는다. 실패해도
    코드로 폴백할 뿐 동기화 자체를 막지 않는다."""
    names = collectors.fetch_kospi200_ticker_names()
    return (names or {}).get(ticker, ticker)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('NOTION_API_KEY', '')}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _notion_request(method: str, path: str, body: dict | None = None) -> dict | None:
    url = f"{NOTION_API_BASE}{path}"
    last_error: Exception | str | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(
                method, url, headers=_headers(), json=body, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            last_error = exc
            logger.warning("notion_request_failed method=%s path=%s attempt=%d error=%s", method, path, attempt, exc)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", RETRY_BACKOFF_SECONDS))
            last_error = "rate_limited"
            logger.warning("notion_rate_limited path=%s attempt=%d retry_after=%.1f", path, attempt, retry_after)
            time.sleep(retry_after)
            continue

        if response.status_code >= 400:
            logger.error(
                "notion_api_error method=%s path=%s status=%d body=%s",
                method,
                path,
                response.status_code,
                response.text[:500],
            )
            return None

        return response.json()

    logger.error("notion_request_exhausted method=%s path=%s error=%s", method, path, last_error)
    return None


# --- 블록 빌더 ---


def _rich_text(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content}}]


def _heading(content: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": _rich_text(content)}}


def _paragraph(content: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(content)}}


def _bulleted(content: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich_text(content)}}


def _bookmark(url: str) -> dict:
    return {"object": "block", "type": "bookmark", "bookmark": {"url": url}}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _truncate(text: str | None, limit: int = 1900) -> str:
    """노션 rich_text 블록 하나는 2000자 제한 — 원문 전체는 항상
    logs/trade_journal.jsonl에 남아있으니 노션 쪽은 잘려도 괜찮다."""
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


# --- 워크스페이스 뼈대 생성 (1회성 셋업) ---


def add_landing_page_content(parent_page_id: str) -> bool:
    """사용자가 미리 만들어 integration과 연결해둔 페이지 자체를 랜딩 페이지로
    쓴다 — 새 페이지를 만드는 게 아니라 그 페이지에 소개+GitHub 링크 블록을
    붙인다."""
    children = [
        _heading("SIMA", level=1),
        _paragraph(
            "한국 주식(KOSPI200) 대상 멀티에이전트 투자 판단 시스템 — "
            "LLM 분석가들이 종목마다 독립적으로 의견을 내고, 결정론적 리스크 게이트가 최종 승인한다."
        ),
        _bookmark(GITHUB_URL),
        _divider(),
    ]
    result = _notion_request("PATCH", f"/blocks/{parent_page_id}/children", {"children": children})
    return result is not None


def _code_block(content: str, language: str = "plain text") -> dict:
    return {"object": "block", "type": "code", "code": {"rich_text": _rich_text(content), "language": language}}


ARCHITECTURE_DIAGRAM = """\
[수집: 코드]      시세·지표(한투·pykrx)   뉴스(RSS·크롤러)   공시(DART API)
                        |                      |                  |
[분석: LLM]        차트 분석가            뉴스 분석가         공시 분석가
                        |                      |                  |
                        +----------------------+------------------+
                                               |
[판단: LLM]                          강세/약세 토론  (반대 논거 강제 생성)
                                               |
                                    포트폴리오 매니저 (근거 종합)
                                               |
[게이트: 코드]                   리스크 게이트 (한도 초과 시 무조건 거부)
                                               |
[집행: 코드]                              주문 집행
"""

CORE_CONTRACT_CODE = """\
class AnalystOpinion(BaseModel):
    agent: str                       # "chart" | "news" | "disclosure" | "quant"
    ticker: str
    score: float                     # -1.0 ~ 1.0
    confidence: float                # 0.0 ~ 1.0
    evidence: list[str]              # 원문 ID + 프롬프트 버전 ("prompt:chart@a3f2c1")
    as_of: datetime                  # 이 판단이 사용한 데이터의 최신 시점

class Decision(BaseModel):
    ticker: str
    action: Literal["BUY", "HOLD"]   # 매도는 별도 경로
    reason: str
    inputs: list[AnalystOpinion]     # 이 결정을 만든 의견 전체
    degraded: bool                   # 분석가 일부가 실패한 상태에서 나온 결정인가

class GateResult(BaseModel):
    approved: bool
    rejected_by: str | None          # "position_limit" | "sector_concentration" | ...
"""


def _intro_page_blocks() -> list[dict]:
    return [
        _paragraph(
            "KOSPI200 유니버스 대상 멀티에이전트 투자 판단 시스템. "
            "LLM 분석가들이 종목마다 독립적으로 의견을 내고, 결정론적 리스크 게이트가 최종 승인한다."
        ),
        _heading("왜 이런 제약이 있는가", level=2),
        _paragraph(
            "이전 버전은 실패했다. 원인은 코드 품질이 아니라 설계였다. 스케줄러가 "
            "\"pending 매수 신호가 5개 미만이면 분석을 다시 실행\"하는 구조였고, "
            "이것이 사실상 일일 매수 할당량으로 작동했다. 같은 종목을 반복해서 보여주니 "
            "결국 통과했다. 문턱을 낮춘 적은 없지만 재시도가 문턱을 대신 낮췄다."
        ),
        _paragraph(
            "그 결과 매일 매수가 발생했고, 신호에 예측력이 있는지 자체를 측정할 수 없게 됐다 — "
            "좋아서 산 건지 채워야 해서 산 건지 구분이 불가능했기 때문이다."
        ),
        _heading("절대 규칙", level=2),
        _bulleted("기본 상태는 현금이다 — 매수 없음이 정상 출력이며, 실패나 예외가 아니다."),
        _bulleted("목표 신호 개수 파라미터를 만들지 않는다 — 상위 N개 선택 금지."),
        _bulleted("절대 문턱으로만 거른다 — 후보가 0개인 날이 있어야 정상이다."),
        _bulleted("재시도는 데이터 수집 실패에만 허용한다 — 결과가 마음에 안 들어서 재시도 금지."),
        _bulleted("LLM에게 종목 비교를 시키지 않는다 — 종목당 독립 호출로 판정, 다른 후보 정보는 컨텍스트에 안 넣는다."),
        _bulleted("최종 승인권은 코드가 갖는다 — LLM은 거부권만 가진다."),
        _bulleted("실거래 연결 금지 — 모의투자로만 검증, 실거래 전환은 사용자 명시적 지시가 있을 때만."),
        _divider(),
        _heading("아키텍처", level=2),
        _paragraph("수집·게이트·집행은 결정론적 코드다. 분석가는 데이터를 직접 가져오지 않고 MarketContext로 주입받는다."),
        _code_block(ARCHITECTURE_DIAGRAM),
        _heading("핵심 계약 (src/schemas.py)", level=2),
        _paragraph(
            "계층 간 데이터는 자연어 문자열이 아니라 이 스키마로만 주고받는다. "
            "분석가는 AnalystOpinion | None을 반환한다 — \"데이터 부족으로 판단 불가\"와 "
            "\"판단했으나 점수가 낮음\"은 다른 상태라 None과 낮은 점수를 구분한다."
        ),
        _code_block(CORE_CONTRACT_CODE, language="python"),
        _heading("리스크 게이트 수치 (2026-08-08 확정)", level=2),
        _bulleted("종목당 최대 비중 15%"),
        _bulleted("섹터 집중도 한도 40%"),
        _bulleted("일일 손실 한도 -5% (도달 시 그날 신규 매수 중단)"),
        _bulleted("총 노출 한도 100% (개별 한도로만 통제, 별도 상한 없음)"),
        _heading("매도 로직 (2026-08-09 확정, 2026-08-15 종목별 조정)", level=2),
        _bulleted(
            "결정론적 안전장치(항상 실행): 손절 도달 시 전량 매도, 익절 트리거부터 잔량의 일정 비율씩 "
            "트레일링 매도. 문턱은 종목마다 다르다 — 진입 시점에 포트폴리오 매니저가 그 종목의 변동성에 "
            "맞춰 정하고(손절 3~15%, 익절은 항상 그 2배), 청산까지 바뀌지 않는다"
        ),
        _bulleted(
            "보유 중에는 문턱을 재조정하지 않는다 — 손실 종목 앞에서 손절선을 다시 물으면 넓히는 쪽 "
            "논거가 반드시 나오기 때문이다. 분석가가 일부 빠진 판단(degraded)은 고정 기본값(-10%/+20%/"
            "1-3씩/-7%)으로 떨어진다"
        ),
        _bulleted(
            "LLM 재량 매도(보유 종목 재평가, 옵션): 매수용 강세/약세 토론+매니저와 대칭 구조이지만 "
            "게이트를 거치지 않고 그대로 실행된다 — 대신 프롬프트 자체를 보수적으로 설계해 상쇄한다"
        ),
        _heading("기술 스택", level=2),
        _bulleted("Python 3.11+, pydantic v2, asyncio"),
        _bulleted(
            "Anthropic API 직접 호출 — LangGraph/CrewAI/AutoGen 미사용. "
            "에이전트 간 대화나 동적 툴 호출이 없고 병렬 호출 후 스키마 취합이 전부라 "
            "프레임워크는 디버깅만 어렵게 만든다"
        ),
        _bulleted(
            "분석가 호출은 asyncio.gather(..., return_exceptions=True) — "
            "DART가 죽어도 차트 분석가는 돌아야 한다. 분석가 일부가 빠진 채 나온 결정은 "
            "Decision.degraded=True로 표시하고 게이트에서 기준을 높인다"
        ),
        _bulleted("테스트는 pytest, LLM 호출은 고정 응답으로 목킹 — 파이프라인 로직은 네트워크 없이 전부 테스트 가능"),
        _heading("구축 순서 — 아래에서 위로", level=2),
        _paragraph(
            "다이어그램의 아래쪽(게이트·집행)부터 만들었다. 분석가부터 만들면 결과가 나오는 순간 "
            "\"이 종목 사볼까\"가 되고 게이트는 계속 뒤로 밀린다 — 이전 프로젝트가 무너진 지점이다."
        ),
        _bulleted("마일스톤 1 — 아무것도 사지 않는 시스템: 분석가 자리에 랜덤 점수 더미를 넣고 게이트·집행·로깅 완성"),
        _bulleted("마일스톤 2 — 분석가 투입: 차트 → 뉴스 → 공시 순으로 하나씩 붙이며 신호 발생률 변화 기록"),
        _bulleted(
            "마일스톤 3 — 측정: IC(정보계수, 예측값과 실제 수익률의 순위상관) 0.03~0.05가 안정적으로 "
            "나오면 유의미한 신호. 백테스트 수익률은 성공 지표로 안 쓴다 — LLM은 과거 뉴스 이후 "
            "무슨 일이 있었는지 이미 학습해서 알고 있어 과거 구간 성능이 부풀려진다"
        ),
        _heading("감시 지표", level=2),
        _bulleted("최근 20영업일 중 매수 신호가 난 날의 비율 — 매일 신호가 나면 시스템이 고장 난 것"),
        _bulleted("게이트 거부 사유별 건수 — 특정 룰만 계속 발동하면 그 룰이 과하거나 분석가가 한쪽으로 쏠려 있다는 뜻"),
        _bulleted("분석가별 호출 수·실패율·토큰 사용량"),
        _divider(),
        _paragraph("전체 코드와 설계 문서(CLAUDE.md, docs/PLAN.md)는 GitHub에서 그대로 볼 수 있다."),
        _bookmark(GITHUB_URL),
    ]


def create_intro_page(parent_page_id: str) -> str | None:
    """CLAUDE.md/docs/PLAN.md 요약 — 왜 이렇게 설계했는지 방문자에게 보여주는 페이지.
    링크를 따라가게 하지 않고 핵심 내용을 페이지 안에 직접 다 적어둔다(사용자 요청,
    2026-08-09)."""
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "properties": {"title": {"title": _rich_text("프로젝트 소개 · 설계 철학")}},
        "children": _intro_page_blocks(),
    }
    result = _notion_request("POST", "/pages", body)
    return result["id"] if result else None


def _list_block_children(block_id: str) -> list[dict]:
    result = _notion_request("GET", f"/blocks/{block_id}/children?page_size=100")
    return result["results"] if result else []


def refresh_intro_page(page_id: str) -> bool:
    """이미 만들어진 소개 페이지의 내용을 최신 _intro_page_blocks()로 통째로
    교체한다 — CLAUDE.md/docs/PLAN.md가 바뀌었을 때 다시 실행해서 노션도
    맞춘다. 기존 블록을 전부 지우고 새로 채우는 방식(부분 diff가 아니다)."""
    for block in _list_block_children(page_id):
        _notion_request("DELETE", f"/blocks/{block['id']}")

    result = _notion_request("PATCH", f"/blocks/{page_id}/children", {"children": _intro_page_blocks()})
    return result is not None


def create_trade_journal_database(parent_page_id: str) -> str | None:
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "매매일지"}}],
        "properties": {
            "이름": {"title": {}},
            "날짜": {"date": {}},
            "티커": {"rich_text": {}},
            "구분": {
                "select": {
                    "options": [
                        {"name": "매수", "color": "green"},
                        {"name": "매도", "color": "red"},
                    ]
                }
            },
            "사유": {
                "select": {
                    "options": [
                        {"name": "신규매수", "color": "green"},
                        {"name": "손절", "color": "red"},
                        {"name": "익절", "color": "orange"},
                        {"name": "LLM재량매도", "color": "yellow"},
                    ]
                }
            },
            "가격": {"number": {"format": "won"}},
            "수량": {"number": {"format": "number"}},
            "총매수금액": {"number": {"format": "won"}},
            "매도금액": {"number": {"format": "won"}},
            # 아래 둘은 **이 종목 보유분을 100%로 놓은** 비율이다(합이 항상 100%).
            # 포트폴리오 전체 대비로 적으면 "1.19% 축소" 같은 값이 되어 읽는 사람이
            # 해석할 수 없다 — 일지는 사람이 읽으라고 쓰는 것이다(사용자 요청).
            "매도비중": {"number": {"format": "percent"}},
            "잔여비중": {"number": {"format": "percent"}},
            "잔여수량": {"number": {"format": "number"}},
            "실현손익률": {"number": {"format": "percent"}},
            "보유일수": {"number": {"format": "number"}},
        },
    }
    result = _notion_request("POST", "/databases", body)
    return result["id"] if result else None


# 이미 만들어진 매매일지 DB에 나중에 추가된 속성들. create_trade_journal_database는
# DB가 없을 때 한 번만 도는데(setup_notion_workspace.py가 .env에 ID가 있으면 건너뜀),
# 노션은 존재하지 않는 속성에 값을 쓰면 400을 낸다 — 스키마만 고치면 그날부터 매도
# 동기화가 통째로 실패한다. 새 속성을 추가할 때는 위 create 함수와 여기 둘 다 넣을 것.
_TRADE_JOURNAL_ADDED_PROPERTIES = {
    "매도금액": {"number": {"format": "won"}},
    "매도비중": {"number": {"format": "percent"}},
    "잔여비중": {"number": {"format": "percent"}},
    "잔여수량": {"number": {"format": "number"}},
}


def ensure_trade_journal_properties(database_id: str) -> bool:
    """매매일지 DB에 나중에 추가된 속성들을 채워 넣는다. 이미 있으면 노션이 그대로
    두므로 몇 번 돌려도 안전하다."""
    result = _notion_request(
        "PATCH", f"/databases/{database_id}", {"properties": _TRADE_JOURNAL_ADDED_PROPERTIES}
    )
    return result is not None


# --- 매매일지 동기화 ---


def _buy_row_properties(entry: dict) -> dict:
    return {
        "이름": {"title": _rich_text(f"{_display_name(entry['ticker'])} 매수")},
        "날짜": {"date": {"start": entry["day"]}},
        "티커": {"rich_text": _rich_text(entry["ticker"])},
        "구분": {"select": {"name": "매수"}},
        "사유": {"select": {"name": "신규매수"}},
        "가격": {"number": entry["entry_price"]},
        "수량": {"number": entry["quantity"]},
        "총매수금액": {"number": entry["entry_price"] * entry["quantity"]},
    }


async def _buy_row_children(entry: dict) -> list[dict]:
    """분석가·토론·매니저는 전부 영어로 판단한다(src/translate.py 참고) — 여기서
    노션에 실제로 적을 때만 한국어로 옮긴다. 원문은 logs/trade_journal.jsonl에
    그대로 남아있다."""
    decision = entry["decision"]
    reason_ko = await translate.to_korean(decision["reason"], label="translate_buy_reason")
    blocks = [_heading("매니저 최종 판단", level=3), _paragraph(_truncate(reason_ko))]

    if decision.get("inputs"):
        blocks.append(_heading("분석가 의견", level=3))
        for op in decision["inputs"]:
            blocks.append(_bulleted(f"{op['agent']}: score={op['score']:.2f}, confidence={op['confidence']:.2f}"))

    if decision.get("debate"):
        blocks.append(_heading("토론 논거", level=3))
        for d in decision["debate"]:
            argument_ko = await translate.to_korean(d["argument"], label="translate_debate_argument")
            blocks.append(_bulleted(f"[{d['stance']}] (강도 {d['strength']:.2f}) {_truncate(argument_ko, 500)}"))

    return blocks


def _sell_row_properties(entry: dict) -> dict:
    properties = {
        "이름": {"title": _rich_text(f"{_display_name(entry['ticker'])} 매도")},
        "날짜": {"date": {"start": entry["day"]}},
        "티커": {"rich_text": _rich_text(entry["ticker"])},
        "구분": {"select": {"name": "매도"}},
        "사유": {"select": {"name": REASON_LABELS.get(entry["reason"], entry["reason"])}},
        "가격": {"number": entry["exit_price"]},
    }
    if entry.get("realized_pnl_pct") is not None:
        properties["실현손익률"] = {"number": round(entry["realized_pnl_pct"], 4)}
    if entry.get("holding_days") is not None:
        properties["보유일수"] = {"number": entry["holding_days"]}
    # 아래 값들은 이 기능(2026-08-15) 이전에 쌓인 매도 로그엔 없다 — 없으면 그냥 비운다.
    if entry.get("shares_sold") is not None:
        properties["수량"] = {"number": entry["shares_sold"]}
    if entry.get("shares_after") is not None:
        properties["잔여수량"] = {"number": entry["shares_after"]}
    if entry.get("sell_amount") is not None:
        properties["매도금액"] = {"number": round(entry["sell_amount"])}
    # 이 종목 보유분 대비 비율(합 100%). 포트폴리오 전체 대비 값(portfolio_weight_*)은
    # 로그엔 남지만 노션엔 안 띄운다 — 읽는 사람에게 의미가 없다.
    if entry.get("position_fraction_sold") is not None:
        properties["매도비중"] = {"number": round(entry["position_fraction_sold"], 4)}
    if entry.get("position_fraction_remaining") is not None:
        properties["잔여비중"] = {"number": round(entry["position_fraction_remaining"], 4)}
    return properties


async def _sell_row_children(entry: dict) -> list[dict]:
    if not entry.get("reasoning"):
        return []
    reasoning_ko = await translate.to_korean(entry["reasoning"], label="translate_sell_reasoning")
    return [_heading("판단 사유", level=3), _paragraph(_truncate(reasoning_ko))]


def _buy_skipped_row_properties(entry: dict) -> dict:
    return {
        "이름": {"title": _rich_text(f"{_display_name(entry['ticker'])} 매수 스킵")},
        "날짜": {"date": {"start": entry["day"]}},
        "티커": {"rich_text": _rich_text(entry["ticker"])},
        "구분": {"select": {"name": "매수스킵"}},
        "사유": {"select": {"name": REASON_LABELS.get(entry["reason"], entry["reason"])}},
    }


def _buy_skipped_row_children(entry: dict) -> list[dict]:
    detail = f" (갭 {entry['gap_pct']:.2%})" if entry.get("gap_pct") is not None else ""
    return [
        _paragraph(
            "리스크 게이트는 승인했지만 체결 직전 단계에서 실제 매수까지는 가지 못했다 — "
            f"사유: {REASON_LABELS.get(entry['reason'], entry['reason'])}{detail}."
        )
    ]


def _entry_key(entry: dict, occurrence: int = 0) -> str:
    """occurrence는 같은 (event, ticker, day) 조합이 로그에 몇 번째로 등장했는지다
    — 트레일링 익절처럼 같은 종목을 같은 날 두 번(1/3씩) 매도하면 기본 키가
    충돌해서 두 번째 매도가 노션에 조용히 스킵됐다(2026-08-12 발견). 첫 번째
    등장은 기존 키 형식을 그대로 써서 이미 동기화된 state 파일과 계속 맞물리게
    하고, 두 번째부터만 접미사를 붙인다."""
    base = f"{entry['event']}:{entry['ticker']}:{entry['day']}"
    return base if occurrence == 0 else f"{base}:{occurrence}"


def _load_synced_keys(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    return set(json.loads(state_path.read_text()).get("synced_keys", []))


def _save_synced_keys(state_path: Path, keys: set[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"synced_keys": sorted(keys)}, ensure_ascii=False, indent=2))


async def sync_trade_journal(
    trade_journal_log_path: Path,
    database_id: str,
    state_path: Path = DEFAULT_SYNC_STATE_PATH,
) -> dict:
    """logs/trade_journal.jsonl에서 아직 노션에 안 올라간 이벤트만 새로 올린다.

    이미 올라간 이벤트는 state_path(로컬 파일)에 키로 남겨 다시 안 올린다 —
    노션에 매번 쿼리해서 중복을 확인하는 대신 로컬 상태를 신뢰한다
    (portfolio_state.json과 같은 패턴). 실패한 이벤트는 키를 안 남기므로
    다음 실행 때 자동으로 재시도된다.
    """
    if not trade_journal_log_path.exists():
        return {"synced": 0, "failed": 0, "skipped": 0}

    synced_keys = _load_synced_keys(state_path)
    lines = [line for line in trade_journal_log_path.read_text().splitlines() if line.strip()]

    occurrence_counts: dict[tuple[str, str, str], int] = {}
    synced = 0
    failed = 0
    skipped = 0
    for line in lines:
        entry = json.loads(line)
        group = (entry["event"], entry["ticker"], entry["day"])
        occurrence = occurrence_counts.get(group, 0)
        occurrence_counts[group] = occurrence + 1
        key = _entry_key(entry, occurrence)
        if key in synced_keys:
            skipped += 1
            continue

        if entry["event"] == "buy":
            properties = _buy_row_properties(entry)
            children = await _buy_row_children(entry)
        elif entry["event"] == "buy_skipped":
            properties = _buy_skipped_row_properties(entry)
            children = _buy_skipped_row_children(entry)
        else:
            properties = _sell_row_properties(entry)
            children = await _sell_row_children(entry)

        result = _notion_request(
            "POST",
            "/pages",
            {"parent": {"database_id": database_id}, "properties": properties, "children": children},
        )
        if result is None:
            logger.warning("notion_sync_row_failed key=%s", key)
            failed += 1
            continue

        synced_keys.add(key)
        synced += 1

    _save_synced_keys(state_path, synced_keys)
    logger.info("notion_sync_done synced=%d failed=%d skipped=%d", synced, failed, skipped)
    return {"synced": synced, "failed": failed, "skipped": skipped}


# --- 일일 리포트 (장마감 후 그날 하루 요약) ---


def create_daily_report_database(parent_page_id: str) -> str | None:
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "일일 리포트"}}],
        "properties": {
            "이름": {"title": {}},
            "날짜": {"date": {}},
            "매수": {"number": {"format": "number"}},
            "매도": {"number": {"format": "number"}},
            "보유종목수": {"number": {"format": "number"}},
            "현금비중": {"number": {"format": "percent"}},
        },
    }
    result = _notion_request("POST", "/databases", body)
    return result["id"] if result else None


def _read_jsonl_for_day(log_path: Path, day: str) -> list[dict]:
    if not log_path.exists():
        return []
    entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    return [e for e in entries if e.get("day") == day]


def _decision_label(entry: dict) -> str:
    if entry["action"] == "BUY":
        return "BUY(승인)" if entry["approved"] else f"BUY(거부:{entry['rejected_by']})"
    return "HOLD"


async def _daily_report_children(
    day: str,
    decisions_today: list[dict],
    buys: list[dict],
    sells: list[dict],
    skips: list[dict],
    portfolio: PortfolioState,
    total_value: float | None = None,
) -> list[dict]:
    blocks = [_heading("오늘의 판단 요약", level=2)]
    if decisions_today:
        approved_buy_decisions = [d for d in decisions_today if d["action"] == "BUY" and d["approved"]]
        rejected = sum(1 for d in decisions_today if not d["approved"])
        blocks.append(
            _paragraph(
                f"총 {len(decisions_today)}개 종목 판단 · BUY 승인 {len(approved_buy_decisions)}개 · "
                f"게이트 거부 {rejected}개 · 나머지는 HOLD"
            )
        )
        # 상세 내용은 승인된 매수만 — HOLD/거부 종목까지 전부 나열하면 너무 길다
        # (사용자 요청, 2026-08-13). 위 요약 문장은 여전히 decisions_today 전체
        # 기준이라 몇 개 중 몇 개가 승인/거부됐는지는 그대로 보인다.
        if approved_buy_decisions:
            for d in approved_buy_decisions:
                avg_score = d.get("avg_score")
                score_text = f"{avg_score:.2f}" if avg_score is not None else "-"
                reason_ko = await translate.to_korean(d.get("reason"), label="translate_daily_report_reason")
                reason = _truncate(reason_ko, 300)
                suffix = f" — {reason}" if reason else ""
                blocks.append(
                    _bulleted(f"{_display_name(d['ticker'])}: {_decision_label(d)} (avg_score={score_text}){suffix}")
                )
        else:
            blocks.append(_paragraph("오늘은 승인된 매수 없음."))
    else:
        blocks.append(_paragraph("오늘은 판단 로그가 없다 — 휴장일이었거나 유니버스 수집에 실패했을 수 있다."))

    blocks.append(_heading(f"매수 ({len(buys)}건)", level=2))
    if buys:
        for b in buys:
            blocks.append(_bulleted(f"{_display_name(b['ticker'])}: {b['quantity']}주 @ {b['entry_price']:,.0f}"))
    else:
        blocks.append(_paragraph("오늘 매수 없음."))

    blocks.append(_heading(f"매도 ({len(sells)}건)", level=2))
    if sells:
        for s in sells:
            reason_label = REASON_LABELS.get(s["reason"], s["reason"])
            pnl = f", 실현손익 {s['realized_pnl_pct']:+.2%}" if s.get("realized_pnl_pct") is not None else ""
            amount = f", 매도금액 {s['sell_amount']:,.0f}원" if s.get("sell_amount") is not None else ""
            # "보유분의 몇 %를 팔았고 몇 %가 남았나" — 주식수까지 같이 적어 해석의
            # 여지를 없앤다.
            portion = ""
            if s.get("position_fraction_sold") is not None:
                shares = ""
                if s.get("shares_sold") is not None and s.get("shares_after") is not None:
                    shares = f" [{s['shares_sold']}주 매도, {s['shares_after']}주 잔여]"
                portion = (
                    f", 보유분의 {s['position_fraction_sold']:.1%} 매도"
                    f" (잔여 {s.get('position_fraction_remaining', 0.0):.1%}){shares}"
                )
            blocks.append(_bulleted(f"{_display_name(s['ticker'])}: {reason_label}{pnl}{amount}{portion}"))
    else:
        blocks.append(_paragraph("오늘 매도 없음."))

    if skips:
        blocks.append(_heading(f"특별한 일 — 게이트는 통과했지만 체결 못 함 ({len(skips)}건)", level=2))
        for sk in skips:
            reason_label = REASON_LABELS.get(sk["reason"], sk["reason"])
            detail = f" (갭 {sk['gap_pct']:.2%})" if sk.get("gap_pct") is not None else ""
            blocks.append(_bulleted(f"{_display_name(sk['ticker'])}: {reason_label}{detail}"))

    blocks.append(_heading(f"장마감 기준 보유 종목 ({len(portfolio.positions)}개)", level=2))
    if portfolio.positions:
        for p in portfolio.positions:
            entry_price = f"{p.entry_price:,.0f}" if p.entry_price is not None else "-"
            quantity = p.quantity if p.quantity is not None else "-"
            blocks.append(_bulleted(f"{_display_name(p.ticker)}: 비중 {p.weight:.2%}, 진입가 {entry_price}, 수량 {quantity}"))
    else:
        blocks.append(_paragraph("보유 종목 없음 — 전액 현금."))
    blocks.append(_paragraph(f"현금 비중: {portfolio.cash_weight:.2%}"))

    blocks.append(_heading("총정리", level=2))
    if total_value is not None:
        cash_amount = total_value * portfolio.cash_weight
        invested_amount = total_value - cash_amount
        blocks.append(_bulleted(f"투자한 금액: {invested_amount:,.0f}원"))
        blocks.append(_bulleted(f"남아있는 현금: {cash_amount:,.0f}원"))
        blocks.append(_bulleted(f"총 금액: {total_value:,.0f}원"))
    else:
        blocks.append(_paragraph("계좌 총평가금액 조회 실패로 금액 총정리를 작성할 수 없다."))

    return blocks


def _daily_report_properties(day: str, buys: list[dict], sells: list[dict], portfolio: PortfolioState) -> dict:
    return {
        "이름": {"title": _rich_text(f"{day} 리포트")},
        "날짜": {"date": {"start": day}},
        "매수": {"number": len(buys)},
        "매도": {"number": len(sells)},
        "보유종목수": {"number": len(portfolio.positions)},
        "현금비중": {"number": round(portfolio.cash_weight, 4)},
    }


def _load_synced_days(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    return set(json.loads(state_path.read_text()).get("synced_days", []))


def _save_synced_days(state_path: Path, days: set[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"synced_days": sorted(days)}, ensure_ascii=False, indent=2))


async def sync_daily_report(
    day: str,
    portfolio: PortfolioState,
    database_id: str,
    pipeline_log_path: Path = DEFAULT_PIPELINE_LOG_PATH,
    trade_journal_log_path: Path = DEFAULT_TRADE_JOURNAL_LOG_PATH,
    state_path: Path = DEFAULT_DAILY_REPORT_STATE_PATH,
    total_value: float | None = None,
) -> bool:
    """하루에 한 번, 장 마감 뒤 그날의 판단·매수·매도·최종 보유 종목을 요약해
    노션에 한 페이지로 남긴다. logs/pipeline.jsonl(그날의 모든 판단)과
    logs/trade_journal.jsonl(그날 실제 체결)을 day로 필터링해서 합치고,
    최종 보유 종목은 그 시점의 PortfolioState 그대로를 스냅샷으로 적는다 —
    매매일지(sync_trade_journal)는 이벤트 이력이고 이건 "그날 하루" 단위
    요약이라 서로 다른 걸 보여준다.

    total_value(계좌 총평가금액)는 호출부가 kis.fetch_account_balance()로 구해
    넘긴다 — PortfolioState는 비중(weight)만 들고 절대 원화 금액을 모르므로,
    "총정리"(투자한 금액/남은 현금/총 금액) 절대 원화 표기는 이 값이 있어야만
    가능하다. None이면(조회 실패) 그 절만 조용히 생략하고 리포트 나머지는 그대로
    올라간다.

    같은 날짜로 이미 만든 적 있으면(로컬 state 파일 기준) 다시 안 만든다 —
    run_daily.py가 같은 날 재실행돼도 중복 리포트가 안 생기게.
    """
    synced_days = _load_synced_days(state_path)
    if day in synced_days:
        logger.info("notion_daily_report_skipped day=%s reason=already_synced", day)
        return False

    decisions_today = _read_jsonl_for_day(pipeline_log_path, day)
    trades_today = _read_jsonl_for_day(trade_journal_log_path, day)
    buys = [e for e in trades_today if e["event"] == "buy"]
    sells = [e for e in trades_today if e["event"] == "sell"]
    skips = [e for e in trades_today if e["event"] == "buy_skipped"]

    body = {
        "parent": {"database_id": database_id},
        "properties": _daily_report_properties(day, buys, sells, portfolio),
        "children": await _daily_report_children(day, decisions_today, buys, sells, skips, portfolio, total_value),
    }
    result = _notion_request("POST", "/pages", body)
    if result is None:
        logger.warning("notion_daily_report_failed day=%s", day)
        return False

    synced_days.add(day)
    _save_synced_days(state_path, synced_days)
    logger.info("notion_daily_report_synced day=%s", day)
    return True
