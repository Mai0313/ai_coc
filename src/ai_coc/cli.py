from __future__ import annotations

import os
import sys
import json
import time
import shlex
from typing import TYPE_CHECKING, Any, get_args
import logging
from pathlib import Path
import argparse
from datetime import date, datetime
from contextlib import nullcontext
import subprocess

from rich.table import Table
from rich.console import Console

# PyInstaller runs this file as `__main__`, which has no package context, so these
# stay absolute even for same-layer modules.
from ai_coc import plans, commands, __version__
from ai_coc.models import (
    Caller,
    RunLog,
    RunEnded,
    AttackRun,
    NoteAction,
    RunStarted,
    StopTarget,
    UpgradeRole,
    AttackSeries,
    ExportReport,
    RestartScope,
    AttackOptions,
    DonateOptions,
    LootOverrides,
    SearchOptions,
    SearchSection,
    StandDownReport,
)
from ai_coc.constants import FRAME_RETENTION_DAYS
from ai_coc.logging_setup import Journal, configure_logging

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from collections.abc import Callable

    from pydantic import BaseModel, JsonValue

logger = logging.getLogger(__name__)

# How long `git rev-parse` may take to name the commit a run is on.
COMMIT_WAIT = 5
# The longest `result.json` copied whole onto a run's `end` line. A village
# export runs to hundreds of kilobytes and is kept in `account_json/` anyway;
# a status, with every running upgrade, comes to a few.
JOURNAL_RESULT_LIMIT = 16_000


def _log_uncaught(kind: type[BaseException], value: BaseException, trace: Any) -> None:  # noqa: ANN401 - matches sys.excepthook
    """Keeps a crash's traceback in `run.log`; the default hook prints it to stderr alone."""
    logger.critical("未處理的例外", exc_info=(kind, value, trace))


# The sub-commands that never capture the screen: `read` parses a PNG off the
# disk, `stop` and `giveback` each change one field of the state file, `watch`
# sleeps on its own section of it, `note` keeps the notes section, and
# `growth` compares saved exports, `turnaround`, `farmed` and `tended` read the
# journal, `wait` reads it and one run's process, and `search` reads the kept
# tables and exports, none of them opening ADB;
# `gamedata` opens it only to read the game's files, and `hold` claims the
# emulator and does nothing with it. Every other one captures the screen, so
# every other one takes `--debug` and `--shot-every`.
NO_SCREEN = (
    "read",
    "stop",
    "giveback",
    "watch",
    "note",
    "growth",
    "turnaround",
    "search",
    "farmed",
    "tended",
    "wait",
    "gamedata",
    "hold",
)

# The sub-commands that hold no claim on the emulator, listed the way round
# that fails safely: the ones that never capture the screen, and `capture`. Everything
# else claims, short ones included — a `collect` holds the display for the
# eight seconds it takes, and a session reading the state file to find out
# whether the screen is free wants that as much as it wants to know about a
# farming run.
#
# Naming the exceptions rather than the claimers is what keeps a command added
# later honest: forgotten here it claims, which costs two writes it did not
# need, where a positive list forgotten would have it drive the emulator while
# the file says nobody is.
#
# `export --last` and `status --last` are the same exception reached by a flag
# rather than by a name, and `_claim_for` is where the two meet: the command
# drives the emulator, that one invocation of it does not. So is `world`
# without `--day` or `--night`.
#
# `capture` only looks, and so does a plain `world`: both take screenshots and
# nothing else, and the file is a lock now, so a claim there would stand a
# farming loop down for one screenshot (#290). `hold` is the one command that
# captures nothing and still claims, since the claim is all it does.
WITHOUT_CLAIM = (*(name for name in NO_SCREEN if name != "hold"), "capture")

# The sub-commands whose stdout is text shaped like the report a skill hands the
# user, and which take `--json` for the model instead. Text is the default
# because whoever reads their stdout is an agent about to write that report, and
# every reshaping it did by hand was a step a skill had to spell out; a program
# reads `result.json`, which is the model whatever the terminal was shown.
TEXT_ANSWERS = ("attack", "status", "growth", "turnaround", "search", "farmed", "tended", "note")


