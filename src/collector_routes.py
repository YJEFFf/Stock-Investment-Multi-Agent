"""Declarative Naver routing. Repair proposals cannot contain executable code.

A process pins the version on first use. Only the trusted deployment path may
change config/collector_routes.json; the Codex OS user cannot read this repo.
"""
import hashlib
import json
import re
import string
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config/collector_routes.json"
HOSTS = frozenset({"fchart.stock.naver.com", "polling.finance.naver.com",
                   "finance.naver.com", "m.stock.naver.com", "stock.naver.com"})
DEFAULT_URLS = {
    "index": "https://fchart.stock.naver.com/sise.nhn",
    "stock_news": "https://m.stock.naver.com/api/news/stock/{ticker}",
    "sector_news": "https://stock.naver.com/api/domestic/news/list",
    "sector_groups": "https://stock.naver.com/api/domestic/market/upjong/list",
    "sector_members": "https://stock.naver.com/api/domestic/market/upjong/{group}/stocklist",
    "quotes": "https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}",
}
SLOTS = {"stock_news": {"ticker"}, "sector_members": {"group"}, "quotes": {"codes"}}
# canonical keys -> response keys, recursively applied, with no expressions.
KEYS = {
    "index": set(),
    "stock_news": {"items", "title", "mobileNewsUrl", "datetime", "officeName"},
    "sector_news": {"articles", "title", "officeId", "articleId", "datetime", "officeHname"},
    "sector_groups": {"no", "type", "name", "totalCnt"},
    "sector_members": {"items", "hasNext", "cursor", "itemCode"},
    # KRX realtime fields must never be remapped to integrated/NXT prices.
    "quotes": set(),
}


def validate_url(url: str) -> None:
    p = urlsplit(url)
    if (p.scheme != "https" or p.hostname not in HOSTS or p.port not in (None, 443)
            or p.username or p.password or p.fragment or p.query
            or not p.path.startswith("/") or ".." in p.path
            or any(c in url for c in ("\\", "%", "\n", "\r", "\t"))):
        raise ValueError("unapproved Naver URL")


def validate_manifest(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "routes"} or value["version"] != 1:
        raise ValueError("invalid route manifest")
    routes = value["routes"]
    if not isinstance(routes, dict) or set(routes) != set(DEFAULT_URLS):
        raise ValueError("route set changed")
    for name, route in routes.items():
        if not isinstance(route, dict) or set(route) != {"url", "root", "keys"}:
            raise ValueError("invalid route fields")
        url, root, keys = route["url"], route["root"], route["keys"]
        if not isinstance(url, str) or len(url) > 400:
            raise ValueError("invalid route URL")
        validate_url(url)
        slots = set()
        for _, field, spec, conversion in string.Formatter().parse(url):
            if field is not None:
                if spec or conversion:
                    raise ValueError("format expressions forbidden")
                slots.add(field)
        if slots != SLOTS.get(name, set()):
            raise ValueError("identity slot changed")
        for slot in slots:
            if url.count("{" + slot + "}") != 1:
                raise ValueError("duplicate identity slot")
        if not isinstance(root, list) or len(root) > 4 or any(
            not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", k) for k in root
        ):
            raise ValueError("invalid JSON root")
        if not isinstance(keys, dict) or set(keys) - KEYS[name]:
            raise ValueError("unapproved response field")
        if any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", k)
               for k in keys.values()) or len(set(keys.values())) != len(keys):
            raise ValueError("invalid response field mapping")
        if name in {"index", "quotes"} and (root or keys):
            raise ValueError("price parser changes require manual review")
    return value


def default_manifest() -> dict:
    return {"version": 1, "routes": {name: {"url": url, "root": [], "keys": {}}
                                     for name, url in DEFAULT_URLS.items()}}


def digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(validate_manifest(manifest), sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=1)
def load() -> dict:
    return validate_manifest(json.loads(CONFIG_PATH.read_text()))


def url(name: str, fallback: str | None = None, **slots: str) -> str:
    route = load()["routes"][name]
    # Retain explicit caller constants for existing tests / public interfaces.
    template = fallback if fallback and route["url"] == DEFAULT_URLS[name] else route["url"]
    return template.format(**slots)


def normalize(name: str, text: str, manifest: dict | None = None) -> str:
    route = (manifest if manifest is not None else load())["routes"][name]
    if not route["root"] and not route["keys"]:
        return text
    value = json.loads(text)
    try:
        for key in route["root"]:
            value = value[key]
    except (KeyError, TypeError) as exc:
        raise ValueError("response root missing") from exc
    reverse = {actual: canonical for canonical, actual in route["keys"].items()}

    def walk(v, depth=0):
        if depth > 20:
            raise ValueError("response nesting too deep")
        if isinstance(v, list):
            return [walk(item, depth + 1) for item in v]
        if isinstance(v, dict):
            out = {}
            for key, item in v.items():
                target = reverse.get(key, key)
                if target in out:
                    raise ValueError("ambiguous response field mapping")
                out[target] = walk(item, depth + 1)
            return out
        return v
    return json.dumps(walk(value), ensure_ascii=False)
