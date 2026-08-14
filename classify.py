#!/usr/bin/env python3
"""
階段分類器 —— 單一真相（single source of truth）

原本分類邏輯只存在於前端 JS，且每天算完即丟。這造成兩個問題：
  1. P3 回測時沒有任何歷史階段標籤可測（只有原始提及數）
  2. 若之後 Python 與 JS 各有一份實作，會悄悄漂移

因此：本模組為權威實作，`fetch.py` 每天呼叫它、把標籤與**當時的參數版本**凍結進
SQLite（`stage_history`），前端在 live 模式直接顯示這裡算出的標籤。

⚠️ 修改 PARAMS 會產生新的 param_version（config 的雜湊）。回測時務必依 version
   分段，不可把不同門檻產生的標籤混在一起當同一個策略評估。
"""

import json
import math
import hashlib

# --------------------------------------------------------------------------- #
# 參數（與前端 index.html 的 CFG / SOURCE_WEIGHTS 必須一致）
# --------------------------------------------------------------------------- #
PARAMS = {
    "NEW_JUMP_X": 5,        # New：最近 3 天相對前期跳升倍數
    "NEW_QUIET_MAX": 0.06,  # New：前期提及「幾乎為 0」的相對水位
    "ACCEL_THRESH": 0.15,   # Accelerating：週變化率門檻
    "FADE_THRESH": -0.15,   # Fading：週變化率門檻
    "TOP_N": 5,             # Saturated：前 N 名算「高位」
    "SAT_DAYS": 10,         # Saturated：高位停留天數門檻
    "DIVERGE_DAYS": 8,      # 背離：檢視價格是否走平/反轉的視窗
    "MIN_OBS": 8,           # 觀察不足此天數 → Warming，不分類
    "SOURCE_WEIGHTS": {     # 多來源加權
        "r/wallstreetbets": 1.0,
        "r/stocks": 1.3,
        "r/options": 1.15,
    },
}


def param_version(params=None):
    """參數集的短雜湊。門檻一改就換版本，讓回測能分段。"""
    p = params if params is not None else PARAMS
    blob = json.dumps(p, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 基礎運算（刻意與前端 JS 逐行對應，避免漂移）
# --------------------------------------------------------------------------- #
def _round_half_up(x):
    """JS Math.round 是 half-up；Python 內建 round 是 banker's rounding。"""
    return math.floor(x + 0.5)


def weighted_total(sources, days, weights):
    """依來源權重加總成注意力分數（對應 JS weightedTotal）。"""
    arr = [0.0] * days
    for name, series in sources.items():
        w = weights.get(name, 1)
        for i in range(days):
            v = series[i] if i < len(series) and series[i] is not None else 0
            arr[i] += v * w
    return [_round_half_up(v) for v in arr]


def sma(arr, w, idx):
    """以 idx 結尾的 w 日平均（對短歷史/越界安全，對應 JS sma）。"""
    idx = min(idx, len(arr) - 1)
    if idx < 0:
        idx = 0
    lo = max(0, idx - w + 1)
    seg = arr[lo:idx + 1]
    return (sum(seg) / len(seg)) if seg else 0


def compute_metrics(total, days):
    last = days - 1
    M = sma(total, 7, last)
    Mprev = sma(total, 7, last - 7)
    if Mprev > 0:
        week_change = (M - Mprev) / Mprev
    else:
        week_change = 3 if M > 0 else 0
    recent3 = sma(total, 3, last)
    baseline_prev = max(1, sma(total, 10, days - 4))
    jump_x = recent3 / baseline_prev
    return {"M": M, "week_change": week_change, "recent3": recent3, "jump_x": jump_x}


def compute_ranks(totals_by_ticker, tickers, days):
    """每日整體排名；當日無提及 → 給「未上榜」大排名（對應 JS computeRanks）。"""
    nr = len(tickers) + 1
    ranks = {t: [0] * days for t in tickers}
    for d in range(days):
        scored = [(t, sma(totals_by_ticker[t], 3, d)) for t in tickers]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        r = 0
        for t, v in scored:
            if v > 0:
                r += 1
                ranks[t][d] = r
            else:
                ranks[t][d] = nr
    return ranks


def days_at_high(rank_series, days, top_n):
    c = 0
    for d in range(days - 1, -1, -1):
        if rank_series[d] <= top_n:
            c += 1
        else:
            break
    return c


def rank_trend(rank_series, n, days):
    i = max(0, days - 1 - n)
    return rank_series[days - 1] - rank_series[i]


def observed_days(total, days):
    for i, v in enumerate(total):
        if v > 0:
            return days - i
    return 0


def divergence_flag(total, price, days, p):
    """注意力仍在高檔但價格走平/反轉 → 過熱背離。"""
    if not price or any(v is None for v in price):
        return False
    w = p["DIVERGE_DAYS"]
    attn_now = sma(total, 3, days - 1)
    tail = total[days - w:] if days - w >= 0 else total
    attn_max = max(tail) if tail else 0
    attn_high = attn_now >= attn_max * 0.92
    then = price[max(0, days - 1 - w)]
    if not then:
        return False
    price_chg = (price[days - 1] - then) / then
    return bool(attn_high and price_chg <= 0.01)


def classify_one(total, rank_series, price, days, p=None):
    """回傳 (stage, detail_dict)。規則順序與前端 JS 完全一致。"""
    p = p or PARAMS
    m = compute_metrics(total, days)
    dah = days_at_high(rank_series, days, p["TOP_N"])
    rt5 = rank_trend(rank_series, 5, days)
    obs = observed_days(total, days)
    wc = m["week_change"]

    detail = {
        "week_change": wc, "jump_x": m["jump_x"], "days_at_high": dah,
        "rank": rank_series[days - 1], "mentions": total[days - 1],
        "observed_days": obs, "hot": False,
    }

    if obs < p["MIN_OBS"]:
        return "Warming", detail

    peak = max(total) if total else 0
    quiet_before = sma(total, 10, days - 4) < peak * p["NEW_QUIET_MAX"]

    if quiet_before and m["jump_x"] >= p["NEW_JUMP_X"]:
        stage = "New"
    elif dah >= p["SAT_DAYS"] and wc <= 0.05:
        stage = "Saturated"
    elif wc >= p["ACCEL_THRESH"] and rt5 <= 0:
        stage = "Accelerating"
    elif wc <= p["FADE_THRESH"] and rt5 >= 0:
        stage = "Fading"
    elif dah >= p["SAT_DAYS"]:
        stage = "Saturated"
    elif wc >= p["ACCEL_THRESH"]:
        stage = "Accelerating"
    elif wc <= p["FADE_THRESH"]:
        stage = "Fading"
    else:
        stage = "Accelerating"

    detail["hot"] = divergence_flag(total, price, days, p)
    return stage, detail


def classify_all(tickers_payload, days, p=None):
    """
    對 data.json 的 tickers 陣列做完整分類。
    回傳 {ticker: {"stage":…, **detail}}。
    """
    p = p or PARAMS
    weights = p["SOURCE_WEIGHTS"]
    names = [t["ticker"] for t in tickers_payload]
    totals = {t["ticker"]: weighted_total(t.get("sources") or {}, days, weights)
              for t in tickers_payload}
    ranks = compute_ranks(totals, names, days)

    out = {}
    for t in tickers_payload:
        tk = t["ticker"]
        stage, detail = classify_one(totals[tk], ranks[tk], t.get("price"), days, p)
        detail["stage"] = stage
        detail["price"] = (t["price"][days - 1] if t.get("price") else None)
        out[tk] = detail
    return out
