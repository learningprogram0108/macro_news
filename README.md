# 總體經濟 AI 監控報告系統

每日自動抓取全球總經新聞，透過 DCC-GARCH 量化分析與 Gemini AI 深度解讀，產出互動式 HTML 報告並推播至 LINE，月營運成本 NT$0。

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
        ├─► Alpha Vantage NEWS_SENTIMENT API
        │       ├─ economy_monetary  → 貨幣政策新聞
        │       ├─ economy_macro     → 總體經濟新聞
        │       ├─ economy_fiscal    → 財政政策新聞
        │       └─ financial_markets → 金融市場新聞
        │
        ▼
   資料清理層
   - 每主題最多 N 篇，標題前 30 字去重
   - 摘要截斷至 200 字
        │
        ▼
Gemini 2.5 Flash API（單次呼叫，Plan A）
   - 輸入：DCC-GARCH 量化結果 + 4 主題新聞文字
   - 輸出：JSON — 含 5 個分析區塊
       ├─ 🏦 貨幣政策分析（380 字 + 影響標籤 + 4 篇摘要）
       ├─ 📊 總體經濟分析（380 字 + 影響標籤 + 4 篇摘要）
       ├─ 💰 財政政策分析（380 字 + 影響標籤 + 4 篇摘要）
       ├─ 📈 金融市場分析（380 字 + 影響標籤 + 4 篇摘要）
       └─ 🤖 AI 綜合研判（主線判斷 + 情緒評分 + 13 個持倉建議）
   - 503/429 錯誤自動 retry（最多 4 次，指數退避 30/60/120 秒）
   - max_output_tokens=16384
        │
        ├─► LINE Messaging API  → 持倉分組完整分析推播
        └─► Jinja2 HTML 模板   → 互動式完整報告
                │
                ▼
        GitHub Pages 公開發布
```

## 技術選型

| 元件 | 方案 | 免費額度 |
|---|---|---|
| 新聞來源 | Alpha Vantage NEWS_SENTIMENT | 25 次/日（免費方案） |
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

每日對 VOO / TLT / GLD 進行兩階段量化估計，結果注入 Gemini prompt：

- **動態條件相關係數**：VOO↔TLT、VOO↔GLD、GLD↔TLT 即時相關性（vs 近 30 日均值），附趨勢箭頭與避險效果判讀
- **條件波動率（年化）**：各資產當前 GARCH(1,1) 條件標準差
- **HRP 配置**：Hierarchical Risk Parity — 階層聚類 + 遞迴二分，不需矩陣求逆，對估計誤差更穩健
- **Risk Parity 配置**：等風險貢獻（ERC），每資產貢獻相同組合風險

### 互動式 HTML 報告（Plan A：單次 Gemini 呼叫）

每日產出一份互動式報告，結構如下：

```
┌─────────────────────────────────────────┐
│  📐 DCC 快速列（相關係數 + 波動率）        │
├───────┬───────┬───────┬───────┬─────────┤
│ 🏦貨幣 │ 📊總經 │ 💰財政 │ 📈市場 │ 🤖綜合 │  ← 5 個主題卡
└───────┴───────┴───────┴───────┴─────────┘
```

- **5 個主題卡**：每卡含 Gemini 深度分析（380 字）+ 4 篇新聞摘要
- **點入主題**：展開完整分析頁面，含影響標籤（看多/看空/中性）
- **展開文章**：點擊標題查看 Alpha Vantage 原文摘要（英文）
- **綜合研判**：AI 主線判斷 + 情緒評分 + 13 個持倉戰術建議

### AI 總經分析

- **今日宏觀主線**：流動性主導 / 基本面主導，附信心度百分比與說明
- **市場情緒評分**：1–10 分，附評分推理
- **風險標籤**：自動標記當日主要風險事件
- **13 個持倉戰術建議**：按群組分析，add/reduce 附完整 rationale，VOO/TLT/GLD 永遠顯示量化說明
- **防禦觀察清單**：4 個關鍵觸發事件及持倉連動分析
- **三大核心主題**：含數據格、持倉影響

### 持倉清單

| 群組 | 代碼 |
|---|---|
| 核心股票 | 0050、VOO、QQQ |
| 品質防禦 | QUAL、XLV、XLU、XLP、00713 |
| 固定收益 | 00679B、00719B |
| 實物資產 | 黃金(GLD)、PDBC、現金 |

---

## 量化模型公式

### Stage 1：GARCH(1,1)

對數報酬：$r_t = \ln(P_t / P_{t-1})$

$$\sigma_t^2 = \omega + \alpha_G \cdot \varepsilon_{t-1}^2 + \beta_G \cdot \sigma_{t-1}^2$$

標準化殘差：$z_t = \varepsilon_t / \sigma_t$

年化波動率：$\sigma_{\text{annual}} = \sigma_T \times \sqrt{252}$

### Stage 2：DCC(1,1)

無條件相關矩陣：

$$\bar{Q} = \frac{1}{T}\sum_{t=1}^{T} z_t z_t'$$

動態偽相關矩陣：

$$Q_t = (1 - \alpha - \beta)\bar{Q} + \alpha \cdot z_{t-1}z_{t-1}' + \beta \cdot Q_{t-1}$$

標準化為相關矩陣：

$$R_t = \operatorname{diag}(Q_t)^{-1/2} \cdot Q_t \cdot \operatorname{diag}(Q_t)^{-1/2}$$

DCC log-likelihood（最大化估計 $\alpha, \beta$，約束 $\alpha>0,\ \beta>0,\ \alpha+\beta<1$）：

$$\mathcal{L}(\alpha,\beta) = -\frac{1}{2}\sum_{t=1}^{T}\bigl[\ln|R_t| + z_t' R_t^{-1} z_t - z_t' z_t\bigr]$$

年化動態共變異數矩陣：

$$H_t = D_t \cdot R_t \cdot D_t \times 252, \quad D_t = \operatorname{diag}(\sigma_{1,t},\, \sigma_{2,t},\, \sigma_{3,t})$$

### Hierarchical Risk Parity（HRP）

**Step 1 — 距離矩陣**

$$d_{ij} = \sqrt{\frac{1 - \rho_{ij}}{2}}$$

其中 $\rho_{ij}$ 為 DCC 動態相關矩陣 $R_t$ 的元素。

**Step 2 — 階層聚類（Single Linkage）**

$$d_{\text{single}}(A, B) = \min_{i \in A,\, j \in B} d_{ij}$$

輸出樹狀結構，將相似資產相鄰排列（Quasi-diagonalization）。

**Step 3 — 遞迴二分配置**

每個葉節點 $i$ 在子叢集 $C$ 中的逆變異數權重：

$$\tilde{w}_i = \frac{1/\sigma_i^2}{\sum_{j \in C} 1/\sigma_j^2}$$

子叢集 $C$ 的組合變異數：

$$\tilde{V}_C = \tilde{w}_C' \cdot H_C \cdot \tilde{w}_C$$

由樹根往下，每次將左右子叢集的配置比例設為：

$$\alpha_L = 1 - \frac{\tilde{V}_L}{\tilde{V}_L + \tilde{V}_R}, \quad \alpha_R = \frac{\tilde{V}_L}{\tilde{V}_L + \tilde{V}_R}$$

### Risk Parity（等風險貢獻）

邊際風險貢獻：$\mathit{MRC}_i = (H_t\, w)_i$

資產 $i$ 的風險貢獻：$\mathit{RC}_i = w_i \cdot \mathit{MRC}_i$

最小化各資產風險貢獻偏離均等的程度：

$$\min_w \sum_{i=1}^{n}\!\left(\mathit{RC}_i - \frac{w'H_t w}{n}\right)^{\!2} \quad \text{s.t.} \quad \sum_i w_i = 1,\; w_i > 0$$

---

## 環境設定

### 本地開發

建立 `.env`（不會被 commit）：

```env
ALPHA_VANTAGE_API_KEY=
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

