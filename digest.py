#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全球宏观 + 地缘政治 24 小时简报 · GitHub Actions 版

流程：
  1. 双通道检索（11 路，限定 24 小时窗口；Tavily 优先，失败降级 Google News RSS）
  2. DeepSeek 生成自包含 HTML 报告（深绿主题 / card-based / 移动优先 / 打印就绪）
  3. DeepSeek 生成精简邮件正文（Top 5 + KPI 表 + 日历）
  4. QQ 邮箱 SMTP 投递（HTML 正文 + HTML 附件）
  5. 报告落盘 archive/YYYY-MM-DD.html

必需环境变量（GitHub Actions Secrets）：
  DEEPSEEK_API_KEY  DeepSeek API Key
  TAVILY_API_KEY    Tavily 搜索 API Key（https://app.tavily.com ，免费额度 1000 次/月）
  QQ_SMTP_USER      发件 QQ 邮箱，如 ttr169@qq.com
  QQ_SMTP_CODE      QQ 邮箱「授权码」（不是登录密码）
可选：
  MAIL_TO           收件人，默认同 QQ_SMTP_USER
  DEEPSEEK_MODEL    默认 deepseek-chat
"""

from __future__ import annotations

import datetime as dt
import os
import re
import smtplib
import ssl
import sys
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

import requests

# ---------------------------------------------------------------- 基础配置

JST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "archive"

TAVILY_URL = "https://api.tavily.com/search"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

HTTP_TIMEOUT = 60
LLM_TIMEOUT = 600


def die(msg: str) -> "None":
    print(f"[FATAL] {msg}", file=sys.stderr)
    sys.exit(1)


def require(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        die(f"缺少环境变量 {name}。请在 GitHub 仓库 Settings → Secrets and variables → Actions 中配置。")
    return v


# ---------------------------------------------------------------- 检索

def build_queries(now: dt.datetime) -> list[str]:
    """中英双语、覆盖宏观/地缘/能源/AI 的 11 路查询。

    ⚠️ 查询式必须「聚焦」—— 一条查询只放一个主题，**并且不要写日期**。
    2026-09-11 实测（Google News RSS + when:1d，同一时段同一批话题）：

        「伊朗 中东 俄罗斯 乌克兰 台海 最新 2026年9月11日 重大事件」 → 1 条
        「Iran Russia Ukraine Middle East Taiwan major news ...」    → 1 条
        「伊朗 以色列 霍尔木兹 红海」                                → 8 条
        「俄乌 停火 谈判 制裁」                                      → 3 条
        「Iran Israel Houthi Red Sea conflict」                      → 8 条

    原因：Google News 把长查询当近似 AND 处理，关键词堆叠 + 把日期写进查询
    会大幅压低召回 —— 结果是地缘板块（本报告的核心）每路只拿到 1 条素材。
    **时间窗交给检索端**（RSS 的 when:1d / Tavily 的 days=1），查询里不要再写日期。
    """
    return [
        # —— 中文（zh-CN / CN）——
        "央行 利率决议 降息 加息 美联储",
        "伊朗 以色列 霍尔木兹 红海 冲突",
        "俄乌 停火 谈判 制裁",
        "台海 中美 军事 演习",
        "原油 油价 OPEC",
        "英伟达 OpenAI 人工智能 芯片 数据中心",
        # —— 英文（en-US / US）——
        "Fed ECB BOJ interest rate decision markets",
        "Iran Israel Houthi Red Sea conflict",
        "Russia Ukraine war ceasefire talks",
        "oil price OPEC Brent WTI supply",
        "Nvidia OpenAI AI datacenter chips",
    ]


def tavily_search(query: str, api_key: str, max_results: int = 6) -> list[dict]:
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "advanced",
        "topic": "news",
        "days": 1,
        "max_results": max_results,
        "include_answer": False,
    }
    try:
        r = requests.post(TAVILY_URL, json=payload, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json().get("results", []) or []
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 检索失败（{query}）：{e}")
        return []


def gnews_search(query: str, max_results: int = 6) -> list[dict]:
    """免密钥降级检索：Google News RSS。

    Tavily 不可用时自动接管。RSS 自带 when:1d 时间窗，
    且对新闻类检索的时效性好于通用搜索 API。
    """
    import urllib.parse
    import xml.etree.ElementTree as ET

    cjk = any("\u4e00" <= ch <= "\u9fff" for ch in query)
    hl, gl, ceid = ("zh-CN", "CN", "CN:zh-Hans") if cjk else ("en-US", "US", "US:en")
    q = urllib.parse.quote(f"{query} when:1d")
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; daily-news-digest/1.0)"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out = []
        for item in root.iter("item"):
            out.append({
                "title": (item.findtext("title") or "").strip(),
                "url": (item.findtext("link") or "").strip(),
                "published_date": (item.findtext("pubDate") or "").strip(),
                "content": re.sub(r"<[^>]+>", "", (item.findtext("description") or "")).strip(),
            })
            if len(out) >= max_results:
                break
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Google News RSS 失败（{query}）：{e}")
        return []


_TAVILY_DEAD = False  # Tavily 一旦确认为失效，后续查询直接跳过，避免逐路空等


def search(query: str, tavily_key: str, max_results: int = 6) -> list[dict]:
    """双通道检索：Tavily 优先，失败或返回空则降级 Google News RSS。

    Tavily 失效（401/403 等）时置全局 _TAVILY_DEAD 标志，
    避免 N 路查询各自重试一次、白白拖慢整体耗时。
    """
    global _TAVILY_DEAD
    if tavily_key and not _TAVILY_DEAD:
        rs = tavily_search(query, tavily_key, max_results)
        if rs:
            return rs
        _TAVILY_DEAD = True
        print("[WARN] Tavily 不可用，后续查询全部改用 Google News RSS。")
    return gnews_search(query, max_results)


MAX_CONTEXT_ITEMS = 70   # 硬上限：无论检索通道返回多少，编进提示词的素材不超过这个数


def build_context(searches: list[tuple[str, list[dict]]]) -> str:
    """把检索结果编成带编号的上下文，便于模型引用来源。

    设总量硬上限 `MAX_CONTEXT_ITEMS`：Tavily 恢复后每路最多 6 条 × 11 路 = 66 条，
    尚在上限内；但若日后加大 max_results，这里能兜住提示词体积，避免超上下文窗口。
    """
    blocks, n = [], 0
    for q, results in searches:
        lines = [f"\n===== 查询：{q} ====="]
        if not results:
            lines.append("（无结果）")
        for r in results:
            if n >= MAX_CONTEXT_ITEMS:
                break
            n += 1
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            published = (r.get("published_date") or r.get("published") or "").strip()
            content = re.sub(r"\s+", " ", (r.get("content") or "").strip())[:1400]
            lines.append(f"[{n}] {title}\n    来源: {url}\n    时间: {published}\n    摘要: {content}")
        blocks.append("\n".join(lines))
        if n >= MAX_CONTEXT_ITEMS:
            blocks.append(f"\n（已达素材上限 {MAX_CONTEXT_ITEMS} 条，其余查询结果略去）")
            break
    return "\n".join(blocks)


# ---------------------------------------------------------------- 提示词

REPORT_SPEC = """你是一名资深宏观研究分析师，正在撰写「全球宏观 + 地缘政治 24 小时简报」。

