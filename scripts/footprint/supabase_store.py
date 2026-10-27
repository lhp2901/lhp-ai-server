"""
Footprint Supabase storage adapter and SQLite trade backfill.

Install: pip install supabase

Required environment:
  SUPABASE_URL=https://<project-ref>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY=<server-only-secret>
  SUPABASE_SYNC_ENABLED=true
  SUPABASE_SOURCE=DNSE

Put this file at scripts/footprint/supabase_store.py.
Never expose the service-role key to browser/Next.js code.
The Supabase tables must already exist (run your footprint schema SQL first).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

try:
    from supabase import create_client
except ImportError:
    create_client = None

logger = logging.getLogger(__name__)

TRADES = "footprint_trades"
CANDLES = "footprint_candles"
EVENTS = "footprint_stream_events"
SYNC_RUNS = "footprint_sync_runs"
SYNC_STATE = "footprint_sync_state"
DEFAULT_SOURCE = "DNSE"
DEFAULT_SYNC_NAME = "sqlite_to_supabase"


class SupabaseStoreError(RuntimeError):
    """Base storage exception."""


class SupabaseConfigurationError(SupabaseStoreError):
    """Missing dependency or configuration."""


class SupabaseOperationError(SupabaseStoreError):
    """Supabase operation failed after retries."""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _first(row: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return default if value is None or value == "" else int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _decimal(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
        return format(number, "f") if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _ms(value: Any) -> Optional[int]:
    """Normalize epoch seconds, epoch milliseconds, datetime, or ISO timestamp."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    try:
        number = float(value)
        if number <= 0:
            return None
        return int(number * 1000) if number < 100_000_000_000 else int(number)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except (json.JSONDecodeError, TypeError):
            return {"raw_text": value}
    return {}


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _side(value: Any) -> str:
    text = str(value or "UNKNOWN").strip().upper()
    text = {
        "B": "BUY", "BID": "BUY", "BUYER": "BUY", "UP": "BUY",
        "S": "SELL", "ASK": "SELL", "SELLER": "SELL", "DOWN": "SELL",
        "": "UNKNOWN", "NA": "UNKNOWN", "N/A": "UNKNOWN", "NONE": "UNKNOWN", "NULL": "UNKNOWN",
    }.get(text, text)
    return text if text in {"BUY", "SELL", "UNKNOWN"} else "UNKNOWN"


def _data(response: Any) -> Any:
    return getattr(response, "data", response)


