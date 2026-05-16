# 總體經濟 AI 監控報告系統

每日自動抓取全球總經新聞，透過 DCC-GARCH 量化分析與 Gemini AI 深度解讀，產出結構化報告並推播至 LINE，月營運成本 NT$0。

---

## 系統架構

```
Cloudflare Worker（每日台灣時間 07:00）
        │
        ▼
GitHub Actions workflow_dispatch
        │
        ├─► [量化分析層] DCC-GARCH(1,1)
        │       ├─ yfinance 抓取 VOO / TLT / GLD 兩年日線
        │       ├─ GARCH(1,1) 估計各資產條件波動率
        │       ├─ DCC(1,1) 估計動態條件相關矩陣
        │       ├─ HRP 階層風險平價配置（Lopez de Prado 2016）
        │       └─ Risk Parity 等風險貢獻配置
        │
        ├─► Finnhub API       → 市場新聞（general + forex）
        ├─► FMP API           → 國際政經新聞（付費帳號可用）
        │
        ▼
   資料清理層
   - 新聞去重（標題前 30 字比對）
   - 摘要截斷至 200 字
        │
        ▼
Gemini 2.5 Flash API
   - 輸入：DCC-GARCH 量化結果 + 新聞文字
   - 輸出：JSON 格式報告
   - 包含主線判斷、情緒評分、13 個持倉戰術建議（數字有量化錨點）
        │
        ├─► LINE Messaging API  → 濃縮摘要推播
        └─► Jinja2 HTML 模板   → 互動式完整報告
                │
                ▼
        GitHub Pages 公開發布
```

## 技術選型

| 元件 | 方案 | 免費額度 |
|---|---|---|
| 新聞來源 | Finnhub API | 60 次/分鐘 |
| 量化分析 | DCC-GARCH（arch + scipy） | 本地運算，無 API 費用 |
| 價格資料 | yfinance（Yahoo Finance） | 免費 |
| AI 分析 | Google Gemini 2.5 Flash | 免費額度充足 |
| 推播 | LINE Messaging API | 200 則/月 |
| 排程 | Cloudflare Workers Cron | 100,000 次/日 |
| CI/CD | GitHub Actions | 免費額度涵蓋 |
| 報告託管 | GitHub Pages | 免費（公開 repo）|
| **月營運成本** | | **NT$ 0** |

---

## 功能特色

### 量化資產配置分析（DCC-GARCH）

每日執行前先對 VOO / TLT / GLD 進行兩階段量化估計：

- **動態條件相關係數**：VOO↔TLT、VOO↔GLD、TLT↔GLD 的即時相關性（vs 近 30 日均值），判斷避險關係是否生效
- **條件波動率（年化）**：各資產當前的 GARCH(1,1) 條件標準差
- **HRP 配置**：Hierarchical Risk Parity（Lopez de Prado 2016）— 以階層聚類建立資產樹狀結構，遞迴二分分配風險，不需矩陣求逆、對估計誤差更穩健
- **Risk Parity 配置**：三資產等風險貢獻配置（橋水 All Weather 概念）

量化結果直接注入 Gemini prompt，讓 AI 的操作建議有具體百分比數字支撐。

### AI 總經分析

- **今日宏觀主線**：流動性主導 / 基本面主導，附信心度百分比
- **市場情緒評分**：1–10 分視覺化量表
- **風險標籤**：自動標記當日主要風險事件
- **13 個持倉戰術建議**：涵蓋核心股票、品質防禦、固定收益、實物資產（引用 DCC 量化數字）
- **防禦觀察清單**：未來 3–5 日 4 個關鍵觸發事件
- **三大核心主題**：含數據格、持倉連動分析

### 持倉清單

| 群組 | 代碼 |
|---|---|
| 核心股票 | 0050、VOO、QQQ |
| 品質防禦 | QUAL、XLV、XLU、XLP、00713 |
| 固定收益 | 00679B、00719B |
| 實物資產 | 黃金(GLD)、PDBC、現金 |

