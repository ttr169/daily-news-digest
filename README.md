# 全球宏观 + 地缘政治 24h 简报 · GitHub Actions 版

每天自动检索过去 24 小时的全球宏观与地缘事件，用 DeepSeek 生成一份自包含 HTML 简报，
通过 QQ 邮箱投递（正文 + HTML 附件），并把当天报告归档回仓库。**本机不需要开机。**

---

## 一、它是怎么跑的

```
┌─ 触发层（三重，任一生效即当天完成）───────────────────────────┐
│  ① GitHub cron × 7     08:33 / 08:53 / 09:13 / 09:33 / 09:53  │
│                        + 13:33 午后兜底 + 20:33 终检班次       │
│  ② 外部调度器（可选）    POST .../daily.yml/dispatches（实时通道）│
│  ③ 本机 WorkBuddy       10:00 / 12:00（未投递时才跑）           │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌─ 幂等层（谁都别想重复发）───────────────────────────────────┐
│  闸门 ①  当日 archive/YYYY-MM-DD.html 已存在 → 跳过           │
│  闸门 ②  IMAP 查自己收件箱，已有当日简报 → 跳过（跨通道账本）  │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌─ 执行层 ───────────────────────────────────────────────────┐
│  1. 检索   11 路聚焦查询（宏观/地缘/能源/AI，中英双语）        │
│            主通道 Tavily；缺失/失效 → 免密钥双引擎 RSS         │
│            （Google News + Bing News 合并去重 + 48h 时效过滤） │
│  2. 生成   DeepSeek → 完整 HTML 报告                          │
│  3. 生成   DeepSeek → 精简邮件正文（Top5 + KPI 表 + 日历）     │
│  4. 投递   QQ 邮箱 SMTP（失败自动重试 3 次）                   │
│  5. 归档   archive/YYYY-MM-DD.html（仅在完全成功时提交）        │
└──────────────────────────────────────────────────────────────┘
```

**为什么要有「闸门 ②（IMAP 账本）」**：GitHub 的 `schedule` 不只是会被丢弃，还可能被
**延迟数小时**执行 —— 一个 08:33 的计划任务完全可能 13:00 才跑起来，而那时本机
WorkBuddy 已经投递过了。仓库里的归档闸门看不到本机投递（本机不 push 回仓库），
所以需要一个跨通道账本。邮件本身就是最好的账本：本简报自发自收，
投递成功后必然落在自己收件箱里，对「GitHub 投递」和「本机投递」一视同仁。

---

## 二、文件

| 文件 | 作用 |
|---|---|
| `digest.py` | 主程序：查重 → 11 路检索 → DeepSeek 生成 HTML 报告 + 邮件正文 → QQ SMTP 投递 → 落盘归档 |
| `.github/workflows/daily.yml` | 7 个 cron + 幂等闸门 + 归档提交（3 次重试）+ 失败告警 + 当日终检 |
| `notify_failure.py` | 失败告警（邮件 + GitHub Issue）；惰性 import，避免在 pip 失败时自己也崩 |
| `requirements.txt` | 依赖（仅 `requests`） |
| `deploy.py` | 一键部署：校验 token → 推文件 → 加密写入 Secrets → 触发一次 |
| `README.md` | 本文件 |

---

## 三、Secrets（仓库 Settings → Secrets and variables → Actions）

| 名称 | 必需 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek 平台 API Key |
| `QQ_SMTP_USER` | ✅ | 发件邮箱，如 `ttr169@qq.com` |
| `QQ_SMTP_CODE` | ✅ | QQ 邮箱**授权码**（不是登录密码；同一授权码同时用于 SMTP 与 IMAP） |
| `MAIL_TO` | ⭕ | 收件人，默认同 `QQ_SMTP_USER` |
| `TAVILY_API_KEY` | ⭕ | 缺失或失效会自动降级到免密钥 RSS，**不影响运行** |

> 本简报依赖「自发自收」（`MAIL_TO` 等于发件人）才能用 IMAP 做跨通道查重。
> 若把 `MAIL_TO` 改成别的地址，闸门 ② 会失效（代码会自动 fail-open，不会因此漏发）。

---

## 四、调度时间表（JST）