【输入】下方是刚刚检索到的、限定在过去 24 小时内的多源新闻素材，每条带编号 [n]、URL、时间与摘要。

【硬性要求 —— 违反即为不合格】
1. 禁止编造任何数据。所有数字、人名、机构、时间必须能在素材中找到出处。
2. 每条 ★★★★★ 与 ★★★★ 级论断必须有 ≥2 个独立来源交叉验证；单源内容必须显式标注「（单源，待确认）」。
3. 不同来源给出互相矛盾的数字时，**不要自行调和**，要在报告「数据分歧提示」章节并列列出各来源的数字与出处，并说明可能原因。
4. 数字宁缺勿错。拿不准就不写，不要用估算值填充。
5. 只覆盖检索窗口内的事件；素材不足时如实减少条目数量，不要凑数。

【重要性分级】
★★★★★ 关键（红左边框 #c0392b）：改变宏观/地缘格局、直接影响大类资产定价
★★★★  重要（金左边框 #d4a017）：重要但影响范围次之
★★★    关注（蓝左边框 #3d8ec9）：值得跟踪、暂无立即影响

【输出格式 —— 一个完整、自包含、无外部依赖的 HTML 文档】
- 深绿主色 #1f6f43；card-based 布局；移动优先（含 @media max-width:640px 适配）
- 打印就绪（含 @media print，避免卡片跨页断裂）
- 配色遵循中国市场惯例：**涨=红 #c0392b，跌=绿 #27ae60**（与欧美相反，切勿弄反）
- 结构顺序：
  1. 头部：标题 + 报告生成时间(JST) + 监测窗口 + 信源数 + 事件数 + 若干关键词标签
  2. 一页速览 Top 5 关键事件（带分级星级）
  3. 关键指标看板（KPI 卡片网格：油价/黄金/美元指数/USD-JPY/美债/加息概率/通胀等，至少 8 个）
  4. A. 全球宏观（央行/利率/汇市/数据）
  5. B. 地缘政治（美伊/俄乌/中东/欧美）
  6. C. 能源传导链（原油/天然气/煤炭/航运，要求写出「事件→运输→库存→炼厂→成品油→价格」的传导链条）
  7. D. AI·科技
  8. E. 全球市场速览（表格：市场/收盘/变动/备注）
  9. 未来数日重点日历（表格：时间/事件/市场预期）
  10. 数据分歧提示（并列呈现冲突数字，不做调和）
  11. 来源清单与可验证性说明
  12. 页脚注明「仅供研究参考，不构成投资建议」
