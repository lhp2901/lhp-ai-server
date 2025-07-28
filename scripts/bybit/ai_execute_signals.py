# ✅ Nâng cấp toàn diện: Chuẩn chỉ theo pipeline mới + log rõ ràng + đồng nhất
import os
import uuid
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from supabase import create_client
import numpy as np

# ===== 1. Load ENV =====
load_dotenv()
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

# ===== 2. Config =====
CONFIDENCE_THRESHOLD = 0.75
TIME_WINDOW_MINUTES = 45
QTY_DEFAULT = 0.01

# ===== 3. Time UTC+7 =====
def get_now_vn():
    return datetime.now(timezone(timedelta(hours=7)))

# ===== 4. Safe convert =====
def safe_float(value):
    try:
        return float(value)
    except:
        return 0.0

# ===== 5. Execute signals =====
def execute_signals():
    now_utc = datetime.utcnow()
    window_start = int((now_utc - timedelta(minutes=TIME_WINDOW_MINUTES)).timestamp() * 1000)

    try:
        res = supabase.table("ai_predictions") \
            .select("*") \
            .gte("confidence", CONFIDENCE_THRESHOLD) \
            .gte("timestamp", window_start) \
            .order("timestamp", desc=True) \
            .limit(50) \
            .execute()
        predictions = res.data or []
        print(f"📅 {len(predictions)} tín hiệu cần xử lý trong {TIME_WINDOW_MINUTES} phút qua")
    except Exception as e:
        print(f"❌ Lỗi khi truy vấn ai_predictions: {e}")
        return

    for p in predictions:
        symbol = p.get("symbol")
        action = p.get("prediction")
        prediction_id = p.get("id")

        if action == "HOLD":
            print(f"⏭️ Bỏ qua HOLD {symbol}")
            continue

        try:
            existed = supabase.table("trading_logs") \
                .select("id") \
                .eq("prediction_id", prediction_id) \
                .maybe_single() \
                .execute()
            if existed.data:
                print(f"⚠️ {symbol} đã log trước đó. Bỏ qua.")
                continue
        except Exception as e:
            print(f"❌ Lỗi khi check trading_logs: {e}")
            continue

        # === Lấy dữ liệu ===
        entry       = safe_float(p.get("entry_price"))
        tp          = safe_float(p.get("tp"))
        sl          = safe_float(p.get("sl"))
        high        = safe_float(p.get("high"))
        low         = safe_float(p.get("low"))
        current     = safe_float(p.get("current_price"))
        qty         = QTY_DEFAULT
        timestamp   = p.get("timestamp")
        model_name  = p.get("model_name", "AI")
        executed_at = get_now_vn().isoformat()

        # Log thời gian tín hiệu
        try:
            signal_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            age = (datetime.now(timezone.utc) - signal_time).total_seconds() / 60
            print(f"🕒 {symbol} cách đây {round(age)} phút")
        except:
            pass

        print(f"➡️ {action} {symbol} @ {entry} | TP: {tp}, SL: {sl}, Now: {current}")

        log = {
            "id": str(uuid.uuid4()),
            "symbol": symbol,
            "action": action,
            "price": entry,
            "tp": tp,
            "sl": sl,
            "high": high,
            "low": low,
            "current_price": current,
            "qty": qty,
            "executed_at": executed_at,
            "created_at": executed_at,
            "prediction_id": prediction_id,
            "predicted_by": model_name,
            "notes": "Auto log from AI"
        }

        try:
            supabase.table("trading_logs").insert(log).execute()
            print(f"✅ Log thành công {symbol}")
            try:
                supabase.table("ai_predictions").update({"executed": True}).eq("id", prediction_id).execute()
            except Exception as e:
                print(f"⚠️ Lỗi khi update executed: {e}")
        except Exception as e:
            print(f"❌ Lỗi khi log {symbol}: {e}")

    print("\n🎯 HOÀN THÀNH TOÀN BỘ Xử lý!")

# ===== Main =====
if __name__ == "__main__":
    execute_signals()
