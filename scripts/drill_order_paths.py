"""주문·게이트 경로 드릴 — **브로커에 아무것도 보내지 않는다.** 결과를 docs/drills/에 남긴다.

왜 필요한가 (2026-09-14, 첫 1개월 평가 P1-5): 22거래일 동안 게이트는 0/1,018건 발동했고,
원장 거부 재시도·주문 응답 유실 처리는 사고가 난 날에만 실전에서 밟혔다. "그 코드가 지금도
의도대로 도는가"를 사고를 기다리지 않고 확인할 방법이 없었다. 단위 테스트는 함수 하나를
목킹 경계로 삼지만, 이 드릴은 **KIS HTTP 응답 층**에서 과거 실제 장애 모양을 재현하고 그 위의
재시도 정책·체결 조회·진입가 체인·게이트를 운영 코드 그대로 태운다.

모의계좌에 드릴 주문을 내지 않는 이유(사용자 확정): 매매일지·성과 기록에 가짜 체결이
섞인다 — 테스트 오염 사고가 네 번 났던 바로 그 기록이다.

실행: uv run python scripts/drill_order_paths.py        (레포 루트에서, 결과를 docs/drills/에 쓴다)
      tests/test_drill_order_paths.py 가 같은 시나리오를 매 테스트 실행마다 돌린다.
"""

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from src import collectors, kis, notify, pipeline, sell, translate  # noqa: E402
from src.schemas import AnalystOpinion, Decision, GateResult, PortfolioState, Position, RiskGateConfig, SellAction  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
# 드릴은 운영 시세 경로를 그대로 타야 한다. 테스트 실행 중에는 conftest가 fetch_quote를
# 목으로 바꿔 끼우므로, 이 모듈이 import되는 시점(픽스처 전)의 원본을 잡아 되돌려 쓴다.
_REAL_FETCH_QUOTE = kis.fetch_quote
TICKER = "005930"
PRICE = 10_000.0
TOTAL = 100_000_000.0
LEDGER_CAPACITY_MSG = "원장에서 허용 가능한 초당 거래건수를 초과하였습니다."  # 2026-08-19 실측 문구, msg_cd 미상
BUSINESS_REJECT = {"rt_cd": "1", "msg_cd": "APBK0913", "msg1": "주문가능금액을 초과 했습니다"}


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@dataclass
class FakeBroker:
    """KIS 모의투자 서버를 흉내 낸다. 상태: 누적 체결, 보유, 요청 기록."""

    order_behavior: str = "accept"  # accept | reject | lost_no_fill | lost_filled
    pre_open_quote: bool = False
    balance_capacity_rejections: int = 0
    held_qty: int = 0
    fills: dict = field(default_factory=lambda: {"buy": [0, 0.0, 0.0], "sell": [0, 0.0, 0.0]})
    gets: list = field(default_factory=list)
    posts: list = field(default_factory=list)

    def get(self, url, headers=None, params=None, timeout=None):
        path = url.replace(kis.BASE_URL, "")
        self.gets.append(path)
        if path == kis.CURRENT_PRICE_PATH:
            if self.pre_open_quote:
                return _Resp({"rt_cd": "0", "output": {"stck_prpr": str(int(PRICE)), "stck_hgpr": "0", "stck_lwpr": "0", "stck_oprc": "0", "prdy_vrss": "0"}})
            return _Resp({"rt_cd": "0", "output": {"stck_prpr": str(int(PRICE)), "stck_hgpr": str(int(PRICE * 1.01)), "stck_lwpr": str(int(PRICE * 0.99)), "stck_oprc": str(int(PRICE)), "prdy_vrss": "0"}})
        if path == kis.DAILY_CHART_PATH:
            today = datetime.now(KST).date()
            rows, px = [], PRICE
            for i in range(30, 0, -1):
                px = px * 1.01 if i % 2 else px / 1.01
                d = today - timedelta(days=i)
                rows.append({"stck_bsop_date": d.strftime("%Y%m%d"), "stck_oprc": f"{px:.0f}", "stck_hgpr": f"{px:.0f}", "stck_lwpr": f"{px:.0f}", "stck_clpr": f"{px:.0f}", "acml_vol": "1000"})
            rows[-1]["stck_clpr"] = str(int(PRICE))
            return _Resp({"rt_cd": "0", "output2": list(reversed(rows))})
        if path == kis.BALANCE_PATH:
            if self.balance_capacity_rejections > 0:
                self.balance_capacity_rejections -= 1
                return _Resp({"rt_cd": "1", "msg_cd": "", "msg1": LEDGER_CAPACITY_MSG})
            holdings = [{"pdno": TICKER, "hldg_qty": str(self.held_qty), "pchs_avg_pric": str(PRICE)}] if self.held_qty else []
            return _Resp({"rt_cd": "0", "output1": holdings, "output2": [{"tot_evlu_amt": str(TOTAL), "dnca_tot_amt": str(TOTAL), "scts_evlu_amt": "0"}]})
        if path == kis.DAILY_CCLD_PATH:
            side = "buy" if params.get("SLL_BUY_DVSN_CD") == "02" else "sell"
            qty, amt, fee = self.fills[side]
            return _Resp({"rt_cd": "0", "output2": {"tot_ccld_qty": str(qty), "tot_ccld_amt": str(amt), "prsm_tlex_smtl": str(fee), "pchs_avg_pric": str(amt / qty if qty else 0)}})
        raise AssertionError(f"드릴이 모르는 GET 경로: {path}")

    def post(self, url, headers=None, data=None, timeout=None, json=None):
        path = url.replace(kis.BASE_URL, "")
        if path == kis.TOKEN_PATH:
            raise AssertionError("드릴은 토큰을 발급받지 않는다")
        body = __import__("json").loads(data)
        self.posts.append(body)
        side = "buy" if headers["tr_id"] == kis.ORDER_BUY_TR_ID else "sell"
        qty = int(body["ORD_QTY"])
        if self.order_behavior == "reject":
            return _Resp(BUSINESS_REJECT)
        if self.order_behavior in ("accept", "lost_filled"):
            f = self.fills[side]
            f[0] += qty
            f[1] += qty * PRICE
            f[2] += round(qty * PRICE * kis.BROKERAGE_FEE_RATE)
            self.held_qty += qty if side == "buy" else -qty
        if self.order_behavior.startswith("lost"):
            raise requests.ConnectionError("Read timed out (drill)")
        return _Resp({"rt_cd": "0", "output": {"ODNO": "0000123"}})


