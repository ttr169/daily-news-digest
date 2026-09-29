#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
华尔街见闻 · 过去 24 小时要闻简报（云端版 / GitHub Actions）

设计目标：在用户**电脑关机**时也能照常投递。
因此完全不依赖 WorkBuddy / MCP / 本机进程，只用：

  1. 华尔街见闻**公开**内容接口（无需任何 API Key）
     - 资讯流: /apiv1/content/information-flow?channel=global-channel
     - 正文:   /apiv1/content/articles/{id}
  2. DeepSeek API 做摘要与三级重要性分级（需要 DEEPSEEK_API_KEY）
  3. QQ 邮箱 SMTP 自发自收投递（需要 QQ_SMTP_USER / QQ_SMTP_CODE）

跨通道去重：以「收件箱里有没有当天主题的邮件」为准（IMAP 账本）。
本机 WorkBuddy 投递的、GitHub 投递的、外部调度器投递的，一视同仁。

无第三方依赖，仅用标准库 + requests。
"""

import base64
import datetime as dt
import html
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

import requests

# ------------------------------------------------------------------ 常量

JST = dt.timezone(dt.timedelta(hours=9))
CST = dt.timezone(dt.timedelta(hours=8))            # 北京时间（华尔街见闻口径）
ROOT = Path(__file__).resolve().parent
# 归档目录必须与旧通道（daily.yml → archive/）隔离，否则两条简报的幂等闸门互相顶掉
ARCHIVE = ROOT / "archive" / "wscn"

WSCN_API = "https://api-one.wallstcn.com/apiv1/content/information-flow"
WSCN_ARTICLE = "https://api-one.wallstcn.com/apiv1/content/articles/{id}?extract=0"

# 简报标识：出现在邮件主题里，同时用于 IMAP 查重（区分同收件箱里的其他简报）
MAIL_TAG = "【华尔街见闻·24h要闻简报】"

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
LLM_MODEL = "deepseek-chat"
LLM_TIMEOUT = 900
LLM_ATTEMPTS = 3
SMTP_ATTEMPTS = 3

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://wallstreetcn.com/",
    "Accept": "application/json, text/plain, */*",
}

WINDOW_HOURS = 24
MAX_ARTICLES = 40          # 进入候选池的条目上限
MAX_DETAIL = 8             # 精读正文的篇数上限（控制 token）


def die(msg: str) -> None:
    print(f"[FATAL] {msg}", file=sys.stderr)
    sys.exit(1)


def require(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        die(f"缺少必需的环境变量/密钥：{name}")
    return v


# ------------------------------------------------------------------ 华尔街见闻抓取

def fetch_flow(sess: requests.Session, limit: int = 40) -> list[dict]:
    """拉取全球资讯流。返回 [{id,title,display_time,uri}]"""
    params = {"channel": "global-channel", "limit": str(limit)}
    url = f"{WSCN_API}?{urllib.parse.urlencode(params)}"
    last = ""
    for i in range(1, 4):
        try:
            r = sess.get(url, headers=UA, timeout=45)
            r.raise_for_status()
            items = (r.json().get("data") or {}).get("items") or []
            out = []
            for it in items:
                res = it.get("resource") or it
                rid, title = res.get("id"), (res.get("title") or "").strip()
                ts = res.get("display_time")
                if not rid or not title or not ts:
                    continue          # 跳过广告位/无标题占位（id=17969 之类）
                out.append({"id": rid, "title": title, "display_time": int(ts),
                            "uri": res.get("uri") or f"/articles/{rid}"})
            print(f"[INFO] 资讯流拉取 {len(out)} 条（有效）")
            return out
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            print(f"[WARN] 资讯流拉取失败（第 {i}/3 次）：{last}")
            time.sleep(min(2 ** i * 3, 20))
    die(f"华尔街见闻资讯流连续 3 次拉取失败：{last}")


def fetch_article(sess: requests.Session, aid: int) -> str:
    """拉取文章正文（HTML 片段）。失败返回空串。"""
    try:
        r = sess.get(WSCN_ARTICLE.format(id=aid), headers=UA, timeout=45)
        r.raise_for_status()
        return ((r.json().get("data") or {}).get("content") or "")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 正文拉取失败 id={aid}：{type(e).__name__}: {e}")
        return ""


def html_to_text(raw: str) -> str:
    """把正文 HTML 剥成纯文本，压缩空白。"""
    if not raw:
        return ""
    t = re.sub(r"(?is)<(script|style|audio|video)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\u3000]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def select_window(items: list[dict], now: dt.datetime) -> list[dict]:
    """筛选过去 WINDOW_HOURS 小时内的条目，按时间倒序。"""
    cutoff = (now - dt.timedelta(hours=WINDOW_HOURS)).timestamp()
    sel = [it for it in items if it["display_time"] >= cutoff]
    sel.sort(key=lambda x: x["display_time"], reverse=True)
    return sel[:MAX_ARTICLES]


# 精读优先级：早餐 FM 早报 > 收盘综述 > 地缘/能源 > 机构观点
PRIORITY_PATTERNS = [
    (0, re.compile(r"早餐FM-Radio|华见早安")),          # 最高：KPI 与机构观点主源
    (1, re.compile(r"收盘|收跌|收涨|盘面|涨跌")),
    (2, re.compile(r"伊朗|霍尔木兹|美伊|俄乌|中东|OPEC|原油|油价|制裁")),
    (3, re.compile(r"研报|高盛|摩根|德银|瑞银|大摩|小摩|美银|花旗|观点")),
]


def rank_articles(items: list[dict]) -> list[dict]:
    """按精读优先级排序，优先级高者排前，同级按时间倒序。"""
    def key(it: dict):
        for lvl, pat in PRIORITY_PATTERNS:
            if pat.search(it["title"]):
                return (lvl, -it["display_time"])
        return (9, -it["display_time"])
    return sorted(items, key=key)


def build_context(sel: list[dict], sess: requests.Session) -> str:
    """构造喂给 LLM 的素材。优先精读高优先级正文，其余只给标题。"""
    ranked = rank_articles(sel)
    detailed, titles_only = [], []
    for it in ranked:
        if len(detailed) < MAX_DETAIL:
            body = html_to_text(fetch_article(sess, it["id"]))
            if len(body) > 200:                     # 正文太短/被墙则降级为仅标题
                detailed.append((it, body[:9000]))  # 单篇截断，控制 token
                continue
        titles_only.append(it)

    parts = []
    parts.append("===== 第一部分：已精读正文（优先据此撰写） =====")
    for it, body in detailed:
        ts = dt.datetime.fromtimestamp(it["display_time"], CST).strftime("%m-%d %H:%M")
        parts.append(f"\n【{ts}】{it['title']}\n{body}\n")

    parts.append("\n\n===== 第二部分：同期其他条目标题（可择优补充） =====")
    for it in titles_only:
        ts = dt.datetime.fromtimestamp(it["display_time"], CST).strftime("%m-%d %H:%M")
        parts.append(f"- 【{ts}】{it['title']}")

    print(f"[INFO] 素材：精读 {len(detailed)} 篇 + 仅标题 {len(titles_only)} 条")
    return "\n".join(parts)


# ------------------------------------------------------------------ LLM

SYSTEM_PROMPT = """你是资深财经新闻编辑，为一位专业投资者编写《华尔街见闻 · 过去 24 小时要闻简报》。

