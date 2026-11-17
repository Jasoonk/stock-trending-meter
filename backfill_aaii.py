#!/usr/bin/env python3
"""
一次性：把 AAII 官方 sentiment.xls（1987 年起每週）灌進 macro.db 的 aaii_weekly，
並從 Yahoo 抓 S&P 500 長歷史日線，填入每週的 spx_close（只存週值，不存日線，控制 DB 大小）。

  pip install xlrd      # 只有這支需要；每日的 fetch.py 仍只用標準庫
  python backfill_aaii.py

重跑會以官方 xls 覆蓋同一週的值（INSERT OR REPLACE）。
macro.db 已 commit 進 repo，所以平常不需要再跑；只有資料庫遺失時才需要。
"""

import sys
import sqlite3
import datetime
import urllib.request

try:
    import xlrd
except ImportError:
    sys.exit("需要 xlrd：pip install xlrd")

import fetch
from aaii import to_thursday

XLS_URL = "https://www.aaii.com/files/surveys/sentiment.xls"


def main():
    raw = urllib.request.urlopen(urllib.request.Request(XLS_URL, headers={"User-Agent": fetch.UA}),
                                 timeout=60).read()
    book = xlrd.open_workbook(file_contents=raw)
    sh = book.sheet_by_name("SENTIMENT")
    rows = []
    for r in range(sh.nrows):
        v = sh.row_values(r)
        if not (isinstance(v[0], float) and all(isinstance(x, float) for x in v[1:4])):
            continue
        d = to_thursday(xlrd.xldate_as_datetime(v[0], book.datemode).date())
        rows.append((d.isoformat(), v[1], v[2], v[3], "xls"))
    conn = sqlite3.connect(fetch.DB_PATH)
    fetch.init_db(conn)
    conn.executemany("INSERT OR REPLACE INTO aaii_weekly (date,bullish,neutral,bearish,source) "
                     "VALUES (?,?,?,?,?)", rows)
    spx = fetch.fetch_yahoo(fetch.SPX, since=datetime.date(1987, 1, 1))
    if spx:
        f = fetch.ffill_lookup(spx)
        conn.executemany("UPDATE aaii_weekly SET spx_close=? WHERE date=?", [(f(r[0]), r[0]) for r in rows])
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"AAII 歷史：{len(rows)} 週（{rows[0][0]} → {rows[-1][0]}）已寫入 macro.db；"
          f"S&P 500 日線 {len(spx)} 筆用來填週收盤")


if __name__ == "__main__":
    main()
