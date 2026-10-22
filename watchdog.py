#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周度投递健康检查（低频看门狗）。

## 为什么需要它

2026-10 之前，两个 workflow 各挂了一个「当日终检」步骤，用来兜住
「GitHub 调度器整轮丢弃 cron」这种静默漏发。但这个设计有致命自伤：
末班班次 20:33 JST 常被 GitHub 延迟到次日 01:0x 才跑，它先正常发出简报，
5 秒后终检去 IMAP 查「今天有没有简报」——刚投递的邮件还没进检索索引，
于是每天必判「未投递」，每天固定误报 2 封失败邮件（Issue #1–#56）。

那两个终检步骤已于 commit aa25403 移除。移除后留下一个缺口：
如果 GitHub 真把某天 7 个 cron 全部丢弃，就完全静默、没有任何提醒。

本脚本补上这个缺口，且**不引入日内噪声**：

- 频率：每周一次（周一 09:00 JST），不是每天；
- 判据：**回看 7 天**，用 IMAP 收件箱做账本，按**邮件实际投递日期**归日
  （不是主题里的日期字符串 —— 那正是 JST/CST 口径打架导致误判的根源）；
- 只在**真缺口**时告警：某通道 7 天内缺 >= MAX_MISS 天；
- 全部正常时**完全静默**，不发邮件、不开 Issue。

## 为什么用「邮件 Delivery Date」而不是「主题日期」

- digest.py 的主题日期用 JST，wscn_digest.py 用 CST（北京时间）；
- 凌晨投递时两者会差一天，任何按主题日期归日的判据都会周期性误判；
- 而邮件的 Date 头是投递那一刻的绝对时间，换算到 JST 后归日无歧义。

## 环境变量

  QQ_SMTP_USER / QQ_SMTP_CODE / MAIL_TO   邮箱账本 + 告警通道（标准库，无需第三方包）
  GITHUB_TOKEN                             可选，开 Issue
  WINDOW_DAYS                              回看天数，默认 7
  MAX_MISS                                 允许的最大缺失天数（达到即告警），默认 2
  WATCHDOG_DRY_RUN=1                       只打印判定结果，不发信、不开 Issue
