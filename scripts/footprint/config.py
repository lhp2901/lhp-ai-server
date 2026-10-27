
"""
DNSE Footprint Configuration
============================

Cấu hình cho hệ thống thu thập dữ liệu Footprint từ DNSE.

Chức năng:
- Đọc thông tin kết nối DNSE từ file .env.
- Quản lý danh sách mã cổ phiếu theo dõi.
- Cấu hình WebSocket và board giao dịch.
- Cấu hình bộ nhớ dữ liệu Footprint.
- Chuẩn hóa dữ liệu cấu hình để engine và stream sử dụng.
- Không ghi API key/secret trực tiếp vào mã nguồn.

Lưu ý:
- Các giá trị mặc định chỉ phục vụ khởi tạo.
- DNSE_WS_URL, board ID và side mapping phải được xác minh
  theo tài liệu SDK/API DNSE mà tài khoản của bạn sử dụng.
"""

from __future__ import annotations

import os
import logging
from typing import Optional

from dotenv import load_dotenv


# ============================================================
# 1. NẠP BIẾN MÔI TRƯỜNG
# ============================================================

# Tìm file .env từ thư mục hiện tại hoặc các thư mục cha.
# Khi chạy python app.py tại thư mục gốc lhp-ai-server,
# file .env ở thư mục gốc thường sẽ được nạp.
load_dotenv()


# ============================================================
# 2. LOGGING
# ============================================================

LOG_LEVEL = os.getenv("FOOTPRINT_LOG_LEVEL", "INFO").strip().upper()

_VALID_LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

FOOTPRINT_LOG_LEVEL = _VALID_LOG_LEVELS.get(
    LOG_LEVEL,
    logging.INFO,
)

logger = logging.getLogger("lhp_ai_server.footprint")


# ============================================================
# 3. HÀM ĐỌC CẤU HÌNH
# ============================================================

def _env_string(
    name: str,
    default: str = "",
    *,
    strip: bool = True,
) -> str:
    """
    Đọc biến môi trường dạng chuỗi.

    Args:
        name: Tên biến môi trường.
        default: Giá trị mặc định.
        strip: Có loại bỏ khoảng trắng đầu/cuối hay không.

    Returns:
        Giá trị chuỗi đã chuẩn hóa.
    """
    value = os.getenv(name, default)

    if value is None:
        value = default

    value = str(value)

    if strip:
        value = value.strip()

    return value


def _env_bool(name: str, default: bool = False) -> bool:
    """
    Đọc biến môi trường dạng boolean.

    Các giá trị được xem là True:
    1, true, yes, y, on

    Các giá trị được xem là False:
    0, false, no, n, off

    Nếu không hợp lệ, sử dụng default.
    """
    value = _env_string(name, str(default)).lower()

    if value in {"1", "true", "yes", "y", "on"}:
        return True

    if value in {"0", "false", "no", "n", "off"}:
        return False

    logger.warning(
        "Biến %s=%r không phải boolean hợp lệ; sử dụng mặc định %s",
        name,
        value,
        default,
    )

    return default


def _env_int(
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """
    Đọc biến môi trường dạng số nguyên.

    Nếu không chuyển đổi được, sử dụng default.
    Có thể giới hạn giá trị bằng minimum/maximum.
    """
    raw_value = _env_string(name, str(default))

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Biến %s=%r không phải số nguyên hợp lệ; sử dụng %s",
            name,
            raw_value,
            default,
        )
        value = default

    if minimum is not None:
        value = max(minimum, value)

    if maximum is not None:
        value = min(maximum, value)

    return value


def _env_csv(
    name: str,
    default: str = "",
    *,
    uppercase: bool = False,
) -> list[str]:
    """
    Đọc danh sách phân cách bằng dấu phẩy.

    Ví dụ:
        FOOTPRINT_SYMBOLS=DXG,HPG,FPT,SSI

    Returns:
        Danh sách đã loại bỏ phần tử rỗng và trùng lặp.
    """
    raw_value = _env_string(name, default)

    values: list[str] = []
    seen: set[str] = set()

    for item in raw_value.split(","):
        item = item.strip()

        if not item:
            continue

        if uppercase:
            item = item.upper()

        # Giữ thứ tự ban đầu và loại bỏ phần tử trùng.
        normalized_key = item.casefold()

        if normalized_key in seen:
            continue

        seen.add(normalized_key)
        values.append(item)

    return values



