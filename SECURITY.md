# 安全策略

## 支持版本

安全派生版只维护最新 Release，不承诺向旧标签回移修复。

| 版本 | 状态 |
|---|---|
| `v1.18.0-safe.x` | 支持 |
| 更早版本 | 不支持 |

安全版不发布到 PyPI。请从 [GitHub Releases](https://github.com/yousizaitianqiong/boss-agent-cli-safe/releases) 下载最新版，并使用同一 Release 中的 `SHA256SUMS.txt` 校验产物。

## 私下报告漏洞

请不要在公开 Issue、讨论或日志中粘贴 Cookie、Token、`security_id`、个人配置、简历、数据库或浏览器 profile。

请通过 [GitHub Security Advisories](https://github.com/yousizaitianqiong/boss-agent-cli-safe/security/advisories/new) 提交私密报告，并包含：

- 漏洞描述与影响范围；
- 最小化、已脱敏的复现步骤；
- 受影响版本与操作系统；
- 可行时附上修复建议。

如果问题来自未修改的上游功能，也可同时参考[上游安全策略](https://github.com/can4hou6joeng4/boss-agent-cli/security/policy)，但不要向任何公开渠道发送凭据。

平台 Cookie、CDP、真实账号测试和研究边界详见[平台风险说明](docs/platform-risk.md)。

## 安全边界

`boss-safe` 的核心边界包括：

- 版本化的真实申请人画像与技能证据；
- 可见、独立 profile、loopback-only CDP 的专用浏览器；
- PID 创建时间、可执行文件、命令行、进程树和端口归属校验；
- 只读探针、一次性预览授权和批次专属交互确认；
- 北京时间每日写请求硬额度、永久岗位去重和写前额度保留；
- 超时记 `unknown`、不自动重试、风险异常立即停止；
- 机器绑定加密的认证与临时安全标识，审计不保存原始凭据。

这些机制不能保证平台账号安全，也不能替代法律、平台规则或人工判断。上游 `boss` 和 `boss-mcp` 入口为兼容性而保留，不自动继承 `boss-safe` 的全部限制。