| cron (UTC) | JST | 角色 |
|---|---|---|
| `33 23 * * *` | 08:33 | 主跑 |
| `53 23 * * *` | 08:53 | 补跑 1 |
| `13 0 * * *` | 09:13 | 补跑 2 |
| `33 0 * * *` | 09:33 | 补跑 3 |
| `53 0 * * *` | 09:53 | 补跑 4 |
| `33 4 * * *` | 13:33 | 午后兜底（早上全落空时补上） |
| `33 11 * * *` | 20:33 | 终检班次（仍无投递 → 告警） |

分钟取 `:13/:33/:53` 是为了避开整点前后的调度波峰。

> ⚠️ **GitHub 的 cron 只是「尽力而为」**。2026-09-11 实测：一个 `*/5 * * * *` 探针
> 在 45 分钟内 9 个边界全部零触发；官方口径是高负载时**直接丢弃**。
> 想彻底解决，请配一个外部调度器（见第六节）。

---

## 五、手动运行（Actions 页面 → Run workflow）

| 输入 | 作用 |
|---|---|
| 都不勾 | 走正常逻辑：已投递则跳过（等同一次定时班次） |
| `dry_run` | **试跑**：完整走一遍检索 + 两次 LLM 调用，但**不发信、不归档**，产物写到 `.dryrun-YYYY-MM-DD.html`。改完代码用它验证最安全 |
| `force` | 强制重跑：忽略两个闸门。⚠️ **会真的再发一封邮件**，仅在需要补发时使用 |

---

## 六、彻底解决定时不触发：外部调度器（推荐）

`workflow_dispatch` 走的是**实时事件通道**，秒级启动，不受定时队列丢弃影响。
用一个免费的外部 cron 服务定时调它即可：

```
POST https://api.github.com/repos/ttr169/daily-news-digest/actions/workflows/daily.yml/dispatches
Headers:
  Authorization: Bearer <你的 PAT>
  Accept: application/vnd.github+json
Body:
  {"ref":"main"}
```

- 成功返回 **HTTP 204**（无 body），30 秒内 `actions/runs` 会多出一条 `event=workflow_dispatch`
- PAT 需具备该仓库的 **Actions: write**（细粒度）或 `repo` scope（经典）
- ⚠️ 仓库改为 private 后，PAT 仍需对该仓库授权，否则会 404
- 有了闸门 ②（IMAP 账本），外部调度器**多调几次也不会重复发信**

---

## 七、排障

```bash
# 最近 5 次运行（重点看 event 是不是 schedule）
curl -s -H "Authorization: Bearer $TOK" \
  "https://api.github.com/repos/ttr169/daily-news-digest/actions/runs?per_page=5" \
| python -c "import json,sys
for r in json.load(sys.stdin)['workflow_runs']:
    print(r['created_at'], r['event'], r['conclusion'])"
```

**本地验证改动（不打扰收件人）**：

```bash
cd github_action
DEEPSEEK_API_KEY=xxx QQ_SMTP_USER=ttr169@qq.com QQ_SMTP_CODE=xxx \
DRY_RUN=1 python digest.py          # 只生成，不投递、不归档
```

**只查「今天到底投递了没有」**（走 IMAP 账本，不需要 DeepSeek）：

```bash
QQ_SMTP_USER=ttr169@qq.com QQ_SMTP_CODE=xxx python digest.py --check
echo $?      # 0 = 已投递；1 = 未投递
```

---

## 八、成本

| 项 | 用量 |
|---|---|
| DeepSeek | 约 ¥0.09–0.13 / 次 → **约 ¥3–4 / 月**（非高峰时段半价） |
| GitHub Actions | 公开仓库免费；私有仓库计入每月 2000 分钟免费额度（本任务约 60–120 分钟/月） |
| Tavily | 免费额度 1000 次/月（当前未启用，走免密钥 RSS） |

---

## 九、安全提示

- `.gitignore` 已排除 `.gh_token` / `secrets.json`，**不要把 token 写进 `git remote`**：
  `git remote set-url origin https://github.com/ttr169/daily-news-digest.git`（不带凭据）。
- 仓库建议设为 **private**：每日报告含宏观判断，公开 commit 等于持续公开研究结论。
  改法：仓库 Settings → General → 最下方 Danger Zone → Change visibility。
  注意细粒度 PAT 需要 `Administration: write` 才能通过 API 修改，Web UI 上点两下最简单。
- 若 PAT 曾出现在聊天/日志里，请及时 revoke 并换新。
