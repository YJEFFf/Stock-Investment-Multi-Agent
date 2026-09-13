"""마일스톤 3 — 신호의 예측력(IC) 측정.

이 모듈은 2026-08-08에 만들어졌지만 **2026-09-13까지 한 달 동안 운영에서 한 번도
호출되지 않았다**(호출처가 tests뿐, 크론 없음). 첫 1개월 평가에서 "측정 규율 2/10"이
나온 직접 원인이다. 2026-09-14부터 scripts/measure_ic.py가 매 거래일 16:00에 돌린다.

측정 관례 — **하나로 고정하고 바꾸지 않는다**(2026-09-14 확정). 첫 평가에서 같은
보고서 안에 진입 시점 관례가 세 가지(전일 종가·당일 시가·당일 종가) 섞였고 그것만으로
결과가 1.2%p 움직였다.

- 판단은 08:30에 전일 데이터로 내려지고 집행은 09:01이다. 그래서 진입은 **판단일 D의
  시가**, 청산은 **D+k 거래일의 종가**다. 종목과 벤치마크(코스피200) 둘 다 같은 창으로
  재고 그 차(초과수익)를 쓴다.
- (day, ticker)가 중복이면 **마지막 것만** 쓴다. 2026-08-12는 UTC 날짜 버그로 유니버스가
  두 번 돌아 37건이 겹쳤고, 이걸 안 거르면 그날 IC에 같은 종목이 두 번 들어간다.
- 무조건부 IC 옆에 **직전 20일 수익률 분위 내 IC**와 **ρ(avg_score, 직전 20일 수익률)**을
  같이 낸다. 첫 평가에서 avg_score가 직전 20일 수익률의 대리변수(ρ=+0.63)로 드러났다 —
  무조건부 IC만 보면 신호가 모멘텀을 재는 건지 모멘텀 너머의 무언가를 재는 건지 모른다.
- 점수는 `avg_score`다. HOLD 판단도 전부 포함한다 — 체결된 10건으로는 영원히 결론이
  안 나고, 매일 30~50건의 HOLD 점수가 유일한 표본이다.

가격은 디스크에 누적한다(PriceHistory). KIS 일봉은 최근 100거래일만 주므로, 매일 받아
합쳐두지 않으면 옛 판단의 선행수익률을 몇 달 뒤엔 못 구한다.
"""

import json
import logging
from datetime import date
from pathlib import Path

from src.schemas import OHLCVBar

logger = logging.getLogger(__name__)

DEFAULT_PRICE_HISTORY_DIR = Path("logs/price_history")
DEFAULT_IC_SUMMARY_PATH = Path("logs/ic_summary.json")

INDEX_KEY = "KOSPI200"
HORIZONS = (5, 10, 20)  # 거래일
MOMENTUM_LOOKBACK = 20  # 직전 N거래일 수익률 — avg_score의 대리변수로 확인된 창
QUINTILES = 5
ENTRY_CONVENTION = "open(D) -> close(D+k), minus KOSPI200 over the same window"

# 사전 등록한 가설 구간(docs/CHANGELOG.md 2026-09-14). 차트 프롬프트에 "이미 오른 것은
# 그 자체로 매수 근거가 아니다"를 넣은 날부터를 따로 잰다 — 전체 IC만 보면 바꾸기 전
# 22거래일이 몇 달 동안 숫자를 끌어내려 변화가 안 보인다. 날짜가 아니라 판단 로그의
# 프롬프트 버전(opinions[].prompt)이 1차 기준이고, 그 필드가 없는 옛 기록은 날짜로 가른다.
SEGMENTS = {
    "all": None,
    "post_chart_prompt_7a9654": "2026-09-14",
}


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


def load_decision_entries(log_path: Path) -> list[dict]:
    """pipeline.jsonl에서 점수가 있는 판단만, (day, ticker) 중복은 마지막 것만."""
    latest: dict[tuple[str, str], dict] = {}
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("avg_score") is None:
            continue
        latest[(entry["day"], entry["ticker"])] = entry
    return sorted(latest.values(), key=lambda e: (e["day"], e["ticker"]))


def entries_since(entries: list[dict], since: str | None) -> list[dict]:
    return entries if since is None else [e for e in entries if e["day"] >= since]