# Every sub-command and the line `--help` gives it, under the heading the help
# lists it under, in the order a person reaches for them: playing the game,
# reading it without spending, upgrading, sharing the emulator, then the tools
# for working on the parsers.
COMMANDS = (
    (
        "操作遊戲",
        (
            ("attack", "進攻遊戲當下所在的村莊，預設打一輪"),
            ("collect", "收掉採集器裡的資源；在夜世界收的是聖水車"),
            ("donate", "部落有人要兵就捐"),
            ("world", "看遊戲停在日世界還是夜世界，加 --day 或 --night 就開船過去"),
            ("launch", "開模擬器和部落衝突，停在村莊畫面"),
            ("view", "把村莊鏡頭拉遠或拉近"),
        ),
    ),
    (
        "讀資訊（不花資源）",
        (
            (
                "status",
                "兩個世界每件升級實際幾點好（藥水和鐘塔已算進去）跟下一級的估價，加上這個村莊的閒置人數、倉庫、寶石和護盾",
            ),
            ("stock", "這個村莊的倉庫存量和容量，還有寶石數"),
            ("export", "從遊戲匯出整個村莊的資料，對上名稱後存起來"),
            ("growth", "不碰遊戲，比對存下的兩份村莊匯出，列出兩個世界升了什麼"),
            ("turnaround", "不碰遊戲，從紀錄算每次升級從到點到按下確認花了幾秒"),
            (
                "farmed",
                "不碰遊戲，從紀錄把一個 session 打資源的每一趟加起來：打了幾場、進帳多少、現在多少",
            ),
            (
                "tended",
                "不碰遊戲，從紀錄列出一個 session 升了什麼、各花多少，開了幾次鐘塔，被搶登幾次",
            ),
            (
                "search",
                "不碰遊戲，照名稱、data_id 或分類查遊戲的表跟村莊匯出，兩個世界分開列出等級、升級中幾點好跟下一級的牌價",
            ),
            (
                "gamedata",
                "從模擬器上的遊戲檔讀出中英文名稱、最高等級和每級的要求、牌價、時間，照遊戲版本存起來；不碰畫面",
            ),
            ("capture", "連續截圖，存進這次的紀錄資料夾"),
        ),
    ),
    (
        "升級（建造面板和鐘塔）",
        (
            ("tap", "點畫面上一個點，存下點完的畫面；升級確認視窗只能點它的 X"),
            ("swipe", "從一點滑到另一點，例如捲動建造面板，存下滑完的畫面"),
            (
                "confirm",
                "確認鈕上的價格跟 --price 一樣、而且是金幣聖水黑水，才按下確認；加 --pay 是從城牆選單的升級鈕開始付；"
                "加 --goblin 是付寶石聘請哥布林工人",
            ),
            ("boost", "夜世界鐘塔能開時（上面有時鐘標記）按下它的免費加速；要寶石的不按"),
        ),
    ),
    (
        "共用模擬器",
        (
            # Its own command rather than a flag on `attack`, because the run being
            # stopped is a different process: whatever put that one in the background
            # cannot send it a Ctrl-C, and killing it leaves the game mid-battle.
            (
                "stop",
                "請正在用模擬器的指令做完手上這件事就收工，盯升級的也一起停；後面加 emulator 或 watch 只停一個",
            ),
            (
                "hold",
                "替這個 session 保留模擬器，什麼都不做；別的 session 的指令在保留到期前不會開跑，"
                "tap、swipe、confirm 要在自己的保留裡才會按；giveback 放掉",
            ),
            (
                "giveback",
                "放掉這個 session 的保留，把借來的模擬器還回去，借出的那一邊會自己接著跑",
            ),
            (
                "watch",
                "睡到 --next、--upgrades、--tower、--minutes 裡最早的那個時間才結束，期間在狀態檔記下是誰在盯升級；丟到背景跑，結束時叫醒你",
            ),
            (
                "wait",
                "不碰遊戲，說一次執行跑完了沒、還在跑、在排隊等模擬器，還是程序已經不在；加 --for 就等它跑完，最多等那麼多秒",
            ),
            (
                "note",
                "使用者的交代，記在狀態檔裡；不給參數就列出來，add 加一條，done 刪掉做完的第幾條，"
                "哪個世界的用 --day 或 --night",
            ),
        ),
    ),
    (
        "開發與量測",
        (
            ("read", "不碰遊戲，把一張截圖交給每個判讀器，印出各自讀到什麼"),
            ("probe", "花一場戰鬥實測部署邊界，對照判讀器的結果"),
            ("bounds", "花一場戰鬥實測地圖邊緣，推回村莊範圍"),
        ),
    ),
)


