import os
import sys
import pandas as pd
from datetime import datetime
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

def fetch_labeled_signals() -> pd.DataFrame:
    try:
        res = supabase.table("ai_market_signals") \
            .select("*") \
            .not_.is_("label_win", None) \
            .execute()
        if res.data:
            df = pd.DataFrame(res.data)
            df["date"] = pd.to_datetime(df["date"])
            return df
        else:
            return pd.DataFrame()
    except Exception as e:
        print(f"❌ Lỗi khi lấy dữ liệu tín hiệu đã gắn label: {e}")
        return pd.DataFrame()

def evaluate_accuracy(df: pd.DataFrame) -> list:
    results = []
    today = datetime.now().strftime("%Y-%m-%d")
    grouped = df.groupby(["index_code", "model_version"])

    for (index_code, model_version), group in grouped:
        total = len(group)
        correct = group["label_win"].sum()
        accuracy = round(correct / total, 4) if total > 0 else 0.0

        print(f"📊 {index_code} - {model_version}: {correct}/{total} đúng → accuracy = {accuracy}")

        results.append({
            "date": today,
            "index_code": index_code,
            "accuracy": accuracy,
            "total": total,
            "correct": int(correct),
            "model_version": model_version
        })

    return results

def insert_accuracy_logs(logs: list):
    try:
        res = supabase.table("ai_accuracy_logs").insert(logs).execute()
        if not res.data:
            print(f"❌ Insert log thất bại. Raw response: {res}")
        else:
            print(f"✅ Đã ghi {len(logs)} bản ghi log accuracy vào bảng ai_accuracy_logs")
    except Exception as e:
        print(f"❌ Lỗi khi insert log: {e}")

def main():
    print("🚀 Bắt đầu đánh giá độ chính xác tín hiệu AI...")
    df = fetch_labeled_signals()
    if df.empty:
        print("⚠️ Không có dữ liệu tín hiệu đã gắn label.")
        return

    logs = evaluate_accuracy(df)
    insert_accuracy_logs(logs)

if __name__ == "__main__":
    main()
