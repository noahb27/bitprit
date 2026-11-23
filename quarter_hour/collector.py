"""Public GET adapters, timed caching and collection. This module has no order API."""

import json
import re
import time
from dataclasses import dataclass, replace
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import Book, Fees, Market, Result, Side, Snapshot, Spot, epoch, number, positive
from .storage import Store

SERIES = "KXBTC15M"
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com"
SPOT_ROUTE = "/products/BTC-USD/ticker"
MAX_BODY = 2 * 1024 * 1024


class Unavailable(ValueError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Response:
    body: dict
    requested: float
    received: float


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class PublicGet:
    def __init__(self, store: Store, *, timeout: float = 3.0, clock=time.time):
        self.store = store
        self.timeout = timeout
        self.clock = clock
        self.opener = build_opener(NoRedirect())
        self.backoff: dict[str, tuple[float, float]] = {}

    def get(self, base: str, path: str, **query) -> Response:
        route = path + ("?" + urlencode(query) if query else "")
        source = "coinbase" if base == COINBASE else "kalshi"
        route = source + route
        requested = self.clock()
        retry_at, next_delay = self.backoff.get(source, (0, 2))
        if requested < retry_at:
            raise Unavailable(f"{source} backoff until {retry_at:.3f}")
        url = base + path + ("?" + urlencode(query) if query else "")
        request = Request(url, headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "Quarter-Hour-Research/1.0",
        })
        status = None
        payload = None
        retry_after = None
        error = None
        try:
            with self.opener.open(request, timeout=self.timeout) as reply:
                status = reply.status
                body = reply.read(MAX_BODY + 1)
                received = self.clock()
                if len(body) > MAX_BODY:
                    raise ValueError("response exceeded the size limit")
                age = reply.headers.get("Age", "0")
                try:
                    data = json.loads(body, parse_float=number, parse_constant=number)
                except (ValueError, UnicodeError):
                    payload = {"unparsed": body[:4096].decode(errors="replace")}
                    raise
                payload = {"body": data, "http_age": age, "http_date": reply.headers.get("Date")}
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                if int(age) != 0:
                    raise ValueError(f"HTTP cache age {age} seconds")
        except HTTPError as exc:
            status = exc.code
            retry_after = exc.headers.get("Retry-After")
            exc.close()
            error = f"HTTP {status}"
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            error = str(exc)
        if error is not None:
            received = self.clock()
            self.store.raw(route, requested, received, status, payload)
            self.store.quality(received, "PUBLIC_REQUEST_FAILED", route=route, error=error, status=status)
            if status in (401, 403, 408, 429) or status is None or status >= 500:
                delay = max(next_delay, retry_delay(retry_after, received))
                self.backoff[source] = (received + delay, min(120, 2 * next_delay))
            raise Unavailable(f"{source} {path}: {error}", status=status)
        self.backoff.pop(source, None)
        self.store.raw(route, requested, received, status, payload)
        return Response(data, requested, received)


def retry_delay(header, now: float) -> float:
    try:
        if header is None:
            return 0
        if header.isdigit():
            return float(int(header))
        date = parsedate_to_datetime(header)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, date.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return 0


def ticker(value) -> str:
    if not isinstance(value, str) or re.fullmatch(r"KXBTC15M-[A-Z0-9-]{1,80}", value) is None:
        raise ValueError("unexpected KXBTC15M ticker")
    return value


def parse_market(data: dict, received: float, expected: str | None = None) -> Market:
    key, event = ticker(data["ticker"]), ticker(data["event_ticker"])
    if (expected and key != expected) or (key != event and not key.startswith(event + "-")):
        raise ValueError("market and event tickers do not match")
    if data.get("market_type") != "binary":
        raise ValueError("expected a binary market")
    opens, closes = epoch(data["open_time"]), epoch(data["close_time"])
    if not opens < closes:
        raise ValueError("invalid market session times")
    strike = data.get("floor_strike")
    early = data.get("can_close_early")
    if type(early) is not bool:
        raise ValueError("missing early-close setting")
    rules = data.get("rules_primary")
    secondary = data.get("rules_secondary", "")
    if not isinstance(rules, str) or not isinstance(secondary, str):
        raise ValueError("invalid market rules")
    return Market(
        ticker=key, event=event, series=SERIES, opens=opens, closes=closes,
        received=received, updated=epoch(data["updated_time"]) if data.get("updated_time") else None,
        status=data["status"], strike=positive(strike) if strike not in (None, "", 0, "0") else None,
        strike_type=data.get("strike_type"), rules=rules, secondary_rules=secondary,
        notional=positive(data["notional_value_dollars"]), can_close_early=early,
        fee_waiver_until=epoch(data["fee_waiver_expiration_time"]) if data.get("fee_waiver_expiration_time") else None,
    )