def _watch_flags(
    watcher: argparse.ArgumentParser,
    stopper: argparse.ArgumentParser,
    waiter: argparse.ArgumentParser,
) -> None:
    """`watch` never claims and never opens ADB: it sleeps on its own section of
    the state file, where a second agent sees somebody is watching and `stop`
    reaches it. `stop` reaches both sections unless told which. `wait` reads
    the journal and one run's process.

    Its own function only because `_parser` is at Ruff's statement limit.
    """
    waiter.add_argument(
        "run", type=Path, help="執行目錄、它的名字，或開它時給的 --label（同一個標籤取最新的那次）"
    )
    waiter.add_argument(
        "--for",
        dest="seconds",
        type=float,
        default=0.0,
        metavar="秒",
        help="等它跑完或程序不在，最多等這麼多秒；不給就照現在的樣子回答",
    )
    watcher.add_argument(
        "--next",
        type=datetime.fromisoformat,
        action="append",
        default=[],
        metavar="時間",
        help="睡到這個時間，ISO 格式；可以給好幾個，跟下面幾個一起比，睡到最早的那個",
    )
    watcher.add_argument(
        "--upgrades",
        nargs="*",
        choices=get_args(UpgradeRole),
        metavar="builder|lab|pet",
        help="也算進最新那份村莊匯出裡每件在升的好的時間，提早 --lead 分鐘（不到那麼多分鐘的就是好的那一刻，"
        "已經好了的就是現在）；給了格子就只算那幾格的",
    )
    watcher.add_argument(
        "--lead", type=int, default=0, metavar="分鐘", help="--upgrades 的每一件提早幾分鐘醒"
    )
    watcher.add_argument(
        "--tower", action="store_true", help="也算進鐘塔能開的時間；現在就能開的不算"
    )
    watcher.add_argument("--minutes", type=int, metavar="分鐘", help="也算進現在起這麼多分鐘後")
    watcher.add_argument("--about", default="", metavar="說明", help="在等什麼，一句話")
    watcher.add_argument(
        "--farms",
        action="store_true",
        help="睡的這一邊在兩次醒來之間打資源；別的 session 讀到就不再派一趟打資源",
    )
    watcher.add_argument(
        "--hold",
        action="store_true",
        help="睡的時候也保留著這個 session 的模擬器，到醒來後兩分鐘（最多從現在起十分鐘）；"
        "這個 session 沒有保留就回 no_hold，不睡",
    )
    stopper.add_argument(
        "target",
        nargs="?",
        choices=get_args(StopTarget),
        default="all",
        help="只停模擬器上的指令（emulator）或只停盯升級（watch）；不給就兩個都停",
    )


def _growth_flags(grower: argparse.ArgumentParser) -> None:
    """`growth` reads only the exports `status` and `export` saved, one a day."""
    for flag, note in (
        ("--start-date", "從這天一開始算起（含這天）；不給就從存下的最舊那一份"),
        ("--end-date", "算到這天結束（含這天）；不給就到存下的最新那一份"),
    ):
        grower.add_argument(flag, type=date.fromisoformat, metavar="YYYY-MM-DD", help=note)


def _village_flags(command: argparse.ArgumentParser, day: str, night: str) -> None:
    """`--day` and `--night`, the one way a command is told which village; never both."""
    which = command.add_mutually_exclusive_group()
    which.add_argument("--day", dest="world", action="store_const", const="day", help=day)
    which.add_argument("--night", dest="world", action="store_const", const="night", help=night)


def _search_flags(searcher: argparse.ArgumentParser) -> None:
    """`search` reads only the game's tables kept on disk and the exports `status` and `export` saved."""
    searcher.add_argument(
        "query",
        nargs="?",
        default="",
        help="全是數字就是 data_id，不然是中文或英文名稱的一段（英文不分大小寫）；"
        "沒有名字含這一段的就列名字最接近的幾種；村莊還沒有的也查得到，不給就只列村莊裡有的",
    )
    searcher.add_argument(
        "--section",
        choices=(*get_args(SearchSection), "all"),
        help="只列這一類：builder 是工人升的（建築、英雄、陷阱、守護者），lab 是實驗室研究的"
        "（兵種、法術、攻城機器），pets 戰寵，equipment 英雄裝備，helpers 助手，all 每一類都列",
    )
    _village_flags(searcher, "只列日世界；兩個都不給就兩個世界各列一段", "只列夜世界")
    searcher.add_argument("--skip-max-lvl", action="store_true", help="已經升到遊戲滿級的不列")
    searcher.add_argument(
        "--rows",
        dest="every_row",
        action="store_true",
        help="村莊裡的每一列分開列，各自的等級、升級幾點好、是不是哥布林那格",
    )


def _farmed_flags(farm: argparse.ArgumentParser) -> None:
    """`farmed` and `tended` read only the journal; at least one of the two is required (`main`)."""
    farm.add_argument(
        "--by", metavar="ID", help="這個 session 跟它的打資源 subagent（ID 加 -farm）的"
    )
    farm.add_argument(
        "--since", type=datetime.fromisoformat, metavar="時間", help="從這個時間起開跑的，ISO 格式"
    )


def _note_flags(noter: argparse.ArgumentParser) -> None:
    """`note` never opens ADB either: it keeps the notes section of the state file.

    The documented lines write the village flag last (`note add "…" --day`):
    an argparse older than 3.12.7 ends the positionals at a flag written
    between them and then refuses the text.
    """
    noter.add_argument(
        "action",
        nargs="?",
        choices=get_args(NoteAction),
        default="list",
        help="不給就是 list，兩個世界的都列",
    )
    noter.add_argument("item", nargs="?", default="", help="add 是交代的內容，done 是第幾條")
    _village_flags(noter, "add 跟 done 的是日世界的交代", "add 跟 done 的是夜世界的交代")


