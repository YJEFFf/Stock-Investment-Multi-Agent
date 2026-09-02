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


class FillRecord(BaseModel):
    """브로커에 실제로 체결된 내역. 매매일지에 적히는 사실은 이것이지 판단 시점
    호가가 아니다 (사용자 확정, 2026-08-15).

    시장가로 주문하므로 판단 시점 호가와 실제 체결가는 항상 다를 수 있다. 실제로
    192820이 호가 210,000에 주문돼 232,000에 체결됐고, 일지가 호가를 적는 바람에
    실제 +6.6% 지점에서 익절이 +20%로 오판돼 발동했다(docs/PLAN.md).

    `price`를 따로 두지 않고 amount/quantity로 계산하는 이유: 부분 체결이 여러 번
    나뉘어도 가중평균이 자동으로 맞기 때문이다.
    """

    quantity: int = Field(gt=0)
    amount: float = Field(gt=0)  # 실제 체결 총액(원)
    complete: bool = True  # 주문 수량만큼 다 잡혔는가. False면 체결이 더 있었는데
    # 조회가 못 따라잡은 것이라 quantity·amount가 실제보다 **작다** (kis.fill_after_order).
    # 이 값으로 판단하는 쪽은 수량을 상태 차이에서 다시 뽑아야 한다.
    fee: float | None = None  # 이 주문의 위탁수수료(원). 브로커가 주는 값이라 조회 실패나
    # 옛 경로에서는 None으로 남는다 — 0.0으로 채우면 "수수료가 없었다"와 "모른다"가
    # 같은 모양이 된다(AnalystOpinion=None과 같은 패턴). 거래세는 여기 안 들어간다:
    # 브로커 응답의 추정제비용(prsm_tlex_smtl)은 매수·매도 양쪽 같은 요율이라
    # 수수료만 담고 있다(2026-09-01 15건 실측). 세금은 kis.SELL_TAX_RATE로 계산한다.

    @property
    def price(self) -> float:
        return self.amount / self.quantity


class ExitPlan(BaseModel):
    """이 포지션의 손절/익절 규칙. 진입 시점에 한 번 정해지고 청산까지 얼어붙는다
    (사용자 확정, 2026-08-15).

    왜 얼리는가: 보유 중에 LLM에게 손절선을 다시 물으면, 이미 -8%인 포지션 앞에서
    묻게 된다. 그러면 "지지선이 조금 아래라 여유를 둘 필요" 같은 논거가 반드시
    나온다 — 모델이 나빠서가 아니라 물어보면 답을 만들어내기 때문이고, 재시도가
    문턱을 대신 낮췄던 실패(CLAUDE.md 서두)와 정확히 같은 모양이다. 아직 이해관계가
    없는 진입 시점에 규칙을 정하게 하고, 그 뒤로는 코드가 그대로 집행한다.

    범위(schemas의 Field 제약)는 정책이 아니라 파싱 사고 방지용 절대 바운드다 —
    LLM이 이 밖의 값을 내면 judgment 단에서 잘라낸다.
    """

    stop_loss_pct: float = Field(ge=-0.15, le=-0.03)  # 진입가 대비, 음수. 도달 시 전량 매도
    take_profit_pct: float = Field(ge=0.06, le=0.30)  # 진입가 대비, 양수. 항상 2 x |stop_loss_pct|
    # (2:1 고정 — LLM은 손절폭 한 숫자만 내고 익절선은 코드가 계산한다. 사용자 확정)
    take_profit_fraction: float = Field(gt=0.0, lt=1.0)  # 트리거마다 "현재" 잔량 대비 매도 비율.
    # 1.0 미만이라 (1-f)^n으로 항상 일부가 남는다 — 익절만으로는 전량 청산되지 않는다.
    trail_pct: float = Field(ge=-0.12, le=-0.03)  # 첫 익절 이후 고점 대비, 음수


class Decision(BaseModel):
    ticker: str
    action: Literal["BUY", "HOLD"]  # 매도는 별도 경로
    reason: str
    inputs: list[AnalystOpinion]  # 이 결정을 만든 의견 전체
    degraded: bool  # 분석가 일부가 실패한 상태에서 나온 결정인가
    debate: list[DebateArgument] = Field(default_factory=list)  # 감사 추적용 강세/약세 논거
    evidence: list[str] = Field(default_factory=list)  # 이 결정 자체를 만든 프롬프트 버전 (매니저)
    exit_plan: ExitPlan | None = None  # 매수 시 이 포지션에 박을 출구 규칙. None이면
    # sell.DEFAULT_EXIT_PLAN(고정 -10%/+20%/1-3/-7%)로 떨어진다 — degraded 판단이거나
    # LLM 응답이 깨졌을 때의 안전한 기본값.


