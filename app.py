from flask import Flask, request, jsonify
import joblib

app = Flask(__name__)

# Load model dùng joblib
try:
    model = joblib.load("model.pkl")
except Exception as e:
    print("❌ Lỗi khi load model:", str(e))
    model = None

@app.route("/predict", methods=["POST"])
def predict():
    if model is None:
        return jsonify({ "error": "Model chưa load được!" }), 500

    try:
        data = request.get_json()

        features = [
            data["close"],
            data["volume"],
            data["ma20"],
            data["rsi"],
            data["bb_upper"],
            data["bb_lower"],
            data["foreign_buy_value"],
            data["foreign_sell_value"],
        ]

        proba = model.predict_proba([features])[0][1]
        recommendation = "MUA" if proba > 0.7 else "KHÔNG"

        return jsonify({
            "probability": round(proba, 4),
            "recommendation": recommendation
        })

    except Exception as e:
        return jsonify({ "error": f"Lỗi xử lý: {str(e)}" }), 500

# ⚠️ Lưu ý: Render yêu cầu host 0.0.0.0 + port 10000
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
