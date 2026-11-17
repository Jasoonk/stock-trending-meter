#!/usr/bin/env python3
"""
總經流動性儀表板 —— 抓取器

  美國財政部官方每日殖利率曲線 → 2Y、10Y（同一來源，避免口徑差異污染利差）
  Yahoo chart API             → DXY（DX-Y.NYB）與驗證用代表資產
  CNN                         → 恐慌貪婪指數
        → macro.db（每日序列 + 象限歷史）
        → data.json（前端只負責顯示）

只用 Python 標準庫，GitHub Actions 免 pip install。

資料陷阱紀錄（勿回退）：
  - Yahoo 的 2YY=F（2 年期殖利率期貨）在本專案建置時數值連續數週不變，是無成交的
    死合約。不可用。2Y 一律取自財政部。
  - Yahoo 的 DX-Y.NYB 會出現標在週日、收盤為 None 的空 K 棒 → 丟棄。日期以 UTC
    日期為準（與 UUP 日變動相關 0.83，已驗證）。
"""

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


def fetch_yahoo(sym):
    """回傳 {date_iso: close}；丟棄 None 與週末 K 棒，日期取 UTC 日期。"""
    try:
        d = json.loads(http_get(YF_URL.format(sym=urllib.parse.quote(sym)),
                                {"User-Agent": UA, "Accept": "application/json"}))
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
        return {
            "score": r1(fg["score"]), "rating": fg.get("rating"), "timestamp": fg.get("timestamp"),
            "previous_close": r1(fg.get("previous_close")), "previous_1_week": r1(fg.get("previous_1_week")),
            "previous_1_month": r1(fg.get("previous_1_month")), "previous_1_year": r1(fg.get("previous_1_year")),
            "history": [{"t": int(p["x"]), "y": round(float(p["y"]), 1)} for p in hist[-30:]],
        }
    except Exception as e:
        print(f"  ! CNN 恐慌貪婪指數抓取失敗（略過）：{e}")
        return None


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

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        # 存入新抓到的資料（抓失敗時保留舊資料，不覆蓋）
        dxy_f = ffill_lookup(dxy) if dxy else None
        for d, (y2, y10) in tsy.items():
            conn.execute("INSERT OR REPLACE INTO macro_daily (date,dxy,y2,y10) VALUES (?,?,?,?)",
                         (d, dxy_f(d) if dxy_f else None, y2, y10))
        for sym, ser in assets.items():
            conn.executemany("INSERT OR REPLACE INTO asset_daily (symbol,date,close) VALUES (?,?,?)",
                             [(sym, d, c) for d, c in ser.items()])
        if fg:
            conn.execute("INSERT OR REPLACE INTO market_indicators (indicator,timestamp,score,rating) "
                         "VALUES (?,?,?,?)", ("cnn_fear_greed", today.isoformat(), fg["score"], fg["rating"]))
        conn.commit()

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
                    "assets": "Yahoo", "sentiment": "CNN Fear & Greed"},
        "dates": dates[start:],
        "dxy": [round(v, 3) for v in dxys[start:]],
        "y2": y2s[start:], "y10": y10s[start:],
        "spread_bp": [round((b - a) * 100, 1) for a, b in zip(y2s[start:], y10s[start:])],
        "regimes": regimes, "current": current, "q3_episodes": episodes,
        "quadrants": rg.QUADRANTS,
        "assets": [{"symbol": s, "label": l} for s, l in rg.ASSETS],
        "market": {"fear_greed": fg} if fg else {},
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    for lb_s, c in current.items():
        q = rg.QUADRANTS[c["q"]]
        fit = c["fit"]
        print(f"  · {lb_s} 日視角：{c['q']} {q['name']}"
              f"{'' if c['clear'] else '（不明確）'}{'' if c['typical'] else '（非典型）'}"
              f" | DXY {c['d_dxy_pct']:+.2f}% 2Y {c['d2_bp']:+.0f}bp 10Y {c['d10_bp']:+.0f}bp"
              f" 利差 {c['d_spread_bp']:+.0f}bp | 已 {c['spell_days']} 天"
              + (f" | 劇本吻合 {fit['hits']}/{fit['total']}" if fit else ""))
    print(f"完成：{len(dates)} 個交易日 → data.json（顯示近 {len(dates) - start} 日，參數版本 {ver}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
