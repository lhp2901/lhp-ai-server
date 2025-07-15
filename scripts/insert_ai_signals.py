import os
import sys
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client
import math

sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY trong .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def compute_rsi(prices: pd.Series, period: int = 14) -> float:
    delta = prices.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ma_up = up.rolling(window=period).mean()
    ma_down = down.rolling(window=period).mean()
    rs = ma_up / ma_down
    rsi = 100 - (100 / (1 + rs))
    return float(round(rsi.iloc[-1], 2))

def fetch_index_data(index_code: str) -> pd.DataFrame:
    table = "vnindex_data" if index_code == "VNINDEX" else "vn30_data"
    try:
        res = supabase.table(table).select("*").order("date", desc=False).execute()
        if res.data:
            df = pd.DataFrame(res.data)
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)
        else:
            return pd.DataFrame()
    except Exception as e:
        print(f"❌ Lỗi khi tải dữ liệu {index_code}: {e}")
        return pd.DataFrame()

def infer_market_sentiment(df: pd.DataFrame) -> str:
    close = df["close"]
    volume = df["volume"]
    rsi = compute_rsi(close)
    vol_spike = volume.iloc[-1] / volume.tail(5).mean()
    volatility = close.pct_change().tail(5).std()
    if rsi > 70 and vol_spike > 1.2:
        return "tham lam"
    if rsi < 30 and volatility > 0.02:
        return "sợ hãi"
    if 40 <= rsi <= 60 and volatility < 0.015:
        return "trung lập"
    return "trung lập"

def generate_signal(df: pd.DataFrame, index_code: str, date: datetime) -> dict:
    if len(df) < 15:
        raise ValueError(f"Không đủ dữ liệu ({len(df)}) để tính tín hiệu cho {index_code} ngày {date}")

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close_today = latest["close"]
    close_yesterday = prev["close"]
    volume_today = latest["volume"]
    avg_volume_5 = df["volume"].tail(5).mean() or 1  # tránh chia 0
    price_change_pct = (close_today - close_yesterday) / close_yesterday * 100

    # 💥 Momentum
    momentum = close_today - df["close"].iloc[-11] if len(df) >= 11 else 0.0

    # 💥 MACD
    ema_12 = df["close"].ewm(span=12, adjust=False).mean()
    ema_26 = df["close"].ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    macd_signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_diff = macd_line.iloc[-1] - macd_signal_line.iloc[-1]
    macd_signal = "tăng" if macd_diff > 0 else "giảm"

    # 💥 Bollinger Band
    sma_20 = df["close"].rolling(window=20).mean()
    std_20 = df["close"].rolling(window=20).std()
    upper_band = sma_20 + 2 * std_20
    lower_band = sma_20 - 2 * std_20
    if close_today > upper_band.iloc[-1]:
        bollinger = "mua quá mức"
    elif close_today < lower_band.iloc[-1]:
        bollinger = "bán tháo"
    else:
        bollinger = "bình thường"

    # 💥 Foreign Flow
    foreign_flow = (latest.get("foreign_buy_value") or 0) - (latest.get("foreign_sell_value") or 0)

    # 🎯 Signal Type
    if price_change_pct > 0.5:
        signal_type = "tăng"
    elif price_change_pct < -0.5:
        signal_type = "giảm"
    else:
        signal_type = "đi ngang"

    # 🎯 RSI & Volume spike
    rsi_score = compute_rsi(df["close"])
    volume_spike_ratio = round(volume_today / avg_volume_5, 2)

    # 🎯 Volatility
    volatility = df["close"].pct_change().tail(5).std()

    # 🎯 Trend strength
    if abs(price_change_pct) > 1:
        trend_strength = "mạnh"
    elif abs(price_change_pct) > 0.5:
        trend_strength = "vừa"
    else:
        trend_strength = "yếu"

    # 🎯 Volume Behavior
    if volume_today > avg_volume_5 * 1.2:
        volume_behavior = "tăng"
    elif volume_today < avg_volume_5 * 0.8:
        volume_behavior = "giảm"
    else:
        volume_behavior = "đi ngang"

    # 🎯 Market sentiment
    market_sentiment = infer_market_sentiment(df)

    # 🎯 ✅ CONFIDENCE SCORE – NÂNG CẤP
    confidence_score = 0.5  # khởi điểm trung lập

    if abs(price_change_pct) > 2:
        confidence_score += 0.15
    elif abs(price_change_pct) > 1:
        confidence_score += 0.1
    elif abs(price_change_pct) > 0.5:
        confidence_score += 0.05

    if volume_today > avg_volume_5 * 1.5:
        confidence_score += 0.15
    elif volume_today > avg_volume_5 * 1.2:
        confidence_score += 0.1
    elif volume_today > avg_volume_5:
        confidence_score += 0.05

    if momentum > 0:
        confidence_score += 0.05

    if macd_signal == "tăng":
        confidence_score += 0.1

    if bollinger in ["mua quá mức", "bán tháo"]:
        confidence_score += 0.1

    if rsi_score < 30:
        confidence_score += 0.1
    elif rsi_score > 70:
        confidence_score -= 0.1

    if volatility > 0.025:
        confidence_score += 0.05
    elif volatility < 0.005:
        confidence_score -= 0.05

    if foreign_flow > 0:
        confidence_score += 0.05

    confidence_score = round(min(max(confidence_score, 0.5), 1.0), 2)

    # ✅ Return object
    return {
        "index_code": index_code,
        "date": latest["date"].strftime("%Y-%m-%d"),
        "signal_type": signal_type,
        "confidence_score": confidence_score,
        "volatility_tag": (
            "cao" if volatility > 0.02 else "thấp" if volatility < 0.005 else "trung bình"
        ),
        "volume_behavior": volume_behavior,
        "label_win": None,
        "notes": f"Tín hiệu thực tế từ {index_code} ngày {latest['date'].strftime('%Y-%m-%d')}",
        "market_sentiment": market_sentiment,
        "rsi_score": round(rsi_score, 2),
        "volume_spike_ratio": volume_spike_ratio,
        "trend_strength": trend_strength,
        "momentum": round(momentum, 2),
        "macd_signal": macd_signal,
        "bollinger_band": bollinger,
        "foreign_flow": round(foreign_flow, 0),
    }

    
