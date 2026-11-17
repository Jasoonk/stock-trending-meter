"""
AAII 散戶情緒調查（每週）—— 解析與統計

  資料：AAII Investor Sentiment Survey（每週四公布，問題是「未來 6 個月股市會漲、持平、跌？」）
  每週抓 https://www.aaii.com/sentimentsurvey/sent_results 的表格（近 ~20 週，免金鑰），
  歷史（1987 起）由 backfill_aaii.py 從 AAII 官方 sentiment.xls 一次性灌入 macro.db。

  日期一律正規化為「該週的週四」（AAII 官方 xls 的慣例；網頁表格標的是週三截止日）。
  情緒是「反向指標」的經典用法，但效果很吵 —— 所以這裡不下結論，只給：
    1. 本週讀數 vs 1987 年以來的長期平均與百分位
    2. 歷史上「同一個情緒區間」之後 S&P 500 26 週報酬，對照全部週的基準
"""

import re
import html
import bisect
import datetime
import statistics as st

SENT_URL = "https://www.aaii.com/sentimentsurvey/sent_results"

# 看多−看空利差的百分位區間（以 1987 年以來全部週為母體）
BANDS = [
    (10, "極度悲觀", "散戶看空程度在歷史最低 10%。傳統上視為反向偏多訊號，但常在下跌途中出現，不代表馬上見底。"),
    (25, "偏悲觀", "散戶比平常悲觀。"),
    (75, "中性", "散戶情緒落在歷史常態範圍內。"),
    (90, "偏樂觀", "散戶比平常樂觀。"),
    (100, "極度樂觀", "散戶看多程度在歷史最高 10%。傳統上視為反向偏空訊號，但多頭中可以持續很久。"),
]
FWD_WEEKS = 26          # 前瞻報酬：26 週 ≈ 6 個月（剛好是問卷問的期間）
HISTORY_WEEKS = 104     # 匯出給前端畫圖的週數（約兩年）

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def to_thursday(d):
    """把任一天正規化成同一 ISO 週的週四。"""
    return d + datetime.timedelta(days=3 - d.weekday())


def parse_sent_results(page, today):
    """解析 AAII 網頁表格 → [(thursday_iso, bull, neutral, bear)]（比例 0–1）。
    表格只有「Nov 11」這種月日，年份由「由新到舊、月份變大就跨年」推回去。"""
    i = page.find("Reported Date")
    if i < 0:
        return []
    tab = page[page.rfind("<table", 0, i): page.find("</table>", i)]
    out, year, prev_m = [], today.year, None
    for row in re.findall(r"<tr.*?</tr>", tab, re.S):
        cells = [html.unescape(re.sub(r"<.*?>", "", c)).strip()
                 for c in re.findall(r"<t[dh].*?</t[dh]>", row, re.S)]
        if len(cells) < 4:
            continue
        m = re.match(r"([A-Z][a-z]{2})\w*\.?\s+(\d{1,2})", cells[0])
        if not m or m.group(1) not in MONTHS:
            continue
        try:
            vals = [float(c.rstrip("%")) / 100 for c in cells[1:4]]
        except ValueError:
            continue
        mon, day = MONTHS[m.group(1)], int(m.group(2))
        if prev_m is None:
            if datetime.date(year, mon, day) > today + datetime.timedelta(days=1):
                year -= 1
        elif mon > prev_m:
            year -= 1
        prev_m = mon
        d = to_thursday(datetime.date(year, mon, day))
        out.append((d.isoformat(), *vals))
    return out


def _pct_rank(sorted_values, v):
    """v 在母體中的百分位（≤ v 的比例）。sorted_values 須已排序。"""
    return 100 * bisect.bisect_right(sorted_values, v) / len(sorted_values)


def _band(p):
    for hi, name, desc in BANDS:
        if p <= hi:
            return name, desc
    return BANDS[-1][1], BANDS[-1][2]


def summarize(rows, spx):
    """rows：[(date_iso, bull, neu, bear)] 由舊到新；spx：f(date_iso) → 該日或之前最近收盤。"""
    if len(rows) < 60:
        return None
    spreads = [b - r for _, b, _, r in rows]
    d, bull, neu, bear = rows[-1]
    sp = spreads[-1]
    ranked = sorted(spreads)
    p = _pct_rank(ranked, sp)
    band, band_desc = _band(p)

    # 連續幾週看空 > 看多（或反之）
    streak = 0
    for s in reversed(spreads):
        if (s < 0) == (sp < 0):
            streak += 1
        else:
            break

    # 歷史基準率：同一區間之後 26 週 S&P 500 報酬 vs 全部週
    lo_hi = {}
    prev = 0
    for hi, name, _ in BANDS:
        lo_hi[name] = (prev, hi)
        prev = hi
    base = None
    if spx:
        last_day = datetime.date.fromisoformat(d)
        all_r, band_r = [], []
        for (di, *_), s in zip(rows, spreads):
            d0 = datetime.date.fromisoformat(di)
            d1 = d0 + datetime.timedelta(weeks=FWD_WEEKS)
            if d1 > last_day:
                break
            p0, p1 = spx(d0.isoformat()), spx(d1.isoformat())
            if not p0 or not p1:
                continue
            r = p1 / p0 - 1
            all_r.append(r)
            lo, hi = lo_hi[band]
            if lo < _pct_rank(ranked, s) <= hi:
                band_r.append(r)
        if len(all_r) >= 100 and len(band_r) >= 20:
            pos = lambda xs: sum(1 for x in xs if x > 0) / len(xs)
            base = {
                "weeks": FWD_WEEKS,
                "band": {"n": len(band_r), "median": round(st.median(band_r), 4), "pos": round(pos(band_r), 3)},
                "all": {"n": len(all_r), "median": round(st.median(all_r), 4), "pos": round(pos(all_r), 3)},
            }

    r1 = lambda v: round(v * 100, 1)
    hist = rows[-HISTORY_WEEKS:]
    ma8 = st.mean(b for _, b, _, _ in rows[-8:])
    pd, pb, pn, pr = rows[-2]
    return {
        "date": d, "bull": r1(bull), "neutral": r1(neu), "bear": r1(bear), "spread": r1(sp),
        "prev": {"date": pd, "bull": r1(pb), "neutral": r1(pn), "bear": r1(pr), "spread": r1(pb - pr)},
        "bull_ma8": r1(ma8),
        "avg": {"bull": r1(st.mean(r[1] for r in rows)), "neutral": r1(st.mean(r[2] for r in rows)),
                "bear": r1(st.mean(r[3] for r in rows)), "spread": r1(st.mean(spreads))},
        "pct": round(p, 1), "band": band, "band_desc": band_desc,
        "streak": streak, "since": rows[0][0], "weeks_total": len(rows),
        "base_rate": base,
        "bands": [{"hi": hi, "name": n} for hi, n, _ in BANDS],
        "history": {"dates": [r[0] for r in hist], "bull": [r1(r[1]) for r in hist],
                    "bear": [r1(r[3]) for r in hist], "spread": [r1(r[1] - r[3]) for r in hist]},
    }
