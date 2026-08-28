"""자체 상태 파일을 브로커 실제 잔고와 대조한다. 기본은 조회만 하고 아무것도 안 쓴다.

    uv run python scripts/reconcile_portfolio.py            # 대조만 (드라이런)
    uv run python scripts/reconcile_portfolio.py --apply    # 브로커 기준으로 교정
    uv run python scripts/reconcile_portfolio.py --alert    # 드라이런 + 어긋나면 텔레그램

`--alert`가 크론(장 마감 후)이 쓰는 모드다. 교정은 안 하고 보고만 한다 —
2026-08-28까지 이 스크립트는 수동 실행뿐이라 아무도 안 돌리면 어긋난 진입가로
손절·익절이 계속 판정됐다(docs/CHANGELOG.md 2026-08-28).

왜 필요한지는 src/reconcile.py docstring 참고 — 요약하면 시장가 체결과 기록된
진입가가 어긋나면 손절·익절이 틀린 기준으로 판정된다.

**장중에 --apply를 돌리지 말 것.** 1분마다 도는 check_stop_loss.py와 같은 상태
파일을 놓고 경쟁한다. 락으로 깨지지는 않지만, 교정 직후 손절선이 바뀌므로 그
판정이 어느 값 기준이었는지 헷갈리게 된다. 장 마감 후에 돌리는 게 안전하다.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kis, notify, reconcile  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402
from src.pipeline import _kst_today  # noqa: E402
from src.portfolio_store import load_portfolio, portfolio_lock, save_portfolio  # noqa: E402

# 크론(--alert)에서 도는 스크립트라 다른 크론 스크립트와 같은 형식으로 남긴다.
# 이게 없으면 notify의 telegram_alert_sent(INFO)가 통째로 사라져서, 알림을 보냈는지
# 로그로 확인할 수 없다 — 2026-08-26 장애 때 도착 여부를 사용자에게 물어서야 알 수
# 있었던 것과 같은 문제다(CHANGELOG 2026-08-27).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reconcile")

APPLY = "--apply" in sys.argv
ALERT = "--alert" in sys.argv


def main() -> int:
    # 크론은 평일에만 돌지만 평일에도 휴장일이 있다. 휴장일엔 체결이 없어 새 드리프트가
    # 생길 수 없고, 남아 있는 드리프트는 다음 거래일에 어차피 다시 잡힌다 — 굳이 그날
    # 알림을 한 번 더 보내면 배경 소음만 는다.
    if ALERT and not is_krx_trading_day(_kst_today()):
        logger.info("not_a_trading_day day=%s — skip", _kst_today().isoformat())
        return 0

    holdings = kis.fetch_holdings()
    if holdings is None:
        print("브로커 잔고 조회 실패 — 아무것도 바꾸지 않았습니다.")
        if ALERT:
            # 조회 실패도 알린다 — 조용하면 "어긋난 곳 없음"과 구분이 안 된다.
            notify.send_telegram_alert(
                notify.format_error_alert("브로커 대조", "잔고 조회 실패 — 대조하지 못했습니다")
            )
        return 1

    with portfolio_lock():
        portfolio = load_portfolio()
        corrected, drifts = reconcile.reconcile(portfolio, holdings)

        print(f"보유 포지션 {len(portfolio.positions)}개, 브로커 보유 종목 {len(holdings)}개\n")
        if not drifts:
            print("어긋난 곳 없음. 상태 파일이 브로커와 일치합니다.")
            return 0

        for d in drifts:
            if d.field == "missing_at_broker":
                print(f"  [!] {d.ticker}: 상태 파일엔 있는데 브로커 잔고엔 없음 (자동으로 지우지 않음)")
            elif d.field == "missing_locally":
                print(f"  [!] {d.ticker}: 브로커는 {d.broker:.0f}주 보유 중인데 상태 파일엔 없음")
            else:
                diff = d.relative_diff
                diff_str = f" ({diff:+.2%})" if diff is not None else ""
                print(f"  {d.ticker} {d.field}: {d.local} -> {d.broker}{diff_str}")

        if not APPLY:
            print("\n(드라이런입니다. 실제로 교정하려면 --apply)")
            if ALERT:
                notify.send_telegram_alert(
                    notify.format_reconcile_drift_alert(_kst_today().isoformat(), drifts)
                )
            return 0

        save_portfolio(corrected)
        fixed = sum(1 for d in drifts if d.corrected)
        print(f"\n교정 완료 — {fixed}개 필드를 브로커 기준으로 맞췄습니다.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
