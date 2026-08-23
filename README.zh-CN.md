<div align="center" markdown="1">

# AI CoC

[![PyPI version](https://img.shields.io/pypi/v/ai_coc.svg)](https://pypi.org/project/ai_coc/)
[![python](https://img.shields.io/badge/-Python_%7C_3.12%7C_3.13%7C_3.14-blue?logo=python&logoColor=white)](https://www.python.org/downloads/source/)
[![uv](https://img.shields.io/badge/-uv_dependency_management-2C5F2D?logo=python&logoColor=white)](https://docs.astral.sh/uv/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![Pydantic v2](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/pydantic/pydantic/main/docs/badge/v2.json)](https://docs.pydantic.dev/latest/contributing/#badges)
[![tests](https://github.com/Mai0313/ai_coc/actions/workflows/test.yml/badge.svg)](https://github.com/Mai0313/ai_coc/actions/workflows/test.yml)
[![code-quality](https://github.com/Mai0313/ai_coc/actions/workflows/code-quality-check.yml/badge.svg)](https://github.com/Mai0313/ai_coc/actions/workflows/code-quality-check.yml)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Mai0313/ai_coc)
[![license](https://img.shields.io/badge/License-MIT-green.svg?labelColor=gray)](https://github.com/Mai0313/ai_coc/tree/main?tab=License-1-ov-file)
[![PRs](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/Mai0313/ai_coc/pulls)
[![contributors](https://img.shields.io/github/contributors/Mai0313/ai_coc.svg)](https://github.com/Mai0313/ai_coc/graphs/contributors)

</div>

一个 Windows 桌面应用程序，用来操作跑在 MuMu 模拟器 12 里的《部落冲突》。Gemini 负责读画面，这个程序把它的回答转成 ADB 点击，并在下一张截图上确认结果。

其他语言版本：[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

## ✨ 功能

- 检测 MuMu 模拟器 12 的实例，启动模拟器并打开游戏
- 通过 ADB 截取画面并询问 Gemini 画面上有什么，结构化回复一律经过 Pydantic 验证
- 执行 agent 循环，可以点击、滑动与返回，每一步之后重新观察画面
- 导入村庄 JSON 与战斗脚本，遇到未知字段与未知的 `data_id` 会保留而不是让导入失败
- 每个 agent 指令都会存成一条任务，中断的执行会在下次启动时继续
- Gemini API key 通过 Windows DPAPI 保存，不会以明文写进设置

实时战术操作刻意不在范围内：战斗脚本只验证兵种需求，到保留的交接边界为止。

## 📋 环境需求

- Windows。程序会调用 `mumu-cli.exe`、通过 `winreg` 读注册表、用 `ctypes.windll` 调用 DPAPI，这些在其他平台上都不存在
- [MuMu 模拟器 12](https://www.mumuplayer.com/)，已安装《部落冲突》，分辨率设为 1600x900
- 一组 Gemini API key，在程序的设置页输入

## 🚀 安装与运行

从 PyPI 直接跑，不留下任何安装：

```bash
uvx ai_coc
```

或者正式安装：

```bash
uv tool install ai_coc
ai_coc
```

每个 [release](https://github.com/Mai0313/ai_coc/releases) 也都附上预先打包好的 Windows 可执行文件。

## 🛠️ 本地开发

```bash
git clone https://github.com/Mai0313/ai_coc.git
cd ai_coc
uv sync --group test          # 安装依赖
uvx pre-commit install        # 安装 git hooks
uv run ai_coc                 # 启动程序
```

另外有两个命令行开关可以做冒烟测试。`--live-test` 会截取一张画面并请 Gemini 描述它，`--agent-command=<text>` 会把指令打进 AI 页并执行。两者在 `COC_LIVE_TEST_SCREENSHOT` / `COC_AGENT_SCREENSHOT` 指向某个路径时会存下截图存证。

## 🧰 命令参考

```bash
# 开发
make help               # 列出可用的 make 目标
make clean              # 清除缓存、产物与生成的文档
make fmt                # 执行所有 pre-commit hooks
make test               # 对整个 repo 执行 pytest
make gen-docs           # 从 src/ 与 scripts/ 生成文档

# 依赖（通过 uv）
make uv-install         # 在系统上安装 uv
uv add <pkg>            # 新增正式依赖
uv add <pkg> --dev      # 新增开发依赖
# 安装可选的分组
uv sync --group dev     # 只装开发用依赖（pre-commit、poe、notebook）
uv sync --group test    # 只装测试用依赖
uv sync --group docs    # 只装文档用依赖
```

## 🧱 架构

三个层次就是三个目录，所以跨层的 import 在 import 那一行就看得出来：

- **UI 与流程调度**：`ui/main_window.py` 放主窗口与所有工作流程，`ui/workers.py` 放线程池的 worker，`ui/render.py` 负责 Markdown 与日志的呈现。`cli.py` 只有 `main()`
- **适配层**：`adapters/mumu.py`（模拟器生命周期）、`adapters/adb.py`（所有 ADB 调用）、`adapters/ai.py`（Gemini）、`adapters/secrets.py`（DPAPI）、`adapters/database.py`（SQLite）
- **纯解析器**：`parsers/village.py`、`parsers/battle.py`

每一个结构化的值都是 Pydantic model，全部集中在 `models.py`。会阻塞的调用一律走 `QThreadPool` 的 worker，再用 signal 回到 UI 线程。

程序状态放在 `~/.ai_coc`：SQLite 数据库、截取的画面、导入的账号 JSON，以及 DPAPI 保护的 key 文件。

## 📚 文档

文档用 [Zensical](https://zensical.org/) 构建，并由 `scripts/gen_docs.py` 从源码自动生成。

```bash
uv sync --group docs
make gen-docs                  # 从源码生成 markdown
uv run zensical serve          # http://0.0.0.0:9987
```

`make gen-docs` 会重建 `docs/`，把三份 README 复制进去，再对 `./src` 与 `./scripts` 执行 `gen_docs.py`。

## 📦 打包与发布

用 uv 构建产物，wheel 与 sdist 会放到 `dist/`：

```bash
uv build
```

发布到 PyPI（需要 `UV_PUBLISH_TOKEN`）：

```bash
UV_PUBLISH_TOKEN=... uv publish
```

推一个 `v*` tag 会触发 `build_release.yml`，它用 `dunamai` 从 git 推导版本号，构建 wheel 与 sdist、发布到 PyPI，再用 PyInstaller 打包 Windows 版本，最后把全部产物挂到 GitHub Release 上。

打包默认走 `--onedir`：zip 里是可执行文件加上一个 `_internal/` 文件夹，启动比单文件版本快好几秒，因为单文件版本每次启动都要先把自己解压开。手动触发这个 workflow 时有一个 `package_mode` 选项，需要单一 `.exe` 的话从那里选。

## 🧭 可选的任务执行器（Poe the Poet）

方便用的任务定义在 `pyproject.toml` 的 `[tool.poe.tasks]`，安装 dev 分组（`uv sync --group dev`）之后或通过 `uvx` 就可以用：

```bash
uv run poe docs        # 生成并启动文档服务器（需要 dev 分组）
uv run poe gen         # 生成并部署文档（gh-deploy）（需要 dev 分组）
uv run poe main        # 启动程序（等同 uv run ai_coc）

# 或者用 uvx 临时执行，不安装到本地
uvx poe docs
```

## 🔁 CI/CD 流程总览

所有的 workflow 都放在 `.github/workflows/`。

- 测试（`test.yml`）

    - 触发时机：推送与 pull request 到 `main` 或 `release/*`（忽略 md 文件）
    - 在 Python 3.12/3.13/3.14 上跑 pytest 与覆盖率，并留下摘要评论

- 代码质量检查（`code-quality-check.yml`）

    - 触发时机：pull request
    - 执行 ruff 与其余的 pre-commit 检查

- 文档部署（`deploy.yml`）

    - 触发时机：推送到 `main` 以及 `v*` tag
    - 构建 `zensical` 网站并发布到 GitHub Pages
    - 需要设置：在 repo 开启 GitHub Pages（Settings → Pages → Source: GitHub Actions）

- 构建与发布（`build_release.yml`）

    - 触发时机：推送 `v*` tag 或手动触发
    - 用 PyInstaller 构建 Windows x64 可执行文件，另外构建 wheel 与 sdist
    - 发布到 PyPI（需要 `UV_PUBLISH_TOKEN` secret），并把所有产物上传到 GitHub Release

- 发布 Docker 镜像（`build_image.yml`）

    - 触发时机：推送到 `main` 以及 `v*` tag
    - 构建镜像并推送到 GHCR：`ghcr.io/<owner>/<repo>`

- Release Drafter（`release_drafter.yml`）

    - 触发时机：推送到 `main` 以及 PR 事件
    - 依照 Conventional Commits 维护一份草稿 release

- 代码扫描（`code_scan.yml`）

    - 触发时机：推送与 PR
    - 执行 gitleaks；CodeQL 那个 job 需要 GitHub Advanced Security，在 repo 还是 private 的期间会被跳过

- 语义化 Pull Request（`semantic-pull-request.yml`）

    - 触发时机：PR 开启、编辑、同步
    - 强制 PR 标题符合 Conventional Commit 格式

### CI/CD 设置检查清单

- PR 标题要用 conventional commits（由 workflow 强制）
- 设置 `UV_PUBLISH_TOKEN` secret 才能发布到 PyPI（Settings → Secrets and variables → Actions）
- 可选：开启 GitHub Pages 以部署文档（Settings → Pages → Source: GitHub Actions）
- Container Registry 的权限由 `GITHUB_TOKEN` 自动处理

## 🤝 参与贡献

- 欢迎开 issue 或 PR
- 遵循既有的代码风格（ruff、type hints）
- 使用 Conventional Commit 消息与清楚的 PR 标题

## 📄 许可

MIT，详见 `LICENSE`。