@contextlib.contextmanager
def _wired(broker: FakeBroker):
    """운영 코드는 그대로, HTTP·토큰·대기·알림만 바꿔 끼운다. 끝나면 전부 되돌린다."""
    saved = {
        (kis, "get_access_token"): kis.get_access_token,
        (kis.requests, "get"): kis.requests.get,
        (kis.requests, "post"): kis.requests.post,
        (kis, "_throttle"): kis._throttle,
        (kis, "FILL_POLL_TIMEOUT_S"): kis.FILL_POLL_TIMEOUT_S,
        (kis, "FILL_POLL_INTERVAL_S"): kis.FILL_POLL_INTERVAL_S,
        (time, "sleep"): time.sleep,
        (pipeline, "PRE_OPEN_QUOTE_WAIT_S"): pipeline.PRE_OPEN_QUOTE_WAIT_S,
        (pipeline, "SECONDARY_QUOTE_MODE"): pipeline.SECONDARY_QUOTE_MODE,
        (pipeline, "display_name"): pipeline.display_name,
        (notify, "send_telegram_alert"): notify.send_telegram_alert,
        (notify, "ALERT_MARKER_DIR"): notify.ALERT_MARKER_DIR,
        (collectors, "fetch_naver_quotes"): collectors.fetch_naver_quotes,
        (kis, "fetch_quote"): kis.fetch_quote,
        (translate, "to_korean"): translate.to_korean,
    }
    alerts: list[str] = []
    old_cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="sima_drill_")
    try:
        os.chdir(tmp)
        kis.get_access_token = lambda: "drill-token"
        kis.requests.get = broker.get
        kis.requests.post = broker.post
        kis._throttle = lambda: None
        kis.FILL_POLL_TIMEOUT_S = 0.0
        kis.FILL_POLL_INTERVAL_S = 0.0
        time.sleep = lambda s: None
        pipeline.PRE_OPEN_QUOTE_WAIT_S = 0.0
        pipeline.display_name = lambda t: t
        notify.send_telegram_alert = lambda m: alerts.append(m) or True
        notify.ALERT_MARKER_DIR = Path(tmp) / "alert_markers"
        collectors.fetch_naver_quotes = lambda tickers, **kw: {}
        kis.fetch_quote = _REAL_FETCH_QUOTE

        async def _no_translate(text, label="translate"):
            return text  # Claude 스위치가 다시 켜져도 드릴은 과금 호출을 하지 않는다

        translate.to_korean = _no_translate
        yield Path(tmp), alerts
    finally:
        os.chdir(old_cwd)
        for (obj, name), value in saved.items():
            setattr(obj, name, value)
        shutil.rmtree(tmp, ignore_errors=True)