def parse_book(response: Response, key: str) -> Book:
    data = response.body
    if data.get("ticker", key) != key:
        raise ValueError("wrong ticker in order book")
    fp = data["orderbook_fp"]
    sides = []
    for name in ("yes_dollars", "no_dollars"):
        levels = {}
        if not isinstance(fp[name], list):
            raise ValueError("order book side must be an array")
        for level in fp[name]:
            if not isinstance(level, list) or len(level) != 2:
                raise ValueError("invalid fixed-point price level")
            price, quantity = map(positive, level)
            if not price < 1 or price != round(price, 6) or quantity != round(quantity, 2) or price in levels:
                raise ValueError("invalid or duplicate price/quantity level")
            levels[price] = quantity
        sides.append(tuple(sorted(levels.items(), reverse=True)))
    stamp = epoch(data["ts"]) if data.get("ts") else None
    if stamp is not None and stamp > response.received:
        raise ValueError("order book source time is in the future")
    return Book(key, response.requested, response.received, stamp, *sides)


def parse_fees(series: Response, event: Response, key: str) -> Fees:
    s, e = series.body["series"], event.body["event"]
    if (s["ticker"], e["series_ticker"], e["event_ticker"]) != (SERIES, SERIES, key):
        raise ValueError("wrong series or event in fee metadata")
    override_type, override_multiplier = e.get("fee_type_override"), e.get("fee_multiplier_override")
    if (override_type is None) != (override_multiplier is None):
        raise ValueError("incomplete event fee override")
    mode = s["fee_type"] if override_type is None else override_type
    multiplier = number(s["fee_multiplier"] if override_multiplier is None else override_multiplier)
    if not 0 <= multiplier <= 100 or not isinstance(mode, str):
        raise ValueError("invalid fee metadata")
    terms = s.get("contract_terms_url")
    sources = tuple(source["name"] for source in s.get("settlement_sources", []))
    if not isinstance(terms, str) or not terms.startswith("https://") or not sources:
        raise ValueError("missing terms URL or settlement source")
    return Fees(mode, multiplier, min(series.received, event.received), key, terms, sources)


def parse_result(response: Response, expected: str, event: str) -> tuple[Market, Result | None]:
    data = response.body["market"]
    market = parse_market(data, response.received, expected)
    if market.event != event:
        raise ValueError("result event does not match recorded market")
    if market.status != "finalized":
        return market, None
    side = Side(data["result"])
    payout = data.get("settlement_value_dollars")
    if market.notional != 1 or (payout not in (None, "") and number(payout) != (side == Side.YES)):
        raise ValueError("non-binary settlement payout")
    settled = epoch(data["settlement_ts"]) if data.get("settlement_ts") else None
    try:
        benchmark = positive(data.get("expiration_value"))
    except ValueError:
        benchmark = None
    return market, Result(market.ticker, market.event, side, response.received, settled, benchmark, market.closes)


BAD_DATA = (KeyError, TypeError, ValueError, ArithmeticError, OverflowError)


