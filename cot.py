"""
聰明錢／傻錢信心指數 —— 近似版（CFTC 期貨持倉）

  原版 Smart Money / Dumb Money Confidence 是 SentimenTrader 的付費專有指標（多項指標加權，
  配方不公開），沒有免費資料。這裡只用其中一個公開成分做近似：

    CFTC 每週 Commitments of Traders（Legacy、Futures Only），E-mini S&P 500（代碼 13874A）
      聰明錢 = 商業（避險）機構的淨部位 ÷ 未平倉量
      傻錢   = 未達申報門檻的小額交易人淨部位 ÷ 未平倉量
    各自換成「近 3 年（156 週）區間內的位置」0–100%（COT Index 的標準作法）。

  回測（1997 起）顯示這個近似版「沒有」原版宣稱的反向指標效果，所以前端一定要附上歷史對照，
  不可把它當成 SentimenTrader 原版。

  資料：CFTC Public Reporting（Socrata API，免金鑰）。每週二的部位、週五美東 15:30 公布。
"""

import json
import datetime
import statistics as st
import urllib.parse

from aaii import to_thursday

API = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
CODE = "13874A"                 # E-MINI S&P 500 - CME
WINDOW = 156                    # 3 年區間
MIN_WINDOW = 52                 # 前 1 年用擴張視窗
FWD_WEEKS = 26
FIELDS = ["report_date_as_yyyy_mm_dd", "open_interest_all",
          "comm_positions_long_all", "comm_positions_short_all",
          "nonrept_positions_long_all", "nonrept_positions_short_all"]

# 聰明錢−傻錢 的區間（與回測時的分組一致）
BANDS = [
    (-50, "傻錢明顯較樂觀"),
    (0, "傻錢略較樂觀"),
    (50, "聰明錢略較樂觀"),
    (101, "聰明錢明顯較樂觀"),
]


def fetch(http_get, since=None):
    """回傳 [(date_iso, oi, comm_l, comm_s, small_l, small_s)]；since＝只抓該日之後（含）。"""
    where = f"cftc_contract_market_code='{CODE}'"
    if since:
        where += f" AND report_date_as_yyyy_mm_dd >= '{since}T00:00:00.000'"
    q = {"$select": ",".join(FIELDS), "$where": where,
         "$order": "report_date_as_yyyy_mm_dd", "$limit": "10000"}
    data = json.loads(http_get(API + "?" + urllib.parse.urlencode(q),
                               {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=60))
    out = []
    for r in data:
        try:
            out.append((r[FIELDS[0]][:10], *(float(r[f]) for f in FIELDS[1:])))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _band(s):
    for hi, name in BANDS:
        if s < hi:
            return name
    return BANDS[-1][1]


def summarize(rows, spx):
    """rows：DB 的 cot_weekly 由舊到新；spx：f(date_iso) → 該日或之前最近收盤（AAII 週四序列）。"""
    rows = [r for r in rows if r[1]]
    if len(rows) < MIN_WINDOW + 10:
        return None
    net_c = [(r[2] - r[3]) / r[1] for r in rows]
    net_s = [(r[4] - r[5]) / r[1] for r in rows]

    def index(vals, i):
        w = vals[max(0, i - WINDOW + 1): i + 1]
        lo, hi = min(w), max(w)
        return 50.0 if hi == lo else 100 * (vals[i] - lo) / (hi - lo)

    idx = range(MIN_WINDOW - 1, len(rows))
    dates = [rows[i][0] for i in idx]
    smart = [index(net_c, i) for i in idx]
    dumb = [index(net_s, i) for i in idx]
    spread = [a - b for a, b in zip(smart, dumb)]

    cur = spread[-1]
    band = _band(cur)
    base = None
    if spx:
        last = datetime.date.fromisoformat(dates[-1])
        all_r, band_r = [], []
        for d, s in zip(dates, spread):
            t0 = to_thursday(datetime.date.fromisoformat(d))
            t1 = t0 + datetime.timedelta(weeks=FWD_WEEKS)
            if t1 > last:
                break
            p0, p1 = spx(t0.isoformat()), spx(t1.isoformat())
            if not p0 or not p1:
                continue
            r = p1 / p0 - 1
            all_r.append(r)
            if _band(s) == band:
                band_r.append(r)
        if len(all_r) >= 100 and len(band_r) >= 20:
            pos = lambda xs: sum(1 for x in xs if x > 0) / len(xs)
            base = {"weeks": FWD_WEEKS,
                    "band": {"n": len(band_r), "median": round(st.median(band_r), 4), "pos": round(pos(band_r), 3)},
                    "all": {"n": len(all_r), "median": round(st.median(all_r), 4), "pos": round(pos(all_r), 3)}}

    r0 = lambda v: round(v)
    return {
        "date": dates[-1], "smart": r0(smart[-1]), "dumb": r0(dumb[-1]), "spread": r0(cur),
        "prev": {"date": dates[-2], "smart": r0(smart[-2]), "dumb": r0(dumb[-2])},
        "comm_net_pct": round(net_c[-1] * 100, 1), "small_net_pct": round(net_s[-1] * 100, 1),
        "band": band, "window_weeks": WINDOW, "since": dates[0],
        "base_rate": base,
        "history": {"dates": dates, "smart": [r0(v) for v in smart], "dumb": [r0(v) for v in dumb]},
    }
