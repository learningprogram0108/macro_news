# 總體經濟新聞 AI 自動化摘要與推播系統
## 技術手冊 v3.0 — Claude Code CLI 開發指引

> 本手冊為交給 Claude Code CLI 的完整開發規格。  
> 請依序實作各模組，最終產出 `main.py`、`report_template.html`、`requirements.txt`、`.github/workflows/macro_cron.yml`。

---

## 1. 系統架構

### 1.1 整體流程

```
[ GitHub Actions — 每日 UTC 23:00（台灣時間 07:00）]
            │
            ▼
     [ main.py 主程式 ]
            │
            ├─► Finnhub API     → 市場新聞 + 總經事件（主力來源）
            ├─► FMP API         → 國際政經一般新聞（補充來源）
            │
            ▼
   [ 資料清理層 ]
   - FMP text 欄位截斷至 200 字
   - 新聞去重（標題相似度比對）
   - 總 token 估算控制
            │
            ▼
   [ Gemini Flash API ]
   - 輸入：清理後新聞文字串流 + 結構化 Prompt
   - 輸出：JSON 格式報告（含主線判斷、評分、主題、配置建議）
            │
            ├─► LINE Messaging API → 濃縮版摘要推播
            └─► 填入 HTML 模板   → 完整互動式報告（附於 LINE 訊息連結）
```

### 1.2 技術選型

| 元件 | 選用方案 | 免費額度 |
|---|---|---|
| 新聞來源 A | Finnhub API | 60 次/分鐘 |
| 新聞來源 B | Financial Modeling Prep (FMP) | 250 次/日 |
| AI 分析 | Google Gemini Flash（`google-genai` SDK） | 每日遠低於限制 |
| 推播 | LINE Messaging API | 200 則/月 |
| 排程 | GitHub Actions | 免費額度涵蓋 |
| **月營運成本** | | **NT$ 0** |

---

## 2. 環境變數

在 `.env`（本地開發）與 GitHub Secrets（生產環境）中設定：

```
FINNHUB_API_KEY=
FMP_API_KEY=
GEMINI_API_KEY=
LINE_CHANNEL_ACCESS_TOKEN=
LINE_USER_ID=
```

---

## 3. 持倉清單（供 Prompt 使用）

AI 分析時需對以下 13 個持倉給出戰術指引，分四個群組：

### 核心股票部位
| 代碼 | 名稱 | 特性 |
|---|---|---|
| 0050 | 元大台灣50 | 台股核心，台積電主導 |
| VOO | Vanguard S&P500 ETF | 美股均衡配置 |
| QQQ | Invesco 那斯達克100 ETF | 高 P/E 科技集中，利率最敏感 |

### 品質防禦部位
| 代碼 | 名稱 | 特性 |
|---|---|---|
| QUAL | iShares MSCI 美國品質因子 ETF | 低槓桿高ROE，流動性收緊時首選防禦 |
| XLV | 醫療保健 SPDR ETF | 需求剛性，景氣循環中性 |
| XLU | 公用事業 SPDR ETF | 高股息，對實質利率敏感 |
| XLP | 必需消費品 SPDR ETF | 純粹防禦，通膨定價能力強 |
| 00713 | 元大台灣高息低波 ETF | 台灣版防禦，兼顧息收與低波動 |

### 固定收益部位
| 代碼 | 名稱 | 特性 |
|---|---|---|
| 00679B | 元大美債20年 ETF | 長存續期，利率最敏感 |
| 00719B | 元大美債1-3年 ETF | 短存續期，利率修正時的避風港 |

### 實物資產與另類配置
| 代碼 | 名稱 | 特性 |
|---|---|---|
| 黃金（GLD） | 實體黃金 / GLD ETF | 避險情緒 + 美元對沖 |
| PDBC | 景順廣泛大宗商品 ETF | 實體供需（能源、農產品），通膨驅動 |
| 現金 | 貨幣市場 / 定存 | 流動性緩衝，方向不明時的停泊 |

---

## 4. 資料抓取模組

### 4.1 Finnhub（主力來源）

```python
import finnhub
import os

finnhub_client = finnhub.Client(api_key=os.environ["FINNHUB_API_KEY"])

def fetch_finnhub_news():
    """
    抓取市場總經新聞，取最新 15 篇。
    category 選項：general | forex | crypto | merger
    """
    news = finnhub_client.general_news('general', min_id=0)
    results = []
    for item in news[:15]:
        results.append({
            "source": "Finnhub",
            "title": item.get("headline", ""),
            "summary": item.get("summary", "")[:200],  # 截斷 200 字
            "datetime": item.get("datetime", "")
        })
    return results
```