---

## 環境設定

### 本地開發

建立 `.env`（不會被 commit）：

```env
FINNHUB_API_KEY=
FMP_API_KEY=
GEMINI_API_KEY=
LINE_CHANNEL_ACCESS_TOKEN=
LINE_USER_ID=
```

安裝依賴並執行：

```bash
pip install -r requirements.txt
python main.py
```

單獨測試 DCC-GARCH（不需要任何 API Key）：

```bash
python dcc_garch.py
```

### GitHub Secrets

前往 `Settings → Secrets and variables → Actions`，新增以下 5 個 Secret：

| Secret 名稱 | 說明 |
|---|---|
| `FINNHUB_API_KEY` | Finnhub 免費 API Key |
| `FMP_API_KEY` | Financial Modeling Prep API Key |
| `GEMINI_API_KEY` | Google AI Studio API Key |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API Channel Token |
| `LINE_USER_ID` | 接收推播的 LINE 使用者 ID |

---

## 排程機制

由 **Cloudflare Worker** 每日 UTC 23:00（台灣時間 07:00）觸發 `workflow_dispatch`，比 GitHub Actions 原生 cron 更準時。

Worker 程式碼位於 `cloudflare-worker/`，部署方式：

```bash
cd cloudflare-worker
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN   # GitHub Fine-grained PAT（Actions: read/write）
npx wrangler secret put GITHUB_REPO    # 填入：learningprogram0108/macro_news
```

---

## 目錄結構

```
macro_news/
├── main.py                        # 主程式（DCC-GARCH → 抓取新聞 → Gemini 分析 → 推播）
├── dcc_garch.py                   # DCC-GARCH(1,1) 量化引擎 + 投資組合最佳化
├── report_template.html           # Jinja2 互動式報告模板
├── requirements.txt
├── .gitignore
├── cloudflare-worker/
│   ├── worker.js                  # Cloudflare Worker 排程觸發器
│   └── wrangler.toml
├── reports/
│   └── report_YYYYMMDD.html       # 每日自動生成
└── .github/
    └── workflows/
        └── macro_cron.yml         # GitHub Actions 工作流程
```

---

## 報告範例

報告發布於 GitHub Pages：

```
https://learningprogram0108.github.io/macro_news/reports/report_YYYYMMDD.html
```

LINE 每日推播格式：

```
📊 每日總經 AI 監控報告
2026 年 05 月 17 日 台灣時間 07:00

🧭 今日主線：基本面主導
地緣政治衝突引發通膨擔憂，推升油價與殖利率

📈 情緒評分：3/10 謹慎偏恐慌

⚠️ 風險標籤：地緣政治風險 · 通膨壓力 · 利率上行風險

🎯 關鍵操作：
• 黃金(GLD) 加碼
• QQQ 減碼

📋 完整報告：https://learningprogram0108.github.io/macro_news/reports/...
```

DCC-GARCH 量化結果（注入 Gemini prompt 範例）：

```
【量化資產配置分析 — DCC-GARCH(1,1)】
回溯 2 年日線（VOO / TLT / GLD），DCC α=0.0382 β=0.7532

▌動態條件相關係數（今日估計 vs 近 30 日均值）
• VOO ↔ TLT：-0.182（30日均：-0.201）→ 持平
• VOO ↔ GLD：+0.051（30日均：+0.038）→ 持平
• TLT ↔ GLD：+0.143（30日均：+0.127）→ 持平

▌條件波動率（年化）
• VOO：15.3%　TLT：12.1%　GLD：13.8%

▌最佳化配置建議
階層風險平價（HRP）：
  VOO 45.2% / TLT 28.6% / GLD 26.2%
等風險貢獻（Risk Parity）：
  VOO 33.8% / TLT 34.2% / GLD 32.0%
```
