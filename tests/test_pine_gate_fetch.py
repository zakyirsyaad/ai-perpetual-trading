import contextlib
import logging
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import httpx

from research.pine_gate import fetch

from research.pine_gate.bars import (
    day_path,
    read_day,
    wib_day_start_ms,
)
from research.pine_gate.fetch import (
    FUNDING_PATH,
    HEADERS,
    KLINES_PATH,
    PROD_HOST,
    VST_HOST,
    MAX_BODY_BYTES,
    MAX_FUNDING_REQUESTS,
    ExchangeError,
    HttpStatusError,
    IncompleteDayError,
    RateLimitedError,
    SyncResult,
    check_status,
    collect,
    fetch_day,
    fetch_funding_times,
    fetch_window,
    parse_json_body,
    read_capped,
    sync_days,
    sync_warmup,
)

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
DAY1 = date(2026, 7, 1)
D1_START = wib_day_start_ms(DAY1)


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def noop(_seconds):
    return None


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def kline(t, base=100.0, **overrides):
    row = {
        "time": t,
        "open": str(base),
        "high": str(base + 2),
        "low": str(base - 1),
        "close": str(base + 1),
        "volume": "5.5",
    }
    row.update(overrides)
    return row


def klines_for(params, drop=0, base=100.0):
    start, end = int(params["startTime"]), int(params["endTime"])
    step = {"1m": MIN, "1h": HOUR, "4h": 4 * HOUR}[params["interval"]]
    rows = [kline(t, base + (t // step) % 50) for t in range(start, end + 1, step)]
    return rows[: len(rows) - drop] if drop else rows


class FakeHttp:
    """Records calls; ``respond(params, call_index)`` returns the payload or raises."""

    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def __call__(self, url, params, headers):
        self.calls.append((url, dict(params), dict(headers)))
        return self.respond(dict(params), len(self.calls) - 1)


def full_klines(params, index):
    return {"code": 0, "data": klines_for(params)}


def ok(data):
    return {"code": 0, "data": data}


class FetchWindowTest(unittest.TestCase):
    def test_request_shape_and_host(self):
        fake = FakeHttp(full_klines)
        rows = fetch_window(fake, VST_HOST, "ETH-USDT", "1m", 10 * MIN, 13 * MIN - 1)
        url, params, headers = fake.calls[0]
        self.assertEqual(url, VST_HOST + KLINES_PATH)
        self.assertEqual(
            params,
            {
                "symbol": "ETH-USDT",
                "interval": "1m",
                "startTime": str(10 * MIN),
                "endTime": str(13 * MIN - 1),
                "limit": "3",
            },
        )
        self.assertEqual(headers, HEADERS)
        self.assertEqual([r[0] for r in rows], [10 * MIN, 11 * MIN, 12 * MIN])

    def test_unsorted_rows_are_sorted(self):
        def respond(params, index):
            return ok(list(reversed(klines_for(params))))

        rows = fetch_window(FakeHttp(respond), PROD_HOST, "BTC-USDT", "1m", 0, 3 * MIN - 1)
        self.assertEqual([r[0] for r in rows], [0, MIN, 2 * MIN])

    def test_duplicate_rows_are_rejected(self):
        def respond(params, index):
            data = klines_for(params)
            return ok(data + [dict(data[0])])

        with self.assertRaisesRegex(ExchangeError, "duplicate"):
            fetch_window(FakeHttp(respond), PROD_HOST, "BTC-USDT", "1m", 0, 3 * MIN - 1)

    def test_nonzero_code_raises(self):
        fake = FakeHttp(lambda p, i: {"code": 100400, "msg": "bad"})
        with self.assertRaisesRegex(ExchangeError, "100400"):
            fetch_window(fake, PROD_HOST, "BTC-USDT", "1m", 0, MIN - 1)

    def test_malformed_payloads_raise(self):
        for payload in ([], {"code": 0}, {"code": 0, "data": {"a": 1}}, {"code": 0, "data": [5]}):
            with self.subTest(payload=payload):
                with self.assertRaises(ExchangeError):
                    fetch_window(FakeHttp(lambda p, i: payload), PROD_HOST, "X", "1m", 0, MIN - 1)

    def test_invalid_rows_raise(self):
        bad_rows = {
            "high below close": kline(0, high="100.5"),
            "low above open": kline(0, low="101"),
            "zero price": kline(0, open="0", low="0"),
            "negative volume": kline(0, volume="-1"),
            "nan price": kline(0, close="nan"),
            "infinite volume": kline(0, volume="inf"),
            "non numeric": kline(0, open="abc"),
            "missing field": {k: v for k, v in kline(0).items() if k != "low"},
            "fractional time": kline(0, time=0.5),
            "bool time": kline(0, time=True),
            "outside window": kline(5 * MIN),
            "off interval": kline(30_000),
        }
        for name, row in bad_rows.items():
            with self.subTest(name):
                fake = FakeHttp(lambda p, i, row=row: ok([row]))
                with self.assertRaises(ExchangeError):
                    fetch_window(fake, PROD_HOST, "BTC-USDT", "1m", 0, MIN - 1)

    def test_string_time_is_accepted(self):
        fake = FakeHttp(lambda p, i: ok([kline("0")]))
        rows = fetch_window(fake, PROD_HOST, "BTC-USDT", "1m", 0, MIN - 1)
        self.assertEqual(rows[0][0], 0)

    def test_bad_arguments_raise_before_any_request(self):
        fake = FakeHttp(full_klines)
        cases = [
            (PROD_HOST, "1m", 0, 1441 * MIN - 1),
            (PROD_HOST, "1m", 0, MIN),
            (PROD_HOST, "1m", 30_000, MIN + 29_999),
            (PROD_HOST, "1m", MIN, 0),
            (PROD_HOST, "7x", 0, MIN - 1),
            ("https://example.com", "1m", 0, MIN - 1),
            ("http://open-api.bingx.com", "1m", 0, MIN - 1),
        ]
        for host, interval, start, end in cases:
            with self.subTest(host=host, interval=interval, start=start, end=end):
                with self.assertRaises(ValueError):
                    fetch_window(fake, host, "BTC-USDT", interval, start, end)
        self.assertEqual(fake.calls, [])


class FetchDayTest(unittest.TestCase):
    def test_sends_fixed_window_and_source_header(self):
        fake = FakeHttp(full_klines)
        rows = fetch_day(fake, PROD_HOST, "BTC-USDT", DAY1, sleep=noop)
        self.assertEqual(len(rows), 1440)
        self.assertEqual(rows[0][0], D1_START)
        url, params, headers = fake.calls[0]
        self.assertEqual(url, PROD_HOST + KLINES_PATH)
        self.assertEqual(params["startTime"], str(D1_START))
        self.assertEqual(params["endTime"], str(D1_START + DAY - 1))
        self.assertEqual(params["limit"], "1440")
        self.assertEqual(params["interval"], "1m")
        self.assertEqual(headers["X-SOURCE-KEY"], "BX-AI-SKILL")

    def test_incomplete_day_is_retried_then_rejected(self):
        fake = FakeHttp(lambda p, i: ok(klines_for(p, drop=1)))
        sleeps = Sleeps()
        with self.assertRaisesRegex(IncompleteDayError, "1439 of 1440"):
            fetch_day(fake, PROD_HOST, "BTC-USDT", DAY1, sleep=sleeps)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(len(sleeps.calls), 2)
        self.assertTrue(all(s >= 1.0 for s in sleeps.calls))

    def test_retries_argument_bounds_attempts(self):
        fake = FakeHttp(lambda p, i: ok(klines_for(p, drop=1)))
        with self.assertRaises(IncompleteDayError):
            fetch_day(fake, PROD_HOST, "BTC-USDT", DAY1, retries=1, sleep=noop)
        self.assertEqual(len(fake.calls), 1)
        with self.assertRaises(ValueError):
            fetch_day(fake, PROD_HOST, "BTC-USDT", DAY1, retries=0, sleep=noop)

    def test_incomplete_then_complete_succeeds(self):
        def respond(params, index):
            return ok(klines_for(params, drop=1 if index == 0 else 0))

        fake = FakeHttp(respond)
        rows = fetch_day(fake, PROD_HOST, "BTC-USDT", DAY1, sleep=noop)
        self.assertEqual(len(rows), 1440)
        self.assertEqual(len(fake.calls), 2)

    def test_transport_error_and_http_5xx_are_retried(self):
        def respond(params, index):
            if index == 0:
                raise httpx.ConnectError("boom")
            if index == 1:
                raise HttpStatusError(503, "HTTP 503 from open-api.bingx.com")
            return ok(klines_for(params))

        fake = FakeHttp(respond)
        sleeps = Sleeps()
        rows = fetch_day(fake, PROD_HOST, "BTC-USDT", DAY1, sleep=sleeps)
        self.assertEqual(len(rows), 1440)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(len(sleeps.calls), 2)

    def test_exchange_and_validation_errors_are_not_retried(self):
        for name, respond in (
            ("exchange code", lambda p, i: {"code": 1, "msg": "no"}),
            ("bad row", lambda p, i: ok([kline(0, high="1")])),
            ("http 400", lambda p, i: (_ for _ in ()).throw(HttpStatusError(400, "HTTP 400"))),
            ("decoding", lambda p, i: (_ for _ in ()).throw(httpx.DecodingError("bad gzip"))),
        ):
            with self.subTest(name):
                fake = FakeHttp(respond)
                with self.assertRaises((ExchangeError, httpx.HTTPError)):
                    fetch_day(fake, PROD_HOST, "BTC-USDT", DAY1, sleep=noop)
                self.assertEqual(len(fake.calls), 1)

    def test_rate_limit_is_never_retried(self):
        def respond(params, index):
            raise RateLimitedError("HTTP 429 from open-api.bingx.com; Retry-After '30'")

        fake = FakeHttp(respond)
        with self.assertRaises(RateLimitedError):
            fetch_day(fake, PROD_HOST, "BTC-USDT", DAY1, sleep=noop)
        self.assertEqual(len(fake.calls), 1)

    def test_programming_errors_propagate(self):
        def respond(params, index):
            raise KeyError("bug")

        with self.assertRaises(KeyError):
            fetch_day(FakeHttp(respond), PROD_HOST, "BTC-USDT", DAY1, sleep=noop)


def no_network():
    """Every other httpx entry point fails the test instead of reaching the network."""
    stack = contextlib.ExitStack()
    for name in ("get", "request", "Client", "AsyncClient"):
        stack.enter_context(mock.patch.object(
            fetch.httpx, name, side_effect=AssertionError(f"network via httpx.{name}")))
    return stack


class HttpPolicyTest(unittest.TestCase):
    """Status, size and body rules of the real transport (B2)."""

    def test_status_classification(self):
        check_status(200, {}, "open-api.bingx.com")
        for status in (403, 418, 429):
            with self.subTest(status=status):
                with self.assertRaises(RateLimitedError) as ctx:
                    check_status(status, {"Retry-After": "30"}, "open-api.bingx.com")
                self.assertIn(f"HTTP {status}", str(ctx.exception))
                self.assertIn("Retry-After '30'", str(ctx.exception))
        with self.assertRaises(RateLimitedError) as ctx:
            check_status(429, {"Retry-After": "x" * 500 + "\nboom"}, "h")
        self.assertLess(len(str(ctx.exception)), 120)
        self.assertNotIn("\n", str(ctx.exception))
        for status, retryable in ((500, True), (503, True), (404, False), (302, False)):
            with self.subTest(status=status):
                with self.assertRaises(HttpStatusError) as ctx:
                    check_status(status, {}, "h")
                self.assertEqual(ctx.exception.status, status)
                self.assertEqual(ctx.exception.retryable, retryable)
                self.assertIsInstance(ctx.exception, ExchangeError)

    def test_bodies_over_the_cap_are_refused(self):
        self.assertEqual(read_capped([b"ab", b"c"], "3"), b"abc")
        self.assertEqual(read_capped([b"{}"], None), b"{}")
        with self.assertRaises(ExchangeError):
            read_capped([b"x"], str(MAX_BODY_BYTES + 1))
        chunk = b"x" * (1024 * 1024)
        with self.assertRaises(ExchangeError):
            read_capped([chunk] * 5, None)  # no Content-Length: counted while reading

    def test_json_body(self):
        self.assertEqual(parse_json_body(b'{"code": 0}'), {"code": 0})
        for body in (b"not json", b'{"x": NaN}', b"\xff\xfe"):
            with self.subTest(body=body):
                with self.assertRaises(ExchangeError):
                    parse_json_body(body)

    def test_httpx_get_streams_without_proxies_env_or_redirects(self):
        seen = {}

        class Response:
            status_code = 200
            headers = {"Content-Length": "12"}

            def iter_bytes(self):
                yield b'{"code": 0}\n'

        @contextlib.contextmanager
        def stream(method, url, **kwargs):
            seen.update(kwargs, method=method, url=url)
            yield Response()

        with mock.patch.object(fetch.httpx, "stream", stream), no_network():
            got = fetch.httpx_get(PROD_HOST + KLINES_PATH, {"symbol": "BTC-USDT"}, HEADERS)
        self.assertEqual(got, {"code": 0})
        self.assertEqual(seen["method"], "GET")
        self.assertIs(seen["trust_env"], False)
        self.assertIs(seen["follow_redirects"], False)
        self.assertEqual(seen["headers"], HEADERS)
        self.assertEqual(seen["params"], {"symbol": "BTC-USDT"})

    def test_httpx_get_raises_on_status_before_reading(self):
        class Response:
            status_code = 429
            headers = {"Retry-After": "5"}

            def iter_bytes(self):
                raise AssertionError("body read")

        @contextlib.contextmanager
        def stream(method, url, **kwargs):
            yield Response()

        with mock.patch.object(fetch.httpx, "stream", stream), no_network():
            with self.assertRaises(RateLimitedError):
                fetch.httpx_get(PROD_HOST + KLINES_PATH, {}, HEADERS)


class SyncDaysTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_skips_existing_and_records_failures(self):
        d1, d2, d3 = date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)
        existing = day_path(self.data_dir, "prod", "BTC-USDT", "1m", d1)
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"sentinel")
        broken_start = wib_day_start_ms(d2)

        def respond(params, index):
            if int(params["startTime"]) == broken_start:
                return ok(klines_for(params, drop=5))
            return ok(klines_for(params))

        fake = FakeHttp(respond)
        result = sync_days(fake, PROD_HOST, "prod", "BTC-USDT", d1, d3, self.data_dir, sleep=noop)
        self.assertEqual(result, SyncResult(fetched=1, skipped=1, failed=(d2,)))
        self.assertEqual(existing.read_bytes(), b"sentinel")
        self.assertFalse(day_path(self.data_dir, "prod", "BTC-USDT", "1m", d2).exists())
        stored = read_day(day_path(self.data_dir, "prod", "BTC-USDT", "1m", d3))
        self.assertEqual(len(stored), 1440)
        self.assertEqual(stored[0][0], wib_day_start_ms(d3))
        leftovers = [p.name for p in existing.parent.iterdir()]
        self.assertEqual(sorted(leftovers), sorted(["2026-07-01.csv.gz", "2026-07-03.csv.gz"]))
        # 1 request for d3 plus 3 attempts for d2.
        self.assertEqual(len(fake.calls), 4)

    def test_waits_at_least_one_second_between_requests(self):
        events = []

        def respond(params, index):
            events.append("get")
            return ok(klines_for(params))

        def sleep(seconds):
            events.append(("sleep", seconds))

        fake = FakeHttp(respond)
        sync_days(
            fake, PROD_HOST, "prod", "BTC-USDT", date(2026, 7, 1), date(2026, 7, 3), self.data_dir, sleep=sleep
        )
        self.assertEqual(events[0], "get")
        self.assertEqual(len(fake.calls), 3)
        for before, after in zip(events, events[1:]):
            if after == "get":
                self.assertIsInstance(before, tuple)
                self.assertGreaterEqual(before[1], 1.0)
        self.assertEqual(sum(1 for e in events if isinstance(e, tuple)), 2)

    def test_failed_transport_day_does_not_stop_the_range(self):
        def respond(params, index):
            if int(params["startTime"]) == D1_START:
                raise httpx.ReadTimeout("slow")
            return ok(klines_for(params))

        result = sync_days(
            FakeHttp(respond), VST_HOST, "vst", "ETH-USDT", DAY1, date(2026, 7, 2), self.data_dir, sleep=noop
        )
        self.assertEqual(result.failed, (DAY1,))
        self.assertEqual(result.fetched, 1)

    def test_three_consecutive_failed_days_abort_the_rest(self):
        days = [DAY1 + timedelta(days=k) for k in range(6)]
        failing = {wib_day_start_ms(d) for d in days[:3]}

        def respond(params, index):
            return ok(klines_for(params, drop=1 if int(params["startTime"]) in failing else 0))

        fake = FakeHttp(respond)
        result = sync_days(fake, PROD_HOST, "prod", "BTC-USDT", days[0], days[-1],
                           self.data_dir, sleep=noop)
        self.assertEqual(result.failed, tuple(days[:3]))
        self.assertEqual(result.not_attempted, tuple(days[3:]))
        self.assertEqual(result.fetched, 0)
        self.assertEqual({int(c[1]["startTime"]) for c in fake.calls}, failing)

    def test_a_success_resets_the_failure_streak(self):
        days = [DAY1 + timedelta(days=k) for k in range(5)]
        failing = {wib_day_start_ms(days[k]) for k in (0, 2, 3)}

        def respond(params, index):
            return ok(klines_for(params, drop=1 if int(params["startTime"]) in failing else 0))

        result = sync_days(FakeHttp(respond), PROD_HOST, "prod", "BTC-USDT", days[0], days[-1],
                           self.data_dir, sleep=noop)
        self.assertEqual(result.failed, (days[0], days[2], days[3]))
        self.assertEqual(result.not_attempted, ())
        self.assertEqual(result.fetched, 2)

    def test_rate_limit_stops_the_whole_range(self):
        def respond(params, index):
            if index == 1:
                raise RateLimitedError("HTTP 429 from open-api.bingx.com")
            return ok(klines_for(params))

        fake = FakeHttp(respond)
        with self.assertRaises(RateLimitedError):
            sync_days(fake, PROD_HOST, "prod", "BTC-USDT", DAY1, DAY1 + timedelta(days=4),
                      self.data_dir, sleep=noop)
        self.assertEqual(len(fake.calls), 2)

    def test_a_day_file_written_meanwhile_is_kept_and_counted_as_skipped(self):
        target = day_path(self.data_dir, "prod", "BTC-USDT", "1m", DAY1)

        def respond(params, index):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"other writer")  # e.g. the cron collector won the race
            return ok(klines_for(params))

        result = sync_days(FakeHttp(respond), PROD_HOST, "prod", "BTC-USDT", DAY1, DAY1,
                           self.data_dir, sleep=noop)
        self.assertEqual((result.fetched, result.skipped, result.failed), (0, 1, ()))
        self.assertEqual(target.read_bytes(), b"other writer")
        self.assertEqual([p.name for p in target.parent.iterdir()], [target.name])

    def test_spans_over_900_days_and_mismatched_hosts_are_refused(self):
        fake = FakeHttp(full_klines)
        with self.assertRaises(ValueError):
            sync_days(fake, PROD_HOST, "prod", "BTC-USDT", DAY1, DAY1 + timedelta(days=900),
                      self.data_dir, sleep=noop)
        for source, host in (("prod", VST_HOST), ("vst", PROD_HOST), ("other", PROD_HOST)):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    sync_days(fake, host, source, "BTC-USDT", DAY1, DAY1, self.data_dir,
                              sleep=noop)
        self.assertEqual(fake.calls, [])

    def test_end_before_start_raises(self):
        with self.assertRaises(ValueError):
            sync_days(FakeHttp(full_klines), PROD_HOST, "prod", "BTC-USDT", date(2026, 7, 2), DAY1, self.data_dir, sleep=noop)


class SyncWarmupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "warmup.csv.gz"

    def test_splits_into_windows_and_writes_one_file(self):
        start = 0
        end = 3000 * HOUR  # 3,000 1h bars -> windows of 1440, 1440, 120
        fake = FakeHttp(full_klines)
        sleeps = Sleeps()
        sync_warmup(fake, PROD_HOST, "BTC-USDT", "1h", start, end, self.path, sleep=sleeps)
        self.assertEqual([c[1]["limit"] for c in fake.calls], ["1440", "1440", "120"])
        self.assertEqual(
            [(int(c[1]["startTime"]), int(c[1]["endTime"])) for c in fake.calls],
            [
                (0, 1440 * HOUR - 1),
                (1440 * HOUR, 2880 * HOUR - 1),
                (2880 * HOUR, 3000 * HOUR - 1),
            ],
        )
        self.assertTrue(all(c[1]["interval"] == "1h" for c in fake.calls))
        rows = read_day(self.path)
        self.assertEqual(len(rows), 3000)
        self.assertEqual(rows[-1][0], 2999 * HOUR)
        self.assertEqual(len(sleeps.calls), 2)
        self.assertTrue(all(s >= 1.0 for s in sleeps.calls))

    def test_four_hour_bars(self):
        fake = FakeHttp(full_klines)
        sync_warmup(fake, PROD_HOST, "ETH-USDT", "4h", 0, 10 * 4 * HOUR, self.path, sleep=noop)
        self.assertEqual(len(read_day(self.path)), 10)
        self.assertEqual(fake.calls[0][1]["interval"], "4h")

    def test_incomplete_window_writes_nothing(self):
        fake = FakeHttp(lambda p, i: ok(klines_for(p, drop=1)))
        with self.assertRaises(IncompleteDayError):
            sync_warmup(fake, PROD_HOST, "BTC-USDT", "1h", 0, 10 * HOUR, self.path, sleep=noop)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_more_than_900_days_is_refused(self):
        fake = FakeHttp(full_klines)
        with self.assertRaises(ValueError):
            sync_warmup(fake, PROD_HOST, "BTC-USDT", "1h", 0, 901 * DAY, self.path, sleep=noop)
        self.assertEqual(fake.calls, [])

    def test_misaligned_range_raises(self):
        for start, end in ((1, 10 * HOUR), (0, 10 * HOUR + 1), (HOUR, HOUR), (2 * HOUR, HOUR)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    sync_warmup(FakeHttp(full_klines), PROD_HOST, "BTC-USDT", "1h", start, end, self.path, sleep=noop)


def funding_row(t):
    return {"symbol": "BTC-USDT", "fundingRate": "0.0001", "fundingTime": t}


class FundingTest(unittest.TestCase):
    def server(self, times):
        def respond(params, index):
            start, end, limit = int(params["startTime"]), int(params["endTime"]), int(params["limit"])
            inside = [t for t in times if start <= t <= end]
            return ok([funding_row(t) for t in inside[:limit]])

        return FakeHttp(respond)

    def test_single_window_with_both_bounds(self):
        times = [i * 8 * HOUR for i in range(10)]
        fake = self.server(times)
        got = fetch_funding_times(fake, PROD_HOST, "BTC-USDT", 0, 100 * HOUR, sleep=noop)
        self.assertEqual(got, times)
        self.assertEqual(len(fake.calls), 1)
        url, params, headers = fake.calls[0]
        self.assertEqual(url, PROD_HOST + FUNDING_PATH)
        self.assertEqual(params["startTime"], "0")
        self.assertEqual(params["endTime"], str(100 * HOUR))
        self.assertEqual(params["limit"], "1000")
        self.assertEqual(params["symbol"], "BTC-USDT")
        self.assertEqual(headers, HEADERS)

    def test_full_window_is_split_not_paginated(self):
        times = [i * 8 * HOUR for i in range(1200)]
        fake = self.server(times)
        sleeps = Sleeps()
        got = fetch_funding_times(fake, PROD_HOST, "BTC-USDT", 0, 1200 * 8 * HOUR, sleep=sleeps)
        self.assertEqual(got, times)
        self.assertEqual(len(fake.calls), 3)
        for _, params, _ in fake.calls:
            self.assertIn("startTime", params)
            self.assertIn("endTime", params)
        windows = [(int(c[1]["startTime"]), int(c[1]["endTime"])) for c in fake.calls]
        halves = windows[1:]
        self.assertEqual(halves[0][0], windows[0][0])
        self.assertEqual(halves[0][1] + 1, halves[1][0])
        self.assertEqual(halves[1][1], windows[0][1])
        self.assertEqual(len(sleeps.calls), 2)
        self.assertTrue(all(s >= 1.0 for s in sleeps.calls))

    def test_unsorted_rows_are_sorted(self):
        fake = FakeHttp(lambda p, i: ok([funding_row(3 * HOUR), funding_row(HOUR)]))
        got = fetch_funding_times(fake, PROD_HOST, "BTC-USDT", 0, 10 * HOUR, sleep=noop)
        self.assertEqual(got, [HOUR, 3 * HOUR])

    def test_duplicates_raise(self):
        fake = FakeHttp(lambda p, i: ok([funding_row(HOUR), funding_row(HOUR)]))
        with self.assertRaisesRegex(ExchangeError, "duplicate"):
            fetch_funding_times(fake, PROD_HOST, "BTC-USDT", 0, 10 * HOUR, sleep=noop)

    def test_invalid_rows_raise(self):
        for name, data in (
            ("outside window", [funding_row(11 * HOUR)]),
            ("missing time", [{"symbol": "BTC-USDT"}]),
            ("not an object", ["x"]),
            ("float time", [funding_row(1.5)]),
        ):
            with self.subTest(name):
                fake = FakeHttp(lambda p, i, data=data: ok(data))
                with self.assertRaises(ExchangeError):
                    fetch_funding_times(fake, PROD_HOST, "BTC-USDT", 0, 10 * HOUR, sleep=noop)

    def test_more_than_900_days_is_refused(self):
        fake = FakeHttp(lambda p, i: ok([]))
        with self.assertRaises(ValueError):
            fetch_funding_times(fake, PROD_HOST, "BTC-USDT", 0, 901 * DAY, sleep=noop)
        self.assertEqual(fake.calls, [])

    def test_the_number_of_split_requests_is_capped(self):
        def dense(params, index):  # a funding time every millisecond: every window is full
            start, end, limit = (int(params[k]) for k in ("startTime", "endTime", "limit"))
            return ok([funding_row(t) for t in range(start, min(end, start + limit - 1) + 1)])

        fake = FakeHttp(dense)
        with self.assertRaisesRegex(ExchangeError, str(MAX_FUNDING_REQUESTS)):
            fetch_funding_times(fake, PROD_HOST, "BTC-USDT", 0, 2_000_000, sleep=noop)
        self.assertEqual(len(fake.calls), MAX_FUNDING_REQUESTS)

    def test_bad_arguments_raise(self):
        fake = FakeHttp(lambda p, i: ok([]))
        with self.assertRaises(ValueError):
            fetch_funding_times(fake, PROD_HOST, "BTC-USDT", 10, 5, sleep=noop)
        with self.assertRaises(ValueError):
            fetch_funding_times(fake, "https://example.com", "BTC-USDT", 0, 5, sleep=noop)
        self.assertEqual(fake.calls, [])


class CollectTest(unittest.TestCase):
    """The daily collector backfills the last seven WIB days per job (B1)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.today = date(2026, 10, 14)
        self.days = [date(2026, 10, 7) + timedelta(days=k) for k in range(7)]

    def test_backfills_the_last_seven_days_for_each_job(self):
        fake = FakeHttp(full_klines)
        sleeps = Sleeps()
        lines = collect(fake, self.data_dir, self.today, sleep=sleeps)
        self.assertEqual(lines, [
            "vst/ETH-USDT/1m 2026-10-07..2026-10-13: fetched 7, skipped 0, failed 0",
            "prod/BTC-USDT/1m 2026-10-07..2026-10-13: fetched 7, skipped 0, failed 0",
            "prod/ETH-USDT/1m 2026-10-07..2026-10-13: fetched 7, skipped 0, failed 0",
        ])
        hosts = [(c[0], c[1]["symbol"]) for c in fake.calls]
        self.assertEqual(hosts, [(VST_HOST + KLINES_PATH, "ETH-USDT")] * 7
                         + [(PROD_HOST + KLINES_PATH, "BTC-USDT")] * 7
                         + [(PROD_HOST + KLINES_PATH, "ETH-USDT")] * 7)
        starts = [int(c[1]["startTime"]) for c in fake.calls[:7]]
        self.assertEqual(starts, [wib_day_start_ms(d) for d in self.days])
        for _, params, headers in fake.calls:
            self.assertEqual(headers, HEADERS)
        for source, symbol in (("vst", "ETH-USDT"), ("prod", "BTC-USDT"), ("prod", "ETH-USDT")):
            for d in self.days:
                self.assertEqual(len(read_day(day_path(self.data_dir, source, symbol, "1m", d))),
                                 1440)
        self.assertEqual(len(sleeps.calls), len(fake.calls) - 1)
        self.assertTrue(all(s >= 1.0 for s in sleeps.calls))

    def test_second_run_skips_without_requests(self):
        collect(FakeHttp(full_klines), self.data_dir, self.today, sleep=noop)
        fake = FakeHttp(full_klines)
        lines = collect(fake, self.data_dir, self.today, sleep=noop)
        self.assertEqual(fake.calls, [])
        self.assertTrue(all(line.endswith(": fetched 0, skipped 7, failed 0") for line in lines))

    def test_a_missed_day_is_backfilled_on_the_next_run(self):
        def respond(params, index):
            day_start = wib_day_start_ms(self.days[3])
            drop = 1 if int(params["startTime"]) == day_start else 0
            return ok(klines_for(params, drop=drop))

        lines = collect(FakeHttp(respond), self.data_dir, self.today, sleep=noop)
        self.assertTrue(all("failed: 2026-10-10" in line for line in lines), lines)
        fake = FakeHttp(full_klines)
        lines = collect(fake, self.data_dir, self.today, sleep=noop)
        self.assertTrue(all(line.endswith(": fetched 1, skipped 6, failed 0") for line in lines))
        self.assertEqual(len(fake.calls), 3)

    def test_one_failing_job_does_not_stop_the_others(self):
        def respond(params, index):
            if params["symbol"] == "BTC-USDT":
                return ok(klines_for(params, drop=3))
            return ok(klines_for(params))

        lines = collect(FakeHttp(respond), self.data_dir, self.today, sleep=noop)
        self.assertTrue(lines[0].endswith(": fetched 7, skipped 0, failed 0"))
        self.assertIn("prod/BTC-USDT/1m 2026-10-07..2026-10-13: fetched 0, skipped 0, failed 3; "
                      "failed: 2026-10-07, 2026-10-08, 2026-10-09; not attempted: 2026-10-10",
                      lines[1])
        self.assertTrue(lines[2].endswith(": fetched 7, skipped 0, failed 0"))

    def test_job_errors_become_one_failed_line_each(self):
        errors = {"vst": httpx.DecodingError("bad gzip"), "BTC-USDT": OSError("disk full")}

        def respond(params, index):
            raise httpx.DecodingError("bad gzip")

        with mock.patch.object(fetch, "sync_days", side_effect=[
                errors["vst"], errors["BTC-USDT"], ValueError("odd")]):
            lines = collect(FakeHttp(respond), self.data_dir, self.today, sleep=noop)
        self.assertEqual(len(lines), 3)
        self.assertIn(": failed: DecodingError: bad gzip", lines[0])
        self.assertIn(": failed: OSError: disk full", lines[1])
        self.assertIn(": failed: ValueError: odd", lines[2])

    def test_a_rate_limit_stops_the_remaining_jobs(self):
        def respond(params, index):
            raise RateLimitedError("HTTP 429 from open-api-vst.bingx.com")

        fake = FakeHttp(respond)
        lines = collect(fake, self.data_dir, self.today, sleep=noop)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn(": failed: RateLimitedError: HTTP 429", lines[0])
        self.assertTrue(lines[1].endswith(": failed: not attempted (rate limited)"))
        self.assertTrue(lines[2].endswith(": failed: not attempted (rate limited)"))

    def test_failure_reason_is_a_single_bounded_line(self):
        def respond(params, index):
            raise httpx.ConnectError("line one\nline two " + "x" * 500)

        lines = collect(FakeHttp(respond), self.data_dir, self.today, sleep=noop)
        for line in lines:
            self.assertIn("failed: 2026-10-07, 2026-10-08, 2026-10-09", line)
            self.assertNotIn("\n", line)
            self.assertLess(len(line), 300)


if __name__ == "__main__":
    unittest.main()
