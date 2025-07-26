import os
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime
import uuid

# ====== 1. Load biến môi trường ======
load_dotenv()
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

# ====== 2. Ngưỡng tín hiệu AI tối thiểu để vào lệnh ======
CONFIDENCE_THRESHOLD = 0.85

# ====== 3. Hàm xử lý tín hiệu AI ======
def execute_signals():
    response = supabase.table("ai_predictions") \
        .select("*") \
        .gte("confidence", CONFIDENCE_THRESHOLD) \
        .order("timestamp", desc=True) \
        .limit(10) \
        .execute()

    predictions = response.data or []
    print(f"📥 Có {len(predictions)} tín hiệu mạnh cần xử lý...")

    for pred in predictions:
        symbol = pred["symbol"]
        action = pred["prediction"]

        # 👉 Bỏ qua HOLD (vì DB không cho phép)
        if action == "HOLD":
            print(f"⏭️ Bỏ qua tín hiệu HOLD cho {symbol}")
            continue

        price = pred.get("close", 0) or 0
        qty = 0.01
        executed_at = datetime.utcnow().isoformat()
        predicted_by = pred["model_name"]
        prediction_id = pred["id"]

        print(f"➡️ Xử lý {action} {symbol} tại {price}")

        # ✍️ Ghi log vào trading_logs
        try:
            supabase.table("trading_logs").insert({
                "id": str(uuid.uuid4()),
                "symbol": symbol,
                "action": action,
                "price": price,
                "qty": qty,
                "executed_at": executed_at,
                "predicted_by": predicted_by,
                "prediction_id": prediction_id,
                "notes": "Tự động tạo từ AI",
                "created_at": executed_at
            }).execute()
        except Exception as e:
            print(f"❌ Lỗi khi ghi log {symbol}: {e}")

    print("✅ Hoàn tất ghi logs!")

# ====== 4. Chạy hàm nếu gọi trực tiếp ======
if __name__ == "__main__":
    execute_signals()
