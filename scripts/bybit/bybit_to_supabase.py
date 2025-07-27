# bybit_to_supabase.py

import requests
import os
import sys
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime

# ====== 1. Nạp biến môi trường từ .env ======
sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY trong .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ====== 2. Cấu hình Bybit API ======
BYBIT_API_URL = "https://api.bybit.com/v5/market/kline"
INTERVAL = "5"
LIMIT = 100
CATEGORY = "linear"

# ====== 3. Hàm in log có timestamp ======
def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

# ====== 4. Lấy danh sách coin đang theo dõi ======
def get_active_symbols():
    try:
        res = supabase.table("watched_symbols") \
                      .select("symbol") \
                      .eq("active", True) \
                      .execute()
        return [row["symbol"] for row in res.data]
    except Exception as e:
        log(f"❌ Lỗi khi lấy danh sách coin: {e}")
        return []

# ====== 5. Lấy dữ liệu nến từ Bybit ======
def fetch_candles(symbol: str):
    params = {
        "category": CATEGORY,
        "symbol": symbol,
        "interval": INTERVAL,
        "limit": LIMIT
    }
    try:
        response = requests.get(BYBIT_API_URL, params=params, timeout=10)
        data = response.json()

        if response.status_code != 200 or data.get("retCode") != 0:
            raise Exception(f"Bybit lỗi: {data.get('retMsg')}")

        candles = data.get("result", {}).get("list", [])
        if not candles:
            raise Exception("Không có dữ liệu nến trả về.")
        return candles
    except requests.exceptions.Timeout:
        raise Exception("⏰ Request tới Bybit bị timeout.")
    except Exception as e:
        raise Exception(f"🚨 Lỗi khi gọi API Bybit: {e}")

# ====== 6. Lưu dữ liệu nến vào Supabase ======
def save_to_supabase(symbol: str, candles: list):
    inserted = 0
    for candle in candles:
        try:
            timestamp, open_, high, low, close, volume, *_ = candle
            timestamp = int(timestamp)

            # Kiểm tra trùng timestamp + symbol
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
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume)
            }).execute()
            inserted += 1
        except Exception as e:
            log(f"⚠️ Lỗi khi lưu nến {symbol} tại {timestamp}: {e}")
    return inserted

# ====== 7. Hàm chính để gọi từ app.py ======
def run_sync(logs=None):
    if logs is None:
        logs = []

    total = 0
    symbols = get_active_symbols()

    if not symbols:
        msg = "⚠️ Không có đồng coin nào đang được theo dõi."
        logs.append(msg)
        log(msg)
        return 0

    for symbol in symbols:
        try:
            logs.append(f"\n📥 Đang xử lý {symbol}...")
            candles = fetch_candles(symbol)
            logs.append(f"🟢 Lấy được {len(candles)} cây nến từ Bybit.")
            count = save_to_supabase(symbol, candles)
            logs.append(f"✅ Đã lưu {count} cây nến mới vào Supabase.")
            total += count
        except Exception as e:
            logs.append(f"❌ {symbol} bị lỗi: {e}")
    return total

# ====== 8. Nếu chạy trực tiếp thì tự chạy ======
if __name__ == "__main__":
    logs = []
    inserted = run_sync(logs)
    for line in logs:
        log(line)
