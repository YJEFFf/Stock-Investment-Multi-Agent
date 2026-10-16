"""Read-only source probes and semantic checks shared by preflight and repair.

No investment decisions or orders. A healthy canary is evidence about that
endpoint, not a guarantee that every later stock request will succeed.
"""
import json
import math
import re
import time as clock
import xml.etree.ElementTree as ET
from contextvars import ContextVar
from datetime import datetime, timedelta, time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from src import collector_routes as routes, collectors
from src.market_calendar import is_krx_trading_day

KST = ZoneInfo("Asia/Seoul")
CANARIES = ("005930", "000660")
MAX_BYTES = 2_000_000
DEADLINE = ContextVar("collector_probe_deadline", default=None)


def remaining():
    deadline = DEADLINE.get()
    seconds = 8.0 if deadline is None else deadline - clock.monotonic()
    if seconds <= 0:
        raise ProbeError("transient", "probe time budget exhausted")
    return min(3.0, seconds), min(5.0, seconds)


class ProbeError(ValueError):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


def expected_session(now):
    day = now.astimezone(KST).date()
    # KRX close (15:30) is independent from the deployment freeze (16:10).
    # 16:00 IC collection must accept today's completed index bar.
    if now.astimezone(KST).time() < time(15, 30) or not is_krx_trading_day(day):
        day -= timedelta(days=1)
    while not is_krx_trading_day(day):
        day -= timedelta(days=1)
    return day


def get_text(url, params):
    routes.validate_url(url)
    for attempt in range(2):
        try:
            with requests.get(url, params=params, headers={"User-Agent": collectors.USER_AGENT},
                              timeout=remaining(), allow_redirects=False, stream=True) as response:
                if response.status_code in (301, 302, 303, 307, 308, 404, 410):
                    raise ProbeError("structure", f"HTTP {response.status_code}: {response.headers.get('Location', '')[:300]}")
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    remaining()
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ProbeError("structure", "response too large")
                    chunks.append(chunk)
                content = b"".join(chunks)
                # XML carries its own encoding; JSON responses are UTF-8.
                encoding = "euc-kr" if b'EUC-KR' in content[:150].upper() else "utf-8"
                return content.decode(encoding)
        except requests.RequestException as exc:
            if attempt:
                raise ProbeError("transient", type(exc).__name__) from exc
    raise AssertionError("unreachable")


def request(manifest, name, params=None, **slots):
    url = manifest["routes"][name]["url"].format(**slots)
    raw = get_text(url, params)
    try:
        return routes.normalize(name, raw, manifest)
    except (ValueError, KeyError, TypeError) as exc:
        raise ProbeError("structure", f"normalization: {type(exc).__name__}") from exc


def index_data(text, now):
    root = ET.fromstring(text)
    chart = root.find("chartdata")
    if chart is None or chart.get("symbol") != "KPI200" or chart.get("timeframe") != "day":
        raise ProbeError("semantic", "not KOSPI200 daily bars")
    bars = collectors._parse_bars(text)
    if len(bars) < 60 or len({b.date for b in bars}) != len(bars):
        raise ProbeError("semantic", "incomplete or duplicate index dates")
    if bars[-1].date != expected_session(now):
        raise ProbeError("stale", f"index last session {bars[-1].date}")
    for b in bars:
        if (not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
                or b.low > min(b.open, b.close) or b.high < max(b.open, b.close)
                or b.volume < 0 or not is_krx_trading_day(b.date)):
            raise ProbeError("semantic", "invalid index OHLCV")
    return {b.date.isoformat(): b.model_dump(mode="json") for b in bars}


def news_data(text, now, *, hub=False):
    items = collectors._parse_news_hub_api(text) if hub else collectors._parse_company_news_api(text)
    # Only the two busy canaries require nonempty fresh news; real stock queries
    # still allow a genuinely empty result and return no opinion.
    if not items:
        raise ProbeError("structure", "canary news unexpectedly empty")
    if any(n.published_at > now + timedelta(minutes=5) for n in items):
        raise ProbeError("semantic", "future news timestamp")
    if max(n.published_at.date() for n in items) < expected_session(now) - timedelta(days=3):
        raise ProbeError("stale", "canary news too old")
    return {n.url: {"title": n.title, "published_at": n.published_at.isoformat()} for n in items}


def groups_data(payload):
    if not isinstance(payload, list) or not payload:
        raise ProbeError("structure", "missing sector groups")
    out = {}
    for g in payload:
        if (not isinstance(g, dict) or not isinstance(g.get("no"), str) or not g["no"].isdigit()
                or g.get("type") != "upjong" or not isinstance(g.get("name"), str)
                or not g["name"].strip() or g["no"] in out):
            raise ProbeError("semantic", "invalid sector group")
        count = int(g["totalCnt"])
        if count < 1 or count > 10000:
            raise ProbeError("semantic", "invalid group size")
        out[g["no"]] = {"name": g["name"], "count": count}
    return out


def fetch_groups(manifest):
    out = {}
    for page_no in range(20):
        page = json.loads(request(manifest, "sector_groups", {"startIdx": page_no * 100, "pageSize": 100, "sortType": "changeRate"}))
        if page == [] and out:
            return out
        groups = groups_data(page)
        if set(out) & set(groups):
            raise ProbeError("semantic", "repeated sector page")
        out.update(groups)
        if len(page) < 100:
            return out
    raise ProbeError("semantic", "too many sector pages")


