import json
from datetime import date
from pathlib import Path

from src import collectors


def _rank(values: list[float]) -> list[float]:
    """동순위는 평균 순위로 처리하는 1-인덱스 순위."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)

    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1

    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x**0.5 * var_y**0.5)


def information_coefficient(pairs: list[tuple[float, float]]) -> float | None:
    """예측 점수와 실현 수익률의 스피어만 순위상관 (IC).

    표본이 2개 미만이거나 한쪽이 전부 같은 값(순위 분산 0)이면 상관을 정의할 수
    없어 None을 반환한다 — 0으로 대체하지 않는다 (억지로 숫자를 만들지 않음).
    """
    if len(pairs) < 2:
        return None
    predicted = [p[0] for p in pairs]
    realized = [p[1] for p in pairs]
    return _pearson(_rank(predicted), _rank(realized))


def compute_forward_return(
    ticker: str, as_of: date, forward_days: int, lookback_days: int = 250
) -> float | None:
    """as_of 시점 종가 대비 forward_days 거래일 뒤 종가의 수익률.

    차트 수집기(무료 스크래핑)만 쓰므로 얼마든지 호출해도 비용이 없다. as_of 이후
    forward_days 거래일치가 아직 존재하지 않으면(미래 시점이거나 수집 실패) None을
    반환한다 — 이게 바로 IC를 오늘 당장 계산할 수 없는 이유다: 시간이 지나야 한다.
    """
    context = collectors.fetch_market_context(ticker, lookback_days=lookback_days)
    if context is None:
        return None

    bars = context.bars  # 오래된 순으로 정렬돼 있음
    start_idx = next((i for i, b in enumerate(bars) if b.date >= as_of), None)
    if start_idx is None:
        return None

    forward_idx = start_idx + forward_days
    if forward_idx >= len(bars):
        return None  # 아직 그만큼 거래일이 지나지 않음

    start_close = bars[start_idx].close
    forward_close = bars[forward_idx].close
    if start_close == 0:
        return None

    return (forward_close - start_close) / start_close


def summarize_ic(log_path: Path, forward_days: int = 5) -> dict:
    """로그에 쌓인 예측 점수와 실제 이후 수익률로 일별 횡단면 IC를 계산한다.

    이 함수 실행 자체는 무료(차트 수집만 쓴다)지만, 의미 있는 결과를 얻으려면
    로그에 실제로 여러 날에 걸쳐 워크포워드로 쌓인 예측이 있어야 한다 — 과거
    백테스트로 대체하지 않는다 (LLM 컴포넌트는 워크포워드·실시간 모의투자로만
    검증한다는 CLAUDE.md 원칙).
    """
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    entries = [json.loads(line) for line in lines]

    by_day: dict[str, list[tuple[float, float]]] = {}
    skipped_no_forward_data = 0

    for e in entries:
        avg_score = e.get("avg_score")
        if avg_score is None:
            continue

        as_of = date.fromisoformat(e["day"])
        forward_return = compute_forward_return(e["ticker"], as_of, forward_days)
        if forward_return is None:
            skipped_no_forward_data += 1
            continue

        by_day.setdefault(e["day"], []).append((avg_score, forward_return))

    daily_ics = [
        ic for _, pairs in sorted(by_day.items()) if (ic := information_coefficient(pairs)) is not None
    ]

    mean_ic = sum(daily_ics) / len(daily_ics) if daily_ics else None
    std_ic = None
    if len(daily_ics) >= 2:
        variance = sum((x - mean_ic) ** 2 for x in daily_ics) / (len(daily_ics) - 1)
        std_ic = variance**0.5

    return {
        "days_measured": len(daily_ics),
        "mean_ic": mean_ic,
        "std_ic": std_ic,
        "skipped_no_forward_data": skipped_no_forward_data,
    }