## 铁律
1. **禁止编造任何数据**。所有事实、数字、引述必须来自我给你的素材，一个字都不许自行发挥。
2. 素材里没有的数字，一律不写。宁可留空，绝不臆造。
3. 关键数字用 <strong> 包裹。
4. 涨用红色、跌用绿色（中国市场惯例）——在文字里写「涨/跌」即可，配色由模板负责。

## 重要性三级分级（核心任务）
- level 1【关键】红色：影响全局资产定价或地缘格局，须立即知晓
- level 2【重要】黄色：影响单一市场、板块或主要资产
- level 3【关注】蓝色：趋势性、背景性或有待验证的信息
同级内按影响力从高到低排列。分级要拉开档次，不要把什么都放进 level 1。

## 输出格式（严格 JSON，不要任何 markdown 代码块包裹）
{
  "window": "覆盖窗口文字，如 2026-09-28 00:00 — 24:00（北京时间）",
  "top5": ["最重要事件1（30字内，含关键数字）", "事件2", "事件3", "事件4", "事件5"],
  "kpi": [
    {"k": "指标名", "v": "数值", "chg": "+0.51%", "dir": "up"}
  ],
  "sections": [
    {
      "name": "A. 地缘政治",
      "items": [
        {
          "level": 1,
          "title": "事件标题（一句话，有信息量）",
          "body": "2-4 句摘要，说明事实、数字与影响。关键数字用 <strong> 包裹。",
          "tag": "板块标签，如 美伊",
          "source": "来源说明，如 华尔街见闻转引新华社"
        }
      ]
    }
  ],
  "watch": ["今日关注1", "关注2"],
  "stats": {"total": 36, "lv1": 8, "lv2": 13, "lv3": 15},
  "sources_note": "来源与可验证性说明（2-3句：主信源为华尔街见闻；哪些条目为单一来源；有无无法验证项）"
}

