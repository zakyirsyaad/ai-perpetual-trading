from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_STORE_PATH = Path("bot_data/tradingview_signals.json")


class TradingViewRelayHandler(BaseHTTPRequestHandler):
    server_version = "TradingViewRelay/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/tradingview/latest":
            self._send_json(404, {"error": "not_found"})
            return
        if not self._authorized(parsed):
            self._send_json(401, {"error": "unauthorized"})
            return
        store_path: Path = self.server.store_path  # type: ignore[attr-defined]
        if not store_path.exists():
            self._send_json(200, {"signals": [], "status": "empty"})
            return
        self._send_json(200, json.loads(store_path.read_text(encoding="utf-8")))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/tradingview/webhook":
            self._send_json(404, {"error": "not_found"})
            return
        if not self._authorized(parsed):
            self._send_json(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"message": raw}

        latest = self._normalize_payload(payload)
        store_path: Path = self.server.store_path  # type: ignore[attr-defined]
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(json.dumps(latest, ensure_ascii=True, indent=2), encoding="utf-8")
        self._send_json(200, {"ok": True, "stored_at": latest["received_at"]})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.log_date_time_string()} | {format % args}")

    def _authorized(self, parsed) -> bool:
        expected_token: str = self.server.token  # type: ignore[attr-defined]
        if not expected_token:
            return True

        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {expected_token}":
            return True

        query_token = parse_qs(parsed.query).get("token", [""])[0]
        if query_token == expected_token:
            return True

        return False

    @staticmethod
    def _normalize_payload(payload: Any) -> dict[str, Any]:
        received_at = datetime.now(timezone.utc).isoformat()
        if isinstance(payload, dict):
            signal = {
                "symbol": payload.get("symbol") or payload.get("ticker") or payload.get("syminfo.ticker"),
                "action": payload.get("action") or payload.get("side") or payload.get("signal"),
                "timeframe": payload.get("timeframe") or payload.get("interval"),
                "indicator": payload.get("indicator") or payload.get("strategy") or "tradingview_alert",
                "confidence": payload.get("confidence"),
                "price": payload.get("price") or payload.get("close"),
                "timestamp": payload.get("timestamp") or received_at,
                "raw": payload,
            }
            return {
                "signals": [signal],
                "market_regime": payload.get("market_regime") or "unknown",
                "received_at": received_at,
            }
        return {
            "signals": [{"indicator": "tradingview_alert", "timestamp": received_at, "raw": payload}],
            "market_regime": "unknown",
            "received_at": received_at,
        }

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local TradingView alert relay for the BingX VST AI bot.")
    parser.add_argument("--host", default=os.getenv("TRADINGVIEW_RELAY_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("TRADINGVIEW_RELAY_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--token", default=os.getenv("TRADINGVIEW_SIGNAL_TOKEN", ""))
    parser.add_argument("--store", default=os.getenv("TRADINGVIEW_RELAY_STORE", str(DEFAULT_STORE_PATH)))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    server = ThreadingHTTPServer((args.host, args.port), TradingViewRelayHandler)
    server.token = args.token
    server.store_path = Path(args.store)
    print(f"TradingView relay listening on http://{args.host}:{args.port}")
    print(f"Webhook path: /tradingview/webhook?token=<TRADINGVIEW_SIGNAL_TOKEN>")
    print(f"Latest path:  /tradingview/latest")
    server.serve_forever()


if __name__ == "__main__":
    main()
