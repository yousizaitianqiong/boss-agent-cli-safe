<div align="center">

# boss-agent-cli-safe

A safety-focused derivative for daily internship search: explicit configuration, a dedicated browser, preview gates, hard quotas, and fail-closed cleanup.

[![CI](https://github.com/yousizaitianqiong/boss-agent-cli-safe/actions/workflows/ci.yml/badge.svg)](https://github.com/yousizaitianqiong/boss-agent-cli-safe/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[中文](README.md) · **English** · [Detailed safety workflow (Chinese)](SAFE_README.md) · [Security](SECURITY.md)

</div>

> [!WARNING]
> This project is not affiliated with or endorsed by BOSS Zhipin. Browser automation can trigger platform restrictions or account risk controls. Users are responsible for complying with applicable laws, platform terms, and privacy requirements. The daily limit is a safety ceiling, not a target.

## Origin

This repository derives from [can4hou6joeng4/boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) at commit [`d045da92471f3317883490b70bbb7243d4423665`](https://github.com/can4hou6joeng4/boss-agent-cli/commit/d045da92471f3317883490b70bbb7243d4423665). The upstream history, copyright notices, and MIT license are preserved.

The defaults are convenient for Sichuan University CS/software/networking students graduating in 2028, but the tool does not assume that users share a major, city, availability, or technical background. Every user must provide truthful personal details and resume evidence.

## Install

This derivative is distributed only through GitHub Releases, not PyPI. Download the wheel and verify it against `SHA256SUMS.txt` from the same release:

```bash
python -m venv .venv
python -m pip install ./boss_agent_cli_safe-1.18.0.post1-py3-none-any.whl
```

Activate the environment with `.\.venv\Scripts\Activate.ps1` on Windows PowerShell or `source .venv/bin/activate` on macOS/Linux. Developers can use `python -m pip install -e ".[dev]"` in the repository root.

## Initialize your own profile

```bash
boss-safe init
boss-safe doctor
```

The wizard pre-fills only Sichuan University, graduation year 2028, and internship job type. It requires each user to enter their exact major, target city, availability, internship duration, enabled technical tracks, truthful skill claims, and corresponding resume evidence.

Until initialization is complete, platform commands such as `login`, `preview`, and `execute` refuse to run. Local diagnostics and cleanup remain available. Legacy configurations are backed up and atomically migrated; cancelling the wizard leaves the old file unchanged.

## Controlled workflow

```bash
boss-safe login --timeout 120
boss-safe status --live
boss-safe acknowledge-risk --reason "Login, dedicated browser, and platform page verified"
boss-safe preview --probe
# Wait for the reported search cooldown.
boss-safe preview
boss-safe execute <batch_id>
boss-safe history
boss-safe browser-stop
```

`execute` still requires the exact batch-specific confirmation phrase in an interactive terminal. Do not automate that confirmation or retry probes, previews, or writes. CAPTCHA, security pages, environment risk, account risk, rate limits, browser failures, network failures, and uncertain write outcomes all stop the flow.

## Default safety boundaries

- At most 20 write attempts per batch and 150 attempts per Beijing calendar day.
- `failed` and `unknown` attempts consume quota; only explicit platform success counts as `sent`.
- Random 15–30 second write delays, 10-minute batch cooldowns, and at least 60 seconds between searches.
- One job per company in a batch. There is no separate per-company daily cap by default, while the global daily quota and permanent job deduplication always apply.
- One `greet` call per job; no `apply`, resume upload, chat reading, auto-reply, or follow-up.
- Uncertain writes stop the batch and clear all temporary security identifiers. Safety failures are fail-closed.

## Dedicated cross-platform browser

`boss-safe` discovers Edge, Chrome, then Chromium, with an explicit executable override available during initialization. It always launches a visible browser with an isolated profile and loopback-only CDP, using port 9222 by default.

`platformdirs` selects the local data directory on Windows, macOS, and Linux. `psutil` verifies PID creation time, executable, command line, process tree, profile, and port ownership. The tool never attaches to a daily browser and never falls back to headless, Bridge, HTTP, or `APIRequestContext`. `browser-stop` terminates only a fully verified process tree and preserves the profile.

## Entrypoints and privacy

- `boss-safe` is the recommended gated workflow.
- `boss` preserves the upstream full CLI and does not automatically inherit the `boss-safe` constraints.
- `boss-mcp` preserves the upstream MCP entrypoint and must not be used to bypass safety gates.

The compatibility entrypoints expose every implemented capability. Current execution paths no longer produce mode-level `COMPLIANCE_BLOCKED`; individual adapters still return stable errors for unavailable capabilities. The retained MCP server with 73 tools follows the upstream schema contract.

Upstream documentation remains available through [Getting Started](docs/getting-started.en.md), [Agent Quickstart](docs/agent-quickstart.en.md), [Capability Matrix](docs/capability-matrix.en.md), [Agent Host Examples](docs/agent-hosts.en.md), [docs/platform-abstraction.en.md](docs/platform-abstraction.en.md), [Platform risk boundaries](docs/platform-risk.en.md), and the [Roadmap](ROADMAP.en.md).

The upstream platform selector is `--platform zhipin|zhilian|qiancheng`. 51job (`qiancheng`) is a registered placeholder that returns `NOT_SUPPORTED`; it is not a working production adapter.

Upstream Bridge diagnostics include `bridge_daemon`, `bridge_extension`, `bridge_protocol`, `bridge_workspace`, `bridge_exec`, `bridge_fetch`, and `bridge_navigate`; its local development command is `python -m boss_agent_cli.bridge.daemon --serve`. Bridge remains available only through compatibility surfaces and must never be used as a risk-control fallback for `boss-safe`.

Configuration, authentication, SQLite state, and browser profiles remain local. Never publish cookies, tokens, security identifiers, local paths, raw run logs, personal configuration, or resume data.

## Development

```bash
python -m pytest tests -q
python -m build
python -m pip install --force-reinstall dist/*.whl
boss-safe --help
```

CI performs offline tests only. It does not log in to or access BOSS Zhipin.

## License

[MIT](LICENSE) © 2026 [can4hou6joeng4](https://github.com/can4hou6joeng4). This derivative preserves upstream attribution and history.
