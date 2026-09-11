# 全球宏观 + 地缘政治 24h 简报 · GitHub Actions 版

每天**东京时间 08:33** 自动生成简报并投递到邮箱。跑在 GitHub 服务器上，**本机不需要开机**——
这是它相对 WorkBuddy 定时自动化的核心优势（后者在客户端离线时会整轮跳过）。

> ⚠️ 但请注意：GitHub Actions 的 `schedule` 是「尽力而为」，不是定时保证。
> 高负载时会延迟，甚至整轮被丢弃（2026-09-11 实测：当天唯一一次定时机会直接没触发）。
> 因此本仓库用 **5 个补跑点 + 幂等闸门 + 当日终检告警** 来对抗，详见第四节。

## 一、文件说明

| 文件 | 作用 |
|---|---|
| `digest.py` | 主程序：7 路检索（Tavily 优先，自动降级 Google News RSS）→ DeepSeek 生成 HTML 报告 + 邮件正文 → QQ SMTP 投递 → 归档 |
| `notify_failure.py` | 失败告警：发邮件 + 在仓库开 Issue。**自身永不失败**（始终 exit 0），避免「失败了且没人知道」 |
| `.github/workflows/daily.yml` | 定时任务 + 幂等闸门 + 终检告警 |
| `requirements.txt` | 仅 `requests` |
| `archive/` | 每日报告落盘，自动 commit 回仓库（也是幂等闸门的判据） |
| `deploy.py` | 一键部署：校验 token → 推代码 → 写 Secrets → 触发试跑 |

## 二、配置 Secrets（必须，否则跑不起来）

仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | https://platform.deepseek.com/api_keys |
| `QQ_SMTP_USER` | ✅ | 发件 QQ 邮箱，如 `ttr169@qq.com` |
| `QQ_SMTP_CODE` | ✅ | QQ 邮箱**授权码**（16 位，不是登录密码）。QQ 邮箱 → 设置 → 账户 → POP3/SMTP 服务 → 开启 → 生成授权码 |
| `MAIL_TO` | ⭕ | 收件人（可省，默认同 `QQ_SMTP_USER`） |
| `TAVILY_API_KEY` | ⭕ | 搜索 API（免费 1000 次/月）。**缺失或失效均不影响运行**：自动降级到 Google News RSS，只是素材量少约两成 |

> 换搜索引擎改 `digest.py` 的 `search()`；换 LLM 改 `DEEPSEEK_URL` 与 `llm_call()` 的请求体。

## 三、手动试跑

仓库 → **Actions** → 左侧「全球宏观+地缘 24h 简报」 → 右上 **Run workflow**。
约 2 分钟完成。失败时点进 run 看日志，绝大多数是 Secret 填错或授权码不对。

## 四、时间与可靠性（重要）

### 时间换算
**UTC = JST − 9 小时**。例如东京 08:33 → `cron: '33 23 * * *'`；东京 20:00 → `cron: '11 0 * * *'`（次日）。

### 5 个补跑点
| cron (UTC) | JST | 角色 |
|---|---|---|
| `33 23 * * *` | 08:33 | 主跑 |
| `53 23 * * *` | 08:53 | 补跑 1 |
| `13 0 * * *` | 09:13 | 补跑 2 |
| `33 0 * * *` | 09:33 | 补跑 3 |
| `53 0 * * *` | 09:53 | 补跑 4 + 当日终检 |

分钟刻意取 `:13 / :33 / :53`，避开整点前后的调度波峰，被服务到的概率更高。

### 三道防线
1. **幂等闸门**：`archive/<今日>.html` 已存在 → 后续步骤全部跳过。任一次跑通，其后各点自动跳过，**不会重复投递**。
2. **只在完全成功时归档**：邮件失败则不落盘，好让下一个补跑点接手重试。
   （注意：归档步骤的条件是 `run_digest.outcome == 'success'`，**不是** `always()`。）
3. **当日终检告警**：09:53 那一班跑完仍没有当日归档 = 5 次机会全落空，基本可判定计划任务被 GitHub 丢弃，主动发信告知。

### 其他要点
- 任何一步失败都会触发 `notify_failure.py`：邮件 + Issue 双通道告警。
  条件写的是 `failure() || steps.run_digest.outcome == 'failure'` —— 因为 `run_digest` 带
  `continue-on-error`，它的失败不会让 `failure()` 变真，只写 `failure()` 会漏掉最该告警的场景。
- 归档推送带 3 次重试（rebase 后重推）。推送失败会导致下一班看不到归档而**重复发信**，所以必须重试。
- 刚改动 workflow 后，GitHub 调度器需要 15–60 分钟才会「注册」新的 cron；
  官方给出的补救办法是「向默认分支推一次提交以重新同步计划任务」。

## 五、与 WorkBuddy 版的关系（重要）

| 通道 | 时间 | 定位 |
|---|---|---|
| **GitHub Actions** | 每天 08:33–09:53 JST（5 次机会） | **主跑**：不依赖本机开机 |
| **WorkBuddy c82e5118** | 每天 10:00 JST | **补漏**：先查本仓库 `archive/YYYY-MM-DD.html`，已存在则立即退出 |
| **WorkBuddy a4dea9c0** | 每天 12:00 JST | **二次兜底**：三重去重判据，宁可不发、不可重发 |

分工逻辑：GitHub 版不依赖连接器、不挑客户端是否在线；WorkBuddy 版能用**华尔街见闻 MCP**（数据更准、可交叉验证），作为 GitHub 漏跑时的加强版补发。

⚠️ **时序约束**：GitHub 侧所有 cron 都刻意排在 **10:00 JST 之前**。若把 GitHub 的补跑点挪到 10:00
之后，就可能与 WorkBuddy 10:00 主线撞车 —— 后者生成的报告只落在本机、不会 push 回仓库，
GitHub 的幂等闸门看不到它，于是同一份简报会被发两封。

## 六、成本

DeepSeek：每期 7 次检索 + 2 次生成，实测**每天人民币几分钱**（约 ¥0.09/次）。
Tavily：免费额度足够（缺失时自动走 Google News RSS，零成本）。
GitHub Actions：公开仓库免费用量不限；私有仓库免费额度 2000 分钟/月，本任务约 2 分钟/次 ≈ 60 分钟/月，也足够。
