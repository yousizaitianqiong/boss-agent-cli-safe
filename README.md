<div align="center">

# boss-agent-cli-safe

面向日常实习求职的安全派生版：显式配置、专用浏览器、逐批预览、硬额度和失败关闭。

[![CI](https://github.com/yousizaitianqiong/boss-agent-cli-safe/actions/workflows/ci.yml/badge.svg)](https://github.com/yousizaitianqiong/boss-agent-cli-safe/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**中文** · [English](README.en.md) · [安全工作流详解](SAFE_README.md) · [安全报告](SECURITY.md)

</div>

> [!WARNING]
> 本项目不是 BOSS 直聘官方产品，也未获其授权或背书。自动化操作可能触发平台限制或账号风控。使用者必须遵守适用法律、平台规则和隐私要求，并自行承担账号及数据风险。每日上限是安全边界，不是必须完成的目标。

## 项目关系

本项目派生自 [can4hou6joeng4/boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli)，基线提交为 [`d045da92471f3317883490b70bbb7243d4423665`](https://github.com/can4hou6joeng4/boss-agent-cli/commit/d045da92471f3317883490b70bbb7243d4423665)。上游作者和贡献者的代码继续按仓库中的 [MIT License](LICENSE) 授权。

安全版适合川大计软网 2028 届同学配置自己的日常实习求职流程，但不会假定大家拥有相同的专业、城市、到岗时间或技术经历。每个人都必须填写真实资料，尤其不能复用他人的技能或简历证据。

## 安装

安全版只通过 GitHub Release 提供构建产物，不发布到 PyPI。下载 Release 中的 wheel 并校验同页 `SHA256SUMS.txt` 后安装：

```bash
python -m venv .venv
python -m pip install ./boss_agent_cli_safe-1.18.0.post1-py3-none-any.whl
```

激活虚拟环境：

```text
Windows PowerShell: .\.venv\Scripts\Activate.ps1
macOS / Linux:     source .venv/bin/activate
```

开发者也可以在仓库根目录运行 `python -m pip install -e ".[dev]"`。Python 最低版本为 3.10。

## 首次初始化

```bash
boss-safe init
```

向导预填学校“四川大学”、届别“2028”和职位类型“实习”，并要求每位使用者单独填写：

- 专业全称、目标城市、到岗时间和可实习时长；
- 实际启用的技术方向；
- 每个方向真实、可由个人简历支撑的技能表述与证据。

城市必须来自工具支持的城市目录，日常实习不会使用“应届”筛选。初始化未完成时，`login`、`preview` 和 `execute` 会拒绝运行；诊断和清理命令仍可使用。旧版安全配置由向导备份并原子迁移，取消向导不会改写原文件。

## 推荐安全流程

```bash
boss-safe doctor
boss-safe login --timeout 120
boss-safe status --live
boss-safe acknowledge-risk --reason "已确认本轮登录态与平台环境正常"
boss-safe preview --probe
# 等待命令报告的搜索冷却结束
boss-safe preview
# 人工核对岗位、公司、方向和招呼语
boss-safe execute <batch_id>
boss-safe history
boss-safe status
boss-safe browser-stop
```

`execute` 仍要求在交互式终端输入当前批次的精确确认短语。不要把该流程放入无人值守任务，不要自动重试探针、预览或写请求。遇到验证码、安全页、环境风险、限流、网络异常或结果不确定时，应立即停止并保留真实结果。

## 默认安全边界

- 每批最多 20 个写请求；北京时间每日最多 150 次写请求尝试。
- 写请求之间随机等待 15–30 秒，批次之间冷却 10 分钟，搜索之间冷却至少 60 秒。
- 同一批次同一公司最多 1 个；默认不另设公司每日上限，但始终受每日硬额度和永久岗位去重约束。
- 每个岗位最多调用一次 `greet`；不调用 `apply`，不上传简历，不读取或自动回复聊天。
- `failed` 和 `unknown` 都占每日额度；写入结果不确定时停止当前批次，不重试。
- 风控、验证码、账号异常、浏览器归属异常或本地完整性故障均失败关闭，恢复前必须重新通过技术门禁。

这些边界不会保证平台接受请求，也不应被调高来追求数量。更完整的状态机和恢复规则见 [SAFE_README.md](SAFE_README.md)。

## 跨平台专用浏览器

`boss-safe` 按 Edge、Chrome、Chromium 的顺序发现本机浏览器，也支持配置显式可执行文件。它始终启动可见窗口和独立 profile，仅在 `127.0.0.1` 上开放严格 CDP，默认端口为 9222。

运行时使用 `platformdirs` 选择 Windows、macOS 或 Linux 的本地数据目录，并使用 `psutil` 验证 PID、创建时间、进程树、命令行、profile 和端口归属。它不会接管日常浏览器，不会使用无头浏览器，也不会回退到 Bridge、HTTP 或 `APIRequestContext`。`browser-stop` 只关闭验证通过的专用进程树并保留 profile；任何归属不确定或残留进程都会按错误处理。

## 三个命令入口

- `boss-safe`：本派生版推荐入口，提供安全初始化、门禁、批次和审计流程。
- `boss`：保留上游完整 CLI 兼容入口，不自动继承 `boss-safe` 的安全边界。
- `boss-mcp`：保留上游 MCP 入口，不应用于绕过 `boss-safe` 的写入门禁。

## 上游兼容与 Agent 文档

安全版保留上游 `boss` 与 `boss-mcp` 的全部已实现能力，方便已有集成继续运行；`boss-safe` 的门禁只约束安全入口。当前版本不主动产生模式级 `COMPLIANCE_BLOCKED`，但平台适配器仍会对未实现能力返回稳定错误。使用兼容入口前请阅读 [Getting Started](docs/getting-started.md)、[Agent Quickstart](docs/agent-quickstart.md)、[Capability Matrix](docs/capability-matrix.md)、[Agent Host Examples](docs/agent-hosts.md) 和[平台风险边界](docs/platform-risk.md)。

上游多平台参数为 `--platform zhipin|zhilian|qiancheng`。前程无忧 / 51job (`qiancheng`) 当前是 `QianchengPlatform (51job 占位适配器，统一返回 NOT_SUPPORTED)`，不能视为已支持的真实平台流程。

上游 Bridge 诊断项包括 `bridge_daemon`、`bridge_extension`、`bridge_protocol`、`bridge_workspace`、`bridge_exec`、`bridge_fetch` 和 `bridge_navigate`；本地调试入口为 `python -m boss_agent_cli.bridge.daemon --serve`。Bridge 属于兼容入口能力，`boss-safe` 明确禁止将其作为风控失败后的回退通道。

## ⚙️ 配置

上游兼容入口使用 `boss config`；安全入口使用 `boss-safe init` 写入独立的版本化配置。两套配置的能力和风险边界不同，不应混用来绕过批次、额度或浏览器门禁。

## 🏗️ 技术架构

上游 CLI、MCP 和 Python 模块继续位于 `boss_agent_cli` 包内；安全派生层由版本化配置、本地状态库、严格 CDP 浏览器运行时和短生命周期 worker 组成。

## 本地数据与隐私

配置、认证、SQLite 状态和专用浏览器 profile 只保存在本机平台数据目录。不要提交或分享 Cookie、Token、数据库、浏览器 profile、`security_id`、原始运行日志、`TODO.md`、`INCIDENT.md`、`PRD.md` 或包含个人简历信息的配置。

发布构建仅包含通用源码与文档。发现安全问题时，请按 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 Issue 中粘贴凭据或个人数据。

## 开发验证

```bash
python -m pytest tests -q
python -m build
python -m pip install --force-reinstall dist/*.whl
boss-safe --help
```

CI 保留上游 Python 3.10–3.14 测试，并在 Windows、macOS、Ubuntu 的 Python 3.12 环境中运行安全测试、构建 wheel 和离线入口冒烟。CI 不登录或访问 BOSS。

## 许可证

[MIT](LICENSE) © 2026 [can4hou6joeng4](https://github.com/can4hou6joeng4)。本派生版保留上游历史、版权声明和许可证文本。
