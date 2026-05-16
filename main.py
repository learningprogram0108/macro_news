import os
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import finnhub
from google import genai
from jinja2 import Environment, FileSystemLoader
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage
)
from dotenv import load_dotenv
import dcc_garch

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Finnhub ────────────────────────────────────────────────────────────────

def fetch_finnhub_news() -> list[dict]:
    client = finnhub.Client(api_key=os.environ["FINNHUB_API_KEY"])
    results = []
    # 抓 general（20篇）+ forex（10篇）作為兩個來源合併
    for category, limit in [("general", 20), ("forex", 10)]:
        try:
            news = client.general_news(category, min_id=0)
            for item in news[:limit]:
                results.append({
                    "source": f"Finnhub/{category}",
                    "title": item.get("headline", ""),
                    "summary": item.get("summary", "")[:200],
                    "datetime": item.get("datetime", ""),
                })
        except Exception as e:
            logger.warning("Finnhub category %s failed: %s", category, e)
    return results

# ── FMP ────────────────────────────────────────────────────────────────────

# FMP 免費 API 自 2025-08 起所有新聞 endpoint 已停用（402/403）。
# 保留此函式架構，發現 4xx 錯誤時靜默返回空列表，不觸發 LINE 錯誤通知。
def fetch_fmp_news() -> list[dict]:
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if not fmp_key:
        return []
    url = "https://financialmodelingprep.com/stable/news/general-latest"
    try:
        resp = requests.get(url, params={"apikey": fmp_key, "limit": 10}, timeout=10)
        if resp.status_code in (402, 403):
            logger.info("FMP endpoint unavailable (status %d), skipping", resp.status_code)
            return []
        resp.raise_for_status()
        results = []
        for item in resp.json():
            results.append({
                "source": "FMP",
                "title": item.get("title", ""),
                "summary": item.get("text", "")[:200],
            })
        return results
    except Exception as e:
        logger.warning("FMP fetch failed: %s", e)
        return []

# ── Dedup ──────────────────────────────────────────────────────────────────

def deduplicate_news(news_list: list[dict]) -> list[dict]:
    seen: set[str] = set()
    cleaned = []
    for item in news_list:
        key = item["title"][:30].lower().strip()
        if key not in seen:
            seen.add(key)
            cleaned.append(item)
    return cleaned

def build_news_text(news_list: list[dict]) -> str:
    lines = []
    for i, item in enumerate(news_list, 1):
        lines.append(f"[{i}] [{item['source']}] {item['title']}\n{item['summary']}")
    return "\n\n".join(lines)

# ── Gemini ─────────────────────────────────────────────────────────────────

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

{dcc_context}{news_text}