def fetch_members(manifest, groups):
    out = {}
    for group, metadata in groups.items():
        members, cursors, cursor = set(), set(), None
        for _ in range(100):
            params = {"size": 100, "sortType": "marketCap", "sortDirection": "desc"}
            if cursor:
                params["cursor"] = cursor
            page = json.loads(request(manifest, "sector_members", params, group=group))
            if not isinstance(page, dict) or not isinstance(page.get("items"), list) or type(page.get("hasNext")) is not bool:
                raise ProbeError("structure", "missing sector member pagination")
            for row in page["items"]:
                code = row.get("itemCode") if isinstance(row, dict) else None
                if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Z]{6}", code) or code in members:
                    raise ProbeError("semantic", "invalid sector member identity")
                members.add(code)
            if not page["hasNext"]:
                break
            cursor = page.get("cursor")
            if not isinstance(cursor, str) or not cursor or cursor in cursors or not page["items"]:
                raise ProbeError("semantic", "invalid sector cursor")
            cursors.add(cursor)
        else:
            raise ProbeError("semantic", "too many member pages")
        if len(members) != metadata["count"]:
            raise ProbeError("semantic", "incomplete sector members")
        out[group] = sorted(members)
    return out


def quotes_data(text, now):
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("datas"), list):
        raise ProbeError("structure", "missing realtime quotes")
    out = {}
    for row in payload["datas"]:
        code = row.get("itemCode")
        exchange = row.get("stockExchangeType", {})
        if code not in CANARIES or code in out or exchange.get("code") != "KS":
            raise ProbeError("semantic", "wrong quote identity/exchange")
        price = collectors._naver_raw_number(row, "closePriceRaw")
        traded = str(row.get("localTradedAt", ""))[:10]
        if price is None or not math.isfinite(price) or traded < expected_session(now).isoformat() or traded > now.date().isoformat():
            raise ProbeError("stale", "invalid quote price/date")
        out[code] = {"price": price, "date": traded}
    if set(out) != set(CANARIES):
        raise ProbeError("semantic", "incomplete canary quotes")
    return out


def probe(manifest, *, now=None, full=False, timeout_s=90):
    token = DEADLINE.set(clock.monotonic() + timeout_s)
    try:
        return _probe(manifest, now=now, full=full)
    finally:
        DEADLINE.reset(token)


def _probe(manifest, *, now=None, full=False):
    now = now or datetime.now(KST)
    routes.validate_manifest(manifest)
    results = {}
    groups = {}
    for name in routes.DEFAULT_URLS:
        try:
            remaining()
            if name == "index":
                data = index_data(request(manifest, name, {"symbol": "KPI200", "timeframe": "day", "count": 60, "requestType": 0}), now)
            elif name == "stock_news":
                data = {code: news_data(request(manifest, name, {"page": 1, "pageSize": 50}, ticker=code), now) for code in CANARIES}
            elif name == "sector_news":
                data = news_data(request(manifest, name, {"category": "FLASHNEWS", "page": 1, "pageSize": 50}), now, hub=True)
            elif name == "sector_groups":
                data = groups = fetch_groups(manifest)
            elif name == "sector_members":
                if not groups:
                    raise ProbeError("dependency", "sector group check failed")
                selected = groups if full else dict(list(sorted(groups.items()))[:2])
                data = fetch_members(manifest, selected)
            else:
                data = quotes_data(request(manifest, name, codes=",".join(CANARIES)), now)
            results[name] = {"status": "ok", "data": data}
        except Exception as exc:
            results[name] = {"status": getattr(exc, "kind", "structure"), "error": str(exc)[:500]}
    return {"checked_at": now.isoformat(), "manifest": routes.digest(manifest), "full": full, "sources": results}


def validate_compatibility(previous, candidate, changed):
    """No historical evidence -> no automatic activation, even after AI approval."""
    for name in changed:
        old, new = previous.get(name), candidate["sources"][name]
        if not old or old.get("status") != "ok" or new["status"] != "ok":
            raise ValueError(f"no comparable successful evidence: {name}")
        a, b = old["data"], new["data"]
        if name == "index":
            common = set(a) & set(b)
            if len(common) < 40 or any(a[k] != b[k] for k in common):
                raise ValueError("index history changed")
        elif name in {"stock_news", "sector_news"}:
            if name == "stock_news" and b[CANARIES[0]] == b[CANARIES[1]]:
                raise ValueError("ticker news collapsed to identical feeds")
            pairs = [(a[c], b[c]) for c in CANARIES] if name == "stock_news" else [(a, b)]
            for left, right in pairs:
                common = set(left) & set(right)
                # Strict historical coverage; if news moved too far to prove
                # equivalence automatically, require manual review.
                if len(common) < max(3, min(len(left), len(right)) // 2) or any(left[k] != right[k] for k in common):
                    raise ValueError("no matching news identity/title/time")
        elif a != b:
            raise ValueError(f"{name} meaning changed; manual review required")
