"""자체 상태 파일을 브로커 실제 잔고와 대조한다. 기본은 조회만 하고 아무것도 안 쓴다.

    uv run python scripts/reconcile_portfolio.py            # 대조만 (드라이런)
    uv run python scripts/reconcile_portfolio.py --apply    # 브로커 기준으로 교정

왜 필요한지는 src/reconcile.py docstring 참고 — 요약하면 시장가 체결과 기록된
진입가가 어긋나면 손절·익절이 틀린 기준으로 판정된다.

**장중에 --apply를 돌리지 말 것.** 1분마다 도는 check_stop_loss.py와 같은 상태
파일을 놓고 경쟁한다. 락으로 깨지지는 않지만, 교정 직후 손절선이 바뀌므로 그
판정이 어느 값 기준이었는지 헷갈리게 된다. 장 마감 후에 돌리는 게 안전하다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kis, reconcile  # noqa: E402
from src.portfolio_store import load_portfolio, portfolio_lock, save_portfolio  # noqa: E402

APPLY = "--apply" in sys.argv


def main() -> int:
    holdings = kis.fetch_holdings()
    if holdings is None:
        print("브로커 잔고 조회 실패 — 아무것도 바꾸지 않았습니다.")
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
            return 0

        save_portfolio(corrected)
        fixed = sum(1 for d in drifts if d.corrected)
        print(f"\n교정 완료 — {fixed}개 필드를 브로커 기준으로 맞췄습니다.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
