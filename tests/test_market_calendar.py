from datetime import date

import pytest

from src.market_calendar import is_krx_trading_day


def test_weekday_non_holiday_is_trading_day():
    assert is_krx_trading_day(date(2026, 8, 10)) is True  # 월요일


def test_weekend_is_not_trading_day():
    assert is_krx_trading_day(date(2026, 8, 8)) is False  # 토요일
    assert is_krx_trading_day(date(2026, 8, 9)) is False  # 일요일


def test_new_years_day_is_not_trading_day():
    assert is_krx_trading_day(date(2026, 1, 1)) is False


def test_substitute_holiday_is_not_trading_day():
    # 삼일절(3/1)이 일요일이라 대체공휴일이 3/2(월)로 지정됨
    assert is_krx_trading_day(date(2026, 3, 2)) is False


def test_year_end_closure_is_not_trading_day():
    assert is_krx_trading_day(date(2026, 12, 31)) is False


def test_unmapped_year_raises_instead_of_silently_running():
    with pytest.raises(ValueError):
        is_krx_trading_day(date(2027, 1, 4))