def _press_flags(
    press: argparse.ArgumentParser, drag: argparse.ArgumentParser, buy: argparse.ArgumentParser
) -> None:
    """The hands the agent drives the build panel with.

    They press where they are told and refuse only where a press would spend;
    reading the screen is the agent's, off the frame each one leaves. Their own
    function for the reason `_watch_flags` is.
    """
    press.add_argument("x", type=int, help="橫座標，1600x900 畫面上")
    press.add_argument("y", type=int, help="縱座標")
    for name in ("x1", "y1", "x2", "y2"):
        drag.add_argument(name, type=int, help="起點到終點的座標")
    drag.add_argument("--ms", type=int, default=commands.SWIPE_TIME, help="滑多久，毫秒")
    for one in (press, drag):
        one.add_argument(
            "--wait", type=float, default=commands.PRESS_SETTLE, metavar="秒", help="等多久再截圖"
        )
    buy.add_argument("--price", type=int, required=True, help="在面板那一列讀到的價格")
    buy.add_argument("--item", metavar="名稱", help="買的是哪一件、升到幾級，記進紀錄給 tended 列")
    how = buy.add_mutually_exclusive_group()
    how.add_argument(
        "--pay",
        choices=("gold", "elixir"),
        help="城牆:從牆的選單按這種資源的升級鈕,再付跳出來的確認",
    )
    how.add_argument(
        "--goblin",
        action="store_true",
        help="聘請哥布林工人:按它視窗上的寶石鈕,寶石數要跟 --price 一樣",
    )