# ============================================================
# 4. THÔNG TIN XÁC THỰC DNSE
# ============================================================

# Ưu tiên biến môi trường; không ghi API key/secret trực tiếp
# vào source code.
DNSE_API_KEY = str(
    _env_string("DNSE_API_KEY", "") or ""
).strip()

DNSE_API_SECRET = str(
    _env_string("DNSE_API_SECRET", "") or ""
).strip()

# Tên biến tương thích với những module cũ.
DNSE_KEY = DNSE_API_KEY
DNSE_SECRET = DNSE_API_SECRET

# Kiểm tra cấu hình xác thực mà không in thông tin bí mật.
DNSE_CREDENTIALS_CONFIGURED = bool(
    DNSE_API_KEY and DNSE_API_SECRET
)


# ============================================================
# 5. CẤU HÌNH KẾT NỐI DNSE STREAM
# ============================================================

# URL WebSocket có thể ghi đè bằng biến môi trường.
# Giá trị mặc định phải khớp với endpoint mà SDK DNSE đang sử dụng.
DNSE_WS_URL = str(
    _env_string(
        "DNSE_WS_URL",
        "wss://ws-openapi.dnse.com.vn",
    ) or "wss://ws-openapi.dnse.com.vn"
).strip().rstrip("/")

# Thời gian chờ trước lần kết nối lại tiếp theo.
DNSE_RECONNECT_BASE_SECONDS = _env_int(
    "DNSE_RECONNECT_BASE_SECONDS",
    2,
    minimum=1,
    maximum=60,
)

# Giới hạn thời gian chờ khi tăng dần thời gian kết nối lại.
DNSE_RECONNECT_MAX_SECONDS = _env_int(
    "DNSE_RECONNECT_MAX_SECONDS",
    60,
    minimum=2,
    maximum=600,
)

# Timeout kết nối WebSocket, tính bằng giây.
DNSE_CONNECT_TIMEOUT_SECONDS = _env_int(
    "DNSE_CONNECT_TIMEOUT_SECONDS",
    15,
    minimum=1,
    maximum=120,
)

# Bật tự động kết nối lại khi stream bị gián đoạn.
DNSE_AUTO_RECONNECT = _env_bool(
    "DNSE_AUTO_RECONNECT",
    True,
)



# ============================================================
# 6. CẤU HÌNH MÃ CỔ PHIẾU
# ============================================================

# Ví dụ:
# FOOTPRINT_SYMBOLS=DXG,HPG,FPT,SSI,VNM,VIC

FOOTPRINT_SYMBOLS = _env_csv(
    "FOOTPRINT_SYMBOLS",
    "DXG",
    uppercase=True,
)

# Danh sách mặc định dự phòng nếu biến môi trường để trống.
if not FOOTPRINT_SYMBOLS:
    FOOTPRINT_SYMBOLS = ["DXG"]

# Chuẩn hóa mã để dùng trong engine.
FOOTPRINT_SYMBOL_SET = set(FOOTPRINT_SYMBOLS)

# Mã sàn/board giao dịch.
# G1 chỉ là giá trị mẫu từ cấu hình trước; hãy xác minh với DNSE.
FOOTPRINT_BOARD_ID = _env_string(
    "FOOTPRINT_BOARD_ID",
    "G1",
)

# Nếu SDK cần board ID theo từng mã, hãy cấu hình bổ sung ở stream.
# Không nên giả định mọi mã cổ phiếu đều sử dụng cùng board.


# ============================================================
# 7. CẤU HÌNH DỮ LIỆU FOOTPRINT
# ============================================================

# Số giao dịch tối đa giữ trong bộ nhớ cho mỗi mã.
FOOTPRINT_MAX_TRADES = _env_int(
    "FOOTPRINT_MAX_TRADES",
    20000,
    minimum=100,
    maximum=500000,
)

# Khung thời gian gom nến, tính bằng giây.
# 60 = nến 1 phút.
# 300 = nến 5 phút.
# 900 = nến 15 phút.
FOOTPRINT_CANDLE_SECONDS = _env_int(
    "FOOTPRINT_CANDLE_SECONDS",
    60,
    minimum=1,
    maximum=86400,
)

# Số nến tối đa lưu trong bộ nhớ cho mỗi mã.
FOOTPRINT_MAX_CANDLES = _env_int(
    "FOOTPRINT_MAX_CANDLES",
    500,
    minimum=10,
    maximum=100000,
)

