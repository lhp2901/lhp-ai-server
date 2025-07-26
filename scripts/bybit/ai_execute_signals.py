import os
import uuid
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from supabase import create_client

# ===== 1. Load biến môi trường =====
load_dotenv()
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

# ===== 2. Cấu hình =====
CONFIDENCE_THRESHOLD = 0.75
TIME_WINDOW_MINUTES = 45

# ===== 3. Lấy giờ Việt Nam =====
def get_now_vn():
    return datetime.now(timezone(timedelta(hours=7)))

# ===== 4. Xử lý tín hiệu =====
def execute_signals():
    now_utc = datetime.utcnow()
    window_start = int((now_utc - timedelta(minutes=TIME_WINDOW_MINUTES)).timestamp() * 1000)

    try:
        response = supabase.table("ai_predictions") \
            .select("*") \
            .gte("confidence", CONFIDENCE_THRESHOLD) \
            .gte("timestamp", window_start) \
            .order("timestamp", desc=True) \
            .limit(50) \
            .execute()

        predictions = response.data or []
        print(f"📥 Có {len(predictions)} tín hiệu mạnh cần xử lý...")

    except Exception as e:
        print(f"❌ Lỗi khi truy vấn ai_predictions: {e}")
        return

    for pred in predictions:
        symbol = pred.get("symbol")
        action = pred.get("prediction")
        prediction_id = pred.get("id")

        if action == "HOLD":
            print(f"⏭️ Bỏ qua tín hiệu HOLD cho {symbol}")
            continue

        try:
            check = supabase.table("trading_logs") \
                .select("id") \
                .eq("prediction_id", prediction_id) \
                .maybe_single() \
                .execute()

            if check and check.data:
                print(f"⚠️ Tín hiệu {symbol} đã được xử lý trước đó. Bỏ qua.")
                continue
        except Exception as e:
            print(f"❌ Lỗi khi kiểm tra log cho {symbol}: {e}")
            continue

        # ✅ Lấy dữ liệu AI
        def safe_float(value):
            try:
                return float(value)
            except:
                return 0.0

        entry_price    = safe_float(pred.get("entry_price"))
        tp             = safe_float(pred.get("tp"))
        sl             = safe_float(pred.get("sl"))
        high           = safe_float(pred.get("high"))
        low            = safe_float(pred.get("low"))
        current_price  = safe_float(pred.get("current_price"))
        qty            = 0.01
        executed_at    = get_now_vn().isoformat()
        predicted_by   = pred.get("model_name", "AI")
        timestamp_ms   = pred.get("timestamp")

        # Hiển thị tuổi tín hiệu
        if timestamp_ms:
            try:
                signal_time = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                age_minutes = (datetime.now(timezone.utc) - signal_time).total_seconds() / 60
                print(f"🕒 Tín hiệu {symbol} cách đây khoảng {round(age_minutes)} phút")
            except Exception as e:
                print(f"⚠️ Lỗi khi xử lý thời gian cho {symbol}: {e}")

        # Kiểm tra giá trị đầu vào
        print(f"✅ DEBUG: {symbol} | entry: {entry_price}, tp: {tp}, sl: {sl}, price_now: {current_price}")

        print(f"➡️ Vào lệnh {action} {symbol} tại giá {entry_price}")

        log_data = {
            "id": str(uuid.uuid4()),
            "symbol": symbol,
            "action": action,
            "price": entry_price,
            "tp": tp,
            "sl": sl,
            "high": high,
            "low": low,
            "current_price": current_price,
            "qty": qty,
            "executed_at": executed_at,
            "predicted_by": predicted_by,
            "prediction_id": prediction_id,
            "notes": "Tự động tạo từ AI",
            "created_at": executed_at
        }

        try:
            supabase.table("trading_logs").insert(log_data).execute()
            print(f"✅ Đã ghi log lệnh {action} cho {symbol}")

            try:
                supabase.table("ai_predictions") \
                    .update({ "executed": True }) \
                    .eq("id", prediction_id) \
                    .execute()
            except Exception as e:
                print(f"⚠️ Không thể cập nhật 'executed' cho {symbol}: {e}")

        except Exception as e:
            print(f"❌ Lỗi khi ghi log cho {symbol}: {e}")

    print("🎯 Hoàn tất xử lý tất cả tín hiệu!")

# ===== 5. Chạy nếu gọi trực tiếp =====
if __name__ == "__main__":
    execute_signals()