## sections 板块要求
按当日实际内容组织 4-6 个板块，参考命名：
A. 地缘政治 / B. 全球宏观与利率 / C. 能源与大商品 / D. 市场与资金 / E. AI·科技 / F. 机构观点精选
没有内容的板块直接省略，不要硬凑。

## kpi 要求
6-10 个关键指标（指数点位、美债收益率、油价、黄金、汇率等），来自素材中明确给出的数字。
dir 取 "up" 或 "down"（表示涨跌方向，决定红绿配色）。

只输出 JSON，不要解释。"""


def strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def llm_call(messages: list[dict], api_key: str, max_tokens: int = 8192) -> str:
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last = ""
    for i in range(1, LLM_ATTEMPTS + 1):
        try:
            r = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=LLM_TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            print(f"[WARN] LLM 调用失败（第 {i}/{LLM_ATTEMPTS} 次）：{last}")
            if i < LLM_ATTEMPTS:
                time.sleep(min(2 ** i * 3, 30))
    die(f"LLM 连续 {LLM_ATTEMPTS} 次调用失败：{last}")


# ------------------------------------------------------------------ 跨通道查重

def already_delivered(day: str, smtp_user: str, smtp_code: str) -> bool:
    """IMAP 查收件箱：当日是否已投递过**本简报**。

    这是**跨通道**账本：本机 WorkBuddy 投递的、GitHub 投递的、外部调度器投递的
    都会落在同一个收件箱里，因此对三者一视同仁。

    ⚠️ 必须**同时**匹配「简报标识 + 日期」，不能只搜日期：
    同一收件箱里还有旧通道「全球宏观+地缘 24h 简报」，其主题同样含当天日期
    （形如【全球宏观+地缘 24h 简报】2026-09-29）。若只搜日期，旧通道先投递
    就会让本通道永久查重命中、永远不再发信。

    失败一律返回 False（fail-open）—— 漏发是主要矛盾，偶发重复是次要矛盾。
    """
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
        try:
            M.login(smtp_user, smtp_code)
            M.select("INBOX", readonly=True)
            ids = _search_subject(M, f'"{MAIL_TAG}"', f'"{day}"')
            if ids:
                print(f"[SKIP] 收件箱已存在 {len(ids)} 封「{MAIL_TAG} + {day}」邮件 —— 今日已投递过。")
            return bool(ids)
        finally:
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 邮箱查重不可用（{type(e).__name__}: {e}），按「未投递」继续。")
        return False


def _search_subject(M: "imaplib.IMAP4_SSL", *terms: str) -> list[bytes]:
    """SUBJECT 多条件 AND 检索，兼容非 ASCII（中文）查询词。

    MAIL_TAG 含中文与「·」，部分服务器在隐式 charset 下会拒绝或静默搜不到，
    因此显式声明 CHARSET UTF-8；若服务器不支持（NO/BAD），退回 ASCII 日期单条件。
    """
    try:
        _t, data = M.search("UTF-8", *[a for t in terms for a in ("SUBJECT", t)])
        return (data[0] or b"").split()
    except Exception as e:  # noqa: BLE001
        # 降级：只用最后一个条件（纯 ASCII 日期）搜，宁可多算也别彻底查不到
        print(f"[WARN] UTF-8 检索失败（{type(e).__name__}），降级为仅按日期检索。")
        last = terms[-1] if terms else '""'
        _t, data = M.search(None, "SUBJECT", last)
        return (data[0] or b"").split()


# ------------------------------------------------------------------ HTML 渲染

def esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def render_kpi(kpi: list[dict]) -> str:
    cells = []
    for k in kpi:
        cls = "up" if str(k.get("dir", "")).lower() == "up" else "down"
        chg = f' <span style="font-size:11px">{esc(k.get("chg"))}</span>' if k.get("chg") else ""
        cells.append(
            f'<div><div class="k">{esc(k.get("k"))}</div>'
            f'<div class="v {cls}">{esc(k.get("v"))}{chg}</div></div>'
        )
    return '<div class="kpi">' + "".join(cells) + "</div>"


def render_sections(sections: list[dict]) -> str:
    out = []
    for sec in sections:
        items = sec.get("items") or []
        if not items:
            continue
        out.append(
            f'<div class="sec"><div class="sec-title">{esc(sec.get("name"))}'
            f'<span class="cnt">{len(items)} 条</span></div>'
        )
        for it in items:
            lv = int(it.get("level") or 3)
            lv = min(max(lv, 1), 3)
            bc = {1: "r", 2: "y", 3: "b"}[lv]
            bt = {1: "关键", 2: "重要", 3: "关注"}[lv]
            out.append(
                f'<div class="card lv{lv}">'
                f'<h3><span class="badge {bc}">{bt}</span>{esc(it.get("title"))}</h3>'
                f'<p>{it.get("body") or ""}</p>'
                f'<div class="src"><span class="tag-inline">{esc(it.get("tag"))}</span>'
                f'来源：{esc(it.get("source"))}</div></div>'
            )
        out.append("</div>")
    return "".join(out)


def render_html(data: dict, now: dt.datetime) -> str:
    st = data.get("stats") or {}
    top5 = data.get("top5") or []
    top_items = "".join(f"<li>{esc(x)}</li>" for x in top5)
    watch = "".join(f"<p>• {esc(x)}</p>" for x in (data.get("watch") or [])) or "<p>—</p>"
    total = st.get("total") or 0
    lv1, lv2, lv3 = st.get("lv1") or 0, st.get("lv2") or 0, st.get("lv3") or 0

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>华尔街见闻 · 24小时要闻简报 | {now.astimezone(CST).strftime('%Y-%m-%d')}</title>
<style>
:root{{
  --red:#c0392b; --gold:#b8860b; --blue:#1f5f9f;
  --red-bg:#fdf2f0; --gold-bg:#fff8e6; --blue-bg:#eef4fb;
  --ink:#1c2321; --ink-2:#4a5652; --ink-3:#8b9793;
  --line:#e3e8e5; --bg:#f4f6f5; --card:#ffffff; --accent:#0f3d26;
}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  font-size:15px;line-height:1.7;-webkit-font-smoothing:antialiased;padding:16px 12px 40px;}}
.wrap{{max-width:760px;margin:0 auto;}}
.header{{background:linear-gradient(135deg,#0f3d26 0%,#1f6f43 100%);color:#fff;
  border-radius:14px;padding:26px 24px 22px;box-shadow:0 4px 18px rgba(15,61,38,.18);}}
.header h1{{font-size:21px;font-weight:700;letter-spacing:.3px;line-height:1.4;}}
.header .sub{{font-size:12.5px;opacity:.82;margin-top:8px;}}
.header .meta{{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px;}}
.header .meta span{{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.2);
  border-radius:20px;padding:4px 11px;font-size:11.5px;}}
.legend{{background:var(--card);border-radius:12px;padding:14px 16px;margin-top:14px;
  display:flex;flex-wrap:wrap;gap:10px 20px;border:1px solid var(--line);font-size:12.5px;color:var(--ink-2);}}
.legend b{{color:var(--ink);}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:middle;}}
.dot.r{{background:var(--red);}} .dot.y{{background:var(--gold);}} .dot.b{{background:var(--blue);}}
.sec{{margin-top:14px;}}
.sec-title{{font-size:15px;font-weight:700;color:var(--accent);padding-bottom:8px;margin-bottom:10px;
  border-bottom:2px solid var(--accent);display:flex;align-items:center;gap:8px;}}
.sec-title .cnt{{font-size:11px;font-weight:500;color:var(--ink-3);background:var(--bg);border-radius:10px;padding:1px 8px;}}
.card{{background:var(--card);border-radius:11px;padding:14px 16px;margin-bottom:10px;
  border:1px solid var(--line);border-left:5px solid var(--ink-3);box-shadow:0 1px 3px rgba(0,0,0,.03);}}
.card.lv1{{border-left-color:var(--red);background:linear-gradient(180deg,var(--red-bg) 0%,#fff 42%);}}
.card.lv2{{border-left-color:var(--gold);background:linear-gradient(180deg,var(--gold-bg) 0%,#fff 42%);}}
.card.lv3{{border-left-color:var(--blue);background:linear-gradient(180deg,var(--blue-bg) 0%,#fff 42%);}}
.card h3{{font-size:15px;font-weight:700;line-height:1.5;margin-bottom:7px;display:flex;gap:8px;align-items:flex-start;}}
.badge{{flex:0 0 auto;font-size:10px;font-weight:700;color:#fff;border-radius:4px;padding:2px 6px;margin-top:3px;letter-spacing:.5px;}}
.badge.r{{background:var(--red);}} .badge.y{{background:var(--gold);}} .badge.b{{background:var(--blue);}}
.card p{{font-size:13.5px;color:var(--ink-2);line-height:1.75;}}
.card .src{{font-size:11.5px;color:var(--ink-3);margin-top:9px;padding-top:8px;border-top:1px dashed var(--line);}}
.tag-inline{{display:inline-block;font-size:10.5px;color:var(--accent);background:#eaf3ee;border-radius:4px;padding:1px 6px;margin-right:5px;}}
.top{{background:var(--card);border-radius:12px;padding:6px 4px;border:1px solid var(--line);}}
.top ol{{list-style:none;}}
.top li{{position:relative;padding:11px 16px 11px 48px;font-size:13.5px;line-height:1.6;
  border-bottom:1px solid var(--line);color:var(--ink-2);counter-increment:t;}}
.top{{counter-reset:t;}}
.top li:last-child{{border-bottom:none;}}
.top li::before{{content:counter(t);position:absolute;left:15px;top:11px;width:20px;height:20px;
  border-radius:50%;background:var(--accent);color:#fff;font-size:11px;font-weight:700;
  display:flex;align-items:center;justify-content:center;}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:8px;}}
.kpi div{{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:10px 11px;}}
.kpi .k{{font-size:11px;color:var(--ink-3);margin-bottom:3px;}}
.kpi .v{{font-size:14.5px;font-weight:700;color:var(--ink);}}
.up{{color:var(--red);}} .down{{color:#27ae60;}}
.foot{{margin-top:20px;padding:14px 16px;background:var(--card);border-radius:11px;
  border:1px solid var(--line);font-size:11.5px;color:var(--ink-3);line-height:1.8;}}
.foot b{{color:var(--ink-2);}}
@media (max-width:640px){{
  body{{font-size:14px;padding:12px 9px 32px;}}
  .header{{padding:20px 17px 18px;border-radius:12px;}}
  .header h1{{font-size:18px;}}
  .card{{padding:12px 13px;}}
  .kpi{{grid-template-columns:repeat(2,1fr);}}
}}
@media print{{
  body{{background:#fff;padding:0;}}
  .card,.header,.legend,.top,.foot{{box-shadow:none;break-inside:avoid;}}
  .header{{background:#0f3d26 !important;-webkit-print-color-adjust:exact;print-color-adjust:exact;}}
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>华尔街见闻 · 过去 24 小时要闻简报</h1>
    <div class="sub">覆盖窗口：{esc(data.get('window'))} · 一级信源：华尔街见闻（公开接口）</div>
    <div class="meta">
      <span>生成时间 {now.astimezone(JST).strftime('%Y-%m-%d %H:%M')} JST</span>
      <span>收录 {total} 条要闻</span>
      <span>🔴 关键 {lv1} · 🟡 重要 {lv2} · 🔵 关注 {lv3}</span>
      <span>云端投递 · 关机可收</span>
    </div>
  </div>

  <div class="legend">
    <span><i class="dot r"></i><b>红色 = 最重要</b> 影响全局资产定价，须立即知晓</span>
    <span><i class="dot y"></i><b>黄色 = 重要</b> 影响单一市场或板块</span>
    <span><i class="dot b"></i><b>蓝色 = 关注</b> 趋势性/背景性信息</span>
  </div>

  <div class="sec">
    <div class="sec-title">一页速览 <span class="cnt">Top 5</span></div>
    <div class="top"><ol>{top_items}</ol></div>
  </div>

  <div class="sec">
    <div class="sec-title">关键指标看板</div>
    {render_kpi(data.get('kpi') or [])}
  </div>

  {render_sections(data.get('sections') or [])}

  <div class="sec">
    <div class="sec-title">今日关注</div>
    <div class="card lv3" style="border-left-color:var(--accent);background:#fff;">{watch}</div>
  </div>

  <div class="foot">
    <b>数据来源与可验证性说明</b><br>
    主信源：华尔街见闻（wallstreetcn.com）公开内容接口，覆盖窗口内全球资讯流与正文。<br>
    {esc(data.get('sources_note'))}<br><br>
    <b>重要性分级标准</b><br>
    <span style="color:var(--red)">■ 红（关键）</span>：影响全局资产定价或地缘格局 ·
    <span style="color:var(--gold)">■ 黄（重要）</span>：影响单一市场、板块或主要资产 ·
    <span style="color:var(--blue)">■ 蓝（关注）</span>：趋势性、背景性信息<br><br>
    <b>免责声明</b>：本简报仅供研究参考，不构成任何投资建议。数字以华尔街见闻披露口径为准，请以原始报道为最终依据。<br>
    <span style="color:#b0b8b4">本报告由 GitHub Actions 云端生成（电脑关机时亦可投递）。</span>
  </div>
</div>
</body>
</html>"""


