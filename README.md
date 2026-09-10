# 全球宏观 + 地缘政治 24h 简报 · GitHub Actions 版

每天**东京时间 08:30**自动生成简报并投递到邮箱。跑在 GitHub 服务器上，**本机不需要开机**——
这是它相对 WorkBuddy 定时自动化的核心优势（后者在客户端离线时会整轮跳过）。

## 一、文件说明

| 文件 | 作用 |
|---|---|
| `digest.py` | 主程序：Tavily 7 路检索 → DeepSeek 生成 HTML 报告 + 邮件正文 → QQ SMTP 投递 → 归档 |
| `notify_failure.py` | 失败告警：发邮件 + 在仓库开 Issue。**自身永不失败**（始终 exit 0），避免"失败了且没人知道" |
| `.github/workflows/daily.yml` | 定时任务：`cron: '30 23 * * *'` = UTC 23:30 = **JST 次日 08:30** |
| `requirements.txt` | 仅 `requests` |
| `archive/` | 每日报告落盘，自动 commit 回仓库 |

## 二、配置 Secrets（必须，否则跑不起来）

仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名称 | 说明 | 获取方式 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | https://platform.deepseek.com/api_keys |
| `TAVILY_API_KEY` | Tavily 搜索 Key（免费 1000 次/月；本任务每天 7 次 ≈ 210 次/月） | https://app.tavily.com |
| `QQ_SMTP_USER` | 发件 QQ 邮箱，如 `ttr169@qq.com` | — |
| `QQ_SMTP_CODE` | QQ 邮箱**授权码**（16 位，不是登录密码） | QQ 邮箱 → 设置 → 账户 → POP3/SMTP 服务 → 开启 → 生成授权码 |
| `MAIL_TO` | 收件人（可省，默认同 `QQ_SMTP_USER`） | — |

> 换搜索引擎改 `digest.py` 的 `tavily_search()`；换 LLM 改 `DEEPSEEK_URL` 与 `llm_call()` 的请求体。

## 三、手动试跑

仓库 → **Actions** → 左侧「全球宏观+地缘 24h 简报」 → 右上 **Run workflow**。
2–4 分钟后应收到邮件。失败时点进 run 看日志，绝大多数是 Secret 填错或授权码不对。

## 四、时间与可靠性

- 换算公式：**UTC = JST − 9 小时**。改东京 09:00 → `cron: '0 0 * * *'`；东京 20:00 → `cron: '0 11 * * *'`。
- ⚠️ GitHub Actions 的 schedule 是「尽力而为」，高峰期可能延迟十几分钟，偶发跳票。
  因此配置了**多层兜底**（见下节）。
- 任何一步失败都会触发 `notify_failure.py`：邮件 + Issue 双通道告警，不会静默消失。
- 归档步骤加了 `if: always()`：即使邮件投递失败，报告仍会落盘，不会白跑一次。

## 五、与 WorkBuddy 版的关系（重要）

| 通道 | 时间 | 定位 |
|---|---|---|
| **GitHub Actions** | 每天 08:30 JST | **主跑**：不依赖本机开机，稳定性最高 |
| **WorkBuddy c82e5118** | 每天 10:00 JST | **补漏**：开工前先查本仓库 `archive/YYYY-MM-DD.html`，已存在则立即退出 |
| **WorkBuddy a4dea9c0** | 每天 12:00 JST | **二次兜底**：三重去重判据，宁可不发、不可重发 |

分工逻辑：GitHub 版不依赖连接器、不挑客户端是否在线，负责"每天一定有一份"；
WorkBuddy 版能用到**华尔街见闻 MCP**（数据更准、可交叉验证），作为 GitHub 漏跑时的加强版补发。
两者不会重复投递——WorkBuddy 靠本仓库当日归档文件判重。

## 六、成本

DeepSeek：每期约 6 次检索 + 2 次生成，实测**每天人民币几分钱**。
Tavily：免费额度足够。
GitHub Actions：公开仓库免费用量不限；私有仓库免费额度 2000 分钟/月，本任务约 4 分钟/次 ≈ 120 分钟/月，也足够。
