import os
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
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

# ── Gemini 模型與速率限制 ──────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_RPM   = 15        # requests per minute（免費方案上限）
GEMINI_TPM   = 250_000   # tokens per minute（免費方案上限）

# Plan A 單次呼叫的預估 token 量：
#   系統提示 ~1,500 + DCC 上下文 ~500 + 4×15篇新聞 ~6,000 + 輸出上限 16,384 ≈ 25,000
_ESTIMATED_TOKENS = 25_000

# 每次請求之間的最小間隔 = 60s / RPM = 4 秒
_MIN_REQUEST_INTERVAL = 60.0 / GEMINI_RPM


class _SlidingWindowRateLimiter:
    """
    滑動視窗速率限制器，同時追蹤 RPM 與 TPM。
    acquire() 在必要時 sleep，確保呼叫 Gemini API 前不超出限制。
    """

    def __init__(self, rpm: int, tpm: int) -> None:
        self._rpm = rpm
        self._tpm = tpm
        self._req_ts: list[float] = []         # 過去 60s 內的請求時間戳
        self._tok_log: list[tuple[float, int]] = []  # (timestamp, tokens)
        self._last_req = 0.0

    def acquire(self, estimated_tokens: int = _ESTIMATED_TOKENS) -> None:
        """阻塞直到可安全發出請求，並記錄本次呼叫。"""
        now = time.monotonic()
        window = 60.0

        # 清除超出視窗的舊記錄
        self._req_ts  = [t for t in self._req_ts if now - t < window]
        self._tok_log = [(t, n) for t, n in self._tok_log if now - t < window]

        # ── RPM 檢查 ──
        if len(self._req_ts) >= self._rpm:
            wait = window - (now - self._req_ts[0]) + 0.5
            if wait > 0:
                logger.info("Gemini RPM 限速：等待 %.1f 秒（已用 %d/%d RPM）", wait, len(self._req_ts), self._rpm)
                time.sleep(wait)
                now = time.monotonic()
                # 重新清理
                self._req_ts  = [t for t in self._req_ts if now - t < window]
                self._tok_log = [(t, n) for t, n in self._tok_log if now - t < window]

        # ── TPM 檢查 ──
        used_tokens = sum(n for _, n in self._tok_log)
        if self._tok_log and used_tokens + estimated_tokens > self._tpm:
            wait = window - (now - self._tok_log[0][0]) + 0.5
            if wait > 0:
                logger.info("Gemini TPM 限速：等待 %.1f 秒（已用 %d/%d TPM）", wait, used_tokens, self._tpm)
                time.sleep(wait)
                now = time.monotonic()
                self._tok_log = [(t, n) for t, n in self._tok_log if now - t < window]

        # ── 最小請求間隔（避免瞬間爆發） ──
        gap = now - self._last_req
        if gap < _MIN_REQUEST_INTERVAL:
            wait = _MIN_REQUEST_INTERVAL - gap
            logger.debug("Gemini 最小間隔：等待 %.2f 秒", wait)
            time.sleep(wait)
            now = time.monotonic()

        # 記錄本次請求
        self._last_req = now
        self._req_ts.append(now)
        self._tok_log.append((now, estimated_tokens))


_rate_limiter = _SlidingWindowRateLimiter(rpm=GEMINI_RPM, tpm=GEMINI_TPM)

# ── Alpha Vantage ─────────────────────────────────────────────────────────────

TOPIC_CONFIG = {
    "monetary": {"icon": "🏦", "title": "貨幣政策・利率前景", "api_topic": "economy_monetary"},
    "macro":    {"icon": "📊", "title": "總體經濟・就業通膨", "api_topic": "economy_macro"},
    "fiscal":   {"icon": "💰", "title": "財政政策・貿易關稅", "api_topic": "economy_fiscal"},
    "markets":  {"icon": "📈", "title": "金融市場・資產動態", "api_topic": "financial_markets"},
}

_SENT_MAP = {
    "Bullish": "bull", "Somewhat-Bullish": "bull",
    "Bearish": "bear", "Somewhat-Bearish": "bear",
}