class GateResult(BaseModel):
    approved: bool
    rejected_by: str | None  # "position_limit" | "total_exposure" | ...


class RiskGateConfig(BaseModel):
    """docs/PLAN.md §5에서 확정한 리스크 게이트 수치 (2026-08-08).

    폐기된 룰 두 개는 필드 자체를 지웠다 — 값만 남겨두면 게이트가 실제보다 촘촘한
    것처럼 보인다(pipeline.check_gate docstring에 각각의 이유):
    `daily_loss_limit`(2026-08-20), `sector_concentration_limit`(2026-09-01).
    """

    position_limit: float = 0.15  # 종목당 최대 비중
    total_exposure_limit: float = 1.0  # 총 노출 한도 (현재는 개별 한도로만 통제)


class Position(BaseModel):
    ticker: str
    sector: str
    weight: float  # 포트폴리오 대비 비중
    entry_day: date | None = None  # 최초 진입일(추가매수해도 갱신 안 함) — 매매일지 보유기간 계산용
    entry_price: float | None = None  # 진입 시 가중평균 단가. 매도 로직(src/sell.py)의
    # 손절/익절 판단 전제조건 — 매수 실행 경로가 아직 진입가를 채우지 않는 포지션은
    # None으로 남는다(알려진 갭, docs/PLAN.md §5). None인 포지션은 결정론적 매도
    # 평가가 "판단 불가"로 건너뛴다(AnalystOpinion=None과 같은 패턴).
    peak_price: float | None = None  # 진입(또는 마지막 부분 익절) 이후 관측된 최고가 —
    # 트레일링 익절의 기준점.
    peak_reset_day: date | None = None  # 트레일링 익절로 peak_price를 리셋한 날.
    # 리셋은 "다음 구간을 새 고점부터 추적한다"는 뜻인데, update_peak_price가 매분
    # max(peak, day_high)를 잡고 **day_high >= 현재가는 항상 참**이라 리셋이 다음
    # 회차에 반드시 되돌아왔다(2026-09-02 발견, 2026-08-27 d6c5864의 부작용).
    # 그날의 고가는 리셋 이전 구간을 포함하므로 리셋한 날엔 day_high를 안 본다 —
    # 진입 당일에 day_high를 빼는 것과 정확히 같은 이유다(sell.update_peak_price).
    take_profit_stage: int = 0  # 부분 익절이 몇 번 실행됐는지
    exit_plan: ExitPlan | None = None  # 진입 시 확정된 손절/익절 규칙. 보유 중에는 절대
    # 갱신하지 않는다(ExitPlan docstring 참고). None인 포지션(이 기능 이전에 열렸거나
    # 시뮬레이션 경로로 열린 것)은 sell.DEFAULT_EXIT_PLAN을 그대로 쓴다.
    quantity: int | None = None  # 실제 보유 주수. pipeline.execute_buy_order가 실제
    # KIS 주문을 넣을 때만 채운다 — 브로커에 실주문을 낸 적 없는(순수 시뮬레이션
    # execute()로 연 포지션은 None으로 남는다. 실제 매도 주문 수량 계산에 쓴다
    # (weight는 비중일 뿐 절대 주수를 모른다).


class SellAction(BaseModel):
    """매도는 Decision과 별도 경로다(Decision.action 주석 참고). 결정론적으로 나온
    매도(손절/트레일링 익절)와 LLM 재량 매도(llm_discretionary — 보유 종목 재평가
    결과) 모두 리스크 게이트를 거치지 않는다 — 보유분을 줄이는 행위 자체가 위험을
    낮추는 방향이라 "한도 초과"라는 개념이 성립하지 않는다."""

    ticker: str
    reason: Literal["stop_loss", "take_profit_trail", "llm_discretionary"]
    sell_fraction: float = Field(gt=0.0, le=1.0)  # 이 포지션의 현재 weight 대비 매도 비율
    reasoning: str | None = None  # llm_discretionary일 때만 채워지는 LLM 판단 사유. 결정론적
    # 매도(stop_loss/take_profit_trail)는 룰이 근거 그 자체라 별도 서술이 필요 없다.


class PortfolioState(BaseModel):
    positions: list[Position] = Field(default_factory=list)
    cash_weight: float = 1.0
