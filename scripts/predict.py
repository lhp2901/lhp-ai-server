# scripts/predict.py

import sys
import json
import joblib
import numpy as np

# ─────────── ✅ Kiểm tra tham số đầu vào ───────────
if len(sys.argv) < 3:
    print(json.dumps({ "error": "❌ Thiếu tham số đầu vào." }))
    sys.exit(1)

model_path = sys.argv[1]
features_json = sys.argv[2]

# ─────────── ✅ Load mô hình ───────────
try:
    model = joblib.load(model_path)
except Exception as e:
    print(json.dumps({ "error": f"❌ Lỗi khi load model: {str(e)}" }))
    sys.exit(1)

# ─────────── ✅ Parse dữ liệu đầu vào ───────────
try:
    features = json.loads(features_json)

    expected_fields = [
        'close', 'volume', 'ma20', 'rsi',
        'bb_upper', 'bb_lower', 'foreign_buy_value', 'foreign_sell_value'
    ]

    # Kiểm tra đầy đủ field
    for field in expected_fields:
        if field not in features:
            raise ValueError(f"Thiếu trường '{field}' trong input")

    X = np.array([[ features[field] for field in expected_fields ]])

except Exception as e:
    print(json.dumps({ "error": f"❌ Lỗi xử lý dữ liệu vào: {str(e)}" }))
    sys.exit(1)

# ─────────── ✅ Dự đoán và trả kết quả ───────────
try:
    prob = model.predict_proba(X)[0][1]
    recommend = (
        "BUY" if prob > 0.6 else
        "SELL" if prob < 0.4 else
        "HOLD"
    )

    print(json.dumps({
        "probability": round(float(prob), 4),
        "recommendation": recommend
    }))
except Exception as e:
    print(json.dumps({ "error": f"❌ Lỗi khi predict: {str(e)}" }))
    sys.exit(1)
