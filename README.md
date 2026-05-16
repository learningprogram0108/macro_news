# 總體經濟 AI 監控報告系統

每日自動抓取全球總經新聞，透過 Gemini AI 深度分析，產出結構化報告並推播至 LINE，月營運成本 NT$0。

---

## 系統架構

```
Cloudflare Worker（每日台灣時間 07:00）
        │
        ▼
GitHub Actions workflow_dispatch
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
   - 輸出：JSON 格式報告
   - 包含主線判斷、情緒評分、13 個持倉戰術建議
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
| AI 分析 | Google Gemini 2.5 Flash | 免費額度充足 |
| 推播 | LINE Messaging API | 200 則/月 |
| 排程 | Cloudflare Workers Cron | 100,000 次/日 |
| CI/CD | GitHub Actions | 免費額度涵蓋 |
| 報告託管 | GitHub Pages | 免費（公開 repo）|
| **月營運成本** | | **NT$ 0** |

---

## 功能特色

- **今日宏觀主線**：流動性主導 / 基本面主導，附信心度百分比
- **市場情緒評分**：1–10 分視覺化量表
- **風險標籤**：自動標記當日主要風險事件
- **13 個持倉戰術建議**：涵蓋核心股票、品質防禦、固定收益、實物資產
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
├── main.py                        # 主程式（抓取 → 分析 → 推播）
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
2026 年 05 月 16 日 台灣時間 07:00

🧭 今日主線：基本面主導
地緣政治衝突引發通膨擔憂，推升油價與殖利率

📈 情緒評分：3/10 謹慎偏恐慌

⚠️ 風險標籤：地緣政治風險 · 通膨壓力 · 利率上行風險

🎯 關鍵操作：
• 黃金(GLD) 加碼
• QQQ 減碼

📋 完整報告：https://learningprogram0108.github.io/macro_news/reports/...
```