# Số mức giá tối đa trả về trong một lần truy vấn.
FOOTPRINT_MAX_PRICE_LEVELS = _env_int(
    "FOOTPRINT_MAX_PRICE_LEVELS",
    1000,
    minimum=10,
    maximum=50000,
)

# Có lưu bảng giao dịch chi tiết trong RAM hay không.
# Tắt sẽ giảm mức sử dụng RAM nhưng tùy engine có thể giảm
# khả năng truy vấn lại từng giao dịch.
FOOTPRINT_KEEP_TRADE_TAPE = _env_bool(
    "FOOTPRINT_KEEP_TRADE_TAPE",
    True,
)


# ============================================================
# 8. CẤU HÌNH BUY / SELL DELTA
# ============================================================

# QUAN TRỌNG:
# Giá trị thực tế của side trong thông điệp DNSE phải được xác minh.
# Để trống có nghĩa là chưa xác định mapping.
#
# Không tự suy đoán rằng:
#   B = BUY, S = SELL
# hoặc:
#   1 = BUY, 2 = SELL
#
# Chỉ đặt các giá trị sau khi kiểm tra payload/tài liệu DNSE.

DNSE_SIDE_BUY_VALUE = _env_string(
    "DNSE_SIDE_BUY_VALUE",
    "",
)

DNSE_SIDE_SELL_VALUE = _env_string(
    "DNSE_SIDE_SELL_VALUE",
    "",
)

# Cho phép so sánh side dạng chữ không phân biệt hoa/thường.
DNSE_SIDE_CASE_INSENSITIVE = _env_bool(
    "DNSE_SIDE_CASE_INSENSITIVE",
    True,
)

# Khi side không rõ, giữ volume ở nhóm UNKNOWN.
# Không tự gán volume chưa phân loại sang BUY hoặc SELL.
FOOTPRINT_UNKNOWN_SIDE_LABEL = "UNKNOWN"

# Delta chỉ đáng tin khi BUY/SELL được xác định theo quy tắc
# có căn cứ từ dữ liệu hoặc tài liệu của DNSE.
FOOTPRINT_REQUIRE_CONFIRMED_SIDE = _env_bool(
    "FOOTPRINT_REQUIRE_CONFIRMED_SIDE",
    True,
)


# ============================================================
# 9. CẤU HÌNH GIÁ VÀ KHỐI LƯỢNG
# ============================================================

# Số chữ số thập phân để hiển thị giá trong API.
# Đây là cấu hình hiển thị; không thay thế quy tắc bước giá
# thực tế của từng thị trường/sàn.
FOOTPRINT_PRICE_DECIMALS = _env_int(
    "FOOTPRINT_PRICE_DECIMALS",
    2,
    minimum=0,
    maximum=8,
)

# Tên trường khối lượng thường gặp trong payload giao dịch.
# Đây là các tên dự phòng cho parser; parser phải chọn trường
# thực sự tồn tại trong payload DNSE.
FOOTPRINT_VOLUME_FIELD_CANDIDATES = [
    "quantity",
    "volume",
    "qty",
    "matchedQuantity",
    "matchedQty",
    "matchedVolume",
]

# Tên trường giá dự phòng.
FOOTPRINT_PRICE_FIELD_CANDIDATES = [
    "price",
    "matchPrice",
    "matchedPrice",
    "lastPrice",
]

# Tên trường thời gian dự phòng.
FOOTPRINT_TIME_FIELD_CANDIDATES = [
    "time",
    "timestamp",
    "tradeTime",
    "matchedAt",
    "transactTime",
]

# Tên trường mã cổ phiếu dự phòng.
FOOTPRINT_SYMBOL_FIELD_CANDIDATES = [
    "symbol",
    "ticker",
    "stockSymbol",
    "code",
]

# Tên trường định danh giao dịch dự phòng.
# Nếu không có trade ID, engine có thể không loại được mọi bản ghi trùng.
FOOTPRINT_TRADE_ID_FIELD_CANDIDATES = [
    "tradeId",
    "trade_id",
    "executionId",
    "matchId",
    "id",
]


# ============================================================
# 10. CẤU HÌNH KHỞI ĐỘNG
# ============================================================

# Mặc định không tự kết nối DNSE khi app Flask vừa khởi động.
# Khởi động stream thủ công qua endpoint sẽ an toàn hơn khi kiểm tra.
DNSE_FOOTPRINT_AUTOSTART = _env_bool(
    "DNSE_FOOTPRINT_AUTOSTART",
    False,
)

