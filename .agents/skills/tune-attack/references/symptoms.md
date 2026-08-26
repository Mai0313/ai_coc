# 症狀對照該去看什麼

這張表只指路, 不給結論. 結論會過期, 而且 `CLAUDE.md` 已經把每一條的來龍去脈寫得比這裡完整. 用法是: 找到症狀, 拿關鍵字去 grep `CLAUDE.md`, 再去讀那個函式.

一輪的走法是 攻擊 -> 兵力檢查 -> 尋找對戰目標 -> scout -> 規劃 -> 相機 -> 探線 -> 出兵 -> 排程 -> 等結束 -> 回營, 全部在 `src/ai_coc/ui/attack.py` 的 `AttackRunner`. 下面照這個順序排.

## 開不了攻擊選單

**症狀**: message 說畫面不在主村, 這一輪什麼都沒做.

看 `_open_attack_menu`, `attack_menu_open`, 以及 `ui/runner.py` 的 `_home`. `_home` 是所有迴圈共用的回家路徑, 它要分辨四種擋路的東西: 遊戲還在載入, 開著的面板, 對話框, 掉線. `CLAUDE.md` 搜 `back` 跟 `確定退出遊戲嗎`, 那一段解釋為什麼在乾淨的村莊上按 back 是災難.

掉線的話 `idle_disconnected` 會認出來, `restart_game` 會重開遊戲並**重新解析 display**.

## 每一輪都說兵力不足

看 `army_strength` 跟 `MIN_ARMY_RATIO`. 這個檢查在付搜尋費**之前**, 所以擋下來是不用錢的, 它本身通常不是 bug.

真的每輪都擋就去看兵營: 兵可能根本沒在練, 或者軍隊配置被改成一個永遠練不滿的組合. 抓一張軍隊畫面丟 `ai_coc read` 看 `army` 欄位讀到什麼, 跟眼睛看到的比對.

## 一直跳過對手

`skipped` 每輪都幾十, 就是 `~/.ai_coc/config.json` 的 `thresholds` 對現在的獎盃區間太高. 這不是 bug, 是設定. 跟使用者講一聲再調.

## 戰利品讀錯

**症狀**: 明明有 100 萬卻讀成 10 萬, 或者一個對手在不同格畫面上讀出不同的數字.

看 `parsers/scout.py` 的 `read_scout` 以及它底下切數字的那一段. `CLAUDE.md` 搜 `1 047 758`, 那一段講了三種靜默的讀錯: 中間的字元配不上就整行放棄, 只有右端配得差才丟掉, 以及數字之間沒有間隙所以超過 18 px 的區塊要切開而且切點要可信.

## 迴圈走出還在打的戰鬥

**症狀**: 戰鬥還在進行卻回報結束, 或者回不了家.

`read_scout` 回 `None` 是**兩種相反的意思**: 正在搜尋對手, 以及這張畫面讀不出來. 把它當成戰鬥結束就會走出還在打的仗. 真正判斷結束的是 `_battle_ended`, 它讀結果畫面自己的綠色回營按鈕, 不需要任何數字. `CLAUDE.md` 搜 `overloaded`.

## 兵丟不出去

**症狀**: 打完一場但幾乎沒搶到東西, 或者 `_wait_out_battle` 說整場戰利品沒動過.

這是最常見也最貴的一類. 依序看:

- `_usable_line` 在線的兩端跟中間丟兵當探針, 回的是**往外推了幾步**而不是那條線
- `_spread_troops` 在整個 pass 都沒消耗的時候再往外推, 因為村莊是菱形, 外圍**不是凸的**, 兩端都在外面的線中間還是可能穿過去
- `push_out` 推到 `DEPLOY_BOUND` 的邊緣就不再動了, 所以一直推同一個點是有可能的
- `boundary_reach` 跟 `fitted_line` 是把預設側翼彎到真實邊界上的那一步, `fitted_line` 用三個錨點而不是兩個, 理由在 `CLAUDE.md` 搜 `chord`

判斷一次出兵成不成功一律看 `card_drained`, 不要看畫面上有沒有兵. `CLAUDE.md` 搜 `你無法在紅線區域內派遣部隊`, 那一段列了為什麼畫面上的紅色橫幅完全不能用.

