# scripts/predict.py

import sys
import json
import joblib
import numpy as np

# ───── 📌 Check tham số ─────
if len(sys.argv) < 3:
    print(json.dumps({ 
        "error": "❌ Thiếu tham số đầu vào. Dùng: python predict.py model.pkl '{...json features...}'" 
    }))
    sys.exit(1)

model_path = sys.argv[1]
features_json = sys.argv[2]

# ───── 🧠 Load mô hình ─────
try:
    model = joblib.load(model_path)
except Exception as e:
    print(json.dumps({ 
        "error": f"❌ Lỗi khi load mô hình từ {model_path}: {str(e)}" 
    }))
    sys.exit(1)

# ───── 📦 Parse input features ─────
try:
    features = json.loads(features_json)

    required_fields = [
        'close', 'volume', 'ma20', 'rsi',
        'bb_upper', 'bb_lower',
        'foreign_buy_value', 'foreign_sell_value'
    ]

    values = []
    for field in required_fields:
        if field not in features:
            raise ValueError(f"⚠️ Thiếu trường: {field}")
        try:
            value = float(features[field])
        except (ValueError, TypeError):
            value = 0.0  # fallback an toàn
        values.append(value)

    X = np.array([values])

except Exception as e:
    print(json.dumps({ 
        "error": f"❌ Lỗi xử lý input features: {str(e)}" 
    }))
    sys.exit(1)

# ───── 🚀 Dự đoán ─────
try:
    if X.shape != (1, len(required_fields)):
        raise ValueError(f"❌ Sai shape input: {X.shape}, cần (1, {len(required_fields)})")

    prob = float(model.predict_proba(X)[0][1])  # class WIN
    recommendation = (
        "BUY" if prob > 0.6 else
        "SELL" if prob < 0.4 else
        "HOLD"
    )

    print(json.dumps({
        "probability": round(prob, 4),
        "recommendation": recommendation
    }))

except Exception as e:
    print(json.dumps({ 
        "error": f"❌ Lỗi khi predict: {str(e)}" 
    }))
    sys.exit(1)
