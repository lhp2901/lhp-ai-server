from flask import Flask, request, jsonify
import subprocess
import joblib
import os
import numpy as np
import json
from dotenv import load_dotenv
from supabase import create_client
import supabase     
    
# ─────────── Load biến môi trường ───────────
load_dotenv()

# ─────────── Khởi tạo Flask ───────────
app = Flask(__name__)

# ─────────── Load mô hình AI ───────────
MODEL_PATH = os.getenv("MODEL_PATH", "model/model.pkl")
model = None

try:
    model = joblib.load(MODEL_PATH)
    print(f"✅ Loaded model từ {MODEL_PATH}")
except Exception as e:
    print(f"❌ Lỗi khi load model từ {MODEL_PATH}: {str(e)}")

# ─────────── Predict cho 1 mã ───────────
@app.route("/predict", methods=["POST"])
def predict():
    if model is None:
        return jsonify({"error": "❌ Model chưa được load"}), 500

    try:
        data = request.get_json()

        expected_fields = [
            'close', 'volume', 'ma20', 'rsi',
            'bb_upper', 'bb_lower', 'foreign_buy_value', 'foreign_sell_value'
        ]

        features = []
        for field in expected_fields:
            if field not in data:
                return jsonify({"error": f"❌ Thiếu trường bắt buộc: {field}"}), 400
            try:
                features.append(float(data[field]))
            except Exception:
                features.append(0)

        X = np.array([features])
        prob = model.predict_proba(X)[0][1]

        recommendation = (
            "MUA" if prob > 0.7 else
            "BÁN" if prob < 0.3 else
            "GIỮ"
        )

        return jsonify({
            "probability": round(float(prob), 4),
            "recommendation": recommendation
        })

    except Exception as e:
        print("🔥 Predict error:", str(e))
        return jsonify({"error": f"❌ Lỗi xử lý dữ liệu: {str(e)}"}), 500

# ─────────── Train mô hình ───────────
@app.route("/train", methods=["POST"])
def train_model():
    try:
        result = subprocess.run(
            ["python", "scripts/train_ai_model.py"],
            capture_output=True,
            text=True
        )
        return jsonify({ "message": result.stdout or result.stderr })
    except Exception as e:
        return jsonify({ "error": f"Lỗi train model: {str(e)}" }), 500

# ─────────── Tối ưu danh mục ───────────
@app.route("/optimize", methods=["POST"])
def optimize():
    try:
        result = subprocess.run(
            ["python", "scripts/portfolio_optimizer.py"],
            capture_output=True,
            text=True
        )
        return jsonify({ "message": result.stdout or result.stderr })
    except Exception as e:
        return jsonify({ "error": f"Lỗi optimize: {str(e)}" }), 500

# ─────────── Dự đoán toàn bộ ───────────
@app.route("/predict_all", methods=["POST"])
def predict_all():
    try:
        result = subprocess.run(
            ["python", "scripts/predict_all.py"],
            capture_output=True,
            text=True
        )
        return jsonify({ "message": result.stdout or result.stderr })
    except Exception as e:
        return jsonify({ "error": f"Lỗi predict_all: {str(e)}" }), 500

@app.route("/portfolio", methods=["POST"])
def portfolio():
    try:
        raw_data = request.get_json()

        if not raw_data or "userId" not in raw_data:
            return jsonify({"error": "Thiếu userId!"}), 400

        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")  # dùng SERVICE ROLE mới được quyền đọc toàn bộ

        sb = create_client(supabase_url, supabase_key)

        resp = sb.table("ai_signals").select("*")\
            .eq("user_id", raw_data["userId"])\
            .order("date", desc=True)\
            .execute()

        records = resp.data or []

        # Gọi portfolio_optimizer.py bằng subprocess
        import subprocess
        import json

        p = subprocess.Popen(
            ["python", "scripts/portfolio_optimizer.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        stdout, stderr = p.communicate(json.dumps(records))

        if p.returncode != 0:
            return jsonify({ "error": "Lỗi khi chạy portfolio_optimizer", "stderr": stderr }), 500

        try:
            portfolio = json.loads(stdout)
        except:
            return jsonify({ "error": "Lỗi parse kết quả JSON từ optimizer", "raw": stdout }), 500

        return jsonify({
            "date": records[0]["date"] if records else None,
            "portfolio": portfolio
        })

    except Exception as e:
        return jsonify({ "error": f"Lỗi xử lý portfolio: {str(e)}" }), 500
     
# ─────────── Gọi toàn bộ pipeline AI: insert → label → evaluate ───────────
@app.route("/run_daily", methods=["POST"])
def run_daily():
    steps = [
        ("Insert AI signals", "scripts/insert_ai_signals.py"),
        ("Label AI signals", "scripts/label_ai_signals.py"),
        ("Evaluate AI accuracy", "scripts/evaluate_ai_accuracy.py"),
    ]

    logs = []

    for step_name, script in steps:
        print(f"🚀 Đang chạy: {step_name}")

        try:
            result = subprocess.run(
                ["python", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace"  # ✅ chống UnicodeDecodeError
            )

            log_entry = {
                "step": step_name,
                "script": script,
                "returncode": result.returncode,
                "stdout": result.stdout.strip() if result.stdout else "",
                "stderr": result.stderr.strip() if result.stderr else "",
            }

            logs.append(log_entry)

            if result.returncode != 0:
                return jsonify({
                    "error": f"Lỗi khi chạy {step_name}",
                    "logs": logs
                }), 500

        except Exception as e:
            logs.append({
                "step": step_name,
                "script": script,
                "returncode": -1,
                "stdout": "",
                "stderr": str(e),
            })
            return jsonify({
                "error": f"Exception tại bước {step_name}",
                "logs": logs
            }), 500

    return jsonify({
        "message": "✅ Đã hoàn thành toàn bộ pipeline AI",
        "logs": logs
    }), 200

        
# ─────────── Endpoint kiểm tra ───────────
@app.route("/", methods=["GET"])
def home():
    return "✅ LHP-AI-SERVER đang hoạt động!"

# ─────────── Chạy server ───────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
