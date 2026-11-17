#!/usr/bin/env python3
"""
總經流動性象限分類器（單一真相）

規則：美元指數 DXY 的方向 × 美債殖利率曲線（10Y−2Y 利差）的方向 → 四個象限。

| 象限 | DXY | 曲線 (10Y−2Y)              | 狀態                     |
|------|-----|----------------------------|--------------------------|
| Q1   | ↑   | 走弱/變平：2Y 上升比 10Y 快 | 流動性最緊縮（殺估值）   |
| Q2   | ↑   | 走強/變陡：10Y 上升比 2Y 快 | 結構性分化               |
| Q3   | ↓   | 走強/變陡：2Y 下跌比 10Y 快 | 流動性釋放初期（風險偏好回升）|
| Q4   | ↓   | 走弱/變平：10Y 下跌比 2Y 快 | 避險主導                 |

方向 = 今日值 對比 L 個交易日前的值。落在死區（變動太小）時標為「不明確」，
但仍給出依正負號最接近的象限，方便觀察醞釀方向。

「典型」檢查：象限只由 DXY 與利差方向決定，但表格同時描述了殖利率**水準**的走向
（Q1/Q2 是殖利率上升的 bear 型，Q3/Q4 是殖利率下跌的 bull 型）。若方向對了但
水準走向不符（例如 DXY 下跌、曲線變陡，但 2Y 其實在上升），標為「非典型」，
代表當下的驅動因素和表格假設的不同，可信度應打折。

⚠ 前端不再有任何分類邏輯，只顯示這裡算出的結果，避免兩份實作漂移。
⚠ 修改 PARAMS 會改變 param_version；比較歷史訊號時應依版本分段。
"""

import json
import hashlib

PARAMS = {
    # 回看視窗（交易日）→ 死區門檻。視窗越長，門檻越寬。
    "LOOKBACKS": {
        "20": {"dxy_pct": 0.5, "spread_bp": 5},    # 約 1 個月
        "60": {"dxy_pct": 1.0, "spread_bp": 10},   # 約 3 個月
    },
}

QUADRANTS = {
    "Q1": {
        "name": "流動性緊縮", "tag": "Bear Flattening",
        "dxy": "走強 ↑", "curve": "走弱 ↓：2Y 上升比 10Y 快",
        "driver": "緊縮預期升溫（如通膨反彈、美聯儲轉鷹），資金成本直接被抬高。",
        "liquidity": "流動性最緊縮（殺估值）：高估值科技股、加密貨幣、黃金與新興市場同步承壓；短債與美元現金最抗跌。",
        "up": ["SHY", "UUP"],
        "down": ["QQQ", "BTC-USD", "GLD", "EEM"],
    },
    "Q2": {
        "name": "結構性分化", "tag": "Bear Steepening",
        "dxy": "走強 ↑", "curve": "走強 ↑：10Y 上升比 2Y 快",
        "driver": "美國經濟過熱或財政發債壓力大，長端期限溢價上升，同時吸引全球資本流入美國。",
        "liquidity": "結構性分化：強美元壓抑大宗商品與跨國企業海外營收，但美國本土價值股、金融股相對抗跌；長久期資產（如 TLT）受創。",
        "up": ["VTV", "XLF"],
        "down": ["DBC", "TLT"],
    },
    "Q3": {
        "name": "流動性釋放", "tag": "Bull Steepening",
        "dxy": "走弱 ↓", "curve": "走強 ↑：2Y 下跌比 10Y 快",
        "driver": "美聯儲轉向寬鬆或啟動降息週期，短端資金成本快速下降。",
        "liquidity": "流動性釋放初期（風險偏好回升）：對利率最敏感的資產率先反彈（加密貨幣、黃金、高 Beta 成長股、新興市場）；美元與短債收益率下滑。",
        "up": ["BTC-USD", "GLD", "ARKK", "EEM"],
        "down": ["UUP"],
    },
    "Q4": {
        "name": "避險主導", "tag": "Bull Flattening",
        "dxy": "走弱 ↓", "curve": "走弱 ↓：10Y 下跌比 2Y 快",
        "driver": "經濟衰退預期主導，長端定價未來通膨與成長放緩，但美聯儲尚未大幅降息。",
        "liquidity": "避險主導（看重防禦與確定性）：長天期美債（TLT）表現最佳；高槓桿風險資產因獲利下修而波動加劇，需防範信用利差擴大。",
        "up": ["TLT"],
        "down": ["ARKK", "HYG"],
    },
}

# 用來檢驗「市場是否照這個象限的劇本走」的代表資產
ASSETS = [
    ("QQQ", "高估值科技"), ("ARKK", "高 Beta 成長"), ("BTC-USD", "加密貨幣"),
    ("GLD", "黃金"), ("EEM", "新興市場"), ("DBC", "大宗商品"),
    ("VTV", "美國價值股"), ("XLF", "金融股"), ("TLT", "長天期美債"),
    ("HYG", "高收益債"), ("SHY", "短天期美債"), ("UUP", "美元"),
]


def param_version():
    blob = json.dumps({"PARAMS": PARAMS, "QUADRANTS": {k: (v["up"], v["down"])
                       for k, v in QUADRANTS.items()}}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def classify(dxy_now, dxy_then, y2_now, y2_then, y10_now, y10_then, lookback):
    """單日分類。殖利率單位為百分比（4.83 = 4.83%）。"""
    db = PARAMS["LOOKBACKS"][str(lookback)]
    d_dxy = (dxy_now / dxy_then - 1) * 100          # %
    d2 = (y2_now - y2_then) * 100                    # bp
    d10 = (y10_now - y10_then) * 100                 # bp
    d_spread = d10 - d2                              # bp；>0 變陡、<0 變平

    dxy_up = d_dxy > 0
    steep = d_spread > 0
    q = ("Q2" if steep else "Q1") if dxy_up else ("Q3" if steep else "Q4")

    clear = abs(d_dxy) >= db["dxy_pct"] and abs(d_spread) >= db["spread_bp"]
    # 表格描述的殖利率水準走向
    typical = {"Q1": d2 > 0, "Q2": d10 > 0, "Q3": d2 < 0, "Q4": d10 < 0}[q]

    return {
        "q": q, "clear": bool(clear), "typical": bool(typical),
        "d_dxy_pct": round(d_dxy, 3), "d2_bp": round(d2, 1),
        "d10_bp": round(d10, 1), "d_spread_bp": round(d_spread, 1),
    }


def fit_check(q, returns):
    """
    檢驗市場是否照劇本走：預期偏強的資產報酬應高於全體中位數、預期承壓的應低於。
    returns: {symbol: 報酬率}。回傳命中數/總數與明細。
    """
    vals = sorted(v for v in returns.values() if v is not None)
    if not vals:
        return None
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    rows, hits, total = [], 0, 0
    for side, syms in (("up", QUADRANTS[q]["up"]), ("down", QUADRANTS[q]["down"])):
        for s in syms:
            r = returns.get(s)
            if r is None:
                continue
            ok = (r > median) if side == "up" else (r < median)
            hits += ok
            total += 1
            rows.append({"symbol": s, "expect": side, "ret": r, "hit": bool(ok)})
    return {"hits": hits, "total": total, "median": median, "rows": rows}