def render_mail_body(data: dict, now: dt.datetime) -> str:
    """邮件正文（HTML），控制在 8KB 内。"""
    st = data.get("stats") or {}
    top5 = "".join(f"<li>{esc(x)}</li>" for x in (data.get("top5") or []))
    rows = "".join(
        f'<tr><td style="padding:6px;border-top:1px solid #e3e8e5">{esc(k.get("k"))}</td>'
        f'<td style="padding:6px;border-top:1px solid #e3e8e5"><b>{esc(k.get("v"))}</b></td>'
        f'<td style="padding:6px;border-top:1px solid #e3e8e5;color:'
        f'{"#c0392b" if str(k.get("dir","")).lower()=="up" else "#27ae60"}">{esc(k.get("chg"))}</td></tr>'
        for k in (data.get("kpi") or [])
    )
    return f"""<div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;font-size:14px;line-height:1.8;color:#1c2321;max-width:640px">
<div style="background:linear-gradient(135deg,#0f3d26,#1f6f43);color:#fff;border-radius:12px;padding:20px">
<div style="font-size:18px;font-weight:700">华尔街见闻 · 过去 24 小时要闻简报</div>
<div style="font-size:12px;opacity:.85;margin-top:6px">{esc(data.get('window'))} · 收录 {st.get('total') or 0} 条 · 🔴{st.get('lv1') or 0} / 🟡{st.get('lv2') or 0} / 🔵{st.get('lv3') or 0}</div>
</div>
<div style="margin-top:14px">
<div style="color:#0f3d26;font-weight:700;border-bottom:2px solid #0f3d26;padding-bottom:6px;margin-bottom:10px">一页速览 · Top 5</div>
<ol style="padding-left:20px;margin:0">{top5}</ol>
</div>
<div style="margin-top:16px">
<div style="color:#0f3d26;font-weight:700;border-bottom:2px solid #0f3d26;padding-bottom:6px;margin-bottom:10px">关键指标看板</div>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<tr style="background:#f0f4f2"><th style="text-align:left;padding:7px">指标</th><th style="text-align:left;padding:7px">数值</th><th style="text-align:left;padding:7px">变动</th></tr>
{rows}
</table>
</div>
<div style="margin-top:16px;background:#fff8e6;border-left:4px solid #b8860b;border-radius:8px;padding:12px;font-size:13px">
<b>重要性配色</b>：附件完整版中，<span style="color:#c0392b;font-weight:700">红色 = 最重要</span>，<span style="color:#b8860b;font-weight:700">黄色 = 重要</span>，<span style="color:#1f5f9f;font-weight:700">蓝色 = 关注</span>。
</div>
<div style="margin-top:16px;font-size:12px;color:#8b9793">
完整分级报告见附件 HTML。数据来源：华尔街见闻公开接口。<br>
本报告由云端自动生成，电脑关机时亦可按时投递。<br>
仅供研究参考，不构成投资建议。
</div>
</div>"""