def _parser() -> argparse.ArgumentParser:
    """Every argument the entry point takes; a sub-command is required."""
    parser = argparse.ArgumentParser(
        prog="ai_coc",
        # Written out, because the action that parses the sub-commands is hidden
        # below and argparse leaves a hidden action out of the usage line too.
        usage="%(prog)s [-h] <指令> ...",
        description=(
            "在模擬器上自動玩部落衝突。每個指令只處理遊戲當下停著的那個村莊，"
            "換村莊用 world --day 或 world --night。"
        ),
    )
    # A metavar instead of the default brace list of every name, which an
    # error naming this argument would print. `prog` is named because a
    # sub-command's own usage line otherwise starts with the whole of the
    # usage written out above.
    sub = parser.add_subparsers(
        dest="command", metavar="<指令>", required=True, help=argparse.SUPPRESS, prog="ai_coc"
    )
    # argparse lists every sub-command under the one action that parses them,
    # so that action is hidden and the line it makes for each sub-command is
    # moved into a section of its own heading.
    for heading, group in COMMANDS:
        section = parser.add_argument_group(heading)
        for name, note in group:
            sub.add_parser(name, help=note, description=note)
            section._group_actions.append(sub._choices_actions.pop())  # noqa: SLF001 - argparse has no public way to group sub-commands
    # It plays whichever village the game is on and never sails there itself:
    # crossing is `world --day` or `--night`, the one command that moves the game.
    run = sub.choices["attack"]
    run.add_argument(
        "--plan",
        metavar="名稱或路徑",
        help=f"照這份戰術打，完全不問 AI；給名稱（{'、'.join(plans.NAMES)}）就用遊戲停著那個村莊的那一份，"
        "給 JSON 路徑就照那一份，它寫明是哪個村莊的，遊戲停在另一邊就不打；不給就每場問 AI",
    )
    run.add_argument("--plan-out", type=Path, help="把實際用的戰術寫成 JSON")
    run.add_argument("--repeat", type=int, default=1, help="打幾輪，0 代表打到叫停為止")
    # Omitted means "whatever the config file says", like the loot thresholds
    # below; 0 is how a test run attacks a village the farming has already
    # filled, where the file's percentage would stand it down first.
    run.add_argument(
        "--stop-at",
        type=int,
        metavar="%",
        help="每種倉庫都滿到這個百分比就收工，蓋過設定檔；0 代表不管倉庫多滿都照打",
    )
    run.add_argument(
        "--until-idle",
        nargs="*",
        choices=("builder", "lab"),
        metavar="builder|lab",
        help="這個村莊有工人或實驗室閒著也收工；只給旗標就兩個都看，倉庫的收工條件照舊",
    )
    # Omitted means "whatever the config file says". Three zeros is how a run
    # being studied gets back to attacking the first opponent it is shown.
    for flag, resource in (("gold", "金幣"), ("elixir", "聖水"), ("dark", "黑水")):
        run.add_argument(
            f"--min-{flag}", type=int, help=f"只打{resource}至少這麼多的對手，蓋過設定檔"
        )
    # The game reopens on whichever village it was closed on, so no other
    # command can assume which one it is looking at. Reading is the default and
    # crossing is what naming a village asks for.
    _village_flags(
        sub.choices["world"],
        "開船到日世界，已經在那邊就什麼都不做；兩個都不給就只看現在在哪邊",
        "開船到夜世界",
    )
    _watch_flags(sub.choices["watch"], sub.choices["stop"], sub.choices["wait"])
    _note_flags(sub.choices["note"])
    _growth_flags(sub.choices["growth"])
    _search_flags(sub.choices["search"])
    _farmed_flags(sub.choices["farmed"])
    _farmed_flags(sub.choices["tended"])
    sub.choices["turnaround"].add_argument(
        "--since",
        type=datetime.fromisoformat,
        metavar="時間",
        help="只算這個時間起的，ISO 格式；不給就是全部",
    )
    give = sub.choices["donate"]
    give.add_argument("--dry-run", action="store_true", help="走完流程但不真的捐，只回報能捐什麼")
    give.add_argument("--repeat", type=int, default=0, help="最多捐幾次，0 代表捐到不能捐為止")
    # Every other command assumes the game is up and gives up when it is not, so
    # this is the one that puts it there. The scopes exist because neither an
    # emulator nor a game that has stopped answering looks any different from a
    # working one down here; only whoever is watching the screen can tell.
    boot = sub.choices["launch"]
    boot.add_argument(
        "--restart",
        choices=get_args(RestartScope),
        default="none",
        help="要重開到哪一層：none 只確保遊戲在跑，game 只重開遊戲，emulator 連模擬器一起重開；預設 none",
    )
    camera = sub.choices["view"]
    camera.add_argument(
        "--zoom",
        choices=("out", "in"),
        default="out",
        help="out 拉遠到所有座標量測時的視野，in 拉近；預設 out",
    )
    camera.add_argument("--times", type=int, default=3, help="做幾次，已經到底的話多做無害")
    _press_flags(sub.choices["tap"], sub.choices["swipe"], sub.choices["confirm"])
    # The game's own village export, which is the only complete account of what
    # the village holds and far more than any screen reader can see.
    village = sub.choices["export"]
    village.add_argument(
        "--table", action="store_true", help="印成表格給人看；不給就跟其他指令一樣輸出 JSON"
    )
    village.add_argument("--last", action="store_true", help="不碰遊戲，直接讀上一次匯出的結果")
    sub.choices["status"].add_argument(
        "--last",
        action="store_true",
        help="不碰遊戲，從存下的最新那份村莊匯出印升級時間；倉庫跟計數不讀",
    )
    sub.choices["status"].add_argument(
        "--within", type=int, metavar="小時", help="表上只列這麼多小時內好的，之後的併成一行"
    )
    for name in TEXT_ANSWERS:
        sub.choices[name].add_argument(
            "--json", action="store_true", help="印 JSON（跟 result.json 一樣），不印給人看的文字"
        )
    sub.choices["gamedata"].add_argument(
        "--refresh", action="store_true", help="這個版本已經存過也重新讀一次"
    )
    # No directory argument: it writes into this run's own `frames/` like every
    # other command that saves what it saw. Whoever ran it used to invent a
    # path, and `--label` is what that need becomes.
    shot = sub.choices["capture"]
    shot.add_argument("--count", type=int, default=1, help="存幾張")
    shot.add_argument("--gap", type=float, default=1.5, metavar="秒", help="每張間隔幾秒")
    sub.choices["read"].add_argument("png", type=Path, help="要判讀的截圖")
    sub.choices["hold"].add_argument(
        "--for",
        dest="seconds",
        type=float,
        metavar="秒",
        help="這次之後保留多久；不給是兩分鐘，最多十分鐘",
    )
    for name, one in sub.choices.items():
        _shared_flags(name, one)
    return parser