"""

from __future__ import annotations

import datetime as dt
import email
import os
import re
import sys
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

JST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(JST)

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993

SERVER = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
REPO = os.getenv("GITHUB_REPOSITORY", "")
RUN_ID = os.getenv("GITHUB_RUN_ID", "")
RUN_URL = f"{SERVER}/{REPO}/actions/runs/{RUN_ID}" if REPO and RUN_ID else "(运行链接不可用)"

# 通道标识：(显示名, 邮件主题里的标识)
# 与 digest.py / wscn_digest.py 里的常量保持一致，改动时三处同步。
CHANNELS = [
    ("全球宏观+地缘 24h", "【全球宏观+地缘 24h 简报】"),
    ("华尔街见闻·24h要闻", "【华尔街见闻·24h要闻简报】"),
]

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


# ---------------------------------------------------------------- IMAP 账本

def _decode(raw) -> str:
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:  # noqa: BLE001
        return str(raw or "")


def fetch_delivered_days(user: str, code: str, window_days: int) -> dict[str, set[str]]:
    """回看收件箱，返回 {通道标识: {投递日期(JST, YYYY-MM-DD), ...}}。

    IMAP 或网络异常时返回 None（由调用方决定「无法确认」，绝不误判为漏发）。
    """
    import imaplib

    # SINCE 是「含当天」，多回看 3 天以覆盖跨时区/延迟投递
    since = (NOW - dt.timedelta(days=window_days + 3)).strftime("%d-%b-%Y")

    out: dict[str, set[str]] = {tag: set() for _name, tag in CHANNELS}
    M = None
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
        M.login(user, code)
        M.select("INBOX", readonly=True)
        _typ, data = M.search(None, "SINCE", since)
        ids = (data[0] or b"").split()
        print(f"[INFO] 收件箱 SINCE {since} 命中 {len(ids)} 封，开始按通道归日……")

        for mid in ids:
            try:
                _t, chunks = M.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])")
                if not chunks or not isinstance(chunks[0], tuple):
                    continue
                msg = email.message_from_bytes(chunks[0][1])
            except Exception:  # noqa: BLE001
                continue

            subject = _decode(msg.get("Subject", ""))
            tag = next((t for _n, t in CHANNELS if t in subject), None)
            if tag is None:
                continue

            day = None
            date_hdr = msg.get("Date")
            if date_hdr:
                try:
                    day = parsedate_to_datetime(date_hdr).astimezone(JST).strftime("%Y-%m-%d")
                except Exception:  # noqa: BLE001
                    day = None
            if day is None:  # 兜底：Date 头缺失时才用主题日期
                m = DATE_RE.search(subject)
                if m:
                    day = m.group(0)

            if day:
                out[tag].add(day)

        return out
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 无法读取邮箱账本（{type(e).__name__}: {e}）")
        return None
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 判定

def evaluate(delivered: dict[str, set[str]], window_days: int, max_miss: int):
    """返回 (需要告警?, 明细列表)。

    明细每项：{name, tag, days:[(day, ok)], miss:[...], last, verdict}
    """
    days = [(NOW - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(window_days)]
    days.reverse()  # 升序，方便阅读

    rows, need = [], False
    for name, tag in CHANNELS:
        got = delivered.get(tag, set())
        marks = [(d, d in got) for d in days]
        miss = [d for d, ok in marks if not ok]
        last = max(got) if got else None
        verdict = "缺口中" if len(miss) >= max_miss else ("个别缺失" if miss else "正常")
        if len(miss) >= max_miss:
            need = True
        rows.append(
            {
                "name": name,
                "tag": tag,
                "days": marks,
                "miss": miss,
                "last": last,
                "verdict": verdict,
                "hit": len(days) - len(miss),
                "total": len(days),
            }
        )
    return need, rows


# ---------------------------------------------------------------- 告警通道

def send_mail(rows: list[dict], window_days: int) -> None:
    import smtplib
    import ssl
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import formataddr, formatdate

    user = os.getenv("QQ_SMTP_USER", "").strip()
    code = os.getenv("QQ_SMTP_CODE", "").strip()
    to = os.getenv("MAIL_TO", user).strip()
    if not (user and code and to):
        print("[WARN] 缺少 SMTP 配置，跳过告警邮件。")
        return

    title = f"【周度投递健康检查·发现缺口】{NOW:%Y-%m-%d} JST"

    blocks = []
    for r in rows:
        if r["miss"]:
            chips = " ".join(
                f'<span style="display:inline-block;padding:2px 7px;border-radius:4px;'
                f'margin:0 4px 4px 0;font-size:12px;'
                f'{"background:#e8f6ee;color:#1e7e4f" if ok else "background:#fdecea;color:#c0392b"}">'
                f'{d[5:]}{"" if ok else " ✗"}</span>'
                for d, ok in r["days"]
            )
        else:
            chips = '<span style="color:#1e7e4f;font-size:12px">近 {} 天全部投递正常</span>'.format(
                r["total"]
            )
        blocks.append(
            f'<tr><td style="padding:10px 12px;border-bottom:1px solid #eee;vertical-align:top">'
            f'<b>{r["name"]}</b><br>'
            f'<span style="font-size:12px;color:#666">命中 {r["hit"]}/{r["total"]} 天，'
            f'最近一次 {r["last"] or "窗口内无投递"}</span></td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #eee">{chips}</td></tr>'
        )

    html = (
        '<div style="font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;'
        'line-height:1.7;color:#1c2b23;max-width:640px">'
        f'<div style="background:#b9770e;color:#fff;padding:14px 16px;border-radius:8px">'
        f'<b style="font-size:16px">{title}</b><br>'
        f'<span style="font-size:12px;opacity:.9">这不是每日告警 —— 每周最多一封，只在真缺口时发出</span></div>'
        '<p style="margin-top:14px">回看最近 '
        f'<b>{window_days}</b> 天的邮箱账本（按邮件实际投递时间归日），发现以下通道存在缺投：</p>'
        '<table style="border-collapse:collapse;width:100%;border:1px solid #eee;border-radius:6px">'
        + "".join(blocks)
        + "</table>"
        '<p style="margin-top:14px"><b>可能原因</b><br>'
        '1. GitHub Actions 调度器整轮丢弃了当天所有 cron（免费额度下偶发）<br>'
        '2. 仓库 60 天无活动 → 计划任务被自动停用（推一次提交即可恢复）<br>'
        '3. Secrets 失效或模型/检索额度用尽</p>'
        f'<p>本次运行日志：<a href="{RUN_URL}">{RUN_URL}</a></p>'
        '<p style="font-size:12px;color:#888">缺口在 2 天以内属可接受的偶发，'
        '达阈值才推送；若持续缺失，可在 Actions 页面手动 Run workflow 补跑。</p></div>'
    )

    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = formataddr((str(Header("简报监控", "utf-8")), user))
    msg["To"] = to
    msg["Subject"] = Header(title, "utf-8")
    msg["Date"] = formatdate(localtime=True)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
        s.login(user, code)
        s.sendmail(user, [to], msg.as_string())
    print("[OK] 健康检查告警邮件已发送。")


def open_issue(rows: list[dict], window_days: int) -> None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not (token and REPO):
        print("[WARN] 无 GITHUB_TOKEN，跳过开 Issue。")
        return
    try:
        import requests
    except ImportError as e:  # noqa: BLE001
        print(f"[WARN] 缺少 requests，跳过开 Issue：{e}")
        return

    detail = "\n".join(
        f"- **{r['name']}**：命中 {r['hit']}/{r['total']} 天，缺失 {', '.join(r['miss']) or '无'}"
        for r in rows
    )
    try:
        r = requests.post(
            f"https://api.github.com/repos/{REPO}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            json={
                "title": f"【投递缺口】{NOW:%Y-%m-%d} JST 周度健康检查",
                "body": f"回看 {window_days} 天，以下通道缺投：\n\n{detail}\n\n"
                        f"运行日志：{RUN_URL}\n\n"
                        f"（此 Issue 由 watchdog.py 每周健康检查自动创建；"
                        f"全部正常时不会创建）",
            },
            timeout=30,
        )
        print(f"[OK] 已开 Issue（HTTP {r.status_code}）")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 开 Issue 失败：{e}")


# ---------------------------------------------------------------- 主流程

def main() -> None:
    user = (os.getenv("QQ_SMTP_USER") or "").strip()
    code = (os.getenv("QQ_SMTP_CODE") or "").strip()
    dry = (os.getenv("WATCHDOG_DRY_RUN") or "").strip().lower() in ("1", "true", "yes")

    try:
        window_days = int(os.getenv("WINDOW_DAYS", "7"))
    except ValueError:
        window_days = 7
    try:
        max_miss = int(os.getenv("MAX_MISS", "2"))
    except ValueError:
        max_miss = 2

    if not (user and code):
        print("[WARN] 缺少 QQ_SMTP_USER / QQ_SMTP_CODE，无法读取账本 —— 本次跳过（不告警）。")
        return

    print(f"[INFO] 周度健康检查：回看 {window_days} 天，缺失阈值 {max_miss} 天，基准时间 {NOW:%Y-%m-%d %H:%M} JST")

    delivered = fetch_delivered_days(user, code, window_days)
    if delivered is None:
        # 账本不可读时**不告警**：无法区分「IMAP 故障」和「真漏发」，
        # 贸然发信等于把老毛病（假告警）请回来。
        print("[WARN] 账本不可读，本次判定为「无法确认」，不告警。")
        return

    need, rows = evaluate(delivered, window_days, max_miss)

    for r in rows:
        print(f"[{'ALERT' if len(r['miss']) >= max_miss else 'OK'}] {r['name']}: "
              f"{r['hit']}/{r['total']} 天，最近 {r['last'] or '(无)'}，缺失 {','.join(r['miss']) or '无'}")

    if not need:
        print(f"[DONE] 全部通道近 {window_days} 天投递正常，静默退出（不发邮件、不开 Issue）。")
        return

    if dry:
        print("[DRY] 判定为需要告警，但已启用 dry-run，跳过发送。")
        return

    try:
        send_mail(rows, window_days)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 告警邮件发送失败：{e}")
    try:
        open_issue(rows, window_days)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 开 Issue 失败：{e}")
    print("[DONE] 周度健康检查结束。")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        # 看门狗自身永不把 workflow 搞成红色失败：失败本身已知，
        # 让它变成「红色 run 但无信息」没有价值。
        print(f"[WARN] watchdog 异常退出：{type(e).__name__}: {e}")
    sys.exit(0)