# ------------------------------------------------------------------ 邮件投递

def send_email(subject: str, body_html: str, attachment: Path,
               smtp_user: str, smtp_code: str, mail_to: str,
               attempts: int = SMTP_ATTEMPTS) -> None:
    msg = MIMEMultipart("mixed")
    msg["From"] = formataddr((str(Header("华尔街见闻简报", "utf-8")), smtp_user))
    msg["To"] = mail_to
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)

    msg.attach(MIMEText(body_html, "html", "utf-8"))

    part = MIMEApplication(attachment.read_bytes(), _subtype="html")
    part.add_header("Content-Disposition", "attachment",
                    filename=("utf-8", "", attachment.name))
    msg.attach(part)

    last = ""
    for i in range(1, attempts + 1):
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
                s.login(smtp_user, smtp_code)
                s.sendmail(smtp_user, [mail_to], msg.as_string())
            print(f"[OK] 邮件已投递 → {mail_to}（第 {i} 次尝试）")
            return
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            print(f"[WARN] 邮件投递失败（第 {i}/{attempts} 次）：{last}")
            if i < attempts:
                time.sleep(min(2 ** i * 3, 30))
    raise RuntimeError(f"邮件连续 {attempts} 次投递失败：{last}")


# ------------------------------------------------------------------ 主流程

