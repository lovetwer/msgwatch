# msgwatch — 个人互动消息聚合监控

监控你在 **哔哩哔哩 / 小红书 / 抖音** 三个平台的账号动态（新评论、评论下的回复、私信），
第一时间推送到 **微信 / 企业微信 / 邮件**，不必再挨个打开 App 查看。

个人自用工具，单机运行，无服务器依赖。

```
┌─────────┐  直连API(Cookie)   ┌──────────────────────────┐   ┌──────────────┐
│ 哔哩哔哩 │ ─────────────────▶ │  msgwatch（本机常驻进程）  │──▶│ Server酱/WxPusher│──▶ 微信
└─────────┘                    │  · 独立检测间隔           │   │ 企业微信应用    │──▶ 企业微信
┌─────────┐  浏览器+响应拦截    │  · SQLite去重             │   │ PushPlus      │
│ 小红书   │ ─────────────────▶ │  · 失败退避+告警          │──▶│ 邮件SMTP       │──▶ 邮箱
└─────────┘                    └──────────────────────────┘   └──────────────┘
┌─────────┐
│ 抖音     │ ── 同小红书方案
└─────────┘
```

---

## 一、微信推送可行性评估（结论）

| 方案 | 可行性 | 说明 |
| --- | --- | --- |
| 微信公众号服务号模板消息 | ❌ 个人不可行 | 服务号需要企业/个体工商户主体注册，个人无法申请；微信认证还要 300 元/年 |
| **企业微信群机器人 Webhook** | ✅ **最简单** | 群聊里加个机器人拿到 Webhook key 即可，无需 corpid/secret。消息发到该企业微信群；限频约 20 条/分钟（本工具自动汇总，远够用） |
| **Server酱 / WxPusher / PushPlus（第三方推送）** | ✅ **推荐** | 第三方服务持有正规服务号资质，你只需扫码/填 Key，消息直达微信。免费额度对个人足够。**零门槛，5 分钟接完** |
| 企业微信自建应用 | ✅ 可行，进阶选项 | 个人可免费注册企业微信并创建自建应用，API 直接推送。消息在**企业微信 App** 内接收；新注册企业已不支持通过「微信插件」在微信内接收（腾讯 2022 年起的收紧政策），老企业可尝试 |
| 邮件 | ✅ 兜底 | 任何邮箱均可，QQ/163 需用「授权码」 |

**建议配置：企业微信群机器人或 Server酱（或 WxPusher）为主渠道 + 邮件兜底**，多个渠道可同时启用。本工具已内置全部六个渠道。

---

## 二、安装（Windows，一次性）

本项目目录已带 `.venv` 虚拟环境（依赖与 Chromium 已装好）。换新机器时重新执行：

```bat
cd msgwatch
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

要求 Python 3.10+。

## 三、快速开始

```bat
cd msgwatch

:: 1. 生成配置（项目里已生成过 config.yaml，可跳过）
.venv\Scripts\python -m msgwatch init

:: 2. 编辑 config.yaml：
::    - 至少启用一个通知渠道（推荐先开 serverchan，5 分钟搞定）
::    - 把要用的平台 enabled: true

:: 3. 逐个平台登录（会弹出浏览器窗口，手动登录一次即可，长期有效）
.venv\Scripts\python -m msgwatch login bilibili
.venv\Scripts\python -m msgwatch login xiaohongshu
.venv\Scripts\python -m msgwatch login douyin

:: 4. 手动跑一轮验证
.venv\Scripts\python -m msgwatch once

:: 5. 发条测试通知确认渠道通了
.venv\Scripts\python -m msgwatch test-notify

