import os
import sys
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY trong .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def fetch_unlabeled_signals():
    try:
        res = supabase.table("ai_market_signals") \
            .select("*") \
            .is_("label_win", None) \
            .execute()
        return pd.DataFrame(res.data) if res.data else pd.DataFrame()
    except Exception as e:
        print(f"❌ Lỗi khi lấy tín hiệu chưa gắn label: {e}")
        return pd.DataFrame()

def fetch_market_data(index_code: str, from_date: str, days_after: int = 3):
    table = "vnindex_data" if index_code == "VNINDEX" else "vn30_data"
    try:
        res = (
            supabase.table(table)
            .select("date, close")
            .gte("date", from_date)
            .order("date", desc=False)  # ✅ sửa ở đây
            .limit(days_after + 1)
            .execute()
        )
        df = pd.DataFrame(res.data)
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)
    except Exception as e:
        print(f"❌ Lỗi khi lấy dữ liệu giá cho {index_code} - {from_date}: {e}")
        return pd.DataFrame()

def update_label(signal_id: str, label_win: bool):
    try:
        res = supabase.table("ai_market_signals") \
            .update({"label_win": int(label_win)}) \
            .eq("id", signal_id) \
            .execute()
        if res.data:
            print(f"✅ Cập nhật {signal_id}: label_win = {label_win}")
        else:
            print(f"⚠️ Không có kết quả cập nhật cho {signal_id}")
    except Exception as e:
        print(f"❌ Lỗi update label: {e}")

def process_signals():
    print("🚀 Bắt đầu gắn label_win cho tín hiệu AI...")
    df_signals = fetch_unlabeled_signals()

    if df_signals.empty:
        print("✅ Không có tín hiệu nào cần gắn label.")
        return

    for _, row in df_signals.iterrows():
        index_code = row["index_code"]
        signal_date = row["date"]
        signal_id = row["id"]

        if isinstance(signal_date, str):
            signal_date = pd.to_datetime(signal_date)

        market_data = fetch_market_data(index_code, signal_date.strftime("%Y-%m-%d"))

        if len(market_data) < 2:
            print(f"⚠️ Không đủ dữ liệu để đánh giá {index_code} ngày {signal_date.date()}")
            continue

        current_close = market_data.iloc[0]["close"]
        future_close = market_data.iloc[-1]["close"]

        label_win = future_close > current_close
        update_label(signal_id, label_win)

if __name__ == "__main__":
    process_signals()