def _decision() -> Decision:
    op = AnalystOpinion(agent="chart", ticker=TICKER, score=0.9, confidence=0.9, evidence=["prompt:drill@0"], as_of=datetime.now(KST))
    return Decision(ticker=TICKER, action="BUY", reason="drill", inputs=[op], degraded=False)


def _journal(tmp: Path) -> list[dict]:
    path = tmp / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _buy(broker: FakeBroker, portfolio: PortfolioState | None = None):
    with _wired(broker) as (tmp, alerts):
        result = asyncio.run(
            pipeline.execute_buy_order(
                _decision(), GateResult(approved=True, rejected_by=None), portfolio or PortfolioState(cash_weight=1.0),
                "반도체", pipeline.TRADE_WEIGHT, log_path=tmp / "journal.jsonl",
            )
        )
        return result, _journal(tmp), alerts


def _order_posts(broker: FakeBroker) -> int:
    return len(broker.posts)


@dataclass
class Outcome:
    name: str
    history: str
    passed: bool
    observed: str


def s_buy_normal() -> Outcome:
    b = FakeBroker()
    result, journal, _ = _buy(b)
    ok = len(result.positions) == 1 and _order_posts(b) == 1 and journal[-1]["entry_price_source"] == "fill" and journal[-1]["exit_plan_source"] == "volatility"
    return Outcome("정상 매수", "기준선", ok, f"주문 POST {_order_posts(b)}회, 포지션 {len(result.positions)}, 진입가 출처 {journal[-1].get('entry_price_source')}, 출구 규칙 {journal[-1].get('exit_plan_source')}")


def s_buy_response_lost_no_fill() -> Outcome:
    b = FakeBroker(order_behavior="lost_no_fill")
    result, journal, _ = _buy(b)
    ok = result.positions == [] and _order_posts(b) == 1 and journal[-1].get("reason") == "order_response_lost"
    return Outcome("매수 응답 유실 + 원장 체결 없음", "재전송하면 두 번 산다(kis.OrderResponseLost)", ok, f"주문 POST {_order_posts(b)}회(재전송 없음), 포지션 {len(result.positions)}, 기록 {journal[-1].get('reason')}")


def s_buy_response_lost_but_filled() -> Outcome:
    b = FakeBroker(order_behavior="lost_filled")
    result, journal, _ = _buy(b)
    ok = len(result.positions) == 1 and _order_posts(b) == 1 and journal[-1].get("event") == "buy" and result.positions[0].entry_price == PRICE
    return Outcome("매수 응답 유실 + 원장에 체결 있음", "포기하면 브로커엔 있고 장부엔 없는 보유가 생긴다", ok, f"주문 POST {_order_posts(b)}회, 포지션 {len(result.positions)}, 진입가 {result.positions[0].entry_price if result.positions else None}")


def s_buy_business_reject() -> Outcome:
    b = FakeBroker(order_behavior="reject")
    result, journal, _ = _buy(b)
    ok = result.positions == [] and _order_posts(b) == 1 and journal[-1].get("reason") == "order_rejected"
    return Outcome("매수 주문 브로커 거부", "비즈니스 오류는 재시도해도 같은 답", ok, f"주문 POST {_order_posts(b)}회, 기록 {journal[-1].get('reason')}")


def s_ledger_capacity_rejection_on_balance() -> Outcome:
    b = FakeBroker(balance_capacity_rejections=2)
    result, journal, _ = _buy(b)
    balance_calls = b.gets.count(kis.BALANCE_PATH)
    ok = len(result.positions) == 1 and balance_calls >= 3
    return Outcome("잔고조회 원장 용량 거부 2회", "2026-08-19 이 문구가 영구 실패로 오분류돼 승인 매수가 날아갔다", ok, f"잔고조회 {balance_calls}회 후 매수 {'성사' if result.positions else '실패'}")


