#!/usr/bin/env python3
"""
總經流動性儀表板 —— 抓取器

  美國財政部官方每日殖利率曲線 → 2Y、10Y（同一來源，避免口徑差異污染利差）
  Yahoo chart API             → DXY（DX-Y.NYB）與驗證用代表資產
  CNN                         → 恐慌貪婪指數
  AAII                        → 每週散戶看多／看空調查（歷史由 backfill_aaii.py 灌入）
  Yahoo ^GSPC                 → 每個 AAII 週的 S&P 500 收盤（算各情緒區間之後的報酬）
        → macro.db（每日序列 + 象限歷史）
        → data.json（前端只負責顯示）

只用 Python 標準庫，GitHub Actions 免 pip install。

資料陷阱紀錄（勿回退）：
  - Yahoo 的 2YY=F（2 年期殖利率期貨）在本專案建置時數值連續數週不變，是無成交的
    死合約。不可用。2Y 一律取自財政部。
  - Yahoo 的 DX-Y.NYB 會出現標在週日、收盤為 None 的空 K 棒 → 丟棄。日期以 UTC
    日期為準（與 UUP 日變動相關 0.83，已驗證）。
"""

import os
import sys
import csv
import io
import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

import regime as rg
import aaii

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).parent
DB_PATH = ROOT / "macro.db"
OUT_PATH = ROOT / "data.json"

HISTORY_DAYS = 260          # 匯出給前端的交易日數（約一年）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/daily-treasury-rates.csv/{y}/all?"
                "type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d"
YF_LONG_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d"
SPX = "^GSPC"
FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FG_HEADERS = {
    "User-Agent": UA, "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cnn.com/markets/fear-and-greed", "Origin": "https://www.cnn.com",
}