def _shared_flags(name: str, command: argparse.ArgumentParser) -> None:
    """The flags a sub-command shares with the others, under their own heading in its `--help`.

    Each goes only where it does something: a flag a command ignores is one
    more line to read past, and a `--yield` on a command that never claims
    reads as if it could be lent.
    """
    shared = command.add_argument_group("共用的旗標")
    # Every sub-command, because why a session names a run — to find it again
    # afterwards — has nothing to do with which one it ran.
    shared.add_argument(
        "--label",
        default="",
        metavar="名稱",
        help="加在這次紀錄資料夾名字的後面，方便之後認出是哪一次",
    )
    # Who asked, for the same reason on every sub-command: `stop` and `read`
    # included, since the log is where a stop somebody issued gets traced.
    # Optional so a person can still type a command bare; the skills are
    # what hold an agent to them.
    shared.add_argument(
        "--agent",
        default="",
        metavar="名稱",
        help="是誰叫的，例如 claude-code、antigravity、codex",
    )
    shared.add_argument(
        "--session", default="", metavar="ID", help="叫它的 agent 自己的 session id"
    )
    shared.add_argument("--mission", default="", metavar="任務", help="這次在做什麼，一句話")
    # Background work says so itself, since only its caller knows whether it
    # will be started again: farming is, a live test is not. Only a command
    # that claims the emulator can be lent, and only a claim can wait out
    # another session's hold. `hold` is never background work.
    if name not in WITHOUT_CLAIM:
        turn = shared.add_mutually_exclusive_group()
        if name != "hold":
            turn.add_argument(
                "--yield",
                dest="yields",
                action="store_true",
                help="背景工作：不停掉別人，等模擬器空出來才開始；別人要用時借出去，還回來再接著跑",
            )
        turn.add_argument(
            "--queue",
            type=float,
            default=0.0,
            metavar="秒",
            help="別的 session 正保留著模擬器時，最多等這麼多秒；不給就馬上回 held，不開跑",
        )
    if name not in NO_SCREEN:
        shared.add_argument(
            "--debug",
            action="store_true",
            help="把這次讀到的每張畫面存進紀錄資料夾，log 也降到 DEBUG",
        )
        shared.add_argument(
            "--shot-every",
            type=float,
            default=0.0,
            metavar="秒",
            help="除了指令自己讀的畫面，每隔這麼多秒再存一張",
        )


def _caller(arguments: argparse.Namespace) -> Caller:
    return Caller(agent=arguments.agent, session=arguments.session, mission=arguments.mission)


def _answer(arguments: argparse.Namespace, run: RunLog) -> BaseModel | str:
    """What one sub-command answers, as the model it reports with or as plain text.

    One table rather than a branch per command: every entry turns the parsed
    flags into whatever `commands` takes, which for the loops is an options
    model, and the caller prints whatever comes back.
    """
    a = arguments
    handlers: dict[str, Callable[[], BaseModel | str]] = {
        "attack": lambda: commands.attack(
            AttackOptions(
                plan=None if a.plan is None or a.plan in plans.NAMES else Path(a.plan),
                plan_name=a.plan if a.plan in plans.NAMES else None,
                plan_out=a.plan_out,
                minimums=LootOverrides(
                    min_gold=a.min_gold, min_elixir=a.min_elixir, min_dark=a.min_dark
                ),
                rounds=a.repeat,
                stop_at=a.stop_at,
                until_idle=[] if a.until_idle is None else a.until_idle or ["builder", "lab"],
            ),
            run.frames,
        ),
        "stop": lambda: commands.stop(_caller(a), a.target),
        "hold": lambda: commands.hold(a.seconds),
        "giveback": lambda: commands.giveback(_caller(a)),
        "watch": lambda: commands.watch(
            _caller(a),
            commands.wake_at(a.next, a.upgrades, a.lead, a.minutes, a.tower),
            a.about,
            run.directory,
            farms=a.farms,
            hold=a.hold,
        ),
        "note": lambda: commands.note(a.action, a.world, a.item),
        "growth": lambda: commands.growth(a.start_date, a.end_date),
        "turnaround": lambda: commands.turnaround(a.since),
        "search": lambda: commands.search(
            SearchOptions(
                query=a.query,
                section=a.section,
                world=a.world,
                skip_max_lvl=a.skip_max_lvl,
                every_row=a.every_row,
            )
        ),
        "farmed": lambda: commands.farmed_runs(a.by, a.since),
        "tended": lambda: commands.tended(a.by, a.since),
        "wait": lambda: commands.wait(a.run, a.seconds),
        "collect": lambda: commands.collect(run.frames),
        "stock": lambda: commands.stock(run.frames),
        "status": lambda: commands.status(run.frames, a.last),
        "world": lambda: commands.world(a.world, run.frames),
        "view": lambda: commands.view(run.frames, a.zoom, a.times),
        "tap": lambda: commands.tap((a.x, a.y), run.frames, a.wait),
        "swipe": lambda: commands.swipe((a.x1, a.y1), (a.x2, a.y2), run.frames, a.ms, a.wait),
        "confirm": lambda: commands.confirm(a.price, run.frames, a.pay, a.goblin, a.item),
        "boost": lambda: commands.boost(run.frames),
        "launch": lambda: commands.launch(a.restart, commands.stop_requested, run.frames),
        "donate": lambda: commands.donate(
            DonateOptions(dry_run=a.dry_run, rounds=a.repeat), run.frames
        ),
        "probe": lambda: commands.probe(run.frames),
        "bounds": lambda: commands.bounds(run.frames),
        "capture": lambda: "\n".join(
            str(path) for path in commands.capture(run.frames, a.count, a.gap)
        ),
        "read": lambda: commands.read(a.png.read_bytes()),
        "export": lambda: commands.export(run.frames, a.last),
        "gamedata": lambda: commands.gamedata(run.frames, a.refresh),
    }
    return handlers[a.command]()


