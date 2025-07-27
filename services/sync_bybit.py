import os
import sys
import requests
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import datetime
from typing import List, Dict

# ====== 1. Load biến môi trường ======
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY trong .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ====== 3. Định nghĩa hằng số ======
BYBIT_API_URL = "https://api.bybit.com/v5/market/kline"
DEFAULT_INTERVAL = "5"
DEFAULT_LIMIT = 100
DEFAULT_CATEGORY = "linear"

# ====== 4. Hàm log có timestamp ======
def log(message: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

# ====== 5. Lấy danh sách symbol đang theo dõi ======
def get_active_symbols_from_supabase() -> List[str]:
    try:
        response = supabase.table("watched_symbols") \
            .select("symbol") \
            .eq("active", True) \
            .execute()
        return [row["symbol"] for row in response.data]
    except Exception as e:
        raise RuntimeError(f"Lỗi khi truy vấn watched_symbols: {e}")

# ====== 6. Gọi API Bybit để lấy dữ liệu nến ======
def fetch_candles(symbol: str, interval=DEFAULT_INTERVAL, limit=DEFAULT_LIMIT, category=DEFAULT_CATEGORY) -> List[list]:
    params = {
        "category": category,
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    try:
        response = requests.get(BYBIT_API_URL, params=params, timeout=10)
        data = response.json()

        if response.status_code != 200 or data.get("retCode") != 0:
            raise ValueError(f"Bybit trả lỗi: {data.get('retMsg')}")

        candles = data.get("result", {}).get("list", [])
        if not candles:
            raise ValueError("Bybit không trả về dữ liệu nến.")
        return candles
    except requests.exceptions.Timeout:
        raise TimeoutError("⏰ Timeout khi gọi Bybit API.")
    except Exception as e:
        raise RuntimeError(f"Lỗi khi fetch từ Bybit: {e}")

# ====== 7. Lưu dữ liệu vào Supabase, kiểm tra trùng timestamp + symbol ======
def insert_klines_to_supabase(symbol: str, klines: List[list]) -> int:
    inserted = 0
    for k in klines:
        try:
            timestamp = int(k[0])

            # Kiểm tra dữ liệu đã tồn tại
            exists = supabase.table("ohlcv_data") \
                .select("id") \
                .eq("timestamp", timestamp) \
                .eq("symbol", symbol) \
                .execute()

            if exists.data:
                continue

            supabase.table("ohlcv_data").insert({
                "timestamp": timestamp,
                "symbol": symbol,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5])
            }).execute()
            inserted += 1

        except Exception as e:
            log(f"⚠️ Bỏ qua 1 nến lỗi của {symbol} tại {k[0]}: {e}")

    return inserted

# ====== 8. Hàm chính đồng bộ toàn bộ ======
def run_sync(logs: List[str]) -> int:
    total_inserted = 0

    try:
        symbols = get_active_symbols_from_supabase()
        if not symbols:
            logs.append("⚠️ Không có đồng coin nào đang được theo dõi.")
            return 0
    except Exception as e:
        logs.append(f"❌ Lỗi khi lấy danh sách coin: {e}")
        return 0

    logs.append(f"🚀 Bắt đầu đồng bộ {len(symbols)} đồng coin...")

    for symbol in symbols:
        logs.append(f"\n📥 Đang xử lý {symbol}...")

        try:
            candles = fetch_candles(symbol)
            logs.append(f"📊 Lấy được {len(candles)} nến từ Bybit.")

            count = insert_klines_to_supabase(symbol, candles)
            logs.append(f"✅ {symbol}: Đã thêm {count} nến mới vào Supabase.")

            total_inserted += count
        except Exception as e:
            logs.append(f"❌ Lỗi khi xử lý {symbol}: {e}")

    logs.append(f"\n✅ Đồng bộ hoàn tất. Tổng cộng đã thêm {total_inserted} nến.")
    return total_inserted


# ====== 9. Entry point ======
if __name__ == "__main__":
    run_sync()