### 4.2 FMP（補充來源）

```python
import requests

def fetch_fmp_news():
    """
    抓取 FMP 最新 10 篇國際政經新聞。
    text 欄位強制截斷至 200 字。
    """
    url = "https://financialmodelingprep.com/api/v3/news/general-latest"
    params = {
        "apikey": os.environ["FMP_API_KEY"],
        "limit": 10
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    results = []
    for item in resp.json():
        results.append({
            "source": "FMP",
            "title": item.get("title", ""),
            "summary": item.get("text", "")[:200],  # 強制截斷 200 字
        })
    return results
```

### 4.3 資料清理與去重

```python
def deduplicate_news(news_list):
    """
    簡易去重：標題前 30 字相同視為重複，保留第一筆。
    """
    seen = set()
    cleaned = []
    for item in news_list:
        key = item["title"][:30].lower().strip()
        if key not in seen:
            seen.add(key)
            cleaned.append(item)
    return cleaned

def build_news_text(news_list):
    """
    將新聞列表組合為送給 Gemini 的純文字串流。
    """
    lines = []
    for i, item in enumerate(news_list, 1):
        lines.append(f"[{i}] [{item['source']}] {item['title']}\n{item['summary']}")
    return "\n\n".join(lines)
```

---

## 5. AI 分析模組（Gemini Flash）

### 5.1 依賴套件

```
pip install google-genai
```

使用官方最新 SDK `google-genai`，**不要使用舊版 `google-generativeai`**。

### 5.2 Prompt 設計

```python
SYSTEM_PROMPT = """
你是一位精通全球總體經濟、景氣循環理論與資產配置的量化資深分析師。
請閱讀以下過去 24 小時的總經與政經新聞，進行深度結構化分析。

你必須嚴格以 JSON 格式回覆，不得包含任何 Markdown 符號或前言說明。
JSON 結構如下：

{
  "macro_theme": {
    "type": "流動性主導 | 基本面主導",
    "title": "一句話點出今日市場定價核心邏輯",
    "description": "說明為何今日是此主線，以及對各類資產的整體影響框架（80字內）",
    "confidence": 整數0到100
  },
  "sentiment": {
    "score": 數字1到10（1=極度恐慌，10=極度過熱）,
    "label": "中文描述（如：中性偏謹慎）",
    "reasoning": "結合貨幣與財政政策言論的體感分析（60字內）"
  },
  "risk_tags": [
    {"label": "標籤文字", "type": "risk | warn | positive"}
  ],
  "tactical_guidance": [
    {
      "ticker": "持倉代碼（如 QQQ）",
      "name": "持倉中文名稱",
      "group": "核心股票 | 品質防禦 | 固定收益 | 實物資產",
      "action": "加碼 | 維持 | 觀察 | 謹慎 | 減碼",
      "action_type": "add | hold | watch | reduce | cash",
      "rationale": "針對今日總經主線的具體操作理由（60字內，直接點名持倉代碼）"
    }
  ],
  "watchlist": [
    {
      "event": "事件名稱",
      "rationale": "為何重要，出現時哪些持倉受影響（50字內）",
      "date_hint": "預計時間或持續監控"
    }
  ],
  "topics": [
    {
      "icon": "單個 emoji",
      "title": "主題標題",
      "category": "主題類別（如：貨幣政策・利率前景）",
      "severity": "high | medium | positive",
      "summary": "主題摘要（100字內）",
      "data_points": [
        {"label": "指標名稱", "value": "指標數值（含變化箭頭）"}
      ],
      "portfolio_impact": [
        {"level": "red | amber | green", "text": "直接點名持倉代碼的影響描述"}
      ],
      "sources": ["Finnhub", "FMP"]
    }
  ]
}

持倉清單（tactical_guidance 必須涵蓋以下全部 13 個）：
核心股票：0050、VOO、QQQ
品質防禦：QUAL、XLV、XLU、XLP、00713
固定收益：00679B、00719B
實物資產：黃金(GLD)、PDBC、現金

關鍵規則：
1. macro_theme.type 只能是「流動性主導」或「基本面主導」，二選一，不得模糊
2. topics 必須恰好 3 個，代表今日最重要的三大總經事件
3. tactical_guidance 中的 rationale 必須具體提及今日主線，不得寫教科書式通則
4. watchlist 恰好 4 個事件，必須有明確的「若發生則哪些持倉受影響」邏輯
5. 所有文字使用繁體中文
6. 整體 JSON 控制在 3000 字以內
"""

USER_PROMPT_TEMPLATE = """
以下是今日（台灣時間 {date}）過去 24 小時的總經與政經新聞：

{news_text}

請依照規定格式輸出 JSON 分析報告。
"""
```