def _print_table(report: ExportReport) -> None:
    """The export as a table for a person, which is what `--table` asks for.

    A row the game's tables have no name for shows `—` rather than its number:
    the number is already in the column beside it, and this project's rule is
    that a blank says "not known" where an invented value would not.
    """
    console = Console()
    if (export := report.export) is not None:
        table = Table(title=f"{export.tag}  {len(list(export.rows()))} 筆  {export.exported_at}")
        for column in ("World", "Section", "Data ID", "Name", "Level", "Max", "Count"):
            table.add_column(column)
        for world, section, row in export.rows():
            table.add_row(
                world,
                section,
                str(row.data),
                row.zh or "—",
                "—" if row.lvl is None else str(row.lvl),
                "—" if row.max_lvl is None else str(row.max_lvl),
                str(row.cnt or 1),
            )
        console.print(table)
    # Always, and it is the whole output when there is no export: one that
    # failed says here which step it stopped at — nothing saved yet, a menu that
    # did not open, a clipboard that stayed empty — and the JSON branch that
    # would have shown it is the one `--table` replaced. `--last --table` on a
    # machine that has never run this is exactly that case, and it is the line
    # the `upgrades` skill tells a session to start with.
    console.print(commands.export_line(report))


def _run_command(arguments: argparse.Namespace, run: RunLog) -> int:
    """Run one headless command; its answer goes to stdout, as text for the commands in `TEXT_ANSWERS`.

    Written rather than logged: the log already carries the running commentary
    on stderr, and this is the answer. It goes into the run's own directory as
    well, so the answer and the log explaining it are found together instead of
    that depending on whoever started the run having redirected stdout, and
    onto the run's `end` line in the journal, which outlives that directory.
    """
    started = time.monotonic()
    cancelled = False
    try:
        # The heartbeat inside the claim, so a run still queued for the
        # emulator, or refused it, takes no tick.
        with (
            _claim_for(arguments, run),
            commands.FrameTicker(frames=run.frames, seconds=getattr(arguments, "shot_every", 0.0)),
        ):
            answer = _answer(arguments, run)
    # A queue that a stop ended is written down like an answer, not left as a
    # crash: `takeover` reads a run with no `result.json` as a killed process. The
    # exit below still says it did not run. So is a command that stood down
    # because another device logged in, which can happen to any of them, and a
    # watch that found no state file to sit on.
    except tuple(commands.STAND_DOWNS) as error:
        answer, cancelled = commands.stand_down(error), True
    # Outside the claim: writing the answer down and printing it touch no
    # emulator, and holding the screen across them would say this run is still
    # driving when it has finished.
    result = answer if isinstance(answer, str) else answer.model_dump_json(indent=2)
    run.answer(result)
    # `result.json` is the answer whatever the terminal is shown, so a run stays
    # machine-readable after a `--table` that was only ever for a person.
    if getattr(arguments, "table", False) and isinstance(answer, ExportReport):
        _print_table(answer)
    else:
        text = (
            None
            if getattr(arguments, "json", False) or isinstance(answer, str)
            else commands.answer_text(answer, getattr(arguments, "within", None))
        )
        sys.stdout.write(f"{result if text is None else text}\n")
    if isinstance(answer, StandDownReport):
        logger.info("%s", commands.STAND_DOWN_LINES[answer.outcome])
    # Still not zero, though: the stop meant nothing lined up behind the loan
    # runs, and a command chained after this one with `&&` would otherwise
    # start with no loan left to be cancelled.
    code = 1 if cancelled else 0
    # An attack keeps its last round alone, the others having `round` lines.
    last = (
        answer.model_copy(update={"rounds": AttackSeries(answer.rounds.root[-1:])})
        if isinstance(answer, AttackRun)
        else answer
    )
    kept: JsonValue = last if isinstance(last, str) else last.model_dump(mode="json")
    Journal(run).write(
        RunEnded(
            exit=code,
            seconds=round(time.monotonic() - started, 1),
            outcome=answer.ending
            if isinstance(answer, AttackRun)
            else getattr(answer, "outcome", None),
            result=kept if len(json.dumps(kept)) <= JOURNAL_RESULT_LIMIT else None,
        )
    )
    return code


