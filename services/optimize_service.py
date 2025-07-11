import numpy as np
import pandas as pd

def optimize_portfolio(data=None):
    """
    Hàm tối ưu hóa danh mục đầu tư dựa trên dữ liệu lợi suất.
    Nếu không có `data`, sẽ random giả định.
    """

    try:
        if data is None:
            # Tạo dữ liệu giả để demo: lợi suất ngẫu nhiên cho 10 mã
            symbols = [f"STOCK{i}" for i in range(10)]
            returns = np.random.rand(10)
        else:
            # data dạng: {"AAPL": 0.12, "GOOG": 0.09, ...}
            symbols = list(data.keys())
            returns = np.array(list(data.values()))

        # Normalize thành trọng số
        weights = returns / np.sum(returns)

        result = [
            {"symbol": symbol, "weight": round(float(weight), 4)}
            for symbol, weight in zip(symbols, weights)
        ]

        return {
            "status": "success",
            "portfolio": result
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
