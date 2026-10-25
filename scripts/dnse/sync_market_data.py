
from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import threading
import time

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from flask import Blueprint, jsonify, request
from supabase import Client, create_client


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env", override=False)

logger = logging.getLogger(__name__)

dnse_sync_bp = Blueprint(
    "dnse_sync",
    __name__,
    url_prefix="/api/dnse",
)

DNSE_BASE_URL = os.getenv(
    "DNSE_BASE_URL",
    "https://openapi.dnse.com.vn",
).rstrip("/")

DNSE_API_VERSION = os.getenv(
    "DNSE_API_VERSION",
    "2026-07-23",
)

DNSE_API_KEY = os.getenv("DNSE_API_KEY", "").strip()
DNSE_API_SECRET = os.getenv("DNSE_API_SECRET", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()

SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY",
    "",
).strip()

DEFAULT_LOOKBACK_DAYS = 365
MAX_LOOKBACK_DAYS = 2000
MAX_SYMBOLS_PER_REQUEST = 50
INSERT_BATCH_SIZE = 100
READ_PAGE_SIZE = 500

VIETNAM_TIMEZONE = timezone(timedelta(hours=7))

STOCK_TABLE = "stock_entries"

INDEX_TABLES = {
    "vnindex": {
        "table": "vnindex_data",
        "symbol": "VNINDEX",
        "bar_type": "INDEX",
    },
    "vn30": {
        "table": "vn30_data",
        "symbol": "VN30",
        "bar_type": "INDEX",
    },
}

DEFAULT_STOCK_SYMBOLS = ["FPT", "HPG", "SSI"]


# ============================================================
# SUPABASE CLIENT
# ============================================================

_supabase_client: Client | None = None
_supabase_lock = threading.Lock()


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")

    if len(parts) != 3:
        return None

    try:
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)

        decoded = base64.urlsafe_b64decode(payload)
        result = json.loads(decoded.decode("utf-8"))

        return result if isinstance(result, dict) else None

    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def get_supabase_client() -> Client:
    """
    Khởi tạo client backend.

    Chấp nhận:
    - Legacy JWT có role=service_role.
    - sb_secret_... nếu phiên bản supabase-py hỗ trợ.

    Không chấp nhận anon hoặc publishable key.
    """

    global _supabase_client

    if _supabase_client is not None:
        return _supabase_client

    with _supabase_lock:
        if _supabase_client is not None:
            return _supabase_client

        if not SUPABASE_URL:
            raise RuntimeError("Thiếu SUPABASE_URL.")

        if not SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError("Thiếu SUPABASE_SERVICE_ROLE_KEY.")

        key = SUPABASE_SERVICE_ROLE_KEY

        if key.startswith("sb_publishable_"):
            raise RuntimeError(
                "Đang dùng publishable key. "
                "Hãy cấu hình secret key dành cho backend."
            )

        if key.startswith("sb_secret_"):
            # Secret key mới không phải JWT.
            # Kiểm tra tính tương thích bằng request thực tế.
            backend_key = key

        else:
            payload = _decode_jwt_payload(key)

            if payload is None:
                raise RuntimeError(
                    "Key Supabase không đúng định dạng. "
                    "Kiểm tra lại key trong Supabase Dashboard."
                )

            role = payload.get("role")

            if role != "service_role":
                raise RuntimeError(
                    "SUPABASE_SERVICE_ROLE_KEY không có "
                    f"role=service_role; role hiện tại={role!r}. "
                    "Không thể dùng anon/authenticated key để bypass RLS."
                )

            backend_key = key

        try:
            _supabase_client = create_client(
                SUPABASE_URL,
                backend_key,
            )
        except Exception as exc:
            logger.exception("Khởi tạo Supabase client thất bại.")

            raise RuntimeError(
                "Không thể khởi tạo Supabase client. "
                "Kiểm tra URL, loại key và phiên bản supabase-py."
            ) from exc

        logger.info("Supabase backend client initialized.")

        return _supabase_client


# ============================================================
# USER AUTHENTICATION
# ============================================================

