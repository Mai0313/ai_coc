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

一个 Windows 桌面应用程序, 用来玩跑在 MuMu 模拟器 12 或雷电模拟器 14 里的《部落冲突》。画面识别是它自己做的, 读到什么就转成 ADB 点击, 并在下一张截图上确认结果。Gemini 每一场只被问一个问题: 这个村庄该怎么打。

其他语言版本：[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

## ✨ 功能

**自己打资源。** 搜索对手, 跳过战利品不到门槛的, 十秒左右把整支军队放下去, 打完回营再来下一场, 仓库满了自己停。画面识别是在本机做的而不是送出去问: 战利品面板、兵力条、卡片行全部靠模板匹配, 所以跳过一个对手不花钱, 一场战斗也不会卡在网络上。

**每一场只问 Gemini 一个问题**, 而且是在对手已经通过门槛之后才问: 这个村庄该怎么打。它回答投兵线的两端、每瓶狂暴跟冰冻的落点, 还有每张英雄卡是谁。没有 API key 的话会改用固定战术, 其他功能照常。

**把打到的花掉。** 城墙付钱的当下就升级完成, 不占工人也不跑计时器, 所以仓库满了就是它的出口: 循环自己在地图上找墙, 算出村庄买得起的最便宜批次再买。闲着的工人可以派去做买得起的最贵升级, 英雄也能一个一个往上升。

**顾着村庄不停摆。** 收采集器、读每个工人在盖什么还剩多久、有人在部落聊天室要兵就捐给他。

**自己爬得回来。** 挂太久被踢掉的连线会自动重开游戏继续跑, 不管当下在跑哪个循环。服务器没回应的时候会等它回来, 而不是一直对着载入画面乱点。

从游戏取出的村庄 JSON 会保留看不懂的字段跟 `data_id` 而不是直接失败, 段落也是读出来的而不是写死一份清单。

## 📋 环境需求

- Windows。程序要调用 `mumu-cli.exe` 或 `ldconsole.exe`、用 `winreg` 读注册表、用 `ctypes.windll` 读写剪贴板, 这些在别的系统上都不存在
- [MuMu 模拟器 12](https://www.mumuplayer.com/) 或 [雷电模拟器 14](https://www.ldmnq.com/), 装好部落冲突, 分辨率 1600x900。雷电要在设置里打开 ADB 调试 (本地连接)
- 一把 Gemini API key, 在设置页填。它只回答三件事: 每场的战术、城墙跟建筑在地图上的位置、以及某个选单是哪一栋建筑。没有它的话进攻循环会退回固定战术, 刷墙跟升级改用扫描找目标, 而收采集器、工人、英雄、捐兵那几个指令本来就不会问它任何事

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
ai_coc giveback                  # 把借来的模拟器还给原本在跑的循环
ai_coc attack --stop-at 0        # 不管仓库多满都照打
ai_coc attack --repeat 0 --until-idle  # 这个村庄有工人或实验室闲着也收工
ai_coc attack --repeat 0 --until-idle builder  # 只看工人 (`lab` 只看实验室)
```

`stop` 在 `~/.ai_coc/state.json` 上做个记号就返回。循环在回合之间跟对手之间读它, 绝不会在战斗中途停, 所以最坏是多打一场: 打到一半弃权会把军队丢在场上, 游戏停在下一轮回不了家的画面。没有东西在跑的时候它会直接说没有, 而不是留一个没人会收的请求。

战利品门槛读配置文件, 也可以只盖过这一次。**不给旗标跟给 `0` 是两件事**: 不给是沿用配置文件的值, `0` 才是把那条门槛整个拿掉:

```bash
ai_coc attack --min-gold 800000
ai_coc attack --min-gold 0 --min-elixir 0 --min-dark 0    # 打第一个看到的对手
```

一轮可以把它看过的画面全部留下来, 那是事后跟这场吵架的依据:

```bash
ai_coc attack --record                # 循环自己读的每一张, 文件名就是它当下在问什么
ai_coc attack --record --shot-every 4 # 另外每四秒再存一张
```

战术是一份文件而不是写死的常量, 所以打得好的那场可以重来, 打得烂的那场可以改。重播的时候完全不会调用 Gemini:

```bash
ai_coc attack --plan-out used.json    # 把这一场实际用的计划存下来
ai_coc attack --plan used.json     # 照那份再打一次, 改过的也算
```

### 把打到的花掉

```bash
ai_coc walls                          # 拿仓库去升级城墙, 买到钱不够为止
ai_coc walls --keep-elixir 2000000    # 留这么多圣水下来练兵
ai_coc upgrade                        # 把闲着的工人派去做买得起最贵的升级
ai_coc hero                           # 每个英雄升下一级要多少
ai_coc hero --upgrade queen           # 真的派一个工人去升那个英雄
```

升级城墙要用掉一个空闲工人, 只是它付钱当下就升好、工人立刻还回来, 所以工人全忙的时候一片也买不了, `walls` 会停下来讲。`hero` 默认只读不花钱, 要指名哪个英雄才会真的升。哪个英雄值得一个工人是关于村庄怎么玩的判断, 不是价格能决定的。

### 顾着村庄

```bash
ai_coc collect                        # 把有东西的采集器全部收掉
ai_coc builders                       # 每个工人在盖什么、还要多久
ai_coc worker                         # 同样的东西，但看当下那个世界，不切世界
ai_coc lab                            # 那个世界的实验室在研究什么、还要多久
ai_coc status                         # 工人、实验室、仓库跟护盾，一次读完
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
ai_coc capture --count 30             # 从活着的游戏连续抓画面,存进这次运行自己的文件夹
ai_coc <任何命令> --label baseline    # 给这次的文件夹取名字,之后找得回来
ai_coc <任何命令> --agent codex --session <id> --mission "打资源"  # 谁叫的; agent 一律要带, 没带会在 log 留 warning
ai_coc read shot.png                  # 每个识别器从这张图读到什么
ai_coc export                         # 从游戏里取出整个村庄,对照过名称,吐 JSON
ai_coc export --table                 # 同一份东西,画成表格给人看
ai_coc export --last                  # 直接读上一次的结果,完全不碰游戏
ai_coc view --zoom out                # 把镜头拉回所有坐标当初测量的那个视野
ai_coc world                          # 现在在日世界还是夜世界
ai_coc world --go day                 # 坐船切过去,已经在那边就什么都不做
ai_coc attack                         # 打游戏当下所在的那个村庄
```

游戏有两个村庄,日世界 (主村) 跟夜世界 (建筑大师基地),而它会开在上次离开的那一个,所以任何预设站在主村上的指令之前都值得先问一次 `world`。读它只花一张截图,不点任何东西也不动镜头。**没有指令会自己坐船**: 换村庄只有 `world --go` 一个,而在当下的村庄没事可做的指令 (例如在夜世界跑 `walls`) 会直接说明,游戏留在原地。

`read` 是回答「是识别错了, 还是那一下没点到」最快的方法: 它把一张图丢给每个识别器, 打印出战利品面板、仓库、卡片行、工人面板各自读到什么。

### 这次运行看到的东西放在哪

每一次运行都有自己的目录, 不管是从哪一边开的:

```
~/.ai_coc/logs/2026-08-29-011423-attack/
├── run.log        # 这一次的记录, 不掺别的
├── result.json    # 这个命令回答了什么
├── plans.jsonl    # 只有 ai_coc attack 有: 每打一场一行, 那一场用的战术
└── frames/        # 只有 --record 才有
```

目录在哪, 开跑第一行就会说, 而名字是「时间-命令」, 所以整个列出来就是一份历史。任何命令都可以加 `--label` 再接一段上去 (`2026-08-29-011423-attack-baseline`), 那是给之后要找回来的那一次用的。没有第二份把所有运行混在一起的文件 —— 要跨好几次找模式就 `grep -r ~/.ai_coc/logs/*/run.log`, 而且它会告诉你每一笔是哪一次跑出来的。

**画面留七天, log 永久留。** 一次运行的 `frames/` 过了七天就会被删掉, 同个目录里其他东西一律不动。这个差别值得讲清楚, 因为它非常不对称: 这台机器上 11 456 张画面加起来 27.2 GB, 而所有 `run.log`、`result.json`、`plans.jsonl` 加起来只有 4.6 MB。要留久一点的东西自己复制出来。

## ⚙️ 设置

`~/.ai_coc/config.json` 窗口跟终端都会读, 所以两边跑出来的结果一样:

```json
{
  "thresholds": {
    "min_gold": 500000,
    "min_elixir": 500000,
    "min_dark": 5000
  },
  "stop_at": 90,
  "adb_serial": "127.0.0.1:16384",
  "gemini": {
    "api_key": "",
    "main": {
      "model": "gemini-3.5-flash",
      "base_url": "",
      "thinking_level": "low"
    },
    "lite": {
      "model": "gemini-3.5-flash-lite",
      "base_url": "",
      "thinking_level": "low"
    }
  },
  "ui": {
    "jobs": {
      "collect": false,
      "donate": false,
      "upgrade": false,
      "walls": false,
      "attack": false
    },
    "cycle_minutes": 10,
    "live_view": true,
    "record_frames": false
  }
}
```

这就是整份文件。**找不到文件的那次执行只会为了记下 `adb_serial` 建一份** —— 其他时候直接用上面这些值, 不动硬盘 —— 所以它是第一次有指令或窗口挑好模拟器、或窗口第一次写它的时候才出现的, 按下「储存设定」或者动一下预览上面那两个开关都算。存在之后每次加载都会被校正一次, 而且是哪一边先加载就由哪一边校正: 程序不再读的键会被删掉, 文件里没有的键会被补上, 所以硬盘上那份永远是「下一次跑会怎么跑」而不是「某一次存过什么」。

- **thresholds**: 谁值得打。订太高的话一轮会跳过几十个对手还开不了打
- **stop_at**: 每一种资源都满到几 % 就收工。**主村跟夜世界共用这一个数字**, 因为仓库的容量是循环自己去游戏里读的 —— 点一下储量条, 游戏就把 最大储存量 写在旁边。要**每一项**都满了才停, 不是任何一项到顶就走 —— 一场仗带回来三种资源, 一个仓库到顶不足以放弃另外两种还在赚的。夜世界的圣水车也算一个: 圣水仓库满了之后, 每一场的防守奖励还会存进车子里, 所以那边要连车子也满到同一个 % 才收工, 车子的上限写在它自己的面板上。`0` 代表永远不收工, 容量读不到的那一项也不列入计算。`--stop-at` 只盖过这一次, 给 `0` 也可以, 那是拿一个刚被打满的村庄测一场战斗时要用的
- **adb_serial**: 要驱动哪一台模拟器, 写 `adb devices` 列出来的那个名字 (`127.0.0.1:16384`、`127.0.0.1:5555`, 或者 `emulator-5554`, 它跟 `127.0.0.1:5555` 是同一台雷电)。留空的话第一次运行会挑正在跑游戏的那一台, 两边都有就用 MuMu, 然后把它的名字写回这里; 在窗口的模拟器列表里选一台也会写进来。写了一个没有模拟器认得的名字会直接报错, 不会去猜
- **gemini**: 哪个模型接哪一种调用。`main` 一整趟只问一次, 而且问的是一整张截图 —— 进攻计划, 找目标 —— 没有在跟谁抢时间, 所以就用比较好的那个。`lite` 是**每一个候选问一次**, 而且只给一条裁下来的窄带, 那是分类不是判断, 便宜的模型的位置在这里。`base_url` 留空就是 Google 官方端点。`api_key` 是两个 tier 共用的那一把密钥, 以明文存放; 设置页的「储存设定」会写进来, 它空着的时候改用环境变量或 `.env` 里的 `GEMINI_API_KEY`
- **ui**: 只有窗口会读的东西, 也是最后一批不在这份文件里的。它们本来住在 Windows 注册表, 那里没有编辑器打得开, 也没有任何一个 `ai_coc` sub-command 读得到 —— 理由是「终端机不会问的设置不该放进共用文件」, 而那条规矩换来的就是用户唯一改不到的东西刚好是窗口自己的行为。`jobs` 是窗口那一轮会轮流跑哪几个命令, 键名就是 sub-command 的名字, 所以 `"walls": true` 跑的跟 `ai_coc walls` 是同一个工作, 只是轮到它的时候只跑一趟, 而不是像命令自己的默认那样刷到仓库撑不住为止。`cycle_minutes` 只会用在什么都没做的那一轮, 因为一个工作跑完会直接排下一轮。`live_view` 是主控分页里那个即时预览, `record_frames` 则是 `--record` 的打勾版

大招与法术的秒数不在这里了。以前它是一张按英雄写死的表, 而要填那张表, 就得在没看过村庄的情况下猜军队要走多久 —— 那是规划那一步的事, 而它是看着村庄做的。现在每一个时钟都写在计划里: 形状看 `plans/flat.json`, 要重播一份就用 `--plan`。

其他东西都放在 `~/.ai_coc`: `ai_coc export` 存下的账号 JSON、每次运行的 log, 还有 `state.json` —— 现在是哪个命令在驱动模拟器、它的 pid、log 目录, 还有是谁开的 (`--agent`、`--session`、`--mission`, 或 `window`)。跑完之后不会删掉, 所以那份记录说的是上一次跑的是什么, 而不是变成空的。它也是一把锁: 要操作模拟器的命令会先请占着的那次运行收工, 等它退出; 那次运行的程序已经不在的话就直接接手。带 `--yield` 开的是后台工作: 它不会停掉别人, 别人要用模拟器时它是被借走而不是被停掉, `ai_coc giveback` 还回来 (或借用的那一边最后一个命令跑完半小时) 之后, 它的 agent 会把它开回去。

## 🤝 参与贡献

开发环境、架构、打包发布跟 CI 的说明都在 [CONTRIBUTING.md](https://github.com/Mai0313/ai_coc/blob/main/.github/CONTRIBUTING.md)。

## ⚠️ 免责声明

本项目与 Supercell 没有任何关系, 也没有得到 Supercell 的认可或赞助。《部落冲突》(Clash of Clans) 是 Supercell 的商标, 游戏的名称与美术素材都属于 Supercell。

Supercell 的服务条款禁止使用自动化软件、模拟器和机器人, 并允许 Supercell 封禁或删除使用这些工具的账号。运行这个工具, 就是让它操作的账号承担这个风险, 请自行斟酌使用。

## 📄 许可

MIT, 见 `LICENSE`。