還沒開打就丟兵也會全部失敗, 那是 `_wait_for_battle` 在管的.

## 下兵線畫穿村莊

`planned_line` 會拒絕中點離螢幕中心太近的線, 因為 `push_out` 是往中心的反方向推, 推不動一個剛好在中心上的點. 被拒絕就掉回 `deploy_candidates` 的四個預設側翼.

如果 AI 一直畫出這種線, 問題在 `src/ai_coc/prompts/attack_plan.md`, 不在迴圈.

## 下方側翼站不下人

看 `_clear_flank` 跟 `FLANK_ROOM`. 卡片列就站在下方側翼上面, 所以要先把村莊拖離卡片列再探線. `CLAUDE.md` 搜 `FLANK_ROOM`.

## 座標整個對不上

相機動過而沒有被記下來. 看 `_settle_camera`, `_pan`, `_panned`, `_onscreen`, 以及 `parsers/boundary.py` 的 `view_shift` 跟 `village_box`.

`view_shift` 是沿著拖曳方向把兩張畫面滑過去比對, 不是量兩次村莊位置, 因為 `village_box` 會被村莊自己的紅色裝飾騙到. `CLAUDE.md` 搜 `98 px`.

已知而且沒修的一件事也寫在 `CLAUDE.md` 裡: `_settle_camera` 拿到的是 scout 畫面, 而 `village_box` 不是為那張畫面校正的.

## 多讀到一張卡

卡片列尾端的空槽是虛線框, 背景亮的時候會被切成一張卡然後被當成多出來的英雄. 分辨的方法是等級徽章, 看 `parsers/scout.py` 的 `_badged`.

`card_groups` 只在**完整**的卡片列上有效, 所以 `_scout` 把它讀到的那張畫面交給 `_deploy`, 而不是讓 `_deploy` 自己去截.

## 英雄沒下去

`field_units` 讀卡片上方那條血條, 靠藍色分辨血條跟草地. 英雄卡不會清空, 因為它變成技能按鈕, 所以不能用 `card_drained` 判斷.

假陰性有代價: 重試會再點一次那張卡, 而那一下就是放技能. 看 `_drop_singles`, `_landed`, `HERO_SETTLE`.

重試的落點是 `single_spots`, 不是單純「再往外一點」, 理由同 `push_out` 那條.

## 法術沒放出去

看 `_cast`, `freeze_cards`, `SPELL_SELECT_DELAY`. 法術的**選取**是慢的, 放置不是; 卡片還有貨的時候會維持選取, 所以後面幾發是快速連發.

冰凍可以放在紅線內, 所以法術失敗是點被吞掉, 不是位置被拒絕.

## 怒吼丟在空地上

計畫是在 scout 畫面上畫的, 而軍隊不會待在那裡. `_onto_army` 用 `parsers/field.py` 的 `army_centre` 把整組落點平移到現在打得最兇的地方. 看 `MOTION_GAP`, `MOTION_FLOOR`, `MOTION_CEILING`.

冰凍不平移, 因為它瞄的是防禦建築而防禦建築不會走路.

## 出兵太慢

整支軍隊該在十秒左右下完. 順序是攻城機具, 部隊, 英雄, 然後所有上時鐘的事情才開始跑. 看 `_deploy` 跟 `_run_schedule`, 以及 `TAP_GAP` / `SINGLE_DROP_DELAY` / `DROP_SETTLE` / `HERO_SETTLE` 這幾個常數旁邊的註解.

`CLAUDE.md` 搜 `7 seconds`, 那一段有改之前跟改之後的實測秒數, 可以拿來對照你現在量到的.

## 刷牆停了

看 `WallReport.message`, 它會說是牆滿級了還是錢不夠. 再往下是 `ui/walls.py` 的 `_pick` 跟 `parsers/building.py` 的 `wall_menu`.

`_pick` 用價格挑下一片牆, 因為牆每一級都變貴, 所以最便宜的選單就是地圖上最矮的牆. 那個函式就是留給 AI 規劃器接手的位置.
