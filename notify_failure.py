#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行失败时的告警：发一封邮件 + 在仓库开一个 Issue。

设计原则：**本脚本自身永不失败**（任何异常都只打印警告并 exit 0），
否则会把「简报生成失败」升级成「失败且无告警」，比原问题更糟。

环境变量（GitHub Actions 自动注入或取自 Secrets）：
  QQ_SMTP_USER / QQ_SMTP_CODE / MAIL_TO   邮件（缺失则跳过邮件，不报错）
  GITHUB_TOKEN                             开 Issue（需在 workflow 里显式传入）
  GITHUB_REPOSITORY / GITHUB_RUN_ID / GITHUB_SERVER_URL   GitHub 自动注入
"""

from __future__ import annotations

import datetime as dt
import os

import requests

JST = dt.timezone(dt.timedelta(hours=9))
STAMP = dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M")

SERVER = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
REPO = os.getenv("GITHUB_REPOSITORY", "")
RUN_ID = os.getenv("GITHUB_RUN_ID", "")
RUN_URL = f"{SERVER}/{REPO}/actions/runs/{RUN_ID}" if REPO and RUN_ID else "(运行链接不可用)"

TITLE = f"【简报生成失败】{STAMP} JST"


def send_mail() -> None:
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

    html = (
        '<div style="font-family:sans-serif;font-size:14px;line-height:1.7;color:#1c2b23">'
        '<div style="background:#c0392b;color:#fff;padding:14px 16px;border-radius:8px">'
        f'<b style="font-size:16px">{TITLE}</b></div>'
        '<p style="margin-top:14px">GitHub Actions 上的「全球宏观+地缘 24h 简报」本次运行<b>未成功</b>，'
        '因此今天<b>没有</b>简报邮件投递。</p>'
        '<p>常见原因：<br>'
        '1. Secrets 未配置或已失效（DeepSeek / Tavily / QQ 授权码）<br>'
        '2. 检索全部无结果（Tavily 额度用尽或网络问题）<br>'
        '3. 模型未返回合法 HTML</p>'
        f'<p>查看日志：<a href="{RUN_URL}">{RUN_URL}</a></p>'
        '<p style="font-size:12px;color:#7d9c8a">确认修复后，可在 Actions 页面点 '
        '<b>Run workflow</b> 手动补跑一次。</p></div>'
    )

    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = formataddr((str(Header("简报监控", "utf-8")), user))
    msg["To"] = to
    msg["Subject"] = Header(TITLE, "utf-8")
    msg["Date"] = formatdate(localtime=True)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=ctx, timeout=60) as s:
        s.login(user, code)
        s.sendmail(user, [to], msg.as_string())
    print("[OK] 失败告警邮件已发送。")


def open_issue() -> None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not (token and REPO):
        print("[WARN] 无 GITHUB_TOKEN，跳过开 Issue。")
        return
    try:
        r = requests.post(
            f"https://api.github.com/repos/{REPO}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            json={
                "title": TITLE,
                "body": f"简报运行失败，请查看日志：{RUN_URL}\n\n"
                        f"（此 Issue 由 notify_failure.py 自动创建）",
                "labels": ["automated"],
            },
            timeout=30,
        )
        # 标签不存在时会 422，忽略即可，Issue 本身仍可能创建成功
        if r.status_code >= 400 and "labels" in r.text.lower():
            requests.post(
                f"https://api.github.com/repos/{REPO}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                },
                json={"title": TITLE, "body": f"简报运行失败，请查看日志：{RUN_URL}"},
                timeout=30,
            )
        print(f"[OK] 已开 Issue（HTTP {r.status_code}）")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 开 Issue 失败：{e}")


def main() -> None:
    try:
        send_mail()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 告警邮件发送失败：{e}")
    try:
        open_issue()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 开 Issue 失败：{e}")
    print("[DONE] 告警流程结束（本脚本始终 exit 0）。")


if __name__ == "__main__":
    main()