:: 6. 常驻运行
.venv\Scripts\python -m msgwatch run
```

### 开机自启（可选）

推荐用「启动文件夹」+ `pythonw`（无窗口后台运行）：

1. `Win+R` 输入 `shell:startup` 回车，打开启动文件夹；
2. 新建快捷方式，目标填：
   ```
   "C:\Users\10292\.zcode\workspace\default\msgwatch\.venv\Scripts\pythonw.exe" -m msgwatch run
   ```
   起始位置填项目目录 `C:\Users\10292\.zcode\workspace\default\msgwatch`；
3. 重启后自动后台运行，用 `python -m msgwatch status` 查看心跳。

也可以用任务计划程序（`schtasks /create /tn MsgWatch /tr ... /sc onlogon`）达到同样效果。

## 四、各平台说明

### 哔哩哔哩 — 直连 API（稳定、轻量）

依据社区维护的 [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) 文档实现：

- 评论与评论回复：`GET /x/msgfeed/reply`（「回复我的」feed，`root_id=0` 为新评论，否则为评论回复）
- 私信：`GET session_svr/get_sessions`（过滤系统助手会话与自己的消息）
- 登录检测：`/x/web-interface/nav`；昵称补全：`/x/web-interface/card`（带本地缓存）

只需要 Cookie（`SESSDATA` 必需）。推荐 `login bilibili` 自动获取；也可在 config 里手填浏览器复制的完整 Cookie。

### 小红书 / 抖音 — 浏览器方案（抗签名变动）

两家的 Web 接口全部带私有签名（小红书 `X-s/X-s-common`、抖音 `a_bogus`）且**没有公开文档**，
直接逆向的维护成本极高。本工具采用最抗变的方案：

> 用 Playwright 打开**真实的消息中心页面**，拦截页面自己发出的 JSON 响应来解析。
> 不构造任何签名请求，签名算法怎么改都不影响。

代价是每次检测会开一个浏览器实例（约 10~20 秒），所以默认检测间隔为 10 分钟（B站是 3 分钟，因为它是纯 HTTP）。

**首次使用必须 `login xiaohongshu` / `login douyin`**，登录态保存在 `userdata/` 下（勿删）。
注意：小红书给未登录访客也发 `web_session` Cookie，所以登录命令会用「打开首页是否被踢回登录页」做二次验证。

已实测确认的小红书路由（2026-10）：

| 监控项 | 页面 | 被拦截的接口 |
| --- | --- | --- |
| 评论和@（新评论+评论回复） | `https://www.xiaohongshu.com/notification` | `/api/sns/web/v1/you/mentions` |
| 私信会话 | `https://www.xiaohongshu.com/chat` | `/api/im/web/v3/chats` |

已实测确认的抖音方案（2026-10）：

| 监控项 | 流程 | 数据来源 |
| --- | --- | --- |
| 评论/回复 | 打开首页 → 点击右上角「通知」 | 拦截 `/aweme/v1/web/notice/`（type=31 为评论类） |
| 私信 | 打开首页 → 点击「消息」 | 解析面板 DOM（`[data-e2e="conversation-item"]`，数据本体走 WebSocket 无法直接拦截） |

**如果页面路由变了**（比如平台改版）：

```bat
.venv\Scripts\python -m msgwatch probe xiaohongshu
```

probe 会打开页面、把捕获到的所有 JSON 响应存到 `data/debug/<平台>/`，并列出命中与解析结果。
登录后打开网页版消息中心，把地址栏实际 URL 填进 config.yaml 的 `pages.*` 即可。
若平台改了响应字段名，调整 `msgwatch/platforms/<平台>.py` 里的解析函数。

## 五、通知内容

每条消息包含要求的全部要素（平台来源、互动类型、对方昵称、内容摘要、原文链接、发生时间），微信内效果：

```
【B站·新评论】张三
来自：视频「xxx教程」
> 楼主讲得真清楚，收藏了！
时间：2026-10-01 14:30
链接：https://www.bilibili.com/video/BVxxxx#reply123
```

一次检测抓到超过 `max_items_per_notify`（默认 8）条时，自动合并为一条汇总通知，防止刷屏。

## 六、稳定性设计（对应你的第 4、5 条要求）

| 机制 | 实现 |
| --- | --- |
| 独立检测间隔 | 每平台 `interval_seconds` 单独配置；浏览器平台强制页面停留随机化 |
| 频率控制 | 间隔下限 60s（低于会警告）；每平台串行轮询 + 随机抖动 ±10%；B站各接口间 1s 间隔 |
| 消息去重 | SQLite `seen` 表，B站用官方通知 ID/私信 msg_key；浏览器平台用「ID 或 昵称+内容+时间 哈希」；同一消息被两个页面重复捕获也会全局去重 |
| 首次基线 | `seed_on_first_run: true`：首次成功抓取只记录不推送，避免把历史消息全推一遍 |
| 旧消息过滤 | 超过 `max_age_hours`（默认 48h）的不推送 |
| 登录态失效 | B站识别接口 `-101`；浏览器平台检查 Cookie。触发后立刻告警（含重新登录命令提示）并按冷却期不重复轰炸 |
| 接口报错/网络异常 | 指数退避（间隔 ×2^(失败次数-1)，封顶 `max_backoff_multiplier` 倍），成功后自动恢复 |
| 连续失败告警 | 连续 `alert_fail_threshold`（默认 3）次失败推告警，冷却 `alert_cooldown_minutes`（默认 6 小时） |
| 日志 | `logs/msgwatch.log` 滚动文件（5MB×3）+ 控制台 |
| 心跳 | `data/heartbeat.json` 每几秒更新，随时 `python -m msgwatch status` 查看各平台上次成功时间 |

