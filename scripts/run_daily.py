import subprocess
from datetime import datetime

def run_script(path: str, description: str):
    print(f"\n🚀 Bắt đầu {description}...")
    try:
        start = datetime.now()
        subprocess.run(["python", path], check=True)
        end = datetime.now()
        print(f"✅ Hoàn tất {description} trong {(end - start).seconds} giây.")
    except subprocess.CalledProcessError as e:
        print(f"❌ Lỗi khi chạy {description}: {e}")

def main():
    print(f"📅 Chạy hệ thống AI ngày {datetime.now().strftime('%Y-%m-%d')}")
    print("⏱️ Thực thi theo quy trình: Insert ➝ Label ➝ Evaluate")

    # 1️⃣ Insert tín hiệu mới
    run_script("scripts/insert_ai_signals.py", "tạo tín hiệu mới (insert_ai_signals.py)")

    # 2️⃣ Gắn nhãn cho tín hiệu cũ
    run_script("scripts/label_ai_signals.py", "gắn label cho tín hiệu cũ (label_ai_signals.py)")

    # 3️⃣ Đánh giá độ chính xác
    run_script("scripts/evaluate_ai_accuracy.py", "đánh giá độ chính xác AI (evaluate_ai_accuracy.py)")

    print("\n🎯 Kết thúc quy trình run_daily.py – All systems ✅ Ready to conquer the market.")

if __name__ == "__main__":
    main()
