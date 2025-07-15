import os
import sys
import pandas as pd
import random
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client

# ✅ Cấu hình môi trường
sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY trong .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# 📥 Lấy dữ liệu thị trường gần nhất
def fetch_index_data(index_code: str) -> pd.DataFrame:
    table = "vnindex_data" if index_code == "VNINDEX" else "vn30_data"
    try:
        res = supabase.table(table).select("*").order("date", desc=True).limit(1).execute()
        if res.data:
            df = pd.DataFrame(res.data)
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)
        else:
            return pd.DataFrame()
    except Exception as e:
        print(f"❌ Lỗi khi tải dữ liệu {index_code}: {e}")
        return pd.DataFrame()

# 🧠 Sinh tín hiệu AI mẫu (chưa gán label_win)
def generate_signal(df: pd.DataFrame, index_code: str) -> dict:
    latest = df.iloc[0]
    return {
        "index_code": index_code,
        "date": latest["date"].strftime("%Y-%m-%d"),
        "signal_type": random.choice(["uptrend", "downtrend", "sideways"]),
        "confidence_score": float(round(random.uniform(0.6, 0.9), 2)),
        "volatility_tag": random.choice(["low", "high", "neutral"]),
        "volume_behavior": random.choice(["rising", "falling", "neutral"]),
        "model_version": "ai_market_v1",
        "label_win": None,  # 🛑 chưa gán label ở bước này
        "notes": f"Tín hiệu test {index_code} ngày {latest['date'].strftime('%Y-%m-%d')}"
    }

# 💾 Ghi vào Supabase nếu chưa có
def insert_signal(signal: dict):
    try:
        existing = supabase.table("ai_market_signals") \
            .select("id") \
            .eq("index_code", signal["index_code"]) \
            .eq("date", signal["date"]) \
            .execute()

        if existing.data and len(existing.data) > 0:
            print(f"⚠️ Bỏ qua: Đã có {signal['index_code']} ngày {signal['date']}")
            return

        res = supabase.table("ai_market_signals").insert(signal).execute()

        if not res.data:
            print(f"❌ Insert thất bại, không có data trả về. Response raw: {res}")
        else:
            print(f"✅ Đã insert {signal['index_code']} {signal['date']} ({signal['signal_type']}, score {signal['confidence_score']})")

    except Exception as e:
        print(f"❌ Lỗi insert: {e}")

# 🚀 Chạy cho VNINDEX & VN30
def main():
    print("🚀 Bắt đầu insert tín hiệu AI (chưa gán label)...")
    for index in ["VNINDEX", "VN30"]:
        df = fetch_index_data(index)
        if df.empty:
            print(f"⚠️ Không có dữ liệu cho {index}")
            continue
        signal = generate_signal(df, index)
        insert_signal(signal)

if __name__ == "__main__":
    main()