def _commit() -> str | None:
    """The commit of the checkout this code runs from, or None outside one."""
    try:
        done = subprocess.run(  # noqa: S603 - git with literal arguments and this file's own directory
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],  # noqa: S607 - git is found on PATH like any developer tool
            capture_output=True,
            text=True,
            timeout=COMMIT_WAIT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def _claim_for(arguments: argparse.Namespace, run: RunLog) -> AbstractContextManager[None]:
    """Hold the emulator for this command, or nothing at all for one that never touches it.

    Mostly that is a property of the command, but `export --last` and
    `status --last` only read a file this machine already has. A claim there would overwrite whatever a
    farming run had written, and release it as `idle` — which reads from
    outside as an emulator nobody is driving, while a battle is still going on.
    """
    if (
        arguments.command in WITHOUT_CLAIM
        or getattr(arguments, "last", False)
        or (arguments.command == "world" and arguments.world is None)
    ):
        return nullcontext()
    return commands.claim(
        arguments.command,
        run.directory,
        _caller(arguments),
        yields=getattr(arguments, "yields", False),
        queue=arguments.queue,
        linger=commands.lingering(arguments.seconds)
        if arguments.command == "hold"
        else commands.HOLD_TIME,
    )


def main() -> int:
    sys.excepthook = _log_uncaught
    parser = _parser()
    # Nothing at all, or `help`, is somebody asking what there is: the same
    # answer as `--help`, and like it no run.
    if sys.argv[1:] in ([], ["help"]):
        parser.print_help()
        return 0
    arguments = parser.parse_args()
    # Here rather than in `search`, so a search with nothing to look for opens
    # no run directory, like any other line the parser refuses.
    if arguments.command == "search" and not (arguments.query or arguments.section):
        parser.error("search 要給名稱、data_id 或 --section")
    if arguments.command in ("farmed", "tended") and not (arguments.by or arguments.since):
        parser.error(f"{arguments.command} 要給 --by 或 --since")
    if arguments.command == "watch" and not (
        arguments.next
        or arguments.upgrades is not None
        or arguments.minutes is not None
        or arguments.tower
    ):
        parser.error("watch 要給 --next、--upgrades、--tower 或 --minutes")
    # A hold is a session's, so one with no session would hold nothing.
    if arguments.command == "hold" and not arguments.session:
        parser.error("hold 要給 --session，保留是那個 session 的")
    # Refused rather than ignored: a list is always both villages, and one that
    # took the flag and printed both would read as if it had narrowed them.
    if arguments.command == "note" and arguments.action == "list" and arguments.world:
        parser.error("note 列出來的是兩個世界的交代，--day 跟 --night 只給 add 跟 done")
    # Opened after parsing, so `--help` and a rejected flag leave no empty
    # directory behind, and so its name can say which command it holds.
    run = RunLog.open(
        arguments.command,
        recording=getattr(arguments, "debug", False),
        label=getattr(arguments, "label", ""),
    )
    configure_logging(run)
    # One switch for a run someone means to debug: the frames it reads, and the
    # lines too bulky to keep for every run (each raw ADB call, Gemini's whole
    # prompt and reply).
    if getattr(arguments, "debug", False):
        logging.getLogger().setLevel(logging.DEBUG)
    caller = _caller(arguments)
    # Before anything this run logs reaches the journal, so its warnings follow
    # the line saying which run they belong to.
    Journal(run).write(
        RunStarted(
            log=run.directory,
            command=arguments.command,
            args=sys.argv[1:],
            caller=caller,
            pid=os.getpid(),
            pid_created=commands.started_at(os.getpid()),
            version=__version__,
            commit=_commit(),
        )
    )
    # First line of every run, because a directory nobody can name is one nobody
    # goes back to: this is what a session reads to find the frames afterwards.
    logger.info("This run is being kept in %s", run.directory)
    # Second line of every run: `state.json` holds only the latest, and the
    # run directories are the history someone greps to find who ran what.
    logger.info(
        "Called by agent=%r session=%r mission=%r", caller.agent, caller.session, caller.mission
    )
    if not (caller.agent and caller.session and caller.mission):
        logger.warning("Missing --agent/--session/--mission; this run will not say who ran it")
    # Third: the flags as typed, which nothing else here records, so a run is
    # read for what it was asked to do rather than inferred from what it did.
    logger.info("Started as: ai_coc %s", shlex.join(sys.argv[1:]))
    # Fourth, and only when it happened: the cull runs inside `RunLog.open`,
    # before any handler exists, and the one destructive thing in the project
    # should not be the one that leaves no trace. A session following a path out
    # of an old report to a directory that is gone could not otherwise tell a
    # cull from a bug.
    if run.culled:
        logger.info(
            "Deleted the frames of %d run(s) older than %d days; their logs are untouched",
            run.culled,
            FRAME_RETENTION_DAYS,
        )
    return _run_command(arguments, run)


if __name__ == "__main__":
    raise SystemExit(main())