def main() -> None:
    mode = (os.getenv("MODE") or "").strip().lower()

    # --check 入口：供 workflow 做「当日终检」判据（比判产物文件准确）
    if mode == "check":
        u, c = os.getenv("QQ_SMTP_USER", "").strip(), os.getenv("QQ_SMTP_CODE", "").strip()
        if not u or not c:
            print("[WARN] 缺少邮箱凭据，终检跳过（按未投递）。")
            sys.exit(1)
        # ⚠️ 日期基准必须与主流程一致（北京时间 CST），否则 JST 与北京差 1 小时，
        #    凌晨投递的邮件会被终检按「第二天」去查而查不到，导致每天误报未投递。
        day = dt.datetime.now(CST).strftime("%Y-%m-%d")
        print(f"[INFO] 终检查询日期（北京时间）：{day}")
        sys.exit(0 if already_delivered(day, u, c) else 1)

    deepseek_key = require("DEEPSEEK_API_KEY")
    smtp_user = require("QQ_SMTP_USER")
    smtp_code = require("QQ_SMTP_CODE")
    mail_to = (os.getenv("MAIL_TO") or smtp_user).strip()

    now = dt.datetime.now(JST)
    news_day = now.astimezone(CST).strftime("%Y-%m-%d")   # 北京时间，与华尔街见闻口径一致
    dry_run = (os.getenv("DRY_RUN") or "").strip().lower() in ("1", "true", "yes")
    force = (os.getenv("SKIP_DEDUPE") or "").strip().lower() in ("1", "true", "yes")

    print(f"[INFO] 运行 {now.strftime('%Y-%m-%d %H:%M')} JST｜新闻日 {news_day}｜窗口 {WINDOW_HOURS}h")

    # 0) 跨通道查重（查收件箱，能同时看到本机/GitHub/外部调度器的投递）
    if dry_run or force:
        print("[WARN] 已跳过邮箱查重（dry_run / force）。")
    elif already_delivered(news_day, smtp_user, smtp_code):
        print("[DONE] 今日已投递，跳过本轮。")
        return

    sess = requests.Session()

    # 1) 抓取华尔街见闻
    flow = fetch_flow(sess, limit=60)
    sel = select_window(flow, now)
    if not sel:
        die("过去 24 小时内无有效条目，终止运行（不生成空报告）。")
    print(f"[INFO] 窗口内 {len(sel)} 条候选")

    # 2) 构造素材（精读高优先级正文）
    context = build_context(sel, sess)

    # 3) LLM 摘要与分级
    print("[INFO] 调用 LLM 生成简报…")
    user_msg = (
        f"今天是 {now.strftime('%Y年%m月%d日 %H:%M')}（JST）。\n"
        f"请基于以下华尔街见闻素材，生成过去 24 小时要闻简报。\n\n{context}"
    )
    raw = llm_call(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": user_msg}],
        deepseek_key,
    )
    try:
        data = json.loads(strip_fences(raw))
    except json.JSONDecodeError as e:
        die(f"LLM 返回非合法 JSON：{e}\n原文前 500 字：{raw[:500]}")

    # 4) 渲染
    report = render_html(data, now)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    out = ARCHIVE / f"{news_day}.html"
    out.write_text(report, encoding="utf-8")
    print(f"[OK] 报告已生成：{out}（{len(report)} 字符）")

    if dry_run:
        draft = ROOT / f".dryrun-{news_day}.html"
        draft.write_text(report, encoding="utf-8")
        print(f"[OK] DRY_RUN：已写入 {draft.name}，不投递。")
        return

    # 5) 投递
    st = data.get("stats") or {}
    lv1, lv2, lv3 = st.get("lv1") or 0, st.get("lv2") or 0, st.get("lv3") or 0
    top5 = data.get("top5") or []
    head = " / ".join(str(x)[:18] for x in top5[:3]) or "要闻简报"
    subject = f"{MAIL_TAG}{news_day} · 🔴{head}"

    send_email(subject, render_mail_body(data, now), out,
               smtp_user, smtp_code, mail_to)
    print(f"[DONE] 完成｜{st.get('total') or 0} 条（🔴{lv1}/🟡{lv2}/🔵{lv3}）")


if __name__ == "__main__":
    main()
