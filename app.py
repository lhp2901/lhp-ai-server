from flask import Flask, request, jsonify
import subprocess
import joblib
import os
import numpy as np
import json
from dotenv import load_dotenv
from supabase import create_client
import supabase     
from scripts.bybit.bybit_to_supabase import run_sync
from scripts.footprint.routes import (
    footprint_bp,
    start_stream_if_enabled,
)
from flask_cors import CORS

import traceback 
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))    
# ─────────── Load biến môi trường ───────────
load_dotenv()

# ─────────── Khởi tạo Flask ───────────
app = Flask(__name__)

# Cho phép frontend gọi API Footprint
CORS(
    app,
    resources={
        r"/api/footprint(?:/.*)?": {
            "origins": [
                "http://localhost:3000",
                "https://lhp-fi.vercel.app",
            ],
            "methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization"],
        }
    },
)

# Đăng ký Blueprint sau khi khởi tạo app
app.register_blueprint(footprint_bp)

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

@app.route("/bybit/bybit_to_supabase", methods=["POST"])
def sync_bybit():
    logs = []

    try:
        logs.append("📡 Nhận yêu cầu POST từ Next.js")
        logs.append("🔄 Bắt đầu gọi hàm run_sync()...")

        inserted = run_sync(logs)  # Truyền logs để ghi chi tiết quá trình

        logs.append(f"\n🎯 Tổng cộng đã thêm {inserted} nến vào Supabase.")
        success_msg = f"✅ Đồng bộ thành công! Đã thêm {inserted} nến."
        logs.append(success_msg)

        return jsonify({
            'message': success_msg,
            'logs': logs
        })

    except Exception as e:
        error_msg = f"❌ Lỗi khi đồng bộ: {str(e)}"
        logs.append(error_msg)
        logs.extend(traceback.format_exc().splitlines())
        return jsonify({ 'error': str(e), 'logs': logs }), 500
        
# ─────────── Gọi toàn bộ pipeline AI: insert → label → evaluate ───────────
# Đảm bảo in được tiếng Việt và emoji ra stdout
os.environ["PYTHONIOENCODING"] = "utf-8"

def run_script(script_filename):
    base_dir = os.path.dirname(os.path.realpath(__file__))
    script_path = os.path.join(base_dir, "scripts", "bybit", script_filename)  # ✅ sửa ở đây

    if not os.path.isfile(script_path):
        return {
            "success": False,
            "stdout": "",
            "stderr": f"❌ KHÔNG TÌM THẤY FILE: {script_path}"
        }

    try:
        completed = subprocess.run(
            [sys.executable, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True
        )
        return {
            "success": True,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip()
        }
    except subprocess.CalledProcessError as e:
        return {
            "success": False,
            "stdout": e.stdout.strip() if e.stdout else "",
            "stderr": e.stderr.strip() if e.stderr else f"Lỗi không xác định trong {script_filename}"
        }

@app.route("/bybit/run_daily", methods=["POST"])
def run_daily_ai():
    stdout = []
    stderr = []

    try:
        stdout.append("🚀 Bắt đầu chạy quy trình AI hàng ngày...")

        steps = [
            ("generate_training_data.py", "📊 Sinh dữ liệu training"),
            ("train_model.py", "🤖 Huấn luyện mô hình"),
            ("predict_signal.py", "🔮 Dự đoán tín hiệu"),
            ("ai_execute_signals.py", "💥 Ghi tín hiệu vào bảng")
        ]

        for filename, description in steps:
            stdout.append(f"\n🔄 {description} ({filename})...")
            result = run_script(filename)

            if result["success"]:
                stdout.append(f"✅ {description} thành công.")
                if result["stdout"]:
                    stdout.append(result["stdout"])
                if result["stderr"]:
                    stdout.append(f"⚠️ Cảnh báo:\n{result['stderr']}")
            else:
                stderr.append(f"❌ {description} thất bại!")
                stderr.append(result["stderr"])
                stdout.append(result["stdout"])
                break  # Dừng quy trình tại đây nếu lỗi

        if stderr:
            raise Exception("Một bước trong quy trình đã thất bại.")

        stdout.append("\n🏁 ✅ TOÀN BỘ QUY TRÌNH ĐÃ CHẠY THÀNH CÔNG.")

        return jsonify({
            "message": "Đã chạy xong quy trình AI hàng ngày",
            "stdout": "\n".join(stdout),
            "stderr": ""
        })

    except Exception as e:
        tb_lines = traceback.format_exc().splitlines()
        error_msg = f"❌ Lỗi khi chạy quy trình AI hàng ngày: {str(e)}"
        stderr.insert(0, error_msg)
        stderr.extend(tb_lines)

        return jsonify({
            "error": error_msg,
            "stdout": "\n".join(stdout),
            "stderr": "\n".join(stderr)
        }), 500
        
# ─────────── Endpoint kiểm tra ───────────
@app.route("/", methods=["GET"])
def home():
    return "✅ LHP-AI-SERVER đang hoạt động!"

# ─────────── Chạy server ───────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