class Collector:
    def __init__(self, store: Store, http: PublicGet, *, interval: float, clock=time.time):
        self.store = store
        self.http = http
        self.interval = interval
        self.clock = clock
        self.cache: dict[str, Response] = {}
        self.previous = store.last_snapshot()
        self.restarting = True

    def _get(self, path: str, ttl: float, **query) -> Response:
        item = self.cache.get(path)
        now = self.clock()
        if item is not None and 0 <= now - item.received < ttl:
            return item
        item = self.http.get(KALSHI, path, **query)
        if len(self.cache) >= 64:
            self.cache = {key: old for key, old in self.cache.items() if now - old.received < 60}
        self.cache[path] = item
        return item

    def _active_market(self) -> Market:
        listing = self._get("/markets", 5, series_ticker=SERIES, status="open", limit=100)
        pages = 1
        cursor = listing.body.get("cursor")
        entries = [(item, listing.received) for item in listing.body["markets"]]
        while cursor:
            if pages >= 4:
                raise ValueError("market listing did not fit in four pages")
            page = self.http.get(KALSHI, "/markets", series_ticker=SERIES, status="open", limit=100, cursor=cursor)
            entries.extend((item, page.received) for item in page.body["markets"])
            cursor = page.body.get("cursor")
            pages += 1
        now = self.clock()
        candidates = []
        for item, received in entries:
            if item["status"] == "active" and epoch(item["open_time"]) <= now < epoch(item["close_time"]):
                candidates.append(parse_market(item, received))
        if len(candidates) != 1:
            raise ValueError(f"expected one active 15-minute market, found {len(candidates)}")
        return candidates[0]

    def _confirm_market(self, market: Market) -> Market:
        response = self.http.get(KALSHI, f"/markets/{quote(market.ticker)}")
        checked = parse_market(response.body["market"], response.received, market.ticker)
        if (checked.event, checked.opens, checked.closes) != (market.event, market.opens, market.closes):
            raise ValueError("event or market timing changed while capturing the order book")
        return replace(checked, status_requested=response.requested)

    def _capture(self, label: str, flags: list[str], read):
        try:
            return read()
        except Unavailable as exc:
            flags.append(f"{label}_UNAVAILABLE: {exc}")
        except BAD_DATA as exc:
            flags.append(f"{label}_INVALID: {exc}")
            self.store.quality(self.clock(), f"{label}_INVALID", error=str(exc))
        return None

    def _spot(self) -> Spot:
        response = self.http.get(COINBASE, SPOT_ROUTE)
        seen = epoch(response.body["time"])
        if seen > response.received:
            raise ValueError("spot source time is in the future")
        return Spot(positive(response.body["price"]), seen, response.received)

    def collect(self) -> Snapshot:
        flags: list[str] = []
        market = self._capture("MARKET", flags, self._active_market)
        if market and not self.store.save_market(market):
            flags.append("MARKET_IDENTITY_CHANGED")
            self.store.quality(self.clock(), "MARKET_IDENTITY_CHANGED", ticker=market.ticker)
            market = None
        fees = None
        if market:
            fees = self._capture("FEES", flags, lambda: parse_fees(
                self._get(f"/series/{SERIES}", 30),
                self._get(f"/events/{quote(market.event)}", 30), market.event,
            ))
        spot = self._capture("SPOT", flags, self._spot)
        book = None
        if market:
            book = self._capture("BOOK", flags, lambda: parse_book(
                self.http.get(KALSHI, f"/markets/{quote(market.ticker)}/orderbook", depth=100),
                market.ticker,
            ))
        if market and market.can_close_early and book:
            checked = self._capture("MARKET_STATUS", flags, lambda: self._confirm_market(market))
            if checked is not None:
                market = checked
        if book:
            if book.crossed:
                flags.append("CROSSED_OR_LOCKED_BOOK")
            if not (book.yes and book.no):
                flags.append("ONE_SIDED_OR_EMPTY_BOOK")
            if book.source_at is None:
                flags.append("BOOK_EXCHANGE_TIME_UNAVAILABLE")
        now = self.clock()
        if spot and now - spot.source_at > 10:
            flags.append("SPOT_SOURCE_AGE_OVER_10S")
        if market and (market.status != "active" or not market.opens <= now < market.closes):
            flags.append("OUTSIDE_MARKET_WINDOW")
        if market and (not market.rules.strip() or market.strike is None):
            flags.append("MISSING_RULES_OR_STRIKE")
        previous_at = self.previous.at if self.previous else None
        gap = self.restarting or (previous_at is not None and now - previous_at > self.interval * 2.5)
        if gap:
            self.store.quality(now, "COLLECTION_GAP", previous=previous_at, interval=self.interval)
        sample = self.store.save_snapshot(Snapshot(now, market, book, spot, fees, tuple(flags), gap))
        self.previous, self.restarting = sample, False
        return sample

    def check_result(self, *, priority: str | None = None):
        now = self.clock()
        item = self.store.due_result(now, priority=priority)
        if item is None:
            return False
        key, event = item["ticker"], item["event"]
        self.store.checked_result(key, now)
        try:
            try:
                reply = self.http.get(KALSHI, f"/markets/{quote(key)}")
            except Unavailable as exc:
                if exc.status != 404:
                    raise
                reply = self.http.get(KALSHI, f"/historical/markets/{quote(key)}")
            market, result = parse_result(reply, key, event)
            if result is not None:
                if not self.store.save_result(market, result):
                    raise ValueError("final result conflicts with the recorded market or result")
                if result.benchmark_value is None:
                    self.store.quality(result.received, "FINAL_BENCHMARK_VALUE_UNAVAILABLE", ticker=key)
        except BAD_DATA as exc:
            self.store.quality(self.clock(), "RESULT_UNAVAILABLE_OR_INVALID", ticker=key, error=str(exc))
        return True
