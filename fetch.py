#!/usr/bin/env python3
"""
注意力生命週期儀表板 — ApeWisdom 抓取器（P1 MVP）

流程（依專案計畫 §5 / §6）：
    ApeWisdom API（免金鑰）
        → 正規化成 {ticker, source, timestamp, mention_count, rank}
        → 寫入 SQLite（每日快照，累積歷史 = 未來能回測）
        → 匯出 data.json（近 N 天視窗）給前端 index.html 讀取

- 只用 Python 標準庫（urllib / sqlite3 / json），無第三方相依，GitHub Actions 免 pip install。
- 冪等：同一天重跑會覆蓋當天快照，不會重複累加。
- 分類（四階段）留在前端做，避免與前端邏輯重複而漂移；這裡只負責抓取與累積。
"""

import sys
import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

import classify as clf

# Windows 主控台預設 cp1252 印不出中文；強制 UTF-8（CI 的 Ubuntu 本就 UTF-8）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).parent
DB_PATH = ROOT / "attention.db"
OUT_PATH = ROOT / "data.json"

# ApeWisdom filter（子版）→ 前端顯示的來源標籤。多來源會並存於同一張 snapshots 表。
SOURCES = {
    "wallstreetbets": "r/wallstreetbets",
    "stocks": "r/stocks",
    "options": "r/options",
}

TOP_TICKERS = 60      # 匯出給前端時保留的熱度前 N 名
EXPORT_DAYS = 30      # 匯出視窗天數
API = "https://apewisdom.io/api/v1.0/filter/{flt}/page/1"
UA = "attention-lifecycle-dashboard/1.0 (personal research; +https://github.com)"

# CNN 恐慌貪婪指數（非官方端點，需完整瀏覽器標頭；失敗時整體不中斷，僅略過此區塊）
FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cnn.com/markets/fear-and-greed",
    "Origin": "https://www.cnn.com",
}
FG_HISTORY_DAYS = 30

# 股價（P2 背離旗標）：Yahoo chart API（免金鑰、免第三方套件）
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2mo&interval=1d"
YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}
PRICE_TARGETS = 60    # 只抓熱度前 N 檔的股價（省請求）


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
def fetch_filter(flt, retries=3):
    """抓某個 ApeWisdom filter 的第 1 頁（熱度前 100）。回傳 results list。"""
    url = API.format(flt=flt)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            return data.get("results", [])
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
    print(f"  ! {flt} 抓取失敗（已重試 {retries} 次）：{last_err}")
    return []


def _r(v):
    return round(float(v), 1) if v is not None else None


def fetch_fear_greed(retries=2):
    """CNN 恐慌貪婪指數。回傳 dict 或 None（失敗不影響主流程）。"""
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(FG_URL, headers=FG_HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                d = json.load(resp)
            fg = d.get("fear_and_greed") or {}
            if "score" not in fg:
                return None
            hist = (d.get("fear_and_greed_historical") or {}).get("data", [])
            hist_slim = [{"t": int(p["x"]), "y": round(float(p["y"]), 1)}
                         for p in hist[-FG_HISTORY_DAYS:]]
            return {
                "score": _r(fg.get("score")),
                "rating": fg.get("rating"),
                "timestamp": fg.get("timestamp"),
                "previous_close": _r(fg.get("previous_close")),
                "previous_1_week": _r(fg.get("previous_1_week")),
                "previous_1_month": _r(fg.get("previous_1_month")),
                "previous_1_year": _r(fg.get("previous_1_year")),
                "history": hist_slim,
            }
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ValueError, KeyError) as e:
            last = e
    print(f"  ! CNN 恐慌貪婪指數抓取失敗（略過此區塊）：{last}")
    return None


def yahoo_daily(ticker, retries=2):
    """抓某檔近 2 個月的每日收盤。回傳 {date_iso: close} 或 None。"""
    url = YF_URL.format(sym=urllib.parse.quote(ticker))
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=YF_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                d = json.load(resp)
            res = (d.get("chart") or {}).get("result")
            if not res:
                return None
            r = res[0]
            ts = r.get("timestamp") or []
            quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            out = {}
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                dt = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat()
                out[dt] = round(float(c), 2)
            return out or None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ValueError, KeyError):
            continue
    return None


