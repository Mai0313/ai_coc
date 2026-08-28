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

一个 Windows 桌面应用程序, 用来玩跑在 MuMu 模拟器 12 里的《部落冲突》。画面识别是它自己做的, 读到什么就转成 ADB 点击, 并在下一张截图上确认结果。Gemini 每一场只被问一个问题: 这个村庄该怎么打。

其他语言版本：[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

## ✨ 功能

**自己打资源。** 搜索对手, 跳过战利品不到门槛的, 十秒左右把整支军队放下去, 打完回营再来下一场, 仓库满了自己停。画面识别是在本机做的而不是送出去问: 战利品面板、兵力条、卡片行全部靠模板匹配, 所以跳过一个对手不花钱, 一场战斗也不会卡在网络上。

**每一场只问 Gemini 一个问题**, 而且是在对手已经通过门槛之后才问: 这个村庄该怎么打。它回答投兵线的两端、每瓶狂暴跟冰冻的落点, 还有每张英雄卡是谁。没有 API key 的话会改用固定战术, 其他功能照常。

**把打到的花掉。** 城墙付钱的当下就升级完成, 不占工人也不跑计时器, 所以仓库满了就是它的出口: 循环自己在地图上找墙, 算出村庄买得起的最便宜批次再买。闲着的工人可以派去做买得起的最贵升级, 英雄也能一个一个往上升。

**顾着村庄不停摆。** 收采集器、读每个工人在盖什么还剩多久、有人在部落聊天室要兵就捐给他。

**自己爬得回来。** 挂太久被踢掉的连线会自动重开游戏继续跑, 不管当下在跑哪个循环。AI 助手打的每一个指令都存成任务, 中途断掉下次启动会接着做完。

**密钥不放在明文设置里。** Gemini API key 走 Windows DPAPI, 不进注册表也不进配置文件。

导入的村庄 JSON 会保留看不懂的字段跟 `data_id` 而不是直接失败; 导入的战斗脚本不会被播放, 只验证兵力需求, 停在保留的交接边界。

## 📋 环境需求

- Windows。程序要调用 `mumu-cli.exe`、用 `winreg` 读注册表、用 `ctypes.windll` 调用 DPAPI, 这些在别的系统上都不存在
- [MuMu 模拟器 12](https://www.mumuplayer.com/), 装好部落冲突, 分辨率 1600x900
- 想要每场的战术建议就需要一把 Gemini API key, 在设置页填。其他功能没有它也能跑

## 🚀 安装与运行

从 PyPI 运行, 不留任何东西在系统上:

```bash
uvx ai_coc
```

或者正常安装:

```bash
uv tool install ai_coc
ai_coc
```

每个 [release](https://github.com/Mai0313/ai_coc/releases) 都附了编译好的 Windows 可执行文件。

## 🎮 从终端玩

窗口是一种用法, 另一种是下 sub-command, 跑的是同一套循环但完全不开窗口。平常都是这样玩的, 因为终端空着就能盯 log。

### 打资源

```bash
ai_coc attack                    # 打一场
ai_coc attack --repeat 5         # 连打五场
ai_coc attack --repeat 0         # 一直打到某个仓库满为止
ai_coc stop                      # 打完当下这一场就收工
```

`stop` 写一个标志文件就返回。循环在回合之间跟对手之间读它, 绝不会在战斗中途停, 所以最坏是多打一场: 打到一半弃权会把军队丢在场上, 游戏停在下一轮回不了家的画面。

战利品门槛读配置文件, 也可以只盖过这一次。**不给旗标跟给 `0` 是两件事**: 不给是沿用配置文件的值, `0` 才是把那条门槛整个拿掉:

```bash
ai_coc attack --min-gold 800000
ai_coc attack --min-gold 0 --min-elixir 0 --min-dark 0    # 打第一个看到的对手
```

一轮可以把它看过的画面全部留下来, 那是事后跟这场吵架的依据:

```bash
ai_coc attack --frames ./run          # 循环自己读的每一张, 文件名就是它当下在问什么
ai_coc attack --frames ./run --shot-every 4   # 另外每四秒再存一张
```

战术是一份文件而不是写死的常量, 所以打得好的那场可以重来, 打得烂的那场可以改。重播的时候完全不会调用 Gemini:

```bash
ai_coc attack --plan-out used.json    # 把这一场实际用的计划存下来
ai_coc attack --plan-in used.json     # 照那份再打一次, 改过的也算
```

### 把打到的花掉

```bash
ai_coc walls                          # 拿仓库去升级城墙, 买到钱不够为止
ai_coc walls --keep-elixir 2000000    # 留这么多圣水下来练兵
ai_coc upgrade                        # 把闲着的工人派去做买得起最贵的升级
ai_coc hero                           # 每个英雄升下一级要多少
ai_coc hero --upgrade queen           # 真的派一个工人去升那个英雄
```

城墙自己不占工人, 但工人全忙的时候游戏会把整批退掉, 所以 `walls` 会停下来讲。`hero` 默认只读不花钱, 要指名哪个英雄才会真的升。哪个英雄值得一个工人是关于村庄怎么玩的判断, 不是价格能决定的。

### 顾着村庄

```bash
ai_coc collect                        # 把有东西的采集器全部收掉
ai_coc builders                       # 每个工人在盖什么、还要多久
ai_coc donate                         # 部落里有人要兵就捐
ai_coc donate --dry-run               # 走完整个流程但停在真的捐出去之前
```

### 把游戏弄起来

其他每个指令都假设游戏已经在跑, 没开的话它们自己会开。它们处理不了的是「开着但不理人」的模拟器或游戏, 那是这几个的用途:

```bash
ai_coc launch                         # 模拟器没开就开, 然后把游戏叫起来
ai_coc launch --restart game          # 只重开游戏, 模拟器不动
ai_coc launch --restart emulator      # 连模拟器一起重开, 再把游戏叫起来
```

### 看它看到了什么

```bash
ai_coc capture ./shots --count 30     # 从活着的游戏连续抓画面
ai_coc read shot.png                  # 每个识别器从这张图读到什么
ai_coc view --zoom out                # 把镜头拉回所有坐标当初测量的那个视野
```

`read` 是回答「是识别错了, 还是那一下没点到」最快的方法: 它把一张图丢给每个识别器, 打印出战利品面板、仓库、卡片行、工人面板、边界各自读到什么。

## ⚙️ 设置

`~/.ai_coc/config.json` 窗口跟终端都会读, 所以两边跑出来的结果一样:

```json
{
  "thresholds": {
    "min_gold": 500000,
    "min_elixir": 500000,
    "min_dark": 5000
  },
  "stock": {
    "stop_gold": 18000000,
    "stop_elixir": 18000000,
    "stop_dark": 0
  },
  "timings": {
    "queen": 1,
    "warden": 30,
    "champion": 45,
    "freeze": 30
  }
}
```

- **thresholds**: 谁值得打。订太高的话一轮会跳过几十个对手还开不了打
- **stock**: 什么时候收工。任何一项到顶就结束, 不是等三项都满。`0` 代表这一项不看
- **timings**: 开打之后几秒放每个英雄的大招, 以及法术什么时候丢。是按英雄而不是按卡片位置记的, 因为升级中的英雄根本没有卡片

其他东西都放在 `~/.ai_coc`: SQLite 数据库、存下来的画面、导入的账号 JSON、log, 还有 DPAPI 保护的密钥文件。

## 🤝 参与贡献

开发环境、架构、打包发布跟 CI 的说明都在 [CONTRIBUTING.md](.github/CONTRIBUTING.md)。

## 📄 许可

MIT, 见 `LICENSE`。
