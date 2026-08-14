#!/usr/bin/env python3
"""
回填歷史階段標籤

`stage_history` 是 P3 回測的原料，但它從今天才開始記錄；而 snapshots / prices
已經累積了一段歷史。本腳本用**同一個** classify.py 把過去每一天的標籤補算回去，
讓已累積的資料不被浪費。

重建方式刻意與 fetch.py 的 export_json 一致：
  - 對每個歷史日 D，取 D 往前 EXPORT_DAYS 天的視窗
  - 以「D 當日加權提及」取前 TOP_TICKERS 檔作為當日宇宙（與當時匯出邏輯相同）
  - 價格前值補齊
  - 呼叫 classify.classify_all

⚠️ 這是「以今日程式碼重建歷史標籤」，不是當時真的產出的標籤。對回測而言可接受
   （規則本身不含未來函數），但必須知道它與「當下即時產生」在概念上有別。
   stage_since 由回填序列本身推導。

用法：
    python backfill_stages.py            # 回填所有可算的日期（跳過已存在的）
    python backfill_stages.py --force    # 重算並覆蓋既有標籤
"""

import sys
import sqlite3
import datetime
import json
from pathlib import Path

import classify as clf

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).parent
DB_PATH = ROOT / "attention.db"

EXPORT_DAYS = 30
TOP_TICKERS = 60
SOURCES = ["r/wallstreetbets", "r/stocks", "r/options"]


def build_price_array(sorted_closes, dates):
    arr, last, j = [], None, 0
    for d in dates:
        while j < len(sorted_closes) and sorted_closes[j][0] <= d:
            last = sorted_closes[j][1]
            j += 1
        arr.append(last)
    return arr if all(v is not None for v in arr) else None


def main():
    force = "--force" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    all_dates = [r[0] for r in cur.execute(
        "SELECT DISTINCT timestamp FROM snapshots ORDER BY timestamp").fetchall()]
    if not all_dates:
        print("snapshots 是空的，沒有可回填的資料。")
        return 1

    done = {r[0] for r in cur.execute("SELECT DISTINCT date FROM stage_history").fetchall()}
    targets = [d for d in all_dates if force or d not in done]
    if not targets:
        print(f"所有 {len(all_dates)} 天皆已有標籤，無需回填（--force 可重算）。")
        return 0

    ver = clf.param_version()
    cur.execute(
        "INSERT OR IGNORE INTO param_versions (version, config_json, first_used) VALUES (?,?,?)",
        (ver, json.dumps(clf.PARAMS, sort_keys=True, ensure_ascii=False), targets[0]))

    # 預先載入全部快照與價格，避免逐日查詢
    snap = {}   # (ticker, source, date) -> mentions
    for tk, src, ts, mc in cur.execute(
            "SELECT ticker, source, timestamp, mention_count FROM snapshots"):
        snap[(tk, src, ts)] = mc
    prices = {}  # ticker -> [(date, close)]
    for tk, dt, cl in cur.execute("SELECT ticker, date, close FROM prices ORDER BY date"):
        prices.setdefault(tk, []).append((dt, cl))

    tickers_all = sorted({k[0] for k in snap})
    prev_stage = {}   # ticker -> (stage, stage_since)，跨日延續
    written = 0

    for day in targets:
        d_end = datetime.date.fromisoformat(day)
        dates = [(d_end - datetime.timedelta(days=EXPORT_DAYS - 1 - i)).isoformat()
                 for i in range(EXPORT_DAYS)]
        dset = {d: i for i, d in enumerate(dates)}

        # 組出每檔的各來源序列
        payload = []
        day_score = {}
        for tk in tickers_all:
            sources, total_today, seen = {}, 0, False
            for src in SOURCES:
                arr = [0] * EXPORT_DAYS
                for d, i in dset.items():
                    v = snap.get((tk, src, d))
                    if v:
                        arr[i] = v
                        seen = True
                sources[src] = arr
                total_today += arr[EXPORT_DAYS - 1] * clf.PARAMS["SOURCE_WEIGHTS"].get(src, 1)
            if not seen:
                continue
            day_score[tk] = total_today
            payload.append({"ticker": tk, "sources": sources,
                            "price": build_price_array(prices.get(tk, []), dates)})

        if not payload:
            continue
        # 當日宇宙：依當日加權提及取前 N（與 export_json 的 kept 邏輯對應）
        payload.sort(key=lambda t: day_score.get(t["ticker"], 0), reverse=True)
        payload = payload[:TOP_TICKERS]

        stages = clf.classify_all(payload, EXPORT_DAYS)
        for tk, d in stages.items():
            pst, psince = prev_stage.get(tk, (None, None))
            since = psince if (pst == d["stage"] and psince) else day
            prev_stage[tk] = (d["stage"], since)
            cur.execute(
                "INSERT OR REPLACE INTO stage_history (ticker, date, stage, stage_since, "
                "week_change, jump_x, rank, days_at_high, mentions, price, hot, "
                "observed_days, param_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tk, day, d["stage"], since, d["week_change"], d["jump_x"], d["rank"],
                 d["days_at_high"], d["mentions"], d["price"], int(bool(d["hot"])),
                 d["observed_days"], ver))
            written += 1
        print(f"  · {day}：{len(stages)} 檔")

    conn.commit()
    total = cur.execute("SELECT COUNT(*) FROM stage_history").fetchone()[0]
    conn.close()
    print(f"回填完成：本次寫入 {written} 筆，stage_history 共 {total} 筆（參數版本 {ver}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