def _format_av_time(time_str: str) -> str:
    try:
        dt = datetime.strptime(time_str, "%Y%m%dT%H%M%S")
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return ""


def fetch_alpha_vantage_news(api_key: str, topic: str, limit: int = 15) -> list[dict]:
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "NEWS_SENTIMENT",
        "topics": topic,
        "limit": str(limit),
        "sort": "LATEST",
        "apikey": api_key,
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    if "Note" in data:
        logger.warning("Alpha Vantage rate limit: %s", data["Note"])
        return []
    if "Information" in data:
        logger.warning("Alpha Vantage info: %s", data["Information"])
        return []
    if "feed" not in data:
        logger.warning("Alpha Vantage %s: unexpected keys %s", topic, list(data.keys()))
        return []

    results = []
    seen: set[str] = set()
    for item in data["feed"]:
        title = item.get("title", "").strip()
        if not title:
            continue
        key = title[:30].lower()
        if key in seen:
            continue
        seen.add(key)
        sentiment = item.get("overall_sentiment_label", "Neutral")
        results.append({
            "title": title,
            "summary": item.get("summary", "")[:300],
            "source": item.get("source", "").upper(),
            "time_display": _format_av_time(item.get("time_published", "")),
            "sentiment": _SENT_MAP.get(sentiment, "neut"),
            "url": item.get("url", "#"),
        })
    return results[:limit]


def _build_news_text(articles: list[dict]) -> str:
    lines = []
    for i, a in enumerate(articles, 1):
        lines.append(f"[{i}] {a['title']}\n{a['summary']}")
    return "\n\n".join(lines) if lines else "（本主題暫無新聞）"


# ── Gemini ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一位精通全球總體經濟、景氣循環理論與資產配置的量化資深分析師。
以下提供 4 個宏觀主題的 Alpha Vantage 新聞，加上 DCC-GARCH 量化結果。
請一次性生成完整分析報告，嚴格以 JSON 格式回覆，不得包含任何 Markdown 符號或前言說明。

JSON 結構如下（注意：analysis 欄位段落間用 \\n\\n 分隔）：

{
  "topics": {
    "monetary": {
      "title": "主題標題（15字內）",
      "snippet": "主頁卡片摘要（60字內，點出今日最關鍵訊息）",
      "analysis": "深度分析（350-400字，分3-4段，段落間以\\n\\n分隔）",
      "impact_tags": [
        {"color": "red|green|amber|blue", "text": "持倉影響標籤（如：🔴 TLT 短期承壓）"}
      ],
      "key_points": ["重點一（30字內）", "重點二", "重點三", "重點四"]
    },
    "macro":    { "title": "...", "snippet": "...", "analysis": "...", "impact_tags": [...], "key_points": [...] },
    "fiscal":   { "title": "...", "snippet": "...", "analysis": "...", "impact_tags": [...], "key_points": [...] },
    "markets":  { "title": "...", "snippet": "...", "analysis": "...", "impact_tags": [...], "key_points": [...] }
  },
  "synthesis": {
    "macro_theme": {
      "type": "流動性主導 | 基本面主導",
      "title": "一句話點出今日市場定價核心邏輯",
      "description": "說明為何今日是此主線，對各類資產的整體影響框架（80字內）",
      "confidence": 整數0到100
    },
    "sentiment": {
      "score": 數字1到10（1=極度恐慌，10=極度過熱）,
      "label": "中文描述（如：謹慎偏悲觀）",
      "reasoning": "60字內"
    },
    "risk_tags": [{"label": "標籤文字", "type": "risk | warn | positive"}],
    "snippet": "綜合研判主頁卡片摘要（60字內，含主線判斷與情緒評分）",
    "key_points": ["【主線】...", "【風險】...", "【機會】...", "【警示】..."],
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
    ]
  }
}

持倉清單（synthesis.tactical_guidance 必須涵蓋以下全部 13 個）：
核心股票：0050、VOO、QQQ
品質防禦：QUAL、XLV、XLU、XLP、00713
固定收益：00679B、00719B
實物資產：黃金(GLD)、PDBC、現金