# Cho phép route điều khiển start/stop stream.
# Trong production, cần bảo vệ các route này bằng xác thực.
FOOTPRINT_ENABLE_STREAM_CONTROL = _env_bool(
    "FOOTPRINT_ENABLE_STREAM_CONTROL",
    True,
)

# Endpoint test tick chỉ dùng để kiểm tra engine trong môi trường local.
# Không bật công khai trên production.
FOOTPRINT_ENABLE_TEST_TICK = _env_bool(
    "FOOTPRINT_ENABLE_TEST_TICK",
    False,
)

# Cảnh báo khi route test tick được bật.
if FOOTPRINT_ENABLE_TEST_TICK:
    logger.warning(
        "FOOTPRINT_ENABLE_TEST_TICK đang bật. "
        "Không nên để route này công khai trên production."
    )


# ============================================================
# 11. CẤU HÌNH SUPABASE (TÙY CHỌN)
# ============================================================

# Phần này chỉ đọc cấu hình nếu sau này bạn bổ sung chức năng
# lưu dữ liệu Footprint vào Supabase.
#
# File config.py không tự tạo bảng hoặc ghi dữ liệu.

FOOTPRINT_SUPABASE_URL = _env_string(
    "SUPABASE_URL",
)

FOOTPRINT_SUPABASE_SERVICE_ROLE_KEY = _env_string(
    "SUPABASE_SERVICE_ROLE_KEY",
)

FOOTPRINT_SUPABASE_ENABLED = _env_bool(
    "FOOTPRINT_SUPABASE_ENABLED",
    False,
)

# Tên bảng gợi ý; bảng phải được tạo trong Supabase trước khi dùng.
FOOTPRINT_SUPABASE_TRADES_TABLE = _env_string(
    "FOOTPRINT_SUPABASE_TRADES_TABLE",
    "footprint_trades",
)

FOOTPRINT_SUPABASE_CANDLES_TABLE = _env_string(
    "FOOTPRINT_SUPABASE_CANDLES_TABLE",
    "footprint_candles",
)


# ============================================================
# 12. KIỂM TRA CẤU HÌNH
# ============================================================

def validate_config(*, require_credentials: bool = False) -> list[str]:
    """
    Kiểm tra cấu hình Footprint.

    Args:
        require_credentials:
            True nếu muốn bắt buộc có API key và secret trước khi
            kết nối stream. False nếu chỉ kiểm tra import/local engine.

    Returns:
        Danh sách lỗi cấu hình. Danh sách rỗng nghĩa là các mục
        được kiểm tra đã hợp lệ.
    """
    errors: list[str] = []

    if not FOOTPRINT_SYMBOLS:
        errors.append(
            "FOOTPRINT_SYMBOLS chưa có mã cổ phiếu hợp lệ."
        )

    if not DNSE_WS_URL:
        errors.append(
            "DNSE_WS_URL chưa được cấu hình."
        )

    if FOOTPRINT_CANDLE_SECONDS <= 0:
        errors.append(
            "FOOTPRINT_CANDLE_SECONDS phải lớn hơn 0."
        )

    if FOOTPRINT_MAX_TRADES <= 0:
        errors.append(
            "FOOTPRINT_MAX_TRADES phải lớn hơn 0."
        )

    if FOOTPRINT_MAX_CANDLES <= 0:
        errors.append(
            "FOOTPRINT_MAX_CANDLES phải lớn hơn 0."
        )

    if require_credentials:
        if not DNSE_API_KEY:
            errors.append(
                "Thiếu DNSE_API_KEY trong file .env."
            )

        if not DNSE_API_SECRET:
            errors.append(
                "Thiếu DNSE_API_SECRET trong file .env."
            )

    if errors:
        for error in errors:
            logger.error("Footprint config: %s", error)

    return errors