def _jwt_role_claim(token: str) -> Optional[str]:
    """Read the JWT role claim for diagnostics only; this does NOT verify the token."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        obj = json.loads(decoded.decode("utf-8"))
        role = obj.get("role") if isinstance(obj, dict) else None
        return str(role) if role is not None else None
    except Exception:
        return None


class SupabaseStore:
    """Supabase adapter matching the Footprint schema."""

    def __init__(
        self, url: Optional[str] = None, key: Optional[str] = None,
        source: Optional[str] = None, batch_size: Optional[int] = None,
        retries: Optional[int] = None, retry_delay: Optional[float] = None,
        client: Any = None, enabled: Optional[bool] = None,
    ) -> None:
        self.url = (url or os.getenv("SUPABASE_URL") or "").strip()
        self.key = (
            key
            or os.getenv("SUPABASE_SECRET_KEY")
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_KEY")
            or ""            
        ).strip()
        self.source = (source or os.getenv("SUPABASE_SOURCE") or DEFAULT_SOURCE).strip().upper()
        self.batch_size = max(1, int(batch_size or os.getenv("SUPABASE_BATCH_SIZE", "300")))
        self.retries = max(0, int(retries if retries is not None else os.getenv("SUPABASE_RETRIES", "3")))
        self.retry_delay = max(0.0, float(retry_delay if retry_delay is not None else os.getenv("SUPABASE_RETRY_DELAY", "0.5")))
        self.enabled = _env_bool("SUPABASE_SYNC_ENABLED", False) if enabled is None else bool(enabled)
        self._client = client
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return self._client is not None or bool(self.url and self.key and create_client is not None)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if create_client is None:
            raise SupabaseConfigurationError("Chưa cài supabase-py. Chạy: pip install supabase")
        if not self.url:
            raise SupabaseConfigurationError("Thiếu SUPABASE_URL.")
        if not self.key:
            raise SupabaseConfigurationError("Thiếu SUPABASE_SERVICE_ROLE_KEY.")
        self._client = create_client(self.url, self.key)
        return self._client

    def _execute(self, name: str, callback: Any) -> Any:
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                with self._lock:
                    return callback(self._get_client())
            except SupabaseConfigurationError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(self.retry_delay * (2 ** attempt) + random.uniform(0, 0.15))
                logger.warning("Supabase %s retry %s/%s: %s", name, attempt + 1, self.retries + 1, exc)
        raise SupabaseOperationError(f"Supabase '{name}' failed: {last_error}") from last_error


    def health_check(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "ok": False,
                "enabled": False,
                "message": "SUPABASE_SYNC_ENABLED chưa bật.",
            }

        role_claim = _jwt_role_claim(self.key)
        is_new_secret_key = self.key.startswith("sb_secret_")

        if role_claim and role_claim != "service_role":
            return {
                "ok": False,
                "enabled": True,
                "message": (
                    f"JWT có role={role_claim!r}, không phải 'service_role'. "
                    "Hãy kiểm tra SUPABASE_SERVICE_ROLE_KEY."
                ),
                "jwt_role_claim": role_claim,
            }

        if self.key.startswith("sb_publishable_"):
            return {
                "ok": False,
                "enabled": True,
                "message": (
                    "Đang dùng Publishable key. "
                    "Hãy cấu hình khóa backend phù hợp."
                ),
                "jwt_role_claim": "publishable",
            }

        try:
            self._execute(
                "health_check",
                lambda c: (
                    c.table(SYNC_STATE)
                    .select("sync_name")
                    .limit(1)
                    .execute()
                ),
            )

            return {
                "ok": True,
                "enabled": True,
                "message": "Kết nối Supabase thành công.",
                "jwt_role_claim": (
                    "secret_key"
                    if is_new_secret_key
                    else role_claim or "không xác định/không phải JWT"
                ),
            }

        except Exception as exc:
            return {
                "ok": False,
                "enabled": True,
                "message": str(exc),
                "jwt_role_claim": (
                    "secret_key"
                    if is_new_secret_key
                    else role_claim or "không xác định/không phải JWT"
                ),
            }


    @staticmethod
    def normalize_trade(trade: Mapping[str, Any], source: str = DEFAULT_SOURCE) -> dict[str, Any]:
        symbol = _symbol(_first(trade, "symbol", "ticker", "stock_code", "code"))
        price = _decimal(_first(trade, "price", "match_price", "matched_price", "last_price"))
        volume = _decimal(_first(trade, "volume", "quantity", "qty", "match_volume", "matched_volume", "vol", "kl"))
        timestamp = _ms(_first(trade, "timestamp_ms", "timestamp", "ts_ms", "time", "trade_time", "match_time", "datetime"))
        received = _ms(_first(trade, "received_at_ms", "received_at", "ingested_at"))
        local_id = _int(_first(trade, "source_local_id", "local_id", "sqlite_id", "row_id", "id"))
        provider_id = _first(trade, "trade_id", "provider_trade_id", "match_id", "transaction_id")
        trade_id = str(provider_id).strip() if provider_id is not None and str(provider_id).strip() else None
        side = _side(_first(trade, "side", "aggressor_side", "match_side", "buy_sell", "direction"))
        if not symbol:
            raise ValueError("Trade thiếu symbol.")
        if price is None or Decimal(price) <= 0:
            raise ValueError(f"Trade {symbol} có price không hợp lệ.")
        if volume is None or Decimal(volume) < 0:
            raise ValueError(f"Trade {symbol} có volume không hợp lệ.")
        if timestamp is None:
            raise ValueError(f"Trade {symbol} thiếu timestamp hợp lệ.")
        raw = _json_object(_first(trade, "raw", "raw_json", "payload", default={}))
        volume_number = Decimal(volume)
        # The provided Supabase schema declares volume as BIGINT. Reject fractional
        # values instead of silently truncating data or causing a database error.
        if volume_number != volume_number.to_integral_value():
            raise ValueError(
                f"Trade {symbol} có volume lẻ ({volume}); cột Supabase volume là BIGINT. "
                "Nếu nguồn thực sự có khối lượng lẻ, đổi cột Supabase sang NUMERIC trước."
            )
        return {
            "source_local_id": local_id, "source": (source or DEFAULT_SOURCE).strip().upper(),
            "symbol": symbol, "trade_id": trade_id, "price": price,
            "volume": int(volume_number), "side": side, "timestamp_ms": timestamp,
            "received_at_ms": received, "raw": _json_safe(raw),
        }

    def save_trade(self, trade: Mapping[str, Any], *, source: Optional[str] = None) -> dict[str, Any]:
        row = self.normalize_trade(trade, source or self.source)

        def op(client: Any) -> dict[str, Any]:
            if row["source_local_id"] is not None:
                # Requires a UNIQUE index on (source, source_local_id).
                response = client.table(TRADES).upsert(row, on_conflict="source,source_local_id").execute()
                data = _data(response) or []
                first = data[0] if isinstance(data, list) and data else {}
                return {"ok": True, "inserted": True, "upsert": True, "id": first.get("id")}
            if row["trade_id"] is not None:
                found = (
                    client.table(TRADES).select("id").eq("source", row["source"])
                    .eq("symbol", row["symbol"]).eq("trade_id", row["trade_id"]).limit(1).execute()
                )
                rows = _data(found) or []
                if rows:
                    return {"ok": True, "inserted": False, "duplicate": True, "id": rows[0].get("id")}
            response = client.table(TRADES).insert(row).execute()
            data = _data(response) or []
            first = data[0] if isinstance(data, list) and data else {}
            return {"ok": True, "inserted": True, "duplicate": False, "id": first.get("id")}
        return self._execute("save_trade", op)

    def save_trades(self, trades: Iterable[Mapping[str, Any]], *, source: Optional[str] = None, strict: bool = False) -> dict[str, Any]:
        result = {"ok": True, "received": 0, "inserted": 0, "duplicates": 0, "failed": 0, "errors": []}
        for trade in trades:
            result["received"] += 1
            try:
                saved = self.save_trade(trade, source=source)
                if saved.get("duplicate"):
                    result["duplicates"] += 1
                elif saved.get("inserted"):
                    result["inserted"] += 1
            except Exception as exc:
                result["ok"] = False
                result["failed"] += 1
                if len(result["errors"]) < 20:
                    result["errors"].append({"error": str(exc), "source_local_id": trade.get("id")})
                logger.exception("Không thể lưu trade lên Supabase")
                if strict:
                    raise
        return result

    @staticmethod
    def normalize_candle(candle: Mapping[str, Any], source: str = DEFAULT_SOURCE) -> dict[str, Any]:
        symbol = _symbol(_first(candle, "symbol", "ticker", "code"))
        timeframe = str(_first(candle, "timeframe", "interval", "resolution", default="1m")).strip().lower()
        timestamp = _ms(_first(candle, "timestamp_ms", "timestamp", "ts_ms", "time", "datetime"))
        prices = {key: _decimal(_first(candle, *aliases)) for key, aliases in {
            "open": ("open", "o"), "high": ("high", "h"), "low": ("low", "l"), "close": ("close", "c")
        }.items()}
        volume = _decimal(_first(candle, "volume", "vol", "v", default=0))
        if not symbol or not timeframe or timestamp is None or any(v is None for v in prices.values()):
            raise ValueError("Candle thiếu symbol/timeframe/timestamp/OHLC.")
        o, h, l, c = (Decimal(prices[k]) for k in ("open", "high", "low", "close"))  # type: ignore[arg-type]
        if min(o, h, l, c) <= 0 or h < max(o, l, c) or l > min(o, h, c):
            raise ValueError(f"Candle {symbol} có OHLC không hợp lệ.")
        if volume is None or Decimal(volume) < 0:
            raise ValueError(f"Candle {symbol} có volume không hợp lệ.")
        return {
            "source": (source or DEFAULT_SOURCE).strip().upper(), "symbol": symbol,
            "timeframe": timeframe, "timestamp_ms": timestamp, **prices, "volume": volume,
            "raw": _json_safe(_json_object(_first(candle, "raw", "raw_json", "payload", default={}))),
            "updated_at": _utc_iso(),
        }

    def save_candle(self, candle: Mapping[str, Any], *, source: Optional[str] = None) -> dict[str, Any]:
        row = self.normalize_candle(candle, source or self.source)
        def op(client: Any) -> dict[str, Any]:
            response = client.table(CANDLES).upsert(row, on_conflict="source,symbol,timeframe,timestamp_ms").execute()
            data = _data(response) or []
            return {"ok": True, "data": data[0] if isinstance(data, list) and data else None}
        return self._execute("save_candle", op)

    def save_stream_event(
        self, event_type: str, *, status: str = "INFO", message: Optional[str] = None,
        symbol: Optional[str] = None, timestamp_ms: Optional[int] = None,
        payload: Optional[Mapping[str, Any]] = None, source: Optional[str] = None,
    ) -> dict[str, Any]:
        row = {
            "source": (source or self.source).strip().upper(),
            "event_type": str(event_type or "UNKNOWN").strip().upper(),
            "status": str(status or "INFO").strip().upper(),
            "message": str(message)[:4000] if message is not None else None,
            "symbol": _symbol(symbol) or None,
            "timestamp_ms": _ms(timestamp_ms) or int(time.time() * 1000),
            "payload": _json_safe(dict(payload or {})),
        }
        def op(client: Any) -> dict[str, Any]:
            response = client.table(EVENTS).insert(row).execute()
            data = _data(response) or []
            return {"ok": True, "data": data[0] if isinstance(data, list) and data else None}
        return self._execute("save_stream_event", op)

    def list_sync_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return a bounded sample of sync history without assuming table columns."""
        safe_limit = max(1, min(int(limit), 200))

        def op(client: Any) -> list[dict[str, Any]]:
            response = client.table(SYNC_RUNS).select("*").limit(safe_limit).execute()
            rows = _data(response) or []
            return [dict(row) for row in rows if isinstance(row, Mapping)]

        return self._execute("list_sync_runs", op)

    def get_sync_state(self, sync_name: str = DEFAULT_SYNC_NAME) -> Optional[dict[str, Any]]:
        def op(client: Any) -> Optional[dict[str, Any]]:
            response = client.table(SYNC_STATE).select("*").eq("sync_name", sync_name).limit(1).execute()
            rows = _data(response) or []
            return rows[0] if rows else None
        return self._execute("get_sync_state", op)

    def update_sync_state(self, sync_name: str = DEFAULT_SYNC_NAME, **fields: Any) -> dict[str, Any]:
        allowed = {"last_local_trade_id", "last_local_candle_id", "last_success_at", "last_attempt_at", "status", "error_message"}
        row = {k: v for k, v in fields.items() if k in allowed}
        row.update({"sync_name": sync_name, "updated_at": _utc_iso()})
        def op(client: Any) -> dict[str, Any]:
            response = client.table(SYNC_STATE).upsert(row, on_conflict="sync_name").execute()
            rows = _data(response) or []
            return {"ok": True, "data": rows[0] if rows else row}
        return self._execute("update_sync_state", op)

    def sync_sqlite_trades(
        self, sqlite_path: str | os.PathLike[str] = "data/footprint.db", *,
        start_id: Optional[int] = None, max_batches: Optional[int] = None,
        dry_run: bool = False, sync_name: str = DEFAULT_SYNC_NAME,
    ) -> dict[str, Any]:
        """Backfill and incrementally sync trades in ascending SQLite ID order."""
        path = Path(sqlite_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Không tìm thấy SQLite database: {path}")
        if not dry_run and not self.enabled:
            raise SupabaseConfigurationError("Đồng bộ đang tắt; đặt SUPABASE_SYNC_ENABLED=true.")
        db = sqlite3.connect(str(path), timeout=30)
        db.row_factory = sqlite3.Row
        result = {
            "ok": True, "dry_run": dry_run, "sqlite_path": str(path), "read": 0,
            "upserted": 0, "failed": 0, "last_local_id": None, "errors": [],
        }
        try:
            cols = {r["name"] for r in db.execute("PRAGMA table_info(footprint_trades)")}
            required = {"id", "symbol", "trade_id", "timestamp", "price", "volume", "side", "received_at", "raw_json"}
            missing = required - cols
            if missing:
                raise SupabaseStoreError(f"footprint_trades thiếu cột: {sorted(missing)}")
            if start_id is None:
                state = None if dry_run else self.get_sync_state(sync_name)
                checkpoint = _int((state or {}).get("last_local_trade_id"), 0) or 0
            else:
                checkpoint = max(0, int(start_id))
            batches = 0
            while True:
                if max_batches is not None and batches >= max_batches:
                    break
                rows = db.execute(
                    """SELECT id, symbol, trade_id, timestamp, price, volume, side, received_at, raw_json
                       FROM footprint_trades WHERE id > ? ORDER BY id ASC LIMIT ?""",
                    (checkpoint, self.batch_size),
                ).fetchall()
                if not rows:
                    break
                batches += 1
                for item in rows:
                    r = dict(item)
                    local_id = int(r["id"])
                    result["read"] += 1
                    try:
                        trade = {
                            "id": local_id, "source_local_id": local_id, "symbol": r["symbol"],
                            "trade_id": r["trade_id"], "timestamp_ms": r["timestamp"],
                            "price": r["price"], "volume": r["volume"], "side": r["side"],
                            "received_at_ms": r["received_at"], "raw_json": r["raw_json"],
                        }
                        normalized = self.normalize_trade(trade, self.source)
                        if not dry_run:
                            self.save_trade(normalized, source=self.source)
                            result["upserted"] += 1
                        else:
                            result["upserted"] += 1
                        checkpoint = local_id
                        result["last_local_id"] = local_id
                    except Exception as exc:
                        result["ok"] = False
                        result["failed"] += 1
                        if len(result["errors"]) < 20:
                            result["errors"].append({"local_id": local_id, "error": str(exc)})
                        logger.exception("SQLite -> Supabase failed at local id %s", local_id)
                        if not dry_run:
                            try:
                                self.update_sync_state(
                                    sync_name, last_local_trade_id=checkpoint,
                                    last_attempt_at=_utc_iso(), status="RETRYING",
                                    error_message=str(exc)[:4000],
                                )
                            except Exception as state_exc:
                                # Preserve the original trade error instead of masking it
                                # with a secondary sync-state/RLS error.
                                if len(result["errors"]) < 20:
                                    result["errors"].append({
                                        "local_id": local_id,
                                        "sync_state_error": str(state_exc),
                                    })
                                logger.error("Không cập nhật được sync state sau lỗi trade: %s", state_exc)
                        return result
                if not dry_run:
                    self.update_sync_state(
                        sync_name, last_local_trade_id=checkpoint,
                        last_attempt_at=_utc_iso(), status="RUNNING", error_message=None,
                    )
            if not dry_run:
                self.update_sync_state(
                    sync_name, last_local_trade_id=checkpoint,
                    last_success_at=_utc_iso(), last_attempt_at=_utc_iso(),
                    status="SUCCESS", error_message=None,
                )
            result["last_local_id"] = checkpoint
            return result
        finally:
            db.close()

    def get_status(self) -> dict[str, Any]:
        role_claim = _jwt_role_claim(self.key)
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "source": self.source,
            "batch_size": self.batch_size,
            "retries": self.retries,
            "url_present": bool(self.url),
            "key_present": bool(self.key or self._client is not None),
            # Safe diagnostic only; never return or print the secret itself.
            "jwt_role_claim": role_claim or "không xác định/không phải JWT",
        }


_default_store: Optional[SupabaseStore] = None
_default_lock = threading.Lock()


def get_supabase_store(*, refresh: bool = False) -> SupabaseStore:
    global _default_store
    with _default_lock:
        if refresh or _default_store is None:
            _default_store = SupabaseStore()
        return _default_store


def close_supabase_store() -> None:
    global _default_store
    with _default_lock:
        _default_store = None


def sync_sqlite_trades(
    sqlite_path: str | os.PathLike[str] = "data/footprint.db", *,
    start_id: Optional[int] = None, max_batches: Optional[int] = None, dry_run: bool = False,
) -> dict[str, Any]:
    return get_supabase_store().sync_sqlite_trades(
        sqlite_path, start_id=start_id, max_batches=max_batches, dry_run=dry_run
    )
