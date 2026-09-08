import pytest

from src import kis


@pytest.fixture(autouse=True)
def _no_fill_polling_wait(monkeypatch):
    """kis.fill_after_order가 테스트에서 실제로 대기하지 않게 한다.

    이 함수는 주문 수량이 다 체결될 때까지 누적 집계를 다시 조회하며 기다린다
    (2026-08-18 부분 체결 누락 대응). 실제 값 그대로면 체결이 안 잡히는 목킹
    경로마다 12초씩 멈춘다. 타임아웃 0이면 조회는 정확히 한 번만 돌아서 폴링
    도입 전과 호출 횟수가 같아진다 — 기존 목이 그대로 유효하다.

    폴링 자체의 동작은 타임아웃을 명시로 넘기는 tests/test_kis_fill.py에서 검증한다.
    """
    monkeypatch.setattr(kis, "FILL_POLL_TIMEOUT_S", 0.0)
    monkeypatch.setattr(kis, "FILL_POLL_INTERVAL_S", 0.0)


@pytest.fixture(autouse=True)
def _isolate_alert_state(monkeypatch, tmp_path):
    """알림 상태 파일(하루 1회 마커 + 시세 공백 구간)을 항상 tmp로 돌린다.

    기본값은 레포의 logs/alert_markers다. 막지 않으면 테스트가 운영 상태를
    덮어쓴다 — EC2에서 테스트를 한 번 돌리면 그날 진짜 장애 알림이 "이미 보냈다"로
    묻히고, 열려 있던 공백 구간도 지워진다. tests/test_evaluate_holdings.py에만
    있던 방어를 여기로 올려 파일을 새로 만들 때 빠뜨릴 수 없게 했다.
    """
    from src import notify

    monkeypatch.setattr(notify, "ALERT_MARKER_DIR", tmp_path / "alert_markers")
    monkeypatch.setattr(notify, "BLACKOUT_STATE_DIR", tmp_path / "alert_markers")


@pytest.fixture(autouse=True)
def _isolate_default_state_paths(monkeypatch, tmp_path):
    """호출부가 경로를 안 넘겼을 때 쓰이는 기본 상태/로그 경로를 전부 tmp로 돌린다.

    _isolate_alert_state와 같은 이유이고, 같은 사고가 2026-08-27 하루에만 두 번 더
    났다 — `sell_judgment.jsonl`과 (지금은 없어진) `observed_range.json`이 각각
    테스트 한 번에 운영 `logs/`로 새어 나갔다. 알림 마커(2026-08-20)까지 세 번째다.

    **패턴이 분명하다: `Path("logs/...")` 기본값을 새로 만들면 여기 등록해야 한다.**
    등록을 잊으면 조용히 운영 기록이 오염되고, 그 기록들은 하나같이 "사후에 무슨
    일이 있었는지 보려고" 만든 것이라 가짜 줄이 섞이는 순간 목적을 잃는다.
    개별 테스트가 명시적으로 경로를 넘기는 건 이 픽스처와 무관하게 그대로 동작한다.

    2026-09-08에 네 번째로 났고, 이번엔 **가장 중요한 두 개가 애초에 등록되어 있지
    않았다**: `pipeline`의 매도 로그와 매매일지. 9/2에 추가한 개장 시세 테스트가
    `finalize_sell`을 목킹하지 않은 채 evaluate_holdings를 통과시켰고(진입가 100원
    포지션이 +20% 익절 문턱을 넘는다), 가짜 매도 3건이 운영 `sell.jsonl`·
    `trade_journal.jsonl`에 적힌 뒤 다음 날 노션 매매일지까지 동기화됐다.
    그래서 이제 **`src` 전체의 `logs/` 기본값을 빠짐없이 등록한다.**
    """
    from src import judgment, llm, notion_sync, pipeline, portfolio_store

    monkeypatch.setattr(judgment, "DEFAULT_SELL_JUDGMENT_LOG_PATH", tmp_path / "sell_judgment.jsonl")
    monkeypatch.setattr(pipeline, "DEFAULT_LOG_PATH", tmp_path / "pipeline.jsonl")
    monkeypatch.setattr(pipeline, "DEFAULT_SELL_LOG_PATH", tmp_path / "sell.jsonl")
    monkeypatch.setattr(pipeline, "DEFAULT_TRADE_JOURNAL_LOG_PATH", tmp_path / "trade_journal.jsonl")
    monkeypatch.setattr(llm, "DEFAULT_LLM_CALL_LOG_PATH", tmp_path / "llm_calls.jsonl")
    monkeypatch.setattr(notion_sync, "DEFAULT_PIPELINE_LOG_PATH", tmp_path / "pipeline.jsonl")
    monkeypatch.setattr(notion_sync, "DEFAULT_TRADE_JOURNAL_LOG_PATH", tmp_path / "trade_journal.jsonl")
    monkeypatch.setattr(
        notion_sync, "DEFAULT_DAILY_REPORT_STATE_PATH", tmp_path / "notion_daily_report_state.json"
    )
    monkeypatch.setattr(notion_sync, "DEFAULT_SYNC_STATE_PATH", tmp_path / "notion_sync_state.json")
    monkeypatch.setattr(portfolio_store, "PORTFOLIO_STATE_PATH", tmp_path / "portfolio_state.json")
    monkeypatch.setattr(portfolio_store, "PORTFOLIO_LOCK_PATH", tmp_path / "portfolio_state.lock")