def authenticate_supabase_user() -> str:
    """
    Xác thực JWT với Supabase Auth.
    Không tin user_id do frontend tự gửi.
    """

    authorization = request.headers.get("Authorization", "")

    if not authorization.startswith("Bearer "):
        raise PermissionError(
            "Thiếu access token. Vui lòng đăng nhập lại."
        )

    token = authorization[7:].strip()

    if not token:
        raise PermissionError("Access token trống.")

    try:
        response = get_supabase_client().auth.get_user(token)

        user = getattr(response, "user", None)

        if not user or not getattr(user, "id", None):
            raise PermissionError(
                "Không xác thực được người dùng."
            )

        return str(UUID(str(user.id)))

    except PermissionError:
        raise

    except Exception as exc:
        logger.warning(
            "Supabase authentication failed: %s",
            type(exc).__name__,
        )

        raise PermissionError(
            "Access token không hợp lệ hoặc đã hết hạn."
        ) from exc


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def vietnam_today() -> date:
    return datetime.now(VIETNAM_TIMEZONE).date()


def safe_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, str):
        value = value.strip()

        if not value:
            return None

        value = value.replace(",", "")

    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None

    return result if math.isfinite(result) else None


def first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]

    return None


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()

    if not re.fullmatch(r"[A-Z0-9]{2,10}", symbol):
        raise ValueError(
            f"Mã chứng khoán không hợp lệ: {symbol!r}"
        )

    return symbol


def parse_trading_date(value: Any) -> date | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=VIETNAM_TIMEZONE)

        return value.astimezone(VIETNAM_TIMEZONE).date()

    if isinstance(value, date):
        return value

    if isinstance(value, (int, float)):
        timestamp = float(value)

        if abs(timestamp) > 100_000_000_000:
            timestamp /= 1000

        try:
            return datetime.fromtimestamp(
                timestamp,
                timezone.utc,
            ).astimezone(VIETNAM_TIMEZONE).date()

        except (ValueError, OSError, OverflowError):
            return None

    text = str(value).strip()

    if not text:
        return None

    if re.fullmatch(r"\d{10,13}", text):
        return parse_trading_date(int(text))

    try:
        dt = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=VIETNAM_TIMEZONE)

        return dt.astimezone(VIETNAM_TIMEZONE).date()

    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue

    return None


# ============================================================
# DNSE RESPONSE PARSER
# ============================================================