def http_get(url, headers=None, timeout=30, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last = e
    raise last


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
def fetch_treasury(years):
    """回傳 {date_iso: (y2, y10)}，單位為百分比。"""
    out = {}
    for y in years:
        try:
            text = http_get(TREASURY_URL.format(y=y)).decode("utf-8-sig")
        except Exception as e:
            print(f"  ! 財政部 {y} 年資料抓取失敗：{e}")
            continue
        for row in csv.DictReader(io.StringIO(text)):
            try:
                d = datetime.datetime.strptime(row["Date"], "%m/%d/%Y").date().isoformat()
                y2, y10 = float(row["2 Yr"]), float(row["10 Yr"])
            except (KeyError, ValueError):
                continue
            out[d] = (y2, y10)
    return out


def fetch_yahoo(sym, since=None):
    """回傳 {date_iso: close}；丟棄 None 與週末 K 棒，日期取 UTC 日期。
    since=datetime.date 時改抓從該日起的完整日線（給 S&P 500 長歷史用）。"""
    if since:
        p1 = int(datetime.datetime(since.year, since.month, since.day, tzinfo=datetime.timezone.utc).timestamp())
        url = YF_LONG_URL.format(sym=urllib.parse.quote(sym), p1=p1,
                                 p2=int(datetime.datetime.now(datetime.timezone.utc).timestamp()))
    else:
        url = YF_URL.format(sym=urllib.parse.quote(sym))
    try:
        d = json.loads(http_get(url, {"User-Agent": UA, "Accept": "application/json"}))
        r = d["chart"]["result"][0]
    except Exception as e:
        print(f"  ! Yahoo {sym} 抓取失敗：{e}")
        return {}
    out = {}
    for t, c in zip(r.get("timestamp") or [], r["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        day = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date()
        if day.weekday() >= 5 and sym != "BTC-USD":
            continue
        out[day.isoformat()] = float(c)
    return out


def fetch_fear_greed():
    try:
        d = json.loads(http_get(FG_URL, FG_HEADERS, timeout=25))
        fg = d.get("fear_and_greed") or {}
        if "score" not in fg:
            return None
        hist = (d.get("fear_and_greed_historical") or {}).get("data", [])
        r1 = lambda v: round(float(v), 1) if v is not None else None
        # 每日一筆（同一天有收盤點與盤中即時點時取最後一筆），只留近 30 天給前端畫走勢
        by_day = {}
        for p in hist:
            day = datetime.datetime.fromtimestamp(float(p["x"]) / 1000, datetime.timezone.utc).date()
            by_day[day] = round(float(p["y"]), 1)
        if by_day:
            cut = max(by_day) - datetime.timedelta(days=30)
            by_day = {d: v for d, v in by_day.items() if d >= cut}
        return {
            "score": r1(fg["score"]), "rating": fg.get("rating"), "timestamp": fg.get("timestamp"),
            "previous_close": r1(fg.get("previous_close")), "previous_1_week": r1(fg.get("previous_1_week")),
            "previous_1_month": r1(fg.get("previous_1_month")), "previous_1_year": r1(fg.get("previous_1_year")),
            "history": [{"d": d.isoformat(), "y": v} for d, v in sorted(by_day.items())],
        }
    except Exception as e:
        print(f"  ! CNN 恐慌貪婪指數抓取失敗（略過）：{e}")
        return None


def fetch_aaii(today):
    try:
        page = http_get(aaii.SENT_URL, {"User-Agent": UA, "Accept": "text/html"}).decode("utf-8", "replace")
        rows = aaii.parse_sent_results(page, today)
        if not rows:
            why = ("被 AAII 的防爬機制擋下" if "Pardon Our Interruption" in page
                   else "網頁格式改變，解析不到表格")
            print(f"  ! AAII {why}（略過，保留資料庫既有資料）")
        return rows
    except Exception as e:
        print(f"  ! AAII 抓取失敗（略過，保留資料庫既有資料）：{e}")
        return []


# --------------------------------------------------------------------------- #
# 資料庫
# --------------------------------------------------------------------------- #
def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS macro_daily (
            date TEXT PRIMARY KEY, dxy REAL, y2 REAL, y10 REAL
        );
        CREATE TABLE IF NOT EXISTS asset_daily (
            symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL,
            PRIMARY KEY (symbol, date)
        );
        CREATE TABLE IF NOT EXISTS regime_history (
            date TEXT NOT NULL, lookback INTEGER NOT NULL,
            quadrant TEXT NOT NULL, clear INTEGER, typical INTEGER,
            d_dxy_pct REAL, d2_bp REAL, d10_bp REAL, d_spread_bp REAL,
            param_version TEXT NOT NULL,
            PRIMARY KEY (date, lookback)
        );
        CREATE TABLE IF NOT EXISTS market_indicators (
            indicator TEXT NOT NULL, timestamp TEXT NOT NULL, score REAL, rating TEXT,
            UNIQUE(indicator, timestamp)
        );
        CREATE TABLE IF NOT EXISTS aaii_weekly (
            date TEXT PRIMARY KEY,          -- 該週週四
            bullish REAL, neutral REAL, bearish REAL,   -- 比例 0–1
            source TEXT,                    -- xls（官方歷史檔）或 web（每週網頁）
            spx_close REAL                  -- 該週四（或之前最近交易日）S&P 500 收盤
        );
        CREATE TABLE IF NOT EXISTS param_versions (
            version TEXT PRIMARY KEY, config_json TEXT NOT NULL, first_used TEXT NOT NULL
        );
    """)
    conn.commit()


def ffill_lookup(series):
    """回傳函式 f(date) → 該日或之前最近一筆值（處理假日錯開）。"""
    keys = sorted(series)

    def f(day):
        lo, hi = 0, len(keys) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if keys[mid] <= day:
                best = keys[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return series[best] if best else None
    return f


# --------------------------------------------------------------------------- #
# 資料來源健康檢查：抓失敗不中斷（保留舊資料），但要讓人看得到
#   - GitHub Actions：輸出 ::warning::（執行頁面出現黃色警告）＋ 執行摘要表格
#   - data.json 的 health → 儀表板頂端黃色橫幅
# --------------------------------------------------------------------------- #
def days_old(latest, today):
    return (today - datetime.date.fromisoformat(latest)).days if latest else None


def report_health(health):
    warns = [h for h in health if not h["ok"]]
    if os.environ.get("GITHUB_ACTIONS"):
        for h in warns:
            print(f"::warning title=資料來源異常：{h['label']}::{h['msg']}")
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write("### 資料來源狀態\n\n| 來源 | 狀態 | 最新資料 | 說明 |\n|---|---|---|---|\n")
                for h in health:
                    f.write(f"| {h['label']} | {'✅' if h['ok'] else '⚠️'} | {h['latest'] or '—'} | {h['msg']} |\n")
    if warns:
        print(f"⚠ {len(warns)} 個資料來源異常：" + "；".join(f"{h['label']}（{h['msg']}）" for h in warns))
    else:
        print("✓ 所有資料來源正常")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    today = datetime.datetime.now(datetime.timezone.utc).date()
    print(f"抓取日期：{today.isoformat()}（UTC）")

    tsy = fetch_treasury([today.year - 1, today.year])
    print(f"  · 財政部殖利率：{len(tsy)} 個交易日")
    dxy = fetch_yahoo("DX-Y.NYB")
    print(f"  · DXY：{len(dxy)} 筆")
    assets = {}
    for sym, _ in rg.ASSETS:
        assets[sym] = fetch_yahoo(sym)
    print(f"  · 驗證資產：{sum(1 for v in assets.values() if v)}/{len(rg.ASSETS)} 檔取得")
    fg = fetch_fear_greed()
    if fg:
        print(f"  · CNN 恐慌貪婪指數：{fg['score']}（{fg['rating']}）")
    aaii_rows = fetch_aaii(today)
    if aaii_rows:
        print(f"  · AAII 網頁：{len(aaii_rows)} 週（最新 {aaii_rows[0][0]}）")

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        # 存入新抓到的資料（抓失敗時保留舊資料，不覆蓋）
        dxy_f = ffill_lookup(dxy) if dxy else None
        # DXY 這次沒抓到時保留資料庫原值（不可用 None 蓋掉）
        for d, (y2, y10) in tsy.items():
            conn.execute("INSERT INTO macro_daily (date,dxy,y2,y10) VALUES (?,?,?,?) "
                         "ON CONFLICT(date) DO UPDATE SET y2=excluded.y2, y10=excluded.y10, "
                         "dxy=COALESCE(excluded.dxy, macro_daily.dxy)",
                         (d, dxy_f(d) if dxy_f else None, y2, y10))
        for sym, ser in assets.items():
            conn.executemany("INSERT OR REPLACE INTO asset_daily (symbol,date,close) VALUES (?,?,?)",
                             [(sym, d, c) for d, c in ser.items()])
        # AAII：已有的週（官方 xls 精度較高）不覆蓋，只補新週
        conn.executemany("INSERT OR IGNORE INTO aaii_weekly (date,bullish,neutral,bearish,source) "
                         "VALUES (?,?,?,?,'web')", aaii_rows)
        # S&P 500：只存每個 AAII 週的收盤。近兩年每次重算（週四早上抓到新週時當天還沒收盤，
        # 先用前一日補，隔天自動更正）；更早的由 backfill_aaii.py 填好。
        spx = fetch_yahoo(SPX)
        if spx:
            f, first = ffill_lookup(spx), min(spx)
            conn.executemany("UPDATE aaii_weekly SET spx_close=? WHERE date=?",
                             [(f(d), d) for (d,) in conn.execute(
                                 "SELECT date FROM aaii_weekly WHERE date>=?", (first,)).fetchall()])
        if fg:
            conn.execute("INSERT OR REPLACE INTO market_indicators (indicator,timestamp,score,rating) "
                         "VALUES (?,?,?,?)", ("cnn_fear_greed", today.isoformat(), fg["score"], fg["rating"]))
        conn.commit()

        # 健康檢查：這次有沒有抓到 + 資料庫裡最新一筆有多舊
        q1 = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
        tsy_latest = q1("SELECT MAX(date) FROM macro_daily WHERE y2 IS NOT NULL")
        aaii_latest = q1("SELECT MAX(date) FROM aaii_weekly")
        got = sum(1 for v in assets.values() if v)
        health = []

        def chk(key, label, fetched, latest, max_age, fail_msg):
            age = days_old(latest, today)
            if not fetched:
                ok, msg = False, fail_msg + (f"，沿用 {latest} 的舊資料" if latest else "")
            elif age is not None and age > max_age:
                ok, msg = False, f"有連線但最新資料停在 {latest}（{age} 天前）"
            else:
                ok, msg = True, "正常"
            health.append({"key": key, "label": label, "ok": ok, "latest": latest, "msg": msg})

        chk("treasury", "美國財政部 2Y／10Y", bool(tsy), tsy_latest, 5, "抓取失敗（可能被擋）")
        chk("dxy", "DXY（Yahoo）", bool(dxy), max(dxy) if dxy else None, 5, "抓取失敗（可能被擋）")
        chk("assets", "驗證資產（Yahoo）", got == len(rg.ASSETS),
            max((max(v) for v in assets.values() if v), default=None), 5,
            f"只取得 {got}/{len(rg.ASSETS)} 檔")
        chk("cnn", "CNN 恐慌貪婪", fg is not None, today.isoformat() if fg else None, 5,
            "抓取失敗（可能被擋），本次頁面不顯示")
        chk("aaii", "AAII 散戶情緒", bool(aaii_rows), aaii_latest, 10, "抓取失敗（可能被擋）")
        chk("spx", "S&P 500（Yahoo）", bool(spx), max(spx) if spx else None, 5, "抓取失敗（可能被擋）")
        report_health(health)

        rows = conn.execute("SELECT date,dxy,y2,y10 FROM macro_daily "
                            "WHERE dxy IS NOT NULL AND y2 IS NOT NULL AND y10 IS NOT NULL "
                            "ORDER BY date").fetchall()
        if len(rows) < 80:
            print(f"資料不足（{len(rows)} 個交易日），中止，不覆蓋既有 data.json。")
            return 1

        asset_ser = {}
        for sym, _ in rg.ASSETS:
            ser = dict(conn.execute("SELECT date,close FROM asset_daily WHERE symbol=?", (sym,)).fetchall())
            asset_ser[sym] = ffill_lookup(ser) if ser else None

        ver = rg.param_version()
        conn.execute("INSERT OR IGNORE INTO param_versions (version,config_json,first_used) VALUES (?,?,?)",
                     (ver, json.dumps({"PARAMS": rg.PARAMS}, ensure_ascii=False), today.isoformat()))

        aaii_hist = conn.execute("SELECT date,bullish,neutral,bearish FROM aaii_weekly "
                                 "ORDER BY date").fetchall()
        spx_wk = dict(conn.execute("SELECT date,spx_close FROM aaii_weekly "
                                   "WHERE spx_close IS NOT NULL").fetchall())
        sentiment = aaii.summarize(aaii_hist, ffill_lookup(spx_wk) if spx_wk else None)

        dates = [r[0] for r in rows]
        dxys = [r[1] for r in rows]
        y2s = [r[2] for r in rows]
        y10s = [r[3] for r in rows]
        start = max(0, len(dates) - HISTORY_DAYS)

        regimes, current, episodes = {}, {}, {}
        for lb_s in rg.PARAMS["LOOKBACKS"]:
            lb = int(lb_s)
            seq = []
            for i in range(start, len(dates)):
                if i - lb < 0:
                    seq.append(None)
                    continue
                c = rg.classify(dxys[i], dxys[i - lb], y2s[i], y2s[i - lb], y10s[i], y10s[i - lb], lb)
                seq.append(c)
                conn.execute(
                    "INSERT OR REPLACE INTO regime_history (date,lookback,quadrant,clear,typical,"
                    "d_dxy_pct,d2_bp,d10_bp,d_spread_bp,param_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (dates[i], lb, c["q"], int(c["clear"]), int(c["typical"]), c["d_dxy_pct"],
                     c["d2_bp"], c["d10_bp"], c["d_spread_bp"], ver))
            regimes[lb_s] = [{"q": c["q"], "c": int(c["clear"]), "t": int(c["typical"])} if c else None
                             for c in seq]

            # 目前象限：持續天數（同象限連續的交易日）
            cur = seq[-1]
            spell = 0
            for c in reversed(seq):
                if c and c["q"] == cur["q"]:
                    spell += 1
                else:
                    break
            spell_start = dates[len(dates) - spell]

            # 劇本吻合度：回看期間各資產報酬
            rets = {}
            for sym, _ in rg.ASSETS:
                f = asset_ser.get(sym)
                if not f:
                    continue
                now_p, then_p = f(dates[-1]), f(dates[-1 - lb])
                if now_p and then_p:
                    rets[sym] = round(now_p / then_p - 1, 5)
            fit = rg.fit_check(cur["q"], rets)
            current[lb_s] = {**cur, "spell_days": spell, "spell_start": spell_start,
                             "returns": rets, "fit": fit,
                             "ref_date": dates[-1 - lb]}

            # Q3（流動性釋放）在視窗內出現過的區段
            eps, run = [], None
            for i, c in enumerate(seq):
                is_q3 = bool(c and c["q"] == "Q3" and c["clear"])
                if is_q3 and run is None:
                    run = [start + i, start + i]
                elif is_q3:
                    run[1] = start + i
                elif run:
                    eps.append(run)
                    run = None
            if run:
                eps.append(run)
            episodes[lb_s] = [{"start": dates[a], "end": dates[b], "days": b - a + 1,
                               "ongoing": b == len(dates) - 1} for a, b in eps]
        conn.commit()
    finally:
        conn.close()

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "param_version": ver, "params": rg.PARAMS,
        "sources": {"yields": "U.S. Treasury Daily Par Yield Curve", "dxy": "Yahoo DX-Y.NYB",
                    "assets": "Yahoo", "sentiment": "CNN Fear & Greed",
                    "aaii": "AAII Investor Sentiment Survey", "spx": "Yahoo ^GSPC"},
        "dates": dates[start:],
        "dxy": [round(v, 3) for v in dxys[start:]],
        "y2": y2s[start:], "y10": y10s[start:],
        "spread_bp": [round((b - a) * 100, 1) for a, b in zip(y2s[start:], y10s[start:])],
        "regimes": regimes, "current": current, "q3_episodes": episodes,
        "quadrants": rg.QUADRANTS,
        "assets": [{"symbol": s, "label": l} for s, l in rg.ASSETS],
        "market": {"fear_greed": fg} if fg else {},
        "aaii": sentiment,
        "health": health,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    for lb_s, c in current.items():
        q = rg.QUADRANTS[c["q"]]
        fit = c["fit"]
        print(f"  · {lb_s} 日視角：{c['q']} {q['name']}"
              f"{'' if c['clear'] else '（不明確）'}{'' if c['typical'] else '（非典型）'}"
              f" | DXY {c['d_dxy_pct']:+.2f}% 2Y {c['d2_bp']:+.0f}bp 10Y {c['d10_bp']:+.0f}bp"
              f" 利差 {c['d_spread_bp']:+.0f}bp | 已 {c['spell_days']} 天"
              + (f" | 劇本吻合 {fit['hits']}/{fit['total']}" if fit else ""))
    if sentiment:
        s = sentiment
        print(f"  · AAII {s['date']}：看多 {s['bull']}% 中立 {s['neutral']}% 看空 {s['bear']}%"
              f" | 利差 {s['spread']:+.1f}（第 {s['pct']:.0f} 百分位，{s['band']}）")
    print(f"完成：{len(dates)} 個交易日 → data.json（顯示近 {len(dates) - start} 日，參數版本 {ver}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