### 5.3 API 呼叫

```python
import json
from google import genai

def analyze_with_gemini(news_text: str, date: str) -> dict:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    user_prompt = USER_PROMPT_TEMPLATE.format(
        date=date,
        news_text=news_text
    )

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=user_prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=3000,
            temperature=0.3,  # 低溫確保格式穩定
        )
    )

    raw = response.text.strip()
    # 去除可能的 markdown code fence
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)
```

---

## 6. HTML 報告模板

### 6.1 模板說明

Gemini 產出的 JSON 需填入以下 HTML 模板，生成完整的互動式報告頁面。
模板檔案為 `report_template.html`，使用 Python `string.Template` 或 Jinja2 填入變數。

**報告結構（由上至下）：**
1. 報告標頭（日期、標題、資料來源）
2. 今日宏觀主線 Banner（流動性 / 基本面，含信心度進度條）
3. 市場風險情緒評分（1–10 分，視覺化進度條）
4. 風險標籤列
5. 戰術配置指引（可展開，13 個持倉分四群組，含操作評級色點）
6. 未來 3–5 日防禦性觀察清單（可展開，4 個關鍵觸發事件）
7. 三大核心主題（各自可展開，含數據格、持倉連動分析、資料來源）

**操作評級色碼對應：**
- 🟢 加碼（add）
- 🔵 維持（hold）
- 🟡 觀察（watch）
- 🟠 謹慎（reduce）
- 🔴 減碼 / 現金（cash）

### 6.2 報告存放

生成的 HTML 報告存為 `report_YYYYMMDD.html`，存於 repo 的 `reports/` 資料夾。
LINE 推播訊息中附上 GitHub Pages 或 raw 連結，供使用者點入完整閱讀。

---

## 7. LINE 推播模組

### 7.1 濃縮版摘要格式

LINE 推播內容為純文字，控制在 500 字以內，格式如下：

```
📊 每日總經 AI 監控報告
{date} 台灣時間 07:00

🧭 今日主線：{macro_theme.type}
{macro_theme.title}

📈 情緒評分：{sentiment.score}/10 {sentiment.label}

⚠️ 風險標籤：{risk_tags 以逗號串接}

🎯 今日關鍵操作提示：
• {tactical_guidance 中 action=加碼/減碼 的持倉，最多 3 筆}

📋 完整報告：{report_url}
```

### 7.2 推播實作

```python
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage
)

def send_line_message(report_data: dict, report_url: str):
    configuration = Configuration(
        access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    )

    # 組裝濃縮摘要
    theme = report_data["macro_theme"]
    sentiment = report_data["sentiment"]
    tags = " · ".join([t["label"] for t in report_data["risk_tags"]])

    # 只取今日有明確操作建議（加碼 or 減碼）的持倉
    key_actions = [
        f"• {g['ticker']} {g['action']}"
        for g in report_data["tactical_guidance"]
        if g["action_type"] in ("add", "reduce", "cash")
    ][:3]

    message = f"""📊 每日總經 AI 監控報告
{theme.get('date', '')} 台灣時間 07:00

🧭 今日主線：{theme['type']}
{theme['title']}

📈 情緒評分：{sentiment['score']}/10 {sentiment['label']}

⚠️ 風險標籤：{tags}

🎯 關鍵操作：
{chr(10).join(key_actions) if key_actions else '• 今日無明確異動建議，維持現有配置'}

📋 完整報告：{report_url}"""

    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message(PushMessageRequest(
            to=os.environ["LINE_USER_ID"],
            messages=[TextMessage(text=message)]
        ))
```

---

## 8. 錯誤處理機制

所有步驟需有錯誤捕捉，失敗時發送 LINE 錯誤通知，確保你知道系統出問題：