def get_config_summary() -> dict:
    """
    Trả về cấu hình an toàn để chẩn đoán.

    Không đưa API key, API secret hoặc Supabase service-role key
    vào kết quả.
    """
    return {
        "symbols": list(FOOTPRINT_SYMBOLS),
        "board_id": FOOTPRINT_BOARD_ID,
        "ws_url": DNSE_WS_URL,
        "reconnect_enabled": DNSE_AUTO_RECONNECT,
        "reconnect_base_seconds": DNSE_RECONNECT_BASE_SECONDS,
        "reconnect_max_seconds": DNSE_RECONNECT_MAX_SECONDS,
        "connect_timeout_seconds": DNSE_CONNECT_TIMEOUT_SECONDS,
        "max_trades": FOOTPRINT_MAX_TRADES,
        "candle_seconds": FOOTPRINT_CANDLE_SECONDS,
        "max_candles": FOOTPRINT_MAX_CANDLES,
        "max_price_levels": FOOTPRINT_MAX_PRICE_LEVELS,
        "keep_trade_tape": FOOTPRINT_KEEP_TRADE_TAPE,
        "side_buy_configured": bool(DNSE_SIDE_BUY_VALUE),
        "side_sell_configured": bool(DNSE_SIDE_SELL_VALUE),
        "require_confirmed_side": FOOTPRINT_REQUIRE_CONFIRMED_SIDE,
        "autostart": DNSE_FOOTPRINT_AUTOSTART,
        "stream_control_enabled": FOOTPRINT_ENABLE_STREAM_CONTROL,
        "test_tick_enabled": FOOTPRINT_ENABLE_TEST_TICK,
        "supabase_enabled": FOOTPRINT_SUPABASE_ENABLED,
    }


# ============================================================
# 13. IN THÔNG TIN KHỞI TẠO AN TOÀN
# ============================================================

def log_config_summary() -> None:
    """
    Ghi log các cấu hình không nhạy cảm.
    Không in API key hoặc API secret.
    """
    summary = get_config_summary()

    logger.info("========== DNSE FOOTPRINT CONFIG ==========")

    for key, value in summary.items():
        logger.info("%s = %s", key, value)

    logger.info("===========================================")


# Chỉ ghi log nếu người vận hành bật FOOTPRINT_LOG_LEVEL=DEBUG.
if FOOTPRINT_LOG_LEVEL <= logging.DEBUG:
    log_config_summary()

# ============================================================
# 14. CẤU HÌNH VOLUME PROFILE
# ============================================================

# Tỷ lệ khối lượng dùng để xác định Value Area.
FOOTPRINT_VALUE_AREA_PERCENT = _env_int(
    "FOOTPRINT_VALUE_AREA_PERCENT",
    70,
    minimum=50,
    maximum=99,
)

# Số mức giá tối đa hiển thị trong Volume Profile.
FOOTPRINT_VOLUME_PROFILE_MAX_LEVELS = _env_int(
    "FOOTPRINT_VOLUME_PROFILE_MAX_LEVELS",
    500,
    minimum=10,
    maximum=50000,
)

# Cách tổng hợp Volume Profile:
# SESSION: toàn bộ phiên giao dịch
# WINDOW: một cửa sổ thời gian do engine xác định
FOOTPRINT_VOLUME_PROFILE_MODE = _env_string(
    "FOOTPRINT_VOLUME_PROFILE_MODE",
    "SESSION",
).upper()

if FOOTPRINT_VOLUME_PROFILE_MODE not in {"SESSION", "WINDOW"}:
    logger.warning(
        "FOOTPRINT_VOLUME_PROFILE_MODE không hợp lệ; "
        "sử dụng SESSION."
    )
    FOOTPRINT_VOLUME_PROFILE_MODE = "SESSION"


# ============================================================
# 15. CẤU HÌNH CẢNH BÁO ORDER FLOW
# ============================================================

# Ngưỡng mất cân bằng khối lượng giữa hai phía.
# Đây là ngưỡng phát hiện ban đầu, không phải xác suất thắng.
FOOTPRINT_IMBALANCE_RATIO = _env_int(
    "FOOTPRINT_IMBALANCE_RATIO",
    300,
    minimum=100,
    maximum=1000,
) / 100.0

# Số lần liên tiếp xác nhận điều kiện trước khi cảnh báo.
FOOTPRINT_ALERT_CONFIRMATIONS = _env_int(
    "FOOTPRINT_ALERT_CONFIRMATIONS",
    2,
    minimum=1,
    maximum=20,
)

# Thời gian tối thiểu giữa hai cảnh báo cùng loại, tính bằng giây.
FOOTPRINT_ALERT_COOLDOWN_SECONDS = _env_int(
    "FOOTPRINT_ALERT_COOLDOWN_SECONDS",
    60,
    minimum=0,
    maximum=3600,
)