def fetch_prices(tickers):
    """對一組 ticker 抓每日收盤。回傳 {ticker: {date: close}}；抓不到的略過（前端顯示 null）。"""
    out, ok = {}, 0
    for t in tickers:
        closes = yahoo_daily(t)
        if closes:
            out[t] = closes
            ok += 1
    print(f"  · 股價：{ok}/{len(tickers)} 檔取得")
    return out


# --------------------------------------------------------------------------- #
# 資料庫（schema 依計畫 §6）
# --------------------------------------------------------------------------- #
def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker        TEXT    NOT NULL,
            source        TEXT    NOT NULL,
            timestamp     TEXT    NOT NULL,   -- ISO 日期（每日快照）
            mention_count INTEGER NOT NULL,
            rank          INTEGER,
            UNIQUE(ticker, source, timestamp)
        );
        CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(timestamp);
        CREATE INDEX IF NOT EXISTS idx_snap_ticker ON snapshots(ticker);

        CREATE TABLE IF NOT EXISTS tickers (
            ticker        TEXT PRIMARY KEY,
            name          TEXT,
            first_seen    TEXT,
            current_stage TEXT,   -- 保留欄位；目前分類在前端做
            stage_since   TEXT
        );

        CREATE TABLE IF NOT EXISTS market_indicators (
            indicator TEXT NOT NULL,
            timestamp TEXT NOT NULL,   -- ISO 日期（每日快照）
            score     REAL,
            rating    TEXT,
            UNIQUE(indicator, timestamp)
        );

        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,      -- ISO 交易日
            close  REAL,
            UNIQUE(ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker);

        -- 每日凍結的階段標籤（P3 回測的原料）。沒有這張表就無法驗證 edge。
        CREATE TABLE IF NOT EXISTS stage_history (
            ticker        TEXT NOT NULL,
            date          TEXT NOT NULL,
            stage         TEXT NOT NULL,
            stage_since   TEXT,        -- 進入目前階段的日期
            week_change   REAL,
            jump_x        REAL,
            rank          INTEGER,
            days_at_high  INTEGER,
            mentions      REAL,        -- 當日加權注意力分數
            price         REAL,        -- 標記當下收盤（用來算前瞻報酬）
            hot           INTEGER,     -- 過熱背離旗標
            observed_days INTEGER,
            param_version TEXT NOT NULL,
            UNIQUE(ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_stage_date ON stage_history(date);
        CREATE INDEX IF NOT EXISTS idx_stage_ticker ON stage_history(ticker);
        CREATE INDEX IF NOT EXISTS idx_stage_stage ON stage_history(stage);

        -- 參數版本：門檻一改就換版本，回測時必須依此分段
        CREATE TABLE IF NOT EXISTS param_versions (
            version     TEXT PRIMARY KEY,
            config_json TEXT NOT NULL,
            first_used  TEXT NOT NULL
        );
        """
    )
    conn.commit()


def store_snapshots(conn, rows_today, rows_yesterday, today, yesterday):
    cur = conn.cursor()
    # 今天：覆蓋（同日重跑用最新值）
    for r in rows_today:
        cur.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(ticker, source, timestamp, mention_count, rank) VALUES (?,?,?,?,?)",
            (r["ticker"], r["source"], today, r["mentions"], r["rank"]),
        )
    # 昨天：僅在缺漏時補（首次啟動用 *_24h_ago 種一個點，不覆蓋真實快照）
    for r in rows_yesterday:
        cur.execute(
            "INSERT OR IGNORE INTO snapshots "
            "(ticker, source, timestamp, mention_count, rank) VALUES (?,?,?,?,?)",
            (r["ticker"], r["source"], yesterday, r["mentions"], r["rank"]),
        )
    conn.commit()


def upsert_tickers(conn, names, today):
    cur = conn.cursor()
    for ticker, name in names.items():
        cur.execute("SELECT ticker FROM tickers WHERE ticker=?", (ticker,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO tickers (ticker, name, first_seen) VALUES (?,?,?)",
                (ticker, name, today),
            )
        else:
            cur.execute("UPDATE tickers SET name=? WHERE ticker=?", (name, ticker))
    conn.commit()


# --------------------------------------------------------------------------- #
# 匯出 data.json
# --------------------------------------------------------------------------- #
def build_price_array(sorted_closes, dates):
    """把 (date,close) 依 dates 前值補齊；缺口用最近一筆收盤延續。全空或有缺口回 None。"""
    arr, last, j = [], None, 0
    for d in dates:
        while j < len(sorted_closes) and sorted_closes[j][0] <= d:
            last = sorted_closes[j][1]
            j += 1
        arr.append(last)
    return arr if all(v is not None for v in arr) else None


def store_stages(conn, stages, today_s):
    """
    把當日階段標籤凍結進 stage_history，並記錄參數版本。
    stage_since：沿用昨日標籤，階段不變則延續、改變則設為今天。
    """
    ver = clf.param_version()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO param_versions (version, config_json, first_used) VALUES (?,?,?)",
        (ver, json.dumps(clf.PARAMS, sort_keys=True, ensure_ascii=False), today_s),
    )

    # 取每檔最近一次（今天以前）的標籤，用來延續 stage_since
    prev = {}
    cur.execute(
        "SELECT ticker, stage, stage_since FROM stage_history WHERE date = "
        "(SELECT MAX(date) FROM stage_history WHERE date < ?)", (today_s,)
    )
    for tk, st, since in cur.fetchall():
        prev[tk] = (st, since)

    changed = 0
    for tk, d in stages.items():
        pst, psince = prev.get(tk, (None, None))
        if pst == d["stage"] and psince:
            since = psince
        else:
            since = today_s
            if pst is not None:
                changed += 1
        cur.execute(
            "INSERT OR REPLACE INTO stage_history (ticker, date, stage, stage_since, "
            "week_change, jump_x, rank, days_at_high, mentions, price, hot, "
            "observed_days, param_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tk, today_s, d["stage"], since, d["week_change"], d["jump_x"], d["rank"],
             d["days_at_high"], d["mentions"], d["price"], int(bool(d["hot"])),
             d["observed_days"], ver),
        )
        # 同步 tickers 表（原本閒置的欄位，現在有真實用途）
        cur.execute("UPDATE tickers SET current_stage=?, stage_since=? WHERE ticker=?",
                    (d["stage"], since, tk))
    conn.commit()
    return ver, changed


def export_json(conn, today, fg=None):
    source_labels = list(SOURCES.values())
    dates = [
        (today - datetime.timedelta(days=EXPORT_DAYS - 1 - i)).isoformat()
        for i in range(EXPORT_DAYS)
    ]
    date_idx = {d: i for i, d in enumerate(dates)}

    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, source, timestamp, mention_count FROM snapshots "
        "WHERE timestamp >= ?",
        (dates[0],),
    )
    # cell[(ticker, source)] = [每日提及...]
    cell = {}
    for ticker, source, ts, mentions in cur.fetchall():
        if ts not in date_idx:
            continue
        key = (ticker, source)
        if key not in cell:
            cell[key] = [0] * EXPORT_DAYS
        cell[key][date_idx[ts]] = mentions

    # 每檔的 total（跨來源加總）
    tickers_in_window = sorted({t for (t, _s) in cell})
    totals = {}
    for t in tickers_in_window:
        arr = [0] * EXPORT_DAYS
        for lbl in source_labels:
            src = cell.get((t, lbl))
            if src:
                arr = [a + b for a, b in zip(arr, src)]
        totals[t] = arr

    # 取最近一天 total 最高的前 N 檔（且視窗內至少出現過）
    def latest_total(t):
        return totals[t][-1]
    kept = [t for t in tickers_in_window if sum(totals[t]) > 0]
    kept.sort(key=latest_total, reverse=True)
    kept = kept[:TOP_TICKERS]

    # 每日整體排名（依當日 total 由高到低）
    rank_by_day = []
    for d in range(EXPORT_DAYS):
        order = sorted(kept, key=lambda t: totals[t][d], reverse=True)
        rmap = {}
        r = 0
        for t in order:
            if totals[t][d] > 0:
                r += 1
                rmap[t] = r
            else:
                rmap[t] = None
        rank_by_day.append(rmap)

    # ticker 名稱與 first_seen
    meta = {}
    cur.execute("SELECT ticker, name, first_seen FROM tickers")
    for ticker, name, first_seen in cur.fetchall():
        meta[ticker] = (name, first_seen)

    # 股價：讀 prices 表、對齊 dates（前值補齊）
    price_series = {}
    if kept:
        ph = ",".join("?" * len(kept))
        cur.execute(
            f"SELECT ticker, date, close FROM prices WHERE ticker IN ({ph}) ORDER BY date",
            kept,
        )
        raw = {}
        for tk, dt, cl in cur.fetchall():
            raw.setdefault(tk, []).append((dt, cl))
        for tk, lst in raw.items():
            price_series[tk] = build_price_array(lst, dates)

    out_tickers = []
    for t in kept:
        name, first_seen = meta.get(t, (t, None))
        out_tickers.append(
            {
                "ticker": t,
                "name": name or t,
                "first_seen": first_seen,
                "total": totals[t],
                "rank": [rank_by_day[d].get(t) for d in range(EXPORT_DAYS)],
                "sources": {
                    lbl: cell.get((t, lbl), [0] * EXPORT_DAYS) for lbl in source_labels
                },
                "price": price_series.get(t),  # 每日收盤（對齊 dates）或 None
            }
        )

    # 階段分類（Python 為單一真相）→ 注入 data.json 供前端顯示，並回傳供凍結
    stages = clf.classify_all(out_tickers, EXPORT_DAYS)
    for t in out_tickers:
        s = stages.get(t["ticker"])
        if s:
            t["stage"] = s["stage"]
            t["hot"] = bool(s["hot"])

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_mode": "apewisdom",
        "window_days": EXPORT_DAYS,
        "dates": dates,
        "sources": source_labels,
        "tickers": out_tickers,
        "market": {"fear_greed": fg} if fg else {},
        "has_price": any(price_series.get(t) for t in kept),
        "param_version": clf.param_version(),
        "params": clf.PARAMS,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(out_tickers), stages


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    today = datetime.datetime.now(datetime.timezone.utc).date()
    yesterday = today - datetime.timedelta(days=1)
    today_s, yesterday_s = today.isoformat(), yesterday.isoformat()

    print(f"抓取日期：{today_s}（UTC）")
    rows_today, rows_yesterday, names = [], [], {}

    for flt, label in SOURCES.items():
        results = fetch_filter(flt)
        print(f"  · {label:20} 取得 {len(results)} 檔")
        for r in results:
            ticker = r.get("ticker")
            if not ticker:
                continue
            names.setdefault(ticker, r.get("name") or ticker)
            rows_today.append(
                {"ticker": ticker, "source": label,
                 "mentions": int(r.get("mentions") or 0), "rank": r.get("rank")}
            )
            # 首次啟動的昨日種子點
            m24 = r.get("mentions_24h_ago")
            if m24 is not None:
                rows_yesterday.append(
                    {"ticker": ticker, "source": label,
                     "mentions": int(m24), "rank": r.get("rank_24h_ago")}
                )

    if not rows_today:
        print("沒有抓到任何資料，中止（不覆蓋既有 data.json）。")
        return 1

    fg = fetch_fear_greed()
    if fg:
        print(f"  · CNN 恐慌貪婪指數：{fg['score']}（{fg['rating']}）")

    # 股價：只抓今日熱度前 N 檔（省請求）
    today_totals = {}
    for r in rows_today:
        today_totals[r["ticker"]] = today_totals.get(r["ticker"], 0) + r["mentions"]
    price_targets = sorted(today_totals, key=today_totals.get, reverse=True)[:PRICE_TARGETS]
    prices = fetch_prices(price_targets)

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        upsert_tickers(conn, names, today_s)
        store_snapshots(conn, rows_today, rows_yesterday, today_s, yesterday_s)
        for tk, closes in prices.items():
            for dt, cl in closes.items():
                conn.execute(
                    "INSERT OR REPLACE INTO prices (ticker, date, close) VALUES (?,?,?)",
                    (tk, dt, cl),
                )
        conn.commit()
        if fg:
            conn.execute(
                "INSERT OR REPLACE INTO market_indicators "
                "(indicator, timestamp, score, rating) VALUES (?,?,?,?)",
                ("cnn_fear_greed", today_s, fg["score"], fg["rating"]),
            )
            conn.commit()
        n, stages = export_json(conn, today, fg)
        ver, changed = store_stages(conn, stages, today_s)
        cur = conn.execute("SELECT COUNT(*) FROM snapshots")
        total_rows = cur.fetchone()[0]
        sh_rows = conn.execute("SELECT COUNT(*) FROM stage_history").fetchone()[0]
        dist = conn.execute(
            "SELECT stage, COUNT(*) FROM stage_history WHERE date=? GROUP BY stage "
            "ORDER BY 2 DESC", (today_s,)).fetchall()
    finally:
        conn.close()

    print(f"  · 階段標籤已凍結（參數版本 {ver}，{changed} 檔階段變動）："
          + "、".join(f"{s} {c}" for s, c in dist))
    print(f"完成：snapshots {total_rows} 筆 / stage_history {sh_rows} 筆 → 匯出 data.json（{n} 檔）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