def s_pre_open_quote() -> Outcome:
    b = FakeBroker(pre_open_quote=True)
    result, journal, _ = _buy(b)
    ok = result.positions == [] and _order_posts(b) == 0 and journal[-1].get("reason") == "pre_open_quote"
    return Outcome("개장 전 기준가 시세만 옴", "2026-08-12 매수 4건이 gap 0.0·진입가=전일 종가", ok, f"시세 조회 {b.gets.count(kis.CURRENT_PRICE_PATH)}회, 주문 POST {_order_posts(b)}회, 기록 {journal[-1].get('reason')}")


def _sell(broker: FakeBroker):
    broker.held_qty = 30
    position = Position(ticker=TICKER, sector="반도체", weight=0.08, entry_price=9_000.0, peak_price=9_000.0, quantity=30)
    portfolio = PortfolioState(positions=[position], cash_weight=0.92)
    action = SellAction(ticker=TICKER, reason="stop_loss", sell_fraction=1.0)
    with _wired(broker):
        return asyncio.run(sell.execute_sell_order(portfolio, action, PRICE))


def s_sell_response_lost_but_filled() -> Outcome:
    b = FakeBroker(order_behavior="lost_filled")
    portfolio, fill = _sell(b)
    ok = portfolio.positions == [] and _order_posts(b) == 1 and fill is not None
    return Outcome("매도 응답 유실 + 원장에 체결 있음", "팔렸는데 안 판 걸로 두면 없는 주식을 계속 관리한다", ok, f"주문 POST {_order_posts(b)}회, 남은 포지션 {len(portfolio.positions)}")


def s_sell_response_lost_no_fill() -> Outcome:
    b = FakeBroker(order_behavior="lost_no_fill")
    portfolio, fill = _sell(b)
    ok = len(portfolio.positions) == 1 and _order_posts(b) == 1 and fill is None
    return Outcome("매도 응답 유실 + 체결 없음", "안 팔렸는데 판 걸로 두면 실제 보유가 손절 대상에서 사라진다", ok, f"주문 POST {_order_posts(b)}회, 남은 포지션 {len(portfolio.positions)}")


def s_secondary_quote_active_stop_loss() -> Outcome:
    """KIS 시세 실패를 네이버가 복구한 뒤 운영 finalize/주문 경로까지 실제로 태운다."""
    b = FakeBroker(held_qty=30)
    position = Position(
        ticker=TICKER, sector="반도체", weight=0.08,
        entry_price=12_000.0, peak_price=12_000.0, quantity=30,
    )
    portfolio = PortfolioState(positions=[position], cash_weight=0.92)
    with _wired(b) as (tmp, _):
        pipeline.SECONDARY_QUOTE_MODE = "active"
        kis.fetch_quote = lambda ticker, policy=None: None
        collectors.fetch_naver_quotes = lambda tickers, **kw: {
            TICKER: kis.Quote(price=PRICE, day_high=PRICE, day_low=PRICE)
        }
        result = asyncio.run(
            pipeline.evaluate_holdings(
                portfolio, datetime.now(KST), sell.execute_sell_order,
                log_path=tmp / "sell.jsonl", trade_journal_log_path=tmp / "journal.jsonl",
            )
        )
        journal = _journal(tmp)
    ok = (
        result.positions == []
        and _order_posts(b) == 1
        and journal[-1].get("event") == "sell"
        and journal[-1].get("reason") == "stop_loss"
    )
    return Outcome(
        "네이버 2차 시세 active 손절",
        "KIS 시세 장애 때 보조 시세가 판정만 하고 주문 경로와 끊기면 안전장치가 작동하지 않는다",
        ok,
        f"네이버 판정 뒤 주문 POST {_order_posts(b)}회, 남은 포지션 {len(result.positions)}, 기록 {journal[-1].get('reason')}",
    )


