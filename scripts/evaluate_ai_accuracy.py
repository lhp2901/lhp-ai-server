import os
import sys
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client

# ✅ Cho in tiếng Việt trên terminal
sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY trong .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ✅ Lấy tín hiệu đã gán nhãn
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
        print(f"❌ Lỗi khi lấy tín hiệu đã gán label: {e}")
        return pd.DataFrame()

# ✅ Tính accuracy theo từng ngày + index
def evaluate_accuracy(df: pd.DataFrame) -> list:
    results = []
    grouped = df.groupby(["date", "index_code"])

    for (signal_date, index_code), group in grouped:
        total = len(group)
        correct = group["label_win"].sum()
        accuracy = round(correct / total, 4) if total > 0 else 0.0

        print(f"📅 {signal_date.date()} | {index_code}: {correct}/{total} đúng → accuracy = {accuracy}")

        results.append({
            "date": signal_date.strftime("%Y-%m-%d"),
            "index_code": index_code,
            "accuracy": accuracy,
            "total": total,
            "correct": int(correct),
        })

    return results

# ✅ Insert hoặc update log nếu đã có
def insert_accuracy_logs(logs: list):
    for log in logs:
        try:
            existing = supabase.table("ai_accuracy_logs") \
                .select("id") \
                .eq("date", log["date"]) \
                .eq("index_code", log["index_code"]) \
                .execute()

            if existing.data and len(existing.data) > 0:
                # 🔁 Cập nhật nếu đã có
                supabase.table("ai_accuracy_logs") \
                    .update({
                        "accuracy": log["accuracy"],
                        "total": log["total"],
                        "correct": log["correct"]
                    }) \
                    .eq("id", existing.data[0]["id"]) \
                    .execute()
                print(f"🔁 Đã cập nhật log: {log['index_code']} - {log['date']}")
            else:
                # 🆕 Insert mới
                supabase.table("ai_accuracy_logs").insert(log).execute()
                print(f"🆕 Đã insert mới log: {log['index_code']} - {log['date']}")

        except Exception as e:
            print(f"❌ Lỗi khi insert/update log {log['index_code']} - {log['date']}: {e}")

# ✅ Hàm chính
def main():
    print("🚀 Bắt đầu đánh giá độ chính xác tín hiệu AI...")
    df = fetch_labeled_signals()
    if df.empty:
        print("⚠️ Không có dữ liệu tín hiệu đã gán label.")
        return

    logs = evaluate_accuracy(df)
    insert_accuracy_logs(logs)

if __name__ == "__main__":
    main()
