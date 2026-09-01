# boss-safe 安全工作流

`boss-safe` 是 [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) 的受限入口，面向日常实习搜索与一次性沟通。它把只读搜索、写入门禁、每日额度、永久去重和专用浏览器隔离放在同一套本地状态机中。

本项目与 BOSS 直聘官方无关。平台规则、账号状态和页面结构可能随时变化；任何自动化都可能触发验证码、限流或账号风控。每日额度是不可突破的上限，不是应当完成的数量目标。

## 初始化

首次运行：

```bash
boss-safe init
boss-safe doctor
```

向导只预填学校“四川大学”、毕业年份“2028”和职位类型“实习”。使用者必须填写并确认自己的专业全称、目标城市、到岗说明、可连续实习月数，以及每个启用技术方向的真实技能表述和简历证据。不要复制别人的专业、项目或技能。

配置采用 `schema_version=1`，分为 `applicant`、`tracks`、`limits` 和 `browser`。旧版配置会先创建时间戳备份，再以临时文件和原子替换方式迁移；取消向导不会更改原文件，也不会触碰认证、额度数据库或浏览器 profile。

未完成配置时，`login`、`preview`、`execute` 和 `acknowledge-risk` 拒绝运行；`doctor`、`status`、`history`、`stop` 与 `browser-stop` 仍可用于本地诊断和清理。

## 一轮受控沟通

```bash
boss-safe login --timeout 120
boss-safe status --live
boss-safe acknowledge-risk --reason "已核验登录态、专用浏览器和平台页面正常"
boss-safe preview --probe
# 等待输出要求的搜索冷却
boss-safe preview
boss-safe execute <batch_id>
boss-safe history
boss-safe status
```

流程含义：

1. `login` 启动或复用可见的专用浏览器。登录失效时，由使用者在该窗口扫码或人工登录。
2. `status --live` 只通过已登记的严格 CDP 浏览器调用一次只读用户状态。
3. `acknowledge-risk` 只记录本轮明确风险确认，不会自动搜索或发送。
4. `preview --probe` 只对一个已配置关键词发出一次只读搜索，不建批次、不保存临时 `security_id`，失败不自动重试。
5. 普通 `preview` 只能复用刚通过探针的同一关键词，也只搜索一次。成功后输出 `batch_id`、编号、岗位、公司、方向和招呼语。
6. `execute` 在交互式终端逐项展示批次，并要求输入精确短语 `确认投递 <batch_id>`。确认不匹配时写请求数为零。
7. 每个岗位最多调用一次 `greet`；不会调用 `apply`、上传简历、读取聊天、自动回复或跟进。

不要在无人值守任务中自动生成确认短语，也不要用 `boss` 或 `boss-mcp` 绕过 `boss-safe` 的门禁。

## 固定安全边界

- 默认每批最多 20 次写请求，北京时间每日最多 150 次写请求尝试。
- `sent`、`failed`、`unknown` 都占每日额度；只有平台明确成功才记为 `sent`。
- 写请求发出前先保留额度；超时或结果不确定记为 `unknown`，立即停止后续岗位并清除批次中全部临时 `security_id`。
- 写请求之间随机等待 15–30 秒，批次之间至少冷却 10 分钟，任意两次搜索之间至少冷却 60 秒。
- 同批同公司最多 1 个；默认不另设公司每日上限，但始终受每日硬额度和永久岗位去重限制。
- 城市必须来自上游城市目录，职位类型固定为日常实习，不使用“应届”筛选。
- 不因岗位不足而放宽城市、职位类型、技术相关性或永久去重规则。

## 探针、恢复与熔断

探针授权与配置摘要、关键词和过期时间绑定，只能消费一次。普通预览创建批次后，下一轮必须重新经过风险确认和探针。

遇到验证码、安全页、`ENVIRONMENT_RISK`、账号风险、限流、浏览器异常或网络异常时，当前命令立即停止，不重放探针、预览或写请求。发生异常后，状态机会要求新的人工恢复授权；处理完成后显式运行：

```bash
boss-safe acknowledge-risk --reason "已在官方页面核验并处理异常"
```

明确认证过期、限流、招呼上限、临时数据无法清理或浏览器归属异常还会打开持久熔断。`acknowledge-risk` 只解除门禁，不会恢复旧批次或自动执行下一步。

## 跨平台专用浏览器

浏览器发现顺序为 Edge、Chrome、Chromium，也可在 `init` 中填写显式可执行文件。Windows、macOS 和 Linux 使用各自的平台数据目录；可用全局 `--data-dir` 覆盖。

浏览器始终可见，使用独立 profile，并仅在 `127.0.0.1` 开放 CDP（默认端口 9222）。运行时使用 `psutil` 同时校验：

- 根 PID 与创建时间；
- 可执行文件和命令行中的精确 profile；
- 浏览器进程树与监听端口归属；
- CDP WebSocket 的 loopback 主机和端口。

工具不连接日常浏览器，不复制 Cookie，不使用无头模式，也不回退到 Bridge、HTTP、HTTPX 或 `APIRequestContext`。`browser-stop` 只终止已验证的专用进程树并保留 profile；权限不足、PID 复用、端口冲突、元数据损坏、profile 锁或残留进程都会失败关闭。

## 本地数据与清理

本机目录包含配置、机器绑定加密的认证、SQLite 审计状态和专用浏览器 profile。不要提交或分享这些内容。批次中的 `security_id` 加密保存，并在完成、停止或过期时清除；审计记录不会保存 Cookie、Token、完整职位描述或明文 `security_id`。

结束使用时：

```bash
boss-safe stop
boss-safe browser-stop
boss-safe status
```

`browser-stop` 保留 profile，便于下次继续使用同一专用登录会话。
