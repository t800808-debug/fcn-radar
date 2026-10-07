# FCN 機會雷達

AI／科技股的 FCN 連結標的研究工具：品質 × 波動地圖、單檔 KI 觸及率估算、worst-of 組合試算。

> 僅供學術研究參考，不構成任何投資建議，亦非任何商品之要約、推介或報價。

網址（開啟 GitHub Pages 後）：`https://t800808-debug.github.io/fcn-radar/`

## 專案內容

| 檔案 | 用途 |
|---|---|
| `index.html` | 網頁本體（單一檔案） |
| `tickers.txt` | 觀察清單，一行一個美股代號 |
| `data/quotes.json`、`data/meta.json` | 個股資料，由自動排程更新 |
| `scripts/update_data.py` | 抓資料的程式（Yahoo Finance，透過 yfinance） |
| `.github/workflows/update-data.yml` | GitHub Actions 自動排程 |

## 第一次設定（約 5 分鐘）

1. **上傳檔案**：在 repo 頁面按 **Add file → Upload files**，把解壓縮後 `fcn-radar` 資料夾裡的**所有內容**（包含 `.github` 資料夾）拖進去，按 **Commit changes**。
   - 如果上傳後看不到 `.github/workflows/update-data.yml`：按 **Add file → Create new file**，檔名輸入 `.github/workflows/update-data.yml`，把壓縮檔裡同名檔案的內容貼上，再按 Commit。
2. **開啟網頁**：**Settings → Pages**，Source 選 **Deploy from a branch**，Branch 選 `main`、資料夾 `/ (root)`，按 **Save**。約 1–2 分鐘後網址就能開。
3. **允許自動更新寫入**：**Settings → Actions → General**，最下面 Workflow permissions 選 **Read and write permissions**，按 **Save**。
4. **跑第一次更新**：到 **Actions** 分頁，左邊選「更新 FCN 資料」，按 **Run workflow**。約 3–5 分鐘完成，之後網頁的 Y 軸就會改用 180 天 Put 隱含波動。

## 日常使用

- **新增或移除個股**：網頁上按「編輯觀察清單」，或直接在 GitHub 編輯 `tickers.txt`，存檔後會自動抓資料。
- **自動更新時間**：每週二到週六台北時間早上約 6:40（美股收盤後）。GitHub 排程尖峰時可能延遲數十分鐘。
- **手動更新**：網頁上按「手動執行更新」→ Run workflow。

## 計算方式

- **波動**：預設為最接近 180 天到期、最接近現價兩個履約價的 Put 隱含波動平均；無選擇權資料時改用歷史波動（近 20／49 日對數報酬標準差 × √252）。
- **品質分數 0–100**：營收成長 25、營業利益率 25、自由現金流率 20、ROE 15、負債權益比 15。
- **百分位**：在觀察清單內的相對排名，用來劃分四個象限。
- **KI 觸及率**：連續觀察障礙機率（零漂移）。組合試算以近 50 日報酬相關係數做 4,000 條路徑的蒙地卡羅模擬，每日觀察。

所有機率皆為簡化模型估算，僅供比較個股相對風險；實際票息與條件以發行機構報價為準。資料可能延遲、缺漏或錯誤。