前往 `Settings → Secrets and variables → Actions`，新增以下 Secret：

| Secret 名稱 | 說明 |
|---|---|
| `ALPHA_VANTAGE_API_KEY` | Alpha Vantage 免費 API Key |
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
├── main.py                        # 主程式（DCC-GARCH → 新聞抓取 → Gemini 分析 → 推播）
├── dcc_garch.py                   # DCC-GARCH(1,1) 量化引擎 + HRP + Risk Parity
├── report_template.html           # Jinja2 互動式報告模板（5 主題卡 + DCC 列）
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

## LINE 推播格式

```
📊 每日總經 AI 監控報告
2026 年 05 月 17 日 台灣時間 07:00

🧭 今日主線：基本面主導（信心度 78%）
聯準會鷹派言論壓制風險資產
通膨數據超預期，市場重新定價降息時程

📈 情緒評分：4/10 謹慎偏悲觀
市場對高利率持續性的擔憂升溫

⚠️ 風險標籤：利率風險 · 通膨壓力 · 美債供給壓力

📐 量化配置分析
• VOO↔TLT：-0.18 ↓ 下降（避險效果增強）
• VOO↔GLD：+0.05 → 持平
• GLD↔TLT：+0.14 → 持平
• 波動率：VOO 15.3% | TLT 12.1% | GLD 13.8%
• HRP：VOO 45% / TLT 29% / GLD 26%
• Risk Parity：VOO 34% / TLT 34% / GLD 32%

━━━━━━━━━━━━━━━
🎯 持倉配置分析
━━━━━━━━━━━━━━━

📈【核心股票】
⚪ 0050 維持
🟡 VOO 觀察
   └ DCC↔TLT -0.18 避險有效；HRP 建議 45%，高利率環境短期承壓
🔴 QQQ 減碼
   └ 科技股估值受壓，建議降低部位

🛡️【品質防禦】
⚪ QUAL 維持
🟢 XLV 加碼
   └ 防禦屬性在基本面主導環境下表現穩健
⚪ XLU 維持
⚪ XLP 維持
⚪ 00713 維持

🏦【固定收益】
🟢 00679B 加碼
   └ Risk Parity 建議 34%，殖利率高點支撐債券配置價值
⚪ 00719B 維持

🥇【實物資產】
🟢 黃金(GLD) 加碼
   └ HRP 建議 26%，GLD↔TLT +0.14，避險需求提升
⚪ PDBC 觀察
💵 現金 謹慎

📋 完整報告：https://learningprogram0108.github.io/macro_news/reports/...
```

---

## 報告網址

```
https://learningprogram0108.github.io/macro_news/reports/report_YYYYMMDD.html
```
