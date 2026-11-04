"""Deterministic, bounded source collection for the holding shadow experiment.

Original analyst snapshots stay at their original scope. Only new evidence is
enriched with article/report bodies; it cannot rewrite what was known at entry.
"""

import hashlib
import io
import json
import os
import re
import time as monotonic_time
import zipfile
from datetime import datetime, time
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import requests

from src import collectors
from src.schemas import (
    Decision, DisclosureContext, DisclosureItem, EntryThesisRecord, MarketContext,
    NewsContext, NewsItem, ThesisDocument,
)

KST = ZoneInfo("Asia/Seoul")
MAX_DOWNLOAD_BYTES = 8_000_000
MAX_DOCUMENT_CHARS = 100_000
DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"


class SourceUnavailable(ValueError):
    pass


class _TextParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, article_only=False):
        super().__init__(convert_charrefs=True)
        self.article_only = article_only
        self.depth = 0
        self.ignore = 0
        self.found = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.ignore += 1
        if self.article_only and dict(attrs).get("id") == "dic_area":
            self.depth = 1
            self.found = True
        elif self.depth and tag not in self.VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if tag not in self.VOID_TAGS:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID_TAGS:
            return
        if tag in {"script", "style", "noscript"}:
            self.ignore = max(0, self.ignore - 1)
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.ignore and (not self.article_only or self.depth):
            self.parts.append(data)

    def text(self):
        value = " ".join(" ".join(self.parts).split())
        if not value or len(value) > MAX_DOCUMENT_CHARS or (self.article_only and not self.found):
            raise SourceUnavailable("empty_or_oversize_body")
        return value


