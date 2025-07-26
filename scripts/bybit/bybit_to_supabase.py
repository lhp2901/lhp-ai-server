import requests
import os
from supabase import create_client, Client
from dotenv import load_dotenv

# ====== 1. Nạp biến môi trường từ .env ======
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# ====== 2. Kết nối Supabase ======
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("⚠️ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY trong file .env!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ====== 3. Hàm lấy danh sách symbol đang được theo dõi ======
def get_active_symbols_from_supabase():
    try:
        response = supabase.table("watched_symbols") \
            .select("symbol") \
            .eq("active", True) \
            .execute()
        symbols = [row["symbol"] for row in response.data]
        return symbols
    except Exception as e:
        print("❌ Lỗi khi lấy danh sách symbol từ Supabase:", e)
        return []

# ====== 4. Hàm lấy dữ liệu nến từ Bybit ======
def fetch_candles(symbol: str, interval="5", limit=100, category="linear"):
    url = f"https://api.bybit.com/v5/market/kline?category={category}&symbol={symbol}&interval={interval}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
        resp_json = response.json()

        if response.status_code != 200 or resp_json.get("retCode") != 0:
            raise Exception(f"Bybit API lỗi: {resp_json.get('retMsg')}")

        result = resp_json.get("result")
        if not result or "list" not in result:
            print("⚠️ DEBUG response:", resp_json)
            raise Exception("Cấu trúc dữ liệu Bybit trả về không đúng.")
        return result["list"]

    except requests.exceptions.Timeout:
        raise Exception("⏰ Request tới Bybit bị timeout.")
    except Exception as e:
        raise Exception(f"🚨 Lỗi khi gọi API Bybit: {e}")

# ====== 5. Lưu dữ liệu vào Supabase ======
def save_to_supabase(symbol: str, candle_data: list):
    inserted = 0
    for candle in candle_data:
        timestamp, open_, high, low, close, volume, turnover = candle

        try:
            # Kiểm tra trùng timestamp + symbol
            existing = supabase.table("ohlcv_data") \
                .select("id") \
                .eq("timestamp", int(timestamp)) \
                .eq("symbol", symbol) \
                .execute()

            if existing.data:
                continue  # Đã có → bỏ qua

            # Insert
            supabase.table("ohlcv_data").insert({
                "timestamp": int(timestamp),
                "symbol": symbol,
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume)
            }).execute()

            inserted += 1
        except Exception as e:
            print(f"❌ Lỗi khi lưu nến {symbol} - timestamp {timestamp}: {e}")

    print(f"✅ {symbol}: Đã lưu {inserted}/{len(candle_data)} cây nến vào Supabase.")

# ====== 6. Chạy chương trình chính ======
if __name__ == "__main__":
    print("🚀 Bắt đầu đồng bộ dữ liệu nến từ Bybit...")

    symbols = get_active_symbols_from_supabase()
    if not symbols:
        print("⚠️ Không có đồng coin nào đang được theo dõi trong Supabase.")
        exit()

    for symbol in symbols:
        try:
            print(f"\n📥 Đang xử lý {symbol}...")
            candles = fetch_candles(symbol)
            print(f"🟢 {symbol}: Lấy được {len(candles)} cây nến từ Bybit.")
            save_to_supabase(symbol, candles)
        except Exception as e:
            print(f"❌ {symbol}: Lỗi - {e}")

    print("\n🎯 Đồng bộ hoàn tất.")