關鍵規則：
1. topics 必須有 monetary/macro/fiscal/markets 四個 key，結構完整
2. impact_tags 每個主題 2-4 個，顏色對應：red=利空持倉、green=利多持倉、amber=中性觀察、blue=量化相關
3. synthesis.macro_theme.type 只能是「流動性主導」或「基本面主導」，二選一
4. synthesis.watchlist 恰好 4 個事件
5. topics[*].analysis 不得少於 350 字，段落間用 \\n\\n 分隔
6. synthesis.tactical_guidance 中的 rationale 必須具體提及今日主線，不得寫教科書式通則
7. 所有文字使用繁體中文
"""

USER_PROMPT_TEMPLATE = """\
以下是今日（台灣時間 {date}）4 個宏觀主題的新聞與量化分析：

{dcc_context}
【economy_monetary — 貨幣政策，共 {n_monetary} 篇】
{monetary_news}

【economy_macro — 總體經濟，共 {n_macro} 篇】
{macro_news}

【economy_fiscal — 財政政策，共 {n_fiscal} 篇】
{fiscal_news}

【financial_markets — 金融市場，共 {n_markets} 篇】
{markets_news}

請依照規定 JSON 格式輸出完整報告。"""


def analyze_with_gemini(topic_news: dict[str, list], date: str, dcc_context: str = "") -> dict:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    dcc_block = f"{dcc_context}\n\n" if dcc_context else ""
    user_prompt = USER_PROMPT_TEMPLATE.format(
        date=date,
        dcc_context=dcc_block,
        n_monetary=len(topic_news.get("monetary", [])),
        monetary_news=_build_news_text(topic_news.get("monetary", [])),
        n_macro=len(topic_news.get("macro", [])),
        macro_news=_build_news_text(topic_news.get("macro", [])),
        n_fiscal=len(topic_news.get("fiscal", [])),
        fiscal_news=_build_news_text(topic_news.get("fiscal", [])),
        n_markets=len(topic_news.get("markets", [])),
        markets_news=_build_news_text(topic_news.get("markets", [])),
    )

    max_retries = 4
    for attempt in range(max_retries):
        _rate_limiter.acquire()  # 每次嘗試前強制通過速率限制器
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
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
            if "429" in err_str:
                # 超過每分鐘配額：等待整個視窗重置（60s）再加退避
                wait = 60 + 30 * attempt  # 60, 90, 120s
            elif "503" in err_str:
                # 服務暫時不可用：指數退避
                wait = 30 * (2 ** attempt)  # 30, 60, 120s
            else:
                raise
            logger.warning(
                "Gemini 暫時不可用（attempt %d/%d，%s），%d 秒後重試：%s",
                attempt + 1, max_retries,
                "429 速率限制" if "429" in err_str else "503 服務異常",
                wait, err_str[:120],
            )
            time.sleep(wait)


# ── 文章翻譯（獨立 Gemini 呼叫） ───────────────────────────────────────────────

def _translate_articles_with_gemini(topic_news: dict[str, list]) -> dict[str, list]:
    """
    將四個主題的所有文章標題與摘要翻譯為繁體中文。
    單次 Gemini 呼叫，輸入/輸出皆為 JSON 陣列，失敗時靜默降級（保留英文）。
    """
    items = []
    for topic_key, arts in topic_news.items():
        for i, art in enumerate(arts):
            items.append({
                "id": f"{topic_key}::{i}",
                "t": art["title"],
                "s": art["summary"][:250],
            })

    if not items:
        return topic_news

    prompt = (
        "將以下每筆新聞的標題（t）與摘要（s）翻譯為繁體中文。\n"
        "嚴格以 JSON 陣列回覆，格式：[{\"id\":\"...\",\"t\":\"繁中標題\",\"s\":\"繁中摘要（≤100字）\"}, ...]。\n"
        "不得包含 Markdown、前言或任何說明文字。數字、百分比、股票代碼、人名、機構名直接保留或音譯。\n"
        f"共 {len(items)} 筆，不得省略任何條目：\n"
        + json.dumps(items, ensure_ascii=False)
    )

    max_retries = 3
    for attempt in range(max_retries):
        _rate_limiter.acquire(estimated_tokens=len(items) * 150)
        try:
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    max_output_tokens=8192,
                    temperature=0.1,
                ),
            )
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            translated = {item["id"]: item for item in json.loads(raw.strip())}
            break
        except Exception as e:
            is_last = attempt == max_retries - 1
            err_str = str(e)
            if is_last or ("503" not in err_str and "429" not in err_str):
                logger.warning("文章翻譯失敗，降級使用英文原文：%s", err_str[:120])
                return {
                    topic: [{**a, "title_zh": a["title"], "summary_zh": a["summary"]} for a in arts]
                    for topic, arts in topic_news.items()
                }
            wait = 60 if "429" in err_str else 30 * (2 ** attempt)
            logger.warning("翻譯 Gemini 暫時不可用（attempt %d/%d），%ds 後重試", attempt + 1, max_retries, wait)
            time.sleep(wait)

    result: dict[str, list] = {}
    for topic_key, arts in topic_news.items():
        new_arts = []
        for i, art in enumerate(arts):
            tr = translated.get(f"{topic_key}::{i}", {})
            new_arts.append({
                **art,
                "title_zh":   tr.get("t") or art["title"],
                "summary_zh": tr.get("s") or art["summary"],
            })
        result[topic_key] = new_arts
    return result


# ── HTML report ────────────────────────────────────────────────────────────────

def render_html_report(report_data: dict, date_str: str, dcc_result: dict | None = None) -> str:
    env = Environment(loader=FileSystemLoader("."), autoescape=True)
    template = env.get_template("report_template.html")
    return template.render(data=report_data, date=date_str, dcc=dcc_result)


# ── LINE push ──────────────────────────────────────────────────────────────────

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
    ("核心股票", "📈"),
    ("品質防禦", "🛡️"),
    ("固定收益", "🏦"),
    ("實物資產", "🥇"),
]

_DCC_TICKERS = {"VOO", "TLT", "GLD", "黃金(GLD)"}


def _dcc_trend(current: float, avg30: float, pair: str) -> str:
    diff = current - avg30
    if abs(diff) < 0.02:
        arrow = "→ 持平"
    elif diff > 0:
        arrow = "↑ 上升"
    else:
        arrow = "↓ 下降"
    if pair == "VOO_TLT":
        if diff < -0.02:
            return f"{arrow}（避險效果增強）"
        elif diff > 0.02:
            return f"{arrow}（避險效果減弱）"
    return arrow


def _format_dcc_section(d: dict) -> str:
    c, c30 = d["corr"], d["corr_30d_avg"]
    v = d["vol_annual"]
    hrp, rp = d["hrp"], d["risk_parity"]
    lines = [
        "📐 量化配置分析",
        f"• VOO↔TLT：{c['VOO_TLT']:+.2f} {_dcc_trend(c['VOO_TLT'], c30['VOO_TLT'], 'VOO_TLT')}",
        f"• VOO↔GLD：{c['VOO_GLD']:+.2f} {_dcc_trend(c['VOO_GLD'], c30['VOO_GLD'], 'VOO_GLD')}",
        f"• GLD↔TLT：{c['TLT_GLD']:+.2f} {_dcc_trend(c['TLT_GLD'], c30['TLT_GLD'], 'TLT_GLD')}",
        f"• 波動率：VOO {v['VOO']:.1%} | TLT {v['TLT']:.1%} | GLD {v['GLD']:.1%}",
        f"• HRP：VOO {hrp['VOO']:.0%} / TLT {hrp['TLT']:.0%} / GLD {hrp['GLD']:.0%}",
        f"• Risk Parity：VOO {rp['VOO']:.0%} / TLT {rp['TLT']:.0%} / GLD {rp['GLD']:.0%}",
    ]
    return "\n".join(lines)


def send_line_message(
    synthesis: dict, report_url: str, date_str: str, dcc_result: dict | None = None
) -> None:
    theme = synthesis.get("macro_theme", {})
    sentiment = synthesis.get("sentiment", {})
    tags = " · ".join([t["label"] for t in synthesis.get("risk_tags", [])])

    by_group: dict[str, list] = {}
    for g in synthesis.get("tactical_guidance", []):
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
        f"🧭 今日主線：{theme.get('type', '')}（信心度 {theme.get('confidence', '')}%）\n"
        f"{theme.get('title', '')}\n"
        f"{theme.get('description', '')}\n\n"
        f"📈 情緒評分：{sentiment.get('score', '')}/10 {sentiment.get('label', '')}\n"
        f"{sentiment.get('reasoning', '')}\n\n"
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        # Step 0: DCC-GARCH 量化分析（失敗時靜默降級）
        dcc_context = ""
        dcc_result = None
        try:
            dcc_result = dcc_garch.run_dcc_analysis()
            dcc_context = dcc_garch.format_dcc_for_prompt(dcc_result)
            logger.info(
                "DCC-GARCH analysis complete (α=%.4f β=%.4f)",
                dcc_result["dcc_alpha"], dcc_result["dcc_beta"],
            )
        except Exception as e:
            logger.warning("DCC-GARCH skipped: %s", e)

        # Step 1: fetch Alpha Vantage news (4 topics)
        av_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
        topic_news: dict[str, list] = {}
        for i, (topic_key, cfg) in enumerate(TOPIC_CONFIG.items()):
            if i > 0:
                time.sleep(1.5)  # Alpha Vantage 免費方案：1 req/s 限制，留 0.5s 緩衝
            try:
                articles = fetch_alpha_vantage_news(av_key, cfg["api_topic"])
                topic_news[topic_key] = articles
                logger.info("Alpha Vantage %s: %d articles", cfg["api_topic"], len(articles))
            except Exception as e:
                logger.warning("Alpha Vantage %s failed: %s", cfg["api_topic"], e)
                topic_news[topic_key] = []

        if not any(topic_news.values()):
            send_error_notification("Alpha Vantage 抓取", Exception("所有主題均無資料，中止執行"))
            return

        # Step 1.5: 翻譯文章標題與摘要為繁體中文（獨立 Gemini 呼叫，失敗時靜默降級）
        topic_news = _translate_articles_with_gemini(topic_news)
        logger.info("Article translation complete (total %d articles)",
                    sum(len(v) for v in topic_news.values()))

        # Step 2: Gemini 一次性分析（Plan A）
        tw_time = datetime.now(timezone(timedelta(hours=8)))
        date_str = tw_time.strftime("%Y 年 %m 月 %d 日")
        try:
            report_data = analyze_with_gemini(topic_news, date_str, dcc_context)
            logger.info("Gemini analysis complete")
        except Exception as e:
            send_error_notification("Gemini 分析", e)
            return

        # Step 3: 合併已翻譯文章與分析結果，預處理段落
        for key in TOPIC_CONFIG:
            topic = report_data.get("topics", {}).get(key, {})
            topic.pop("articles", None)  # 移除分析呼叫可能殘留的欄位
            topic["news"] = topic_news.get(key, [])  # 已含 title_zh / summary_zh
            topic["analysis_paragraphs"] = [
                p.strip() for p in topic.get("analysis", "").split("\n\n") if p.strip()
            ]

        # Step 4: generate HTML report
        report_filename = ""
        try:
            Path("reports").mkdir(exist_ok=True)
            html_content = render_html_report(report_data, date_str, dcc_result)
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
            send_line_message(report_data.get("synthesis", {}), report_url, date_str, dcc_result)
            logger.info("LINE push sent")
        except Exception as e:
            send_error_notification("LINE 推播", e)

    except Exception as e:
        send_error_notification("未知錯誤", e)
        raise


if __name__ == "__main__":
    main()