class PriceHistory:
    """종목별 일봉을 `{dir}/{key}.json`에 누적한다. 같은 날짜는 새 값으로 덮는다."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, key: str) -> dict[date, OHLCVBar]:
        path = self._path(key)
        if not path.exists():
            return {}
        bars = [OHLCVBar(**row) for row in json.loads(path.read_text())]
        return {b.date: b for b in bars}

    def merge(self, key: str, bars: list[OHLCVBar]) -> int:
        """새로 추가된 날짜 수를 돌려준다."""
        existing = self.load(key)
        before = len(existing)
        for bar in bars:
            existing[bar.date] = bar
        self.directory.mkdir(parents=True, exist_ok=True)
        ordered = [existing[d].model_dump(mode="json") for d in sorted(existing)]
        self._path(key).write_text(json.dumps(ordered, ensure_ascii=False))
        return len(existing) - before

    def refresh(self, keys: list[str], fetch_fn) -> dict[str, int]:
        """키마다 fetch_fn(key)로 일봉을 받아 합친다. 실패한 키는 건너뛰고 기록한다."""
        added: dict[str, int] = {}
        for key in keys:
            bars = fetch_fn(key)
            if not bars:
                logger.warning("price_history_refresh_failed key=%s", key)
                continue
            added[key] = self.merge(key, bars)
        return added


def _trading_dates(bars: dict[date, OHLCVBar]) -> list[date]:
    return sorted(bars)


def forward_excess_return(
    stock: dict[date, OHLCVBar], index: dict[date, OHLCVBar], day: date, k: int
) -> float | None:
    """판단일 D 시가 진입 → D+k 거래일 종가 청산 수익률에서 같은 창의 지수 수익률을 뺀 값.

    D가 그 종목의 거래일이 아니면(거래정지 등) 그 이후 첫 거래일을 D로 본다. D+k
    거래일이 아직 오지 않았거나 지수에 같은 날짜가 없으면 None — 0으로 채우지 않는다.
    """
    dates = _trading_dates(stock)
    start = next((i for i, d in enumerate(dates) if d >= day), None)
    if start is None or start + k >= len(dates):
        return None
    entry_date, exit_date = dates[start], dates[start + k]
    entry_price = stock[entry_date].open
    exit_price = stock[exit_date].close
    if entry_price <= 0:
        return None
    if entry_date not in index or exit_date not in index or index[entry_date].open <= 0:
        return None
    stock_return = exit_price / entry_price - 1
    index_return = index[exit_date].close / index[entry_date].open - 1
    return stock_return - index_return


def trailing_return(stock: dict[date, OHLCVBar], day: date, lookback: int = MOMENTUM_LOOKBACK) -> float | None:
    """판단일 D **직전** 거래일 종가 대비, 그보다 lookback 거래일 전 종가의 수익률.

    판단이 08:30에 전일 데이터로 내려지므로 D 당일은 포함하지 않는다."""
    dates = _trading_dates(stock)
    start = next((i for i, d in enumerate(dates) if d >= day), None)
    if start is None or start - 1 - lookback < 0:
        return None
    recent = stock[dates[start - 1]].close
    past = stock[dates[start - 1 - lookback]].close
    if past <= 0:
        return None
    return recent / past - 1


def summarize_ic(
    entries: list[dict],
    prices: dict[str, dict[date, OHLCVBar]],
    index: dict[date, OHLCVBar],
    k: int,
) -> dict:
    """일별 횡단면 IC(스피어만)의 평균·표준편차·t와, 모멘텀을 통제한 보조 지표."""
    by_day: dict[str, list[tuple[float, float]]] = {}
    pooled: list[tuple[float, float, float]] = []  # (score, excess, momentum) — 선행수익률이 있는 것만
    score_momentum: list[tuple[float, float]] = []  # 선행수익률 유무와 무관하게 전부
    skipped_no_forward_data = 0
    skipped_no_price = 0

    for e in entries:
        stock = prices.get(e["ticker"])
        if not stock:
            skipped_no_price += 1
            continue
        day = date.fromisoformat(e["day"])
        momentum = trailing_return(stock, day)
        if momentum is not None:
            # "점수가 모멘텀의 대리변수인가"는 선행수익률이 아직 없는 최근 판단까지 포함해 잰다.
            # 측정된 부분집합으로만 재면 가장 최근 판단(보통 표본의 1/5)이 늘 빠진다.
            score_momentum.append((e["avg_score"], momentum))
        excess = forward_excess_return(stock, index, day, k)
        if excess is None:
            skipped_no_forward_data += 1
            continue
        by_day.setdefault(e["day"], []).append((e["avg_score"], excess))
        if momentum is not None:
            pooled.append((e["avg_score"], excess, momentum))

    daily = [(d, ic) for d, pairs in sorted(by_day.items()) if (ic := information_coefficient(pairs)) is not None]
    ics = [ic for _, ic in daily]
    n = len(ics)
    mean_ic = sum(ics) / n if n else None
    std_ic = (sum((x - mean_ic) ** 2 for x in ics) / (n - 1)) ** 0.5 if n >= 2 else None
    t_stat = mean_ic / (std_ic / n**0.5) if std_ic else None

    rho_score_momentum = information_coefficient(score_momentum)

    quintile_ic: list[float | None] = []
    if len(pooled) >= QUINTILES * 2:
        ordered = sorted(pooled, key=lambda p: p[2])
        size = len(ordered) // QUINTILES
        for q in range(QUINTILES):
            chunk = ordered[q * size : (q + 1) * size] if q < QUINTILES - 1 else ordered[q * size :]
            quintile_ic.append(information_coefficient([(s, x) for s, x, _ in chunk]))

    return {
        "k": k,
        "convention": ENTRY_CONVENTION,
        "days_measured": n,
        "n_pairs": sum(len(p) for p in by_day.values()),
        "mean_ic": mean_ic,
        "std_ic": std_ic,
        "t_stat": t_stat,
        "positive_days": sum(1 for ic in ics if ic > 0),
        "first_day": daily[0][0] if daily else None,
        "last_day": daily[-1][0] if daily else None,
        "rho_score_momentum_20d": rho_score_momentum,
        "momentum_quintile_ic": quintile_ic,
        "skipped_no_forward_data": skipped_no_forward_data,
        "skipped_no_price": skipped_no_price,
    }
