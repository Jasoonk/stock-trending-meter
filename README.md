# 注意力生命週期儀表板

追蹤每檔股票／題材在群眾注意力曲線上的位置（🌱剛冒出 → 🚀加速 → 🥵飽和 → 📉退潮），
幫你判斷該**跟動能**還是該**反著做**。**決策輔助儀表板，不是自動下單機。**

> 完整理論與路線圖見 [`注意力生命週期儀表板 - 專案計畫.md`](./注意力生命週期儀表板%20-%20專案計畫.md)。
> 目前階段：**P1 MVP**（真實資料源 = ApeWisdom／Reddit，每日排程，SQLite 存歷史）。

---

## 檔案結構

| 檔案 | 作用 |
|------|------|
| `fetch.py` | 抓取器：ApeWisdom API → 正規化 → SQLite → 匯出 `data.json`。只用 Python 標準庫，無第三方相依。 |
| `attention.db` | SQLite 歷史庫（`snapshots` / `tickers` 兩張表，依計畫 §6）。**每天累積 = 未來能回測。** |
| `data.json` | 前端讀的檔（近 30 天視窗）。由 `fetch.py` 產生。 |
| `index.html` | 單頁儀表板（零外部相依，內建 SVG 繪圖）。優先讀 `data.json`，讀不到回退內建範例。 |
| `.github/workflows/update.yml` | GitHub Actions：每日抓取並 commit 更新後的 `data.json` + `attention.db`。 |

---

## 資料來源

- **ApeWisdom**（免金鑰、免費）彙整 Reddit 各子版的 ticker 提及數與排名。
- 目前以三個子版當三個「來源」：`r/wallstreetbets`、`r/stocks`、`r/options`。
- 要增減來源：改 `fetch.py` 最上方的 `SOURCES` 字典即可（值是前端顯示的標籤）。
- **CNN 恐慌貪婪指數**（市場情緒卡片）：來自 CNN 的非官方端點，需完整瀏覽器標頭。
  - 抓取失敗時**整體流程不中斷**，僅略過此區塊、前端自動隱藏卡片。
  - ⚠️ GitHub Actions 的機房 IP 有時會被 CNN 擋（HTTP 418）；若雲端抓不到，本機仍可抓。屆時 Pages 上就不顯示此卡片。
- **股價 / 價格背離旗標（P2，已接）**：來自 **Yahoo chart API**（免金鑰、免第三方套件），只抓熱度前 60 檔近 2 個月每日收盤，前值補齊後對齊到 `data.json` 的 `price`。
  - 股價可回溯，一抓就有完整 30 天；疊圖會立刻有完整價格線（提及線仍在暖身）。
  - 「過熱背離」旗標：注意力仍在高檔、但價格已走平/反轉時,於該檔加註 🔴過熱（需該檔脫離暖身、有足夠提及歷史後才會觸發）。
  - 抓不到股價的標的（如未上市的 SpaceX、部分加密貨幣代號）`price` 為 `null`，前端自動隱藏疊圖。

---

## 本機執行

```bash
# 1) 抓一次資料（產生／更新 attention.db 與 data.json）
python fetch.py

# 2) 起一個本機 http server（前端用 fetch 讀 data.json，需 http，不能用 file://）
python -m http.server 8137
#   → 瀏覽器開 http://127.0.0.1:8137/index.html
```

> 直接雙擊 `index.html`（file://）也能開，但瀏覽器會擋跨來源 fetch，此時會**自動回退到內建範例資料**（畫面右上角顯示「範例」）。要看真實資料請用上面的 http 方式。

### 「即時 / 範例」切換
- **即時**：讀 `data.json`（真實 ApeWisdom）。
- **範例**：內建 30 天假資料，用來即時展示四階段分類效果（P0 原型）。

### ⏳ 暖身中
本工具**從第一次執行當天才開始累積每日快照**。歷史不足 8 天的標的會標記為「暖身中」，
不做四階段分類（避免資料集本身才幾天就把所有標的誤判成「新進榜」）。
隨每日快照累積，會自動解鎖 🌱新／🚀加速／🥵飽和／📉退潮 分類 —— 這是誠實的冷啟動，不是 bug。

---

## 部署每日自動更新（GitHub Actions + Pages）

1. 把整個資料夾推上 GitHub repo。
2. Actions 已內建（`.github/workflows/update.yml`），每日 13:00 UTC 自動跑，也可在 Actions 頁面手動觸發（workflow_dispatch）。
   - workflow 需要寫入權限：Repo → Settings → Actions → General → Workflow permissions → **Read and write permissions**。
3. 開啟 GitHub Pages：Settings → Pages → Source 選 `main` 分支根目錄。
   - 之後 `https://<user>.github.io/<repo>/` 就是永遠自動更新的儀表板。
4. 想改排程時間：編輯 workflow 裡的 `cron`。

> **歷史儲存方式**：Actions 是無狀態的，所以每次跑完會把 `attention.db` 與 `data.json` commit 回 repo，
> 靠 git 歷史累積快照 —— 免 VPS、免資料庫伺服器。等資料長大再依計畫換 Postgres。

---

## 分類邏輯（規則版，計畫 §7）

門檻集中在 `index.html` 最上方的 `CFG` 物件，**先求可解釋、不求最佳**：

| 階段 | 規則（簡述） |
|------|------|
| 🌱 New | 前期提及近 0，最近 3 天跳升 ≥ 5× |
| 🚀 Accelerating | 週變化率 ≥ +15% 且排名上升 |
| 🥵 Saturated | 排名前 5 名停留 ≥ 10 天，且熱度見頂（週變化轉平/負） |
| 📉 Fading | 週變化率 ≤ −15% 且排名下滑 |
| ⏳ Warming | 觀察歷史 < 8 天，暫不分類 |

門檻（X／threshold／N／D）為手調初值，**須待累積 3–6 個月資料後用樣本外回測校準（P3）**。
在 P3 之前，所有分類都只是假設。

---

## 下一步（路線圖）

- **P2**：接股價（yfinance）加背離旗標；加 Google Trends 題材級追蹤；多來源加權。
- **P3**：累積 3–6 個月後回測「新進榜跟進 / 飽和反做」的樣本外勝率 → 才知道 edge 是否真的存在。

*非投資建議。訊號會很吵，儀表板的價值是「提醒你去看」，不是「告訴你買」。*