```python
def send_error_notification(step: str, error: str):
    """
    任何步驟失敗時，推播錯誤通知至 LINE。
    """
    message = f"⚠️ 總經報告系統異常\n步驟：{step}\n錯誤：{str(error)[:200]}\n請手動檢查 GitHub Actions logs。"
    # 呼叫 LINE push（同上，但使用最簡化版本）
    ...

def main():
    try:
        # Step 1: 抓取新聞
        try:
            finnhub_news = fetch_finnhub_news()
        except Exception as e:
            send_error_notification("Finnhub 抓取", e)
            finnhub_news = []

        try:
            fmp_news = fetch_fmp_news()
        except Exception as e:
            send_error_notification("FMP 抓取", e)
            fmp_news = []

        if not finnhub_news and not fmp_news:
            send_error_notification("資料抓取", "兩個來源均失敗，中止執行")
            return

        # Step 2: 清理與去重
        all_news = deduplicate_news(finnhub_news + fmp_news)
        news_text = build_news_text(all_news)

        # Step 3: Gemini 分析
        try:
            from datetime import datetime, timezone, timedelta
            tw_time = datetime.now(timezone(timedelta(hours=8)))
            date_str = tw_time.strftime("%Y 年 %m 月 %d 日")
            report_data = analyze_with_gemini(news_text, date_str)
        except Exception as e:
            send_error_notification("Gemini 分析", e)
            return

        # Step 4: 生成 HTML 報告
        try:
            html_content = render_html_report(report_data)
            report_filename = f"reports/report_{tw_time.strftime('%Y%m%d')}.html"
            with open(report_filename, "w", encoding="utf-8") as f:
                f.write(html_content)
        except Exception as e:
            send_error_notification("HTML 生成", e)
            # 非致命錯誤，繼續推播

        # Step 5: LINE 推播
        try:
            report_url = f"https://{os.environ.get('GITHUB_REPOSITORY', 'your/repo')}.github.io/{report_filename}"
            send_line_message(report_data, report_url)
        except Exception as e:
            send_error_notification("LINE 推播", e)

    except Exception as e:
        send_error_notification("未知錯誤", e)

if __name__ == "__main__":
    main()
```

---

## 9. GitHub Actions 排程

建立 `.github/workflows/macro_cron.yml`：

```yaml
name: 每日總經 AI 報告

on:
  schedule:
    # UTC 23:00 = 台灣時間 07:00（UTC+8）
    - cron: '0 23 * * *'
  workflow_dispatch:  # 允許手動觸發

jobs:
  run-report:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run macro report
        env:
          FINNHUB_API_KEY: ${{ secrets.FINNHUB_API_KEY }}
          FMP_API_KEY: ${{ secrets.FMP_API_KEY }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          LINE_CHANNEL_ACCESS_TOKEN: ${{ secrets.LINE_CHANNEL_ACCESS_TOKEN }}
          LINE_USER_ID: ${{ secrets.LINE_USER_ID }}
          GITHUB_REPOSITORY: ${{ github.repository }}
        run: python main.py

      - name: Commit HTML report to repo
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add reports/
          git diff --staged --quiet || git commit -m "report: $(date +'%Y-%m-%d') 自動生成"
          git push
```

---

## 10. requirements.txt

```
google-genai>=1.0.0
finnhub-python>=2.4.0
requests>=2.31.0
line-bot-sdk>=3.5.0
python-dotenv>=1.0.0
```

---

## 11. 專案目錄結構

```
macro-report/
├── main.py                    # 主程式
├── report_template.html       # HTML 報告模板（靜態框架）
├── requirements.txt
├── .env                       # 本地開發用（不 commit）
├── .gitignore
├── reports/                   # 每日自動生成的 HTML 報告
│   └── report_YYYYMMDD.html
└── .github/
    └── workflows/
        └── macro_cron.yml
```

---

## 12. 設計決策紀錄

| 決策項目 | 選擇 | 理由 |
|---|---|---|
| 新聞來源 | Finnhub（主）+ FMP（補） | Finnhub 免費 60次/分鐘，資料更即時；FMP 補充國際政經覆蓋 |
| AI 模型 | Gemini Flash | 免費額度充足，速度快，適合每日單次任務 |
| 輸出格式 | JSON（非 Markdown） | 結構化輸出方便 HTML 模板填入，格式穩定 |
| FMP text 截斷 | 200 字 | 控制 token 用量，避免單篇新聞佔用過多上下文 |
| 持倉硬編碼位置 | Prompt 內（非 config 檔） | 持倉變動頻率低，Prompt 內直接維護即可 |
| Obsidian 同步 | 不實作 | 非核心功能，HTML 報告存於 repo 已足夠 |
| 投資組合比例 | 不寫入 Prompt | 報告聚焦總經分析與戰術方向，不做個人化資產配置計算 |
| 錯誤處理 | LINE 錯誤通知 | 確保系統異常時你能即時得知，無需主動查看 logs |
```