def unwrap_ohlc_rows(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        rows = body

    elif isinstance(body, dict):
        rows = None

        column_keys = ("t", "o", "h", "l", "c")

        if all(
            isinstance(body.get(key), (list, tuple))
            for key in column_keys
        ):
            columns = {
                key: body[key]
                for key in (*column_keys, "v")
                if isinstance(body.get(key), (list, tuple))
            }

            lengths = {len(values) for values in columns.values()}

            if len(lengths) != 1:
                raise ValueError(
                    "Các mảng OHLC từ DNSE có độ dài không đồng nhất."
                )

            rows = []

            for i in range(len(body["t"])):
                row = {
                    key: body[key][i]
                    for key in column_keys
                }

                if "v" in columns:
                    row["v"] = body["v"][i]

                rows.append(row)

        else:
            for key in (
                "data",
                "content",
                "items",
                "results",
                "bars",
                "ohlc",
            ):
                candidate = body.get(key)

                if isinstance(candidate, list):
                    rows = candidate
                    break

            if rows is None:
                aliases = ("open", "openPrice", "open_price", "o")

                if any(key in body for key in aliases):
                    rows = [body]
                else:
                    raise ValueError(
                        "Không nhận diện được cấu trúc OHLC từ DNSE. "
                        f"Keys: {list(body.keys())[:30]}"
                    )

    else:
        raise ValueError(
            "Phản hồi OHLC từ DNSE không phải list hoặc object."
        )

    normalized = []

    for item in rows:
        if isinstance(item, dict):
            normalized.append(item)

        elif hasattr(item, "model_dump"):
            normalized.append(item.model_dump())

        elif hasattr(item, "dict"):
            normalized.append(item.dict())

        else:
            raise ValueError("Bản ghi OHLC không đúng định dạng.")

    return normalized


# ============================================================
# FETCH DNSE OHLC
# ============================================================

_dnse_client: Any | None = None


def get_dnse_client() -> Any:
    global _dnse_client

    if _dnse_client is not None:
        return _dnse_client

    if not DNSE_API_KEY or not DNSE_API_SECRET:
        raise RuntimeError(
            "Thiếu DNSE_API_KEY hoặc DNSE_API_SECRET."
        )

    try:
        from dnse import DNSEClient
    except ImportError as exc:
        raise RuntimeError(
            "Không import được DNSEClient. "
            "Kiểm tra package dnse-sdk-openapi."
        ) from exc

    _dnse_client = DNSEClient(
        api_key=DNSE_API_KEY,
        api_secret=DNSE_API_SECRET,
        base_url=DNSE_BASE_URL,
        api_version=DNSE_API_VERSION,
    )

    return _dnse_client


def fetch_dnse_ohlc(
    symbol: str,
    bar_type: str,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    symbol = normalize_symbol(symbol)
    bar_type = str(bar_type).strip().upper()

    if bar_type not in {"INDEX", "STOCK"}:
        raise ValueError(f"bar_type không hợp lệ: {bar_type}")

    if start_date > end_date:
        raise ValueError("Ngày bắt đầu lớn hơn ngày kết thúc.")

    start_dt = datetime.combine(
        start_date,
        datetime.min.time(),
        tzinfo=VIETNAM_TIMEZONE,
    )

    end_dt = datetime.combine(
        end_date,
        datetime.max.time(),
        tzinfo=VIETNAM_TIMEZONE,
    )

    query = {
        "symbol": symbol,
        "resolution": (
            os.getenv("DNSE_OHLC_RESOLUTION", "1D").strip()
            or "1D"
        ),
        "from": int(start_dt.timestamp()),
        "to": int(end_dt.timestamp()),
    }

    try:
        response = get_dnse_client().get_ohlc(
            bar_type=bar_type,
            query=query,
            dry_run=False,
        )

    except Exception as exc:
        logger.exception("DNSE request failed | symbol=%s", symbol)

        raise RuntimeError(
            f"Không gọi được DNSE OHLC cho {symbol}: "
            f"{type(exc).__name__}"
        ) from exc

    if not isinstance(response, tuple) or len(response) != 2:
        raise RuntimeError(
            "DNSE SDK không trả về tuple (status, body)."
        )

    status, body = response

    if isinstance(status, bool):
        raise RuntimeError("HTTP status DNSE không hợp lệ.")

    try:
        http_status = int(status)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("Không đọc được HTTP status DNSE.") from exc

    if not 200 <= http_status < 300:
        preview = str(body)[:500]

        raise RuntimeError(
            f"DNSE HTTP {http_status} cho {symbol}: {preview}"
        )

    if body is None:
        raise RuntimeError(
            f"DNSE trả body rỗng cho {symbol}."
        )

    if isinstance(body, bytes):
        body = body.decode("utf-8")

    if isinstance(body, str):
        try:
            body = json.loads(body.strip())
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Response DNSE của {symbol} không phải JSON hợp lệ."
            ) from exc

    rows = unwrap_ohlc_rows(body)

    logger.info(
        "DNSE OHLC fetched | symbol=%s | rows=%s",
        symbol,
        len(rows),
    )

    return rows


# ============================================================
# OHLC NORMALIZATION
# ============================================================

def normalize_ohlc_row(
    raw: dict[str, Any],
    user_id: str,
    symbol: str | None = None,
) -> dict[str, Any] | None:

    trading_date = parse_trading_date(
        first_value(
            raw,
            "tradingDate",
            "trading_date",
            "tradingDay",
            "trading_day",
            "date",
            "timestamp",
            "time",
            "t",
        )
    )

    if trading_date is None:
        return None

    open_price = safe_number(
        first_value(raw, "open", "openPrice", "open_price", "o")
    )

    high_price = safe_number(
        first_value(raw, "high", "highPrice", "high_price", "h")
    )

    low_price = safe_number(
        first_value(raw, "low", "lowPrice", "low_price", "l")
    )

    close_price = safe_number(
        first_value(raw, "close", "closePrice", "close_price", "c")
    )

    volume = safe_number(
        first_value(raw, "volume", "totalVolume", "total_volume", "v")
    )

    prices = [open_price, high_price, low_price, close_price]

    if any(value is None for value in prices):
        return None

    if min(prices) < 0:
        return None

    if high_price < max(open_price, low_price, close_price):
        return None

    if low_price > min(open_price, high_price, close_price):
        return None

    if volume is not None and volume < 0:
        return None

    row: dict[str, Any] = {
        "user_id": user_id,
        "date": trading_date.isoformat(),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
    }

    if volume is not None:
        row["volume"] = volume

    value = safe_number(
        first_value(
            raw,
            "value",
            "assetValue",
            "asset_value",
            "totalValue",
            "total_value",
            "turnover",
        )
    )

    if value is not None:
        row["asset_value" if symbol else "value"] = value

    if symbol is not None:
        row["symbol"] = symbol

    return row


# ============================================================
# DATABASE ERROR HANDLING
# ============================================================

def extract_db_error(exc: Exception) -> dict[str, str]:
    code = getattr(exc, "code", None)
    message = str(exc)

    if not code:
        match = re.search(
            r"\b(42501|23505|23503|23502|42P01|42703)\b",
            message,
        )

        if match:
            code = match.group(1)

    return {
        "code": str(code or "UNKNOWN"),
        "message": message[:1000],
    }


# ============================================================
# READ EXISTING RECORDS
# ============================================================

def get_existing_dates(
    supabase: Client,
    table: str,
    user_id: str,
    start_date: date,
    end_date: date,
    symbol: str | None = None,
) -> set[str]:

    existing: set[str] = set()
    offset = 0

    while True:
        query = (
            supabase.table(table)
            .select("date")
            .eq("user_id", user_id)
            .gte("date", start_date.isoformat())
            .lte("date", end_date.isoformat())
            .order("date")
            .range(offset, offset + READ_PAGE_SIZE - 1)
        )

        if symbol is not None:
            query = query.eq("symbol", symbol)

        response = query.execute()
        data = response.data or []

        for item in data:
            if not isinstance(item, dict):
                continue

            parsed = parse_trading_date(item.get("date"))

            if parsed is not None:
                existing.add(parsed.isoformat())

        if len(data) < READ_PAGE_SIZE:
            break

        offset += READ_PAGE_SIZE

    return existing


# ============================================================
# INSERT MISSING ROWS
# ============================================================

def insert_missing_rows(
    supabase: Client,
    table: str,
    rows: list[dict[str, Any]],
) -> dict[str, int]:

    result = {
        "inserted": 0,
        "skipped": 0,
        "failed": 0,
    }

    for offset in range(0, len(rows), INSERT_BATCH_SIZE):
        batch = rows[offset:offset + INSERT_BATCH_SIZE]

        try:
            response = (
                supabase.table(table)
                .insert(batch)
                .execute()
            )

            returned = response.data or []

            if returned:
                result["inserted"] += len(returned)
            else:
                # Không giả định INSERT thành công nếu không có xác nhận.
                logger.warning(
                    "Insert response không có data | table=%s",
                    table,
                )
                result["failed"] += len(batch)

        except Exception:
            logger.exception(
                "Batch insert failed | table=%s | size=%d",
                table,
                len(batch),
            )

            # Cô lập lỗi theo từng bản ghi.
            for row in batch:
                try:
                    response = (
                        supabase.table(table)
                        .insert(row)
                        .execute()
                    )

                    returned = response.data or []

                    if returned:
                        result["inserted"] += len(returned)
                    else:
                        result["failed"] += 1

                except Exception as row_exc:
                    error = extract_db_error(row_exc)

                    if error["code"] == "23505":
                        result["skipped"] += 1
                        continue

                    logger.error(
                        "Insert failed | table=%s | date=%s | "
                        "code=%s | error=%s",
                        table,
                        row.get("date"),
                        error["code"],
                        error["message"],
                    )

                    result["failed"] += 1

    return result


# ============================================================
# SYNC ONE TABLE
# ============================================================

def sync_ohlc_to_table(
    *,
    supabase: Client,
    user_id: str,
    symbol: str,
    bar_type: str,
    table: str,
    start_date: date,
    end_date: date,
    is_stock: bool,
) -> dict[str, Any]:

    started = time.monotonic()

    result: dict[str, Any] = {
        "target": "stocks" if is_stock else symbol.lower(),
        "symbol": symbol,
        "table": table,
        "success": False,
        "fetched": 0,
        "valid": 0,
        "inserted": 0,
        "skipped": 0,
        "failed": 0,
        "message": "",
    }

    try:
        raw_rows = fetch_dnse_ohlc(
            symbol=symbol,
            bar_type=bar_type,
            start_date=start_date,
            end_date=end_date,
        )

        result["fetched"] = len(raw_rows)

        unique_rows: dict[str, dict[str, Any]] = {}

        for raw in raw_rows:
            row = normalize_ohlc_row(
                raw=raw,
                user_id=user_id,
                symbol=symbol if is_stock else None,
            )

            if row is None:
                continue

            row_date = parse_trading_date(row["date"])

            if row_date is None:
                continue

            if not start_date <= row_date <= end_date:
                continue

            # Một ngày chỉ giữ một bản ghi cho mỗi symbol.
            unique_rows.setdefault(str(row["date"]), row)

        normalized_rows = list(unique_rows.values())

        result["valid"] = len(normalized_rows)

        existing_dates = get_existing_dates(
            supabase=supabase,
            table=table,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
            symbol=symbol if is_stock else None,
        )

        missing_rows = [
            row
            for row in normalized_rows
            if row["date"] not in existing_dates
        ]

        result["skipped"] = (
            len(normalized_rows) - len(missing_rows)
        )

        inserted_result = insert_missing_rows(
            supabase=supabase,
            table=table,
            rows=missing_rows,
        )

        result["inserted"] = inserted_result["inserted"]

        result["skipped"] += inserted_result["skipped"]

        result["failed"] = inserted_result["failed"]

        result["success"] = result["failed"] == 0

        if result["success"]:
            result["message"] = (
                f"{symbol}: lấy {result['fetched']} bản ghi, "
                f"hợp lệ {result['valid']}, "
                f"thêm {result['inserted']}, "
                f"bỏ qua {result['skipped']}."
            )
        else:
            result["message"] = (
                f"{symbol}: ghi dữ liệu chưa hoàn tất; "
                f"thêm {result['inserted']}, "
                f"lỗi {result['failed']}."
            )

    except Exception as exc:
        logger.exception(
            "Sync failed | symbol=%s | table=%s",
            symbol,
            table,
        )

        result["success"] = False
        result["error"] = str(exc)
        result["message"] = (
            f"Không thể đồng bộ {symbol}: {exc}"
        )

    result["duration_seconds"] = round(
        time.monotonic() - started,
        3,
    )

    return result


# ============================================================
# REQUEST VALIDATION
# ============================================================

def parse_targets(payload: dict[str, Any]) -> list[str]:
    raw_targets = payload.get("targets")

    if not isinstance(raw_targets, list):
        raise ValueError("targets phải là một mảng.")

    allowed = {"vnindex", "vn30", "stocks"}
    targets: list[str] = []

    for raw in raw_targets:
        if not isinstance(raw, str):
            raise ValueError(
                "Mỗi phần tử targets phải là chuỗi."
            )

        target = raw.strip().lower()

        if target not in allowed:
            raise ValueError(
                f"Nhóm dữ liệu không hợp lệ: {target}"
            )

        if target not in targets:
            targets.append(target)

    if not targets:
        raise ValueError(
            "Vui lòng chọn ít nhất một nhóm dữ liệu."
        )

    return targets


def parse_symbols(payload: dict[str, Any]) -> list[str]:
    raw_symbols = payload.get(
        "symbols",
        DEFAULT_STOCK_SYMBOLS,
    )

    if not isinstance(raw_symbols, list):
        raise ValueError("symbols phải là một mảng.")

    symbols: list[str] = []

    for raw in raw_symbols:
        symbol = normalize_symbol(raw)

        if symbol not in symbols:
            symbols.append(symbol)

    if len(symbols) > MAX_SYMBOLS_PER_REQUEST:
        raise ValueError(
            f"Tối đa {MAX_SYMBOLS_PER_REQUEST} mã mỗi yêu cầu."
        )

    return symbols


def parse_lookback_days(payload: dict[str, Any]) -> int:
    raw = payload.get("lookback_days", DEFAULT_LOOKBACK_DAYS)

    if isinstance(raw, bool):
        raise ValueError("lookback_days phải là số nguyên.")

    try:
        days = int(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "lookback_days phải là số nguyên."
        ) from exc

    if days < 1 or days > MAX_LOOKBACK_DAYS:
        raise ValueError(
            f"lookback_days phải từ 1 đến {MAX_LOOKBACK_DAYS}."
        )

    return days


# ============================================================
# FLASK ROUTE
# ============================================================

@dnse_sync_bp.route("/sync", methods=["POST"])
def sync_market_data_route():

    started_at = utc_now_iso()
    started_clock = time.monotonic()

    try:
        payload = request.get_json(silent=True)

        if not isinstance(payload, dict):
            return jsonify({
                "success": False,
                "error": "Body yêu cầu phải là JSON object hợp lệ.",
                "started_at": started_at,
                "finished_at": utc_now_iso(),
            }), 400

        # Không lấy user_id từ JSON để quyết định danh tính.
        user_id = authenticate_supabase_user()

        targets = parse_targets(payload)
        symbols = parse_symbols(payload)
        lookback_days = parse_lookback_days(payload)

        if "stocks" in targets and not symbols:
            return jsonify({
                "success": False,
                "error": "Bạn đã chọn stocks nhưng chưa chọn mã cổ phiếu.",
            }), 400

        end_date = vietnam_today()

        start_date = end_date - timedelta(
            days=lookback_days - 1
        )

        supabase = get_supabase_client()
        results: list[dict[str, Any]] = []

        # VNINDEX và VN30.
        for target in targets:
            if target not in INDEX_TABLES:
                continue

            config = INDEX_TABLES[target]

            item = sync_ohlc_to_table(
                supabase=supabase,
                user_id=user_id,
                symbol=config["symbol"],
                bar_type=config["bar_type"],
                table=config["table"],
                start_date=start_date,
                end_date=end_date,
                is_stock=False,
            )

            item["target"] = target
            results.append(item)

        # Cổ phiếu.
        if "stocks" in targets:
            for symbol in symbols:
                item = sync_ohlc_to_table(
                    supabase=supabase,
                    user_id=user_id,
                    symbol=symbol,
                    bar_type="STOCK",
                    table=STOCK_TABLE,
                    start_date=start_date,
                    end_date=end_date,
                    is_stock=True,
                )

                item["target"] = "stocks"
                results.append(item)

        inserted = sum(
            int(item.get("inserted", 0))
            for item in results
        )

        skipped = sum(
            int(item.get("skipped", 0))
            for item in results
        )

        failed = sum(
            int(item.get("failed", 0))
            for item in results
        )

        successful = sum(
            item.get("success") is True
            for item in results
        )

        failed_results = sum(
            item.get("success") is not True
            for item in results
        )

        all_success = failed_results == 0

        finished_at = utc_now_iso()

        body = {
            "success": all_success,
            "message": (
                "Đồng bộ hoàn tất."
                if all_success
                else "Đồng bộ có nhóm dữ liệu thất bại."
            ),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": round(
                time.monotonic() - started_clock,
                3,
            ),
            "date_range": {
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
            },
            "summary": {
                "targets_requested": targets,
                "results_count": len(results),
                "successful_results": successful,
                "failed_results": failed_results,
                "inserted": inserted,
                "skipped": skipped,
                "failed_records": failed,
            },
            "results": results,
        }

        return jsonify(body), 200

    except PermissionError as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
            "started_at": started_at,
            "finished_at": utc_now_iso(),
        }), 401

    except ValueError as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
            "started_at": started_at,
            "finished_at": utc_now_iso(),
        }), 400

    except Exception:
        logger.exception("Unexpected DNSE sync error")

        return jsonify({
            "success": False,
            "error": (
                "Lỗi máy chủ khi đồng bộ DNSE. "
                "Kiểm tra log backend để biết chi tiết."
            ),
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "duration_seconds": round(
                time.monotonic() - started_clock,
                3,
            ),
        }), 500


# ============================================================
# HEALTH CHECK
# ============================================================

@dnse_sync_bp.route("/health", methods=["GET"])
def dnse_sync_health():
    return jsonify({
        "success": True,
        "service": "LHP-FI DNSE Market Data Sync",
        "endpoint": "/api/dnse/sync",
        "timestamp": utc_now_iso(),
    }), 200