def _run_day_with_buys(portfolio: PortfolioState, universe: list[tuple[str, str]]):
    # 분석가는 항상 의견을 내는 고정 함수다. 난수 더미(make_dummy_analyst_fn)는 날짜로 시드를
    # 잡아 가끔 의견을 안 내고, 그러면 판단이 안 생겨 게이트까지 가지도 않는다 — 드릴이 시각에
    # 따라 통과·실패를 오갔다. 드릴이 보려는 건 게이트이지 분석가가 아니다.
    async def analyst(ticker, sector, day):
        return [AnalystOpinion(agent="chart", ticker=ticker, score=0.9, confidence=0.9, evidence=["prompt:drill@0"], as_of=day)]

    async def judge(opinions, total_expected_analysts):
        if not opinions:
            return None
        return Decision(ticker=opinions[0].ticker, action="BUY", reason="drill", inputs=opinions, degraded=False)

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "pipeline.jsonl"
        _, results = asyncio.run(
            pipeline.run_day(
                universe, datetime.now(KST), portfolio, RiskGateConfig(),
                analyst, judge, pipeline.execute_simulated, log_path=log_path,
            )
        )
        rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    return results, rows


def s_gate_total_exposure() -> Outcome:
    portfolio = PortfolioState(cash_weight=0.16, positions=[Position(ticker="HELD", sector="x", weight=0.84)])
    results, rows = _run_day_with_buys(portfolio, [("A00001", "x"), ("A00002", "x"), ("A00003", "x")])
    rejected = [r["rejected_by"] for r in rows if not r["approved"]]
    ok = sum(1 for _, g in results if g.approved) == 2 and rejected == ["total_exposure"]
    return Outcome("게이트: 총노출 한도(운영 run_day 체인)", "22거래일 0/1,018 — 거부 경로가 운영에서 한 번도 안 밟혔다", ok, f"승인 {sum(1 for _, g in results if g.approved)}건, 거부 {rejected}")


def s_gate_position_limit() -> Outcome:
    portfolio = PortfolioState(cash_weight=0.92, positions=[Position(ticker="A00001", sector="x", weight=0.08)])
    results, rows = _run_day_with_buys(portfolio, [("A00001", "x")])
    rejected = [r["rejected_by"] for r in rows if not r["approved"]]
    ok = rejected == ["position_limit"]
    return Outcome("게이트: 종목당 한도(추가매수)", "8% + 8% = 16% > 15%", ok, f"거부 {rejected}")


SCENARIOS = [
    s_buy_normal,
    s_buy_response_lost_no_fill,
    s_buy_response_lost_but_filled,
    s_buy_business_reject,
    s_ledger_capacity_rejection_on_balance,
    s_pre_open_quote,
    s_sell_response_lost_but_filled,
    s_sell_response_lost_no_fill,
    s_secondary_quote_active_stop_loss,
    s_gate_total_exposure,
    s_gate_position_limit,
]


def run_all() -> list[Outcome]:
    outcomes = []
    for scenario in SCENARIOS:
        try:
            outcomes.append(scenario())
        except Exception as exc:  # noqa: BLE001 - 시나리오 하나가 터져도 나머지는 돌고, 실패로 기록한다
            outcomes.append(Outcome(scenario.__name__, "", False, f"예외: {exc!r}"))
    return outcomes


def render(outcomes: list[Outcome], commit: str) -> str:
    today = datetime.now(KST).date().isoformat()
    lines = [
        f"# 주문·게이트 경로 드릴 — {today}",
        "",
        f"커밋 `{commit}`. 브로커 주문 0건. KIS HTTP 응답 층에서 장애를 재현하고 운영 코드를 그대로 태웠다",
        "(`scripts/drill_order_paths.py`).",
        "",
        f"**결과: {sum(o.passed for o in outcomes)}/{len(outcomes)} 통과**",
        "",
        "| 시나리오 | 왜 보는가 | 결과 | 관측 |",
        "|---|---|---|---|",
    ]
    for o in outcomes:
        lines.append(f"| {o.name} | {o.history} | {'통과' if o.passed else '**실패**'} | {o.observed} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    import subprocess

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() or "unknown"
    outcomes = run_all()
    report = render(outcomes, commit)
    out_dir = Path(__file__).resolve().parent.parent / "docs" / "drills"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{datetime.now(KST).date().isoformat()}-order-paths.md"
    out_path.write_text(report)
    print(report)
    print(f"-> {out_path}")
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