def sanitize_signal(signal: dict) -> dict:
    for k, v in signal.items():
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            print(f"⚠️ Field '{k}' có giá trị không hợp lệ: {v} → set None")
            signal[k] = None
    return signal

def insert_signal(signal: dict):
    try:
        existing = supabase.table("ai_market_signals") \
            .select("id") \
            .eq("index_code", signal["index_code"]) \
            .eq("date", signal["date"]) \
            .execute()

        if existing.data and len(existing.data) > 0:
            print(f"⚠️ Đã tồn tại: {signal['index_code']} ngày {signal['date']}")
            return

        res = supabase.table("ai_market_signals").insert(signal).execute()
        if not res.data:
            print(f"❌ Insert thất bại! Response: {res}")
        else:
            print(f"✅ Đã insert {signal['index_code']} {signal['date']} ({signal['signal_type']}, score {signal['confidence_score']})")
    except Exception as e:
        print(f"❌ Lỗi khi insert: {e}")

def main():
    print("🚀 Bắt đầu sinh tín hiệu AI từ dữ liệu lịch sử...")
    for index_code in ["VNINDEX", "VN30"]:
        df = fetch_index_data(index_code)
        if df.empty or len(df) < 20:
            print(f"⚠️ Không đủ dữ liệu cho {index_code}")
            continue

        for i in range(14, len(df)):
            sub_df = df.iloc[i-14:i+1].reset_index(drop=True)
            date = sub_df.iloc[-1]["date"]
            try:
                signal = generate_signal(sub_df, index_code, date)
                insert_signal(sanitize_signal(signal))
            except Exception as e:
                print(f"❌ Lỗi xử lý {index_code} ngày {date.date()}: {e}")

if __name__ == "__main__":
    main()