def _download(url, *, params=None):
    # No redirects, arbitrary URLs or downloaded files on disk. Never log params:
    # DART includes the API key in its query string.
    started = monotonic_time.monotonic()
    for attempt in range(2):
        try:
            with requests.get(url, params=params, headers={"User-Agent": collectors.USER_AGENT},
                              timeout=(5, 10), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise SourceUnavailable("http_status_" + str(response.status_code))
                chunks, size = [], 0
                for chunk in response.iter_content(4096):
                    if monotonic_time.monotonic() - started > 30:
                        raise SourceUnavailable("document_download_deadline")
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise SourceUnavailable("document_too_large")
                    chunks.append(chunk)
                return b"".join(chunks)
        except requests.RequestException:
            if attempt == 1:
                raise SourceUnavailable("document_transport_failed") from None
    raise SourceUnavailable("document_transport_failed")


def fetch_news_body(item: NewsItem) -> str:
    if not re.fullmatch(r"https://n\.news\.naver\.com/mnews/article/\d+/\d+", item.url):
        raise SourceUnavailable("unsupported_news_url")
    parser = _TextParser(article_only=True)
    parser.feed(_download(item.url).decode("utf-8", errors="strict"))
    return parser.text()


def parse_disclosure_body(payload: bytes) -> str:
    # The API returns a ZIP on success and XML status on failure. No extraction,
    # external entities, filesystem paths, or silent truncation are allowed.
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [x for x in archive.infolist() if x.filename.lower().endswith((".xml", ".html", ".htm"))]
        if not members or sum(x.file_size for x in members) > MAX_DOWNLOAD_BYTES:
            raise SourceUnavailable("empty_or_oversize_disclosure")
        texts = []
        for member in members:
            content = archive.read(member).decode("utf-8-sig", errors="strict")
            if "<!ENTITY" in content.upper():
                raise SourceUnavailable("disclosure_entity_declaration")
            parser = _TextParser()
            parser.feed(content)
            texts.append(parser.text())
    body = "\n".join(texts)
    if len(body) > MAX_DOCUMENT_CHARS:
        raise SourceUnavailable("document_too_large")
    return body


def fetch_disclosure_body(item: DisclosureItem) -> str:
    if not re.fullmatch(r"\d{14}", item.receipt_no):
        raise SourceUnavailable("invalid_receipt")
    key = os.getenv("DART_API_KEY")
    if not key:
        raise SourceUnavailable("dart_key_unavailable")
    try:
        return parse_disclosure_body(_download(DOCUMENT_URL, params={"crtfc_key": key, "rcept_no": item.receipt_no}))
    except (zipfile.BadZipFile, UnicodeError) as exc:
        raise SourceUnavailable("disclosure_body_parse_failed") from None


def original_documents(decision: Decision) -> list[ThesisDocument]:
    documents = {}
    for opinion in decision.inputs:
        context = opinion.source_context
        if context is None or context.ticker != decision.ticker or not opinion.reasoning:
            raise SourceUnavailable("original_reasoning_or_context_missing")
        common = {"ticker": decision.ticker, "observed_at": context.as_of}
        if isinstance(context, MarketContext) and opinion.agent == "chart":
            source_id = "kis:daily_ohlcv:" + decision.ticker + ":" + hashlib.sha256(context.model_dump_json().encode()).hexdigest()
            documents[source_id] = ThesisDocument(
                **common, source_id=source_id, kind="chart", scope="market_data",
                content=context.model_dump_json(), published_on=context.bars[-1].date if context.bars else None,
            )
        elif isinstance(context, NewsContext) and opinion.agent == "news":
            for item in context.company_news:
                documents[item.url] = ThesisDocument(**common, source_id=item.url, kind="news", scope="headline",
                                                     content=item.model_dump_json(), published_at=item.published_at)
        elif isinstance(context, DisclosureContext) and opinion.agent == "disclosure":
            for item in context.disclosures:
                documents[item.url] = ThesisDocument(**common, source_id=item.url, kind="disclosure", scope="headline",
                                                     content=item.model_dump_json(), published_on=item.received_at)
        else:
            raise SourceUnavailable("original_context_agent_mismatch")
    allowed = {x for opinion in decision.inputs for x in opinion.evidence}
    if not documents or any(key not in allowed for key in documents):
        raise SourceUnavailable("original_source_id_mismatch")
    return list(documents.values())


def _disclosures(ticker):
    # The legacy collector conflates missing corp-code mappings with zero reports.
    # For this experiment an unverified mapping is an unavailable source.
    if not collectors._load_corp_code_map().get(ticker):
        raise SourceUnavailable("dart_corp_code_unavailable")
    return collectors.fetch_disclosures(ticker)


def collect_new_documents(thesis: EntryThesisRecord, now: datetime) -> tuple[list[ThesisDocument], list[str]]:
    """Latest collector windows only; no claims of exhaustive news coverage.

    A date-only filing from the entry date is not proven newer than the entry.
    Missing/oversized bodies remain explicit failures, never neutral evidence.
    """
    ticker = thesis.ticker
    documents, failures = [], []
    known = {d.source_id for d in thesis.documents}
    cutoff = thesis.completed_at or thesis.started_at
    today = now.astimezone(KST).date()
    for kind, fetch in (("chart", lambda: collectors.fetch_market_context(ticker)),
                        ("news", lambda: collectors.fetch_company_news(ticker)),
                        ("disclosure", lambda: _disclosures(ticker))):
        try:
            items = fetch()
            if items is None:
                raise SourceUnavailable("collection_failed")
            if kind == "chart":
                if items.ticker != ticker:
                    raise SourceUnavailable("chart_ticker_mismatch")
                if not items.bars or items.bars[-1].date != today:
                    raise SourceUnavailable("stale_chart")
                content = items.model_dump_json()
                documents.append(ThesisDocument(
                    ticker=ticker, source_id="chart:" + ticker + ":" + hashlib.sha256(content.encode()).hexdigest(),
                    kind="chart", scope="market_data", content=content, observed_at=now, published_on=today,
                ))
                continue
            for item in items:
                if item.url in known:
                    continue
                if kind == "news":
                    if item.published_at is None or item.published_at.tzinfo is None:
                        failures.append("news:publication_time_unknown:" + item.url)
                        continue
                    if not cutoff < item.published_at <= now:
                        continue
                elif not cutoff.astimezone(KST).date() < item.received_at <= today:
                    continue
                try:
                    body = fetch_news_body(item) if kind == "news" else fetch_disclosure_body(item)
                    documents.append(ThesisDocument(
                        ticker=ticker, source_id=item.url, kind=kind, scope="full_text", content=body,
                        observed_at=now, published_at=item.published_at if kind == "news" else None,
                        published_on=item.received_at if kind == "disclosure" else None,
                    ))
                except (SourceUnavailable, UnicodeError, ValueError):
                    failures.append(kind + ":body_unavailable:" + item.url)
        except Exception:
            failures.append(kind + ":collection_unavailable")
    return documents, failures