請依照規定格式輸出 JSON 分析報告。
"""

def analyze_with_gemini(news_text: str, date: str, dcc_context: str = "") -> dict:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    dcc_block = f"{dcc_context}\n\n" if dcc_context else ""
    user_prompt = USER_PROMPT_TEMPLATE.format(date=date, dcc_context=dcc_block, news_text=news_text)

    max_retries = 4
    backoff = 30  # 秒，每次翻倍
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_prompt,
                config=genai.types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    max_output_tokens=16384,
                    temperature=0.3,
                ),
            )
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as e:
            is_last = attempt == max_retries - 1
            if is_last:
                raise
            err_str = str(e)
            # 只對 503/429 重試，其他錯誤直接拋出
            if "503" not in err_str and "429" not in err_str:
                raise
            wait = backoff * (2 ** attempt)
            logger.warning("Gemini 暫時不可用（attempt %d/%d），%d 秒後重試：%s", attempt + 1, max_retries, wait, err_str[:120])
            time.sleep(wait)

# ── HTML report ────────────────────────────────────────────────────────────

def render_html_report(report_data: dict, date_str: str) -> str:
    env = Environment(loader=FileSystemLoader("."), autoescape=True)
    template = env.get_template("report_template.html")
    return template.render(data=report_data, date=date_str)

# ── LINE push ──────────────────────────────────────────────────────────────

def _line_push(message: str) -> None:
    configuration = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message(PushMessageRequest(
            to=os.environ["LINE_USER_ID"],
            messages=[TextMessage(text=message)],
        ))

_ACTION_EMOJI = {
    "add":    "🟢",
    "hold":   "⚪",
    "watch":  "🟡",
    "reduce": "🔴",
    "cash":   "💵",
}

_GROUP_ORDER = [
    ("核心股票",   "📈"),
    ("品質防禦",   "🛡️"),
    ("固定收益",   "🏦"),
    ("實物資產",   "🥇"),
]

# VOO/TLT/GLD 為 DCC 量化標的，永遠顯示 rationale
_DCC_TICKERS = {"VOO", "TLT", "GLD", "黃金(GLD)"}

def _dcc_trend(current: float, avg30: float, pair: str) -> str:
    diff = current - avg30
    if abs(diff) < 0.02:
        arrow = "→ 持平"
    elif diff > 0:
        arrow = "↑ 上升"
    else:
        arrow = "↓ 下降"
    # VOO↔TLT：負相關越強代表避險效果越好
    if pair == "VOO_TLT":
        if diff < -0.02:
            return f"{arrow}（避險效果增強）"
        elif diff > 0.02:
            return f"{arrow}（避險效果減弱）"
    return arrow


def _format_dcc_section(d: dict) -> str:
    c, c30 = d["corr"], d["corr_30d_avg"]
    v = d["vol_annual"]
    ms, rp = d["max_sharpe"], d["risk_parity"]
    lines = [
        "📐 量化配置分析",
        f"• VOO↔TLT：{c['VOO_TLT']:+.2f} {_dcc_trend(c['VOO_TLT'], c30['VOO_TLT'], 'VOO_TLT')}",
        f"• VOO↔GLD：{c['VOO_GLD']:+.2f} {_dcc_trend(c['VOO_GLD'], c30['VOO_GLD'], 'VOO_GLD')}",
        f"• GLD↔TLT：{c['TLT_GLD']:+.2f} {_dcc_trend(c['TLT_GLD'], c30['TLT_GLD'], 'TLT_GLD')}",
        f"• 波動率：VOO {v['VOO']:.1%} | TLT {v['TLT']:.1%} | GLD {v['GLD']:.1%}",
        f"• Max Sharpe：VOO {ms['VOO']:.0%} / TLT {ms['TLT']:.0%} / GLD {ms['GLD']:.0%}",
        f"• Risk Parity：VOO {rp['VOO']:.0%} / TLT {rp['TLT']:.0%} / GLD {rp['GLD']:.0%}",
    ]
    return "\n".join(lines)


def send_line_message(report_data: dict, report_url: str, date_str: str, dcc_result: dict | None = None) -> None:
    theme = report_data["macro_theme"]
    sentiment = report_data["sentiment"]
    tags = " · ".join([t["label"] for t in report_data["risk_tags"]])

    # 以 group 欄位建立索引
    by_group: dict[str, list] = {}
    for g in report_data["tactical_guidance"]:
        by_group.setdefault(g["group"], []).append(g)

    sections: list[str] = []
    for group_name, emoji in _GROUP_ORDER:
        holdings = by_group.get(group_name, [])
        if not holdings:
            continue
        lines = [f"{emoji}【{group_name}】"]
        for h in holdings:
            action_icon = _ACTION_EMOJI.get(h["action_type"], "•")
            show_rationale = (
                h["action_type"] in ("add", "reduce", "cash")
                or h["ticker"] in _DCC_TICKERS
            )
            if show_rationale:
                lines.append(f"{action_icon} {h['ticker']} {h['action']}")
                lines.append(f"   └ {h['rationale']}")
            else:
                lines.append(f"{action_icon} {h['ticker']} {h['action']}")
        sections.append("\n".join(lines))

    holdings_text = "\n\n".join(sections)
    dcc_section = f"\n{_format_dcc_section(dcc_result)}\n" if dcc_result else ""

    message = (
        f"📊 每日總經 AI 監控報告\n"
        f"{date_str} 台灣時間 07:00\n\n"
        f"🧭 今日主線：{theme['type']}（信心度 {theme['confidence']}%）\n"
        f"{theme['title']}\n"
        f"{theme['description']}\n\n"
        f"📈 情緒評分：{sentiment['score']}/10 {sentiment['label']}\n"
        f"{sentiment['reasoning']}\n\n"
        f"⚠️ 風險標籤：{tags}\n"
        f"{dcc_section}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 持倉配置分析\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{holdings_text}\n\n"
        f"📋 完整報告：{report_url}"
    )
    _line_push(message)

def send_error_notification(step: str, error: Exception) -> None:
    message = (
        f"⚠️ 總經報告系統異常\n"
        f"步驟：{step}\n"
        f"錯誤：{str(error)[:200]}\n"
        f"請手動檢查 GitHub Actions logs。"
    )
    try:
        _line_push(message)
    except Exception as e:
        logger.error("LINE error notification failed: %s", e)

# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        # Step 0: DCC-GARCH 量化分析（失敗時靜默降級，不中止主流程）
        dcc_context = ""
        dcc_result = None
        try:
            dcc_result = dcc_garch.run_dcc_analysis()
            dcc_context = dcc_garch.format_dcc_for_prompt(dcc_result)
            logger.info("DCC-GARCH analysis complete (α=%.4f β=%.4f)", dcc_result["dcc_alpha"], dcc_result["dcc_beta"])
        except Exception as e:
            logger.warning("DCC-GARCH skipped: %s", e)

        # Step 1: fetch news
        try:
            finnhub_news = fetch_finnhub_news()
            logger.info("Finnhub: %d articles", len(finnhub_news))
        except Exception as e:
            send_error_notification("Finnhub 抓取", e)
            finnhub_news = []

        # FMP 靜默處理（endpoint 已停用時不發 LINE 通知）
        fmp_news = fetch_fmp_news()
        logger.info("FMP: %d articles", len(fmp_news))

        if not finnhub_news and not fmp_news:
            send_error_notification("資料抓取", Exception("所有來源均無資料，中止執行"))
            return

        # Step 2: clean & dedup
        all_news = deduplicate_news(finnhub_news + fmp_news)
        news_text = build_news_text(all_news)
        logger.info("After dedup: %d articles", len(all_news))

        # Step 3: Gemini analysis
        tw_time = datetime.now(timezone(timedelta(hours=8)))
        date_str = tw_time.strftime("%Y 年 %m 月 %d 日")
        try:
            report_data = analyze_with_gemini(news_text, date_str, dcc_context)
            logger.info("Gemini analysis complete")
        except Exception as e:
            send_error_notification("Gemini 分析", e)
            return

        # Step 4: generate HTML report
        report_filename = ""
        try:
            Path("reports").mkdir(exist_ok=True)
            html_content = render_html_report(report_data, date_str)
            report_filename = f"reports/report_{tw_time.strftime('%Y%m%d')}.html"
            with open(report_filename, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info("HTML saved: %s", report_filename)
        except Exception as e:
            send_error_notification("HTML 生成", e)

        # Step 5: LINE push
        try:
            repo = os.environ.get("GITHUB_REPOSITORY", "your/repo")
            owner, repo_name = repo.split("/", 1) if "/" in repo else (repo, "")
            if report_filename and repo_name:
                report_url = f"https://{owner}.github.io/{repo_name}/{report_filename}"
            else:
                report_url = "（報告連結暫不可用）"
            send_line_message(report_data, report_url, date_str, dcc_result)
            logger.info("LINE push sent")
        except Exception as e:
            send_error_notification("LINE 推播", e)

    except Exception as e:
        send_error_notification("未知錯誤", e)
        raise


if __name__ == "__main__":
    main()