- 每条事件卡片末尾用一行小字标注「来源：xxx、yyy（n 源交叉）」
- 优先采信：央视、新华社、路透 Reuters、Bloomberg、华尔街见闻、东方财富、每日经济新闻、新浪财经、FX678、证券时报、上海证券报；回避自媒体标题党与无出处社媒传闻

【输出约束】
- 只输出 HTML 源码本身，不要包裹 ```html 代码围栏，不要任何解释性前后缀
- 单个完整 HTML 文件，CSS 内联在 <style> 中，不要引用任何外部资源（无 CDN、无外链图片、无外部字体）
- 体积控制在 40KB 以内
- 语言：简体中文；专业术语可保留英文原名
"""

EMAIL_SPEC = """基于同一批素材，生成一封**精简邮件正文**（内联样式的 HTML 片段，不是完整文档）。

要求：
- 最外层一个 <div>，max-width:760px，内联 style，不要 <html>/<head>/<body>，不要外部资源
- 结构：① 深绿头部（#1f6f43）标题 + 生成时间/监测窗口/信源数 ② Top 5 关键事件（<ol>，含星级与加粗标题）③ 关键指标表格（≥8 行，涨红跌绿）④ 未来数日重点日历（<ul>）⑤ 数据分歧提示（黄底块）⑥ 免责声明小字
- 只输出 HTML 片段源码，不要代码围栏，不要解释
- 体积控制在 12KB 以内
"""


def strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:html)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


# ---------------------------------------------------------------- LLM

def llm_call(messages: list[dict], api_key: str, max_tokens: int, temperature: float = 0.3) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    r = requests.post(DEEPSEEK_URL, headers=headers, json=body, timeout=LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------- 邮件

def send_email(subject: str, body_html: str, attachment: Path,
               smtp_user: str, smtp_code: str, mail_to: str) -> None:
    msg = MIMEMultipart("mixed")
    msg["From"] = formataddr((str(Header("全球宏观简报", "utf-8")), smtp_user))
    msg["To"] = mail_to
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)

    msg.attach(MIMEText(body_html, "html", "utf-8"))

    raw = attachment.read_bytes()
    part = MIMEApplication(raw, _subtype="html")
    part.add_header("Content-Disposition", "attachment",
                    filename=("utf-8", "", attachment.name))
    msg.attach(part)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
        s.login(smtp_user, smtp_code)
        s.sendmail(smtp_user, [mail_to], msg.as_string())
    print(f"[OK] 邮件已投递 → {mail_to}")


# ---------------------------------------------------------------- 主流程

def main() -> None:
    deepseek_key = require("DEEPSEEK_API_KEY")
    # Tavily 为可选：缺失或失效时自动降级 Google News RSS（免密钥）
    tavily_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not tavily_key:
        print("[WARN] 未提供 TAVILY_API_KEY，使用 Google News RSS 降级检索。")
    smtp_user = require("QQ_SMTP_USER")
    smtp_code = require("QQ_SMTP_CODE")
    mail_to = os.getenv("MAIL_TO", smtp_user).strip()

    now = dt.datetime.now(JST)
    day = now.strftime("%Y-%m-%d")
    window_start = (now - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    window_end = now.strftime("%Y-%m-%d %H:%M")
    stamp = now.strftime("%Y-%m-%d %H:%M")

    print(f"[INFO] 运行日期(JST): {day}　监测窗口: {window_start} → {window_end} JST")

    # 1) 检索
    queries = build_queries(now)
    searches: list[tuple[str, list[dict]]] = []
    total = 0
    for q in queries:
        rs = search(q, tavily_key)
        searches.append((q, rs))
        total += len(rs)
        print(f"[INFO] 检索「{q}」→ {len(rs)} 条")
    print(f"[INFO] 素材合计 {total} 条")

    if total == 0:
        die("全部检索均无结果，终止运行（不生成空报告）。")

    context = build_context(searches)

    meta = (
        f"【本次任务元信息】\n"
        f"- 报告日期：{day}\n"
        f"- 生成时间戳：{stamp} JST\n"
        f"- 监测窗口：{window_start} → {window_end} JST（约 24 小时）\n"
        f"- 检索路数：{len(queries)} 路，素材 {total} 条\n"
        f"- 事件条目目标：20–26 条\n\n"
        f"【素材开始】\n{context}\n【素材结束】"
    )

    # 2) 生成完整报告
    print("[INFO] 生成 HTML 报告 …")
    report = strip_fences(llm_call(
        [{"role": "system", "content": REPORT_SPEC},
         {"role": "user", "content": meta + "\n\n请按规范输出完整 HTML 报告。"}],
        deepseek_key, max_tokens=20000,
    ))
    if "<html" not in report.lower():
        die("模型未返回合法 HTML，终止运行。")
    print(f"[INFO] 报告生成完毕（{len(report.encode('utf-8'))/1024:.1f} KB）")

    # 3) 生成邮件正文
    print("[INFO] 生成邮件正文 …")
    mail_body = strip_fences(llm_call(
        [{"role": "system", "content": EMAIL_SPEC},
         {"role": "user", "content": meta + "\n\n请输出邮件正文 HTML 片段。"}],
        deepseek_key, max_tokens=6000,
    ))
    if "<div" not in mail_body.lower():
        print("[WARN] 邮件正文异常，改用报告摘要回退。")
        mail_body = f'<div style="font-family:sans-serif">今日简报已生成，完整内容见附件。</div>'

    # 4) 落盘
    ARCHIVE.mkdir(exist_ok=True)
    out = ARCHIVE / f"{day}.html"
    out.write_text(report, encoding="utf-8")
    print(f"[OK] 报告已保存：archive/{out.name}")

    # 5) 投递
    subject = f"【全球宏观+地缘 24h 简报】{day}"
    try:
        send_email(subject, mail_body, out, smtp_user, smtp_code, mail_to)
    except Exception as e:  # noqa: BLE001
        die(f"邮件投递失败：{e}（报告已保存至 archive/{out.name}）")

    print("[DONE] 全部完成。")


if __name__ == "__main__":
    main()