## 七、常用命令

| 命令 | 作用 |
| --- | --- |
| `python -m msgwatch init` | 生成示例配置 |
| `python -m msgwatch login <平台>` | 登录平台保存登录态（`--timeout 600` 可延长等待） |
| `python -m msgwatch run` | 常驻运行 |
| `python -m msgwatch once [--platform X]` | 立即检测一轮（测试） |
| `python -m msgwatch probe <平台>` | 抓包调试，导出原始响应 |
| `python -m msgwatch test-notify` | 发送测试通知 |
| `python -m msgwatch status` | 查看运行状态与统计 |

统一参数：`--config path/to/config.yaml` 指定配置文件（默认当前目录或项目根）。

## 八、配置速查

所有凭据集中在 `config.yaml`（已被 `.gitignore` 排除，不会被误提交）。要点：

- `notify.channels[].enabled`：启用渠道；`serverchan` 只需要填一个 `sendkey`
- `platforms.<name>.enabled / interval_seconds / monitors.comments / monitors.dms`：平台开关、间隔、监控类型
- `platforms.bilibili.cookie` 或 `cookies_file`：B站凭据二选一
- `platforms.xiaohongshu/douyin.browser_profile / headless / pages.*`：浏览器方案参数
- `general.max_age_hours / alert_fail_threshold / max_items_per_notify`：行为微调

## 九、注意事项

1. **合规与风险**：监控自己的账号数据属于个人数据访问，但自动化访问仍可能违反平台用户协议。
   请保持默认（或更长）的检测间隔，不要用于他人账号或商业化场景。账号被风控（验证码、限流）时请立即调大间隔或停用。
2. B站 Cookie 有效期通常数月，失效会收到告警，重新 `login bilibili` 即可。
3. 小红书/抖音的登录态如遇平台主动下线（改密码、异地登录），同样会收到告警，重新登录即可。
4. 通知的「原文链接」：B站来自平台返回的跳转地址（可直达那条评论）；小红书由笔记 ID 构造
   （`/explore/{note_id}`）；抖音评论由 aweme_id 构造（`/video/{aweme_id}`），抖音私信无独立链接。
5. 私信监控目前针对**用户会话**（B站已过滤 UP主小助手/系统通知等官方会话）；同一会话连续多条消息只报最新一条（会话级摘要），这是各平台消息列表接口的天然限制。

## 十、目录结构

```
msgwatch/
├─ config.example.yaml      # 带注释的示例配置（init 会复制为 config.yaml）
├─ config.yaml              # 你的实际配置（含凭据，勿外传）
├─ requirements.txt
├─ .gitignore
├─ msgwatch/                # 源码包
│  ├─ cli.py                # 命令行入口
│  ├─ config.py             # 配置加载与校验
│  ├─ scheduler.py          # 调度器：间隔/退避/告警/心跳
│  ├─ notify.py             # 五种通知渠道
│  ├─ store.py              # SQLite 去重与状态
│  ├─ context.py            # 进程内共享 Store
│  ├─ log.py / models.py
│  └─ platforms/
│     ├─ base.py            # 适配器基类 + 通用提取器
│     ├─ browser.py         # Playwright 采集/登录/Cookie导出
│     ├─ bilibili.py        # B站直连 API
│     ├─ xiaohongshu.py     # 小红书浏览器方案
│     └─ douyin.py          # 抖音浏览器方案
├─ userdata/                # 平台登录态（勿删、勿提交）
├─ data/                    # state.db / heartbeat.json / debug 抓包
└─ logs/                    # 滚动日志
```