@pytest.fixture(autouse=True)
def _never_reach_the_real_broker(monkeypatch):
    """테스트가 실제 KIS를 때리지 못하게 **한 지점에서** 막는다.

    `_kis_request`는 토큰이 없으면 즉시 None을 돌려주므로, 여기만 막으면 조회도
    주문도 실제로 나가지 않는다. `_kis_get`/`_kis_post`/`requests`를 목킹하는
    기존 테스트는 이 아래 계층이라 영향이 없다.

    왜 필요한가: 2026-08-27에 `evaluate_holdings`가 `fetch_current_price` 대신
    `fetch_quote`를 쓰게 바꿨더니, 앞의 것만 목킹하던 테스트가 진짜 시세를 받아
    **실제 매도 주문까지 냈다**(`모의투자 장종료 입니다`로 거부돼 살았다 — 장중이었으면
    체결됐다). .env에 자격증명이 살아있는 머신에서는 목킹 한 군데를 빠뜨리는 것과
    실제 주문을 내는 것 사이에 아무것도 없었다.

    주문 자체를 검증하는 테스트는 이 아래 계층(`_kis_post`·`requests`)을 목킹하거나
    `get_access_token`을 직접 되돌려 놓으면 된다.
    """
    from src import kis

    monkeypatch.setattr(kis, "get_access_token", lambda: None)


@pytest.fixture(autouse=True)
def _quote_follows_the_price_mock(monkeypatch):
    """`fetch_quote`가 기본적으로 `fetch_current_price`를 따라가게 한다.

    당일 고가/저가를 현재가와 같게 둔다. 구간 판정은 low=high=현재가면 점 판정과
    똑같아지므로(`sell._threshold_crossed` docstring) 꺼진 것과 같고, 기존 테스트의
    `fetch_current_price` 목킹이 그대로 유효하다. 당일 범위가 필요한 테스트는
    `fetch_quote`를 직접 목킹하면 이 픽스처를 덮어쓴다(monkeypatch 순서상 테스트가 나중).

    **고가/저가를 None으로 두지 않는 이유**(2026-09-08): 그건 "오늘 아직 체결이
    없다"는 뜻이고, 이제 그런 종목은 판정 자체를 건너뛴다(kis.Quote.traded_today).
    None으로 두면 매도를 검증하는 테스트가 전부 조용히 통과해버린다 — 아무것도
    판정하지 않았는데 "매도가 안 났다"로 보인다.
    """
    from src import kis

    def _quote(ticker, *, policy=None):
        price = kis.fetch_current_price(ticker, policy=policy)
        return None if price is None else kis.Quote(price=price, day_high=price, day_low=price)

    monkeypatch.setattr(kis, "fetch_quote", _quote)
