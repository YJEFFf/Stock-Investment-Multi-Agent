from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class OHLCVBar(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class MarketContext(BaseModel):
    """수집기(collectors.py)의 출력이자 분석가의 입력 계약.

    분석가는 데이터를 직접 가져오지 않고 이 객체로 주입받는다.
    """

    ticker: str
    as_of: datetime
    bars: list[OHLCVBar]  # 최근 N영업일 일봉, 오래된 순
    indicators: dict[str, float]  # SMA/RSI/거래량비율 등 파생 지표


class NewsItem(BaseModel):
    title: str
    press: str | None
    published_at: datetime | None
    url: str


class NewsContext(BaseModel):
    """뉴스 수집기의 출력이자 뉴스 분석가의 입력 계약.

    company_news가 메인 판단 대상이고, sector_news는 "이 종목이 속한 업종의 시장
    배경" 참고용이다 — 다른 종목과 비교하는 게 아니라 이 종목 하나를 둘러싼 맥락이다.
    """

    ticker: str
    sector: str
    as_of: datetime
    company_news: list[NewsItem]
    sector_news: list[NewsItem]


class DisclosureItem(BaseModel):
    report_name: str
    submitter: str
    received_at: date
    receipt_no: str
    remark: str | None
    url: str


class DisclosureContext(BaseModel):
    """DART 수집기의 출력이자 공시 분석가의 입력 계약."""

    ticker: str
    as_of: datetime
    disclosures: list[DisclosureItem]


class AnalystOpinion(BaseModel):
    agent: str  # "chart" | "news" | "disclosure" | "quant"
    ticker: str
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str]  # 원문 ID + 프롬프트 버전 ("prompt:chart@a3f2c1")
    as_of: datetime  # 이 판단이 사용한 데이터의 최신 시점


class DebateArgument(BaseModel):
    """강세/약세 토론에서 나온 논거 하나. 종목 비교가 아니라 이 종목 하나에 대한
    찬반 논거를 강제로 둘 다 만들게 한 결과다 (docs/PLAN.md §2 — 동조 편향 방지)."""

    stance: Literal["bull", "bear"]
    ticker: str
    argument: str
    strength: float = Field(ge=0.0, le=1.0)  # 이 논거가 실제로 얼마나 설득력 있는가
    evidence: list[str]  # 프롬프트 버전 추적 (AnalystOpinion과 동일한 관례)


class Decision(BaseModel):
    ticker: str
    action: Literal["BUY", "HOLD"]  # 매도는 별도 경로
    reason: str
    inputs: list[AnalystOpinion]  # 이 결정을 만든 의견 전체
    degraded: bool  # 분석가 일부가 실패한 상태에서 나온 결정인가
    debate: list[DebateArgument] = Field(default_factory=list)  # 감사 추적용 강세/약세 논거
    evidence: list[str] = Field(default_factory=list)  # 이 결정 자체를 만든 프롬프트 버전 (매니저)


class GateResult(BaseModel):
    approved: bool
    rejected_by: str | None  # "position_limit" | "sector_concentration" | ...


class RiskGateConfig(BaseModel):
    """docs/PLAN.md §5에서 확정한 리스크 게이트 수치 (2026-08-08)."""

    position_limit: float = 0.15  # 종목당 최대 비중
    sector_concentration_limit: float = 0.40  # 섹터 집중도 한도
    daily_loss_limit: float = -0.05  # 일일 손실 한도 (도달 시 신규 매수 중단)
    total_exposure_limit: float = 1.0  # 총 노출 한도 (현재는 개별 한도로만 통제)


class Position(BaseModel):
    ticker: str
    sector: str
    weight: float  # 포트폴리오 대비 비중
    entry_price: float | None = None  # 진입 시 가중평균 단가. 매도 로직(src/sell.py)의
    # 손절/익절 판단 전제조건 — 매수 실행 경로가 아직 진입가를 채우지 않는 포지션은
    # None으로 남는다(알려진 갭, docs/PLAN.md §5). None인 포지션은 결정론적 매도
    # 평가가 "판단 불가"로 건너뛴다(AnalystOpinion=None과 같은 패턴).
    peak_price: float | None = None  # 진입(또는 마지막 부분 익절) 이후 관측된 최고가 —
    # 트레일링 익절의 기준점.
    take_profit_stage: int = 0  # 부분 익절이 몇 번 실행됐는지


class SellAction(BaseModel):
    """매도는 Decision과 별도 경로다(Decision.action 주석 참고). 결정론적으로 나온
    매도(손절/트레일링 익절)는 리스크 게이트를 거치지 않는다 — 보유분을 줄이는
    행위 자체가 위험을 낮추는 방향이라 "한도 초과"라는 개념이 성립하지 않는다."""

    ticker: str
    reason: Literal["stop_loss", "take_profit_trail"]
    sell_fraction: float = Field(gt=0.0, le=1.0)  # 이 포지션의 현재 weight 대비 매도 비율


class PortfolioState(BaseModel):
    positions: list[Position] = Field(default_factory=list)
    cash_weight: float = 1.0
    daily_pnl_pct: float = 0.0
