# 症狀對照該去看什麼

這張表只指路, 不給結論; 來龍去脈在常數或函式旁邊的註解. 找到症狀, 拿關鍵字 grep `src/` (跟釘住它的 `tests/`), 規則面看 `AGENTS.md`, 再讀那個函式.

整條路是 **開起遊戲 -> 站上要打的那個村莊 -> 打完一場 -> 回到村莊 -> 開下一輪**, 中間那段在 `src/ai_coc/ui/attack.py` 的 `AttackRunner`: 攻擊 -> 倉庫檢查 -> 尋找對戰目標 -> 兵力檢查 -> scout -> 縮放與置中 -> 規劃 -> 清側翼 -> 照 plan 的步驟出兵、開技能、放法術 (`_play_tactic`) -> 等結束 -> 回營 (不等偵察倒數, 理由在「兵丟不出去」末尾). 下面照這個順序排.

容易記反的兩處: **兵力檢查在尋找對戰目標之後** (兩步都在付搜尋費之前); **相機在規劃之前**, 先縮放再置中. 倉庫檢查在攻擊選單一開就做, 滿了整輪收工, 不搜對手.

## 遊戲起不來, 或者重開之後沒回來

**症狀**: `ai_coc launch` 回 `at_village: false` (log 裡有 `Gave up after 180s waiting for the game: ...`, 冒號後面寫著最後在等什麼), 或一輪連攻擊選單都沒碰到就結束.

`ai_coc launch` 的收工條件是**站在村莊上而且鏡頭在最遠處**, 不是 pid (`ensure_coc` 只看 `pidof`): 看 `LaunchReport.at_village`. 重開範圍 `none` / `game` / `emulator` 在 `AGENTS.md` 搜 `RestartScope`; `emulator` 要等實例真的倒下 (`_await_shutdown`).

**`launch` 等村莊** (`_settle_game`) 有三處會出錯:

- **等太短.** 冷開機時間差很多, `RESTART_POLLS` 照最壞情況抓; 每次 poll 在 DEBUG 寫下還在等什麼 (要 `COC_LOG_LEVEL=DEBUG`), 平常只看得到放棄那一行冒號後面的最後狀態.
- **鏡頭沒回到最遠處.** 重開成功, 然後連兩輪英雄一張都沒下去, 兵卡也都還有貨 (當時的 log 是 `0 of 4 hero card(s) landed` 跟 `3 troop card(s) still hold something`; 現在英雄那行寫成 `After the burst: N of N one-off card(s) never landed` 接 `0 of N retry card(s) landed at`). `launch` 收尾會 pinch, 沒 pinch 的是掉線後 `_open_attack_menu` 叫的 `restart_game`. 確認方法見「整場一隻兵都沒下去」.
- **等的是畫完而不是哪個村莊.** `_settle_game` 只問 `current_world` 讀不讀得出 (不用 `read_stock`, 它在兩個世界都答得出來, 分不出是哪一個), 兩個世界都算; 落在另一邊 attack 回 `other_village`, 主村指令回 `builder_base`. 載入時倉庫水位是從零跑上來的動畫, 那時讀到的數字不能回報.

## 開不了攻擊選單

**症狀**: `AttackReport.outcome` 是 `no_attack_menu`.

看 `_open_attack_menu` (它自己分辨掉線、載入、結算畫面, 讀不到村莊就交給 `ui/world.py` 的 `uncovered`) 跟 `attack_menu_open`. `ui/runner.py` 的 `_home` 是 `walls`、`upgrade` 那些 `GameRunner` 的回家路徑, 攻擊迴圈不走它. 在乾淨的村莊上按 back 是災難, `AGENTS.md` 搜 `back` 跟 `確定退出遊戲嗎`.

三個常被懷疑的原因:

- 掉線: `idle_disconnected` 認 (閒置跟 連線已中斷 都算), `restart_game` 重開並重新解析 display. 但每次重開連同接下來的載入畫面大約佔兩次 `HOME_ATTEMPTS`, 所以反覆掉線三次左右就會報成 `no_attack_menu` (手機搶登是不是走這條沒量過).
- 伺服器 (不會報成這個): `loading_screen` 認, `_wait_out_loading` 等 (每次載入一次, 最多 45 分鐘), outcome 是 `server_loading` 或 `server_flapping`.
- 站錯村莊 (不會報成這個): `_open_attack_menu` 先問 `current_world`, 連兩張是另一個村莊就回 `other_village`, 不坐船.

**夜世界多一個來源.** 搜尋等滿 `SEARCH_PATIENCE` 被取消後, `_reopen_search` 要按 攻擊 叫回對話框; 叫不回來就是 `no_attack_menu`, 前一行是 `The attack dialog did not come back after the search was cancelled`. 那就看 `_reopen_search` 當下那張 `attack-menu` 畫面, 不是 `_open_attack_menu`.

**全螢幕彈窗** (活動獎勵, 賽季通行證) 最貴: 攻擊鈕被蓋住, 重試全點在彈窗上, 實測卡過 40 分鐘. `current_world` 讀不出東西時 `uncovered` 會按 back, log 是 `Something is over the village; pressing back to get at it`. 沒這行就是彈窗被誤讀了, 有兩種形狀:

- 讀成**結算畫面**: 量 `RETURN_HOME_BOX` 的綠色比例 (活動獎勵頁 0.2009, 真結算 0.3283).
- 讀成**戰鬥**: log 是 `A battle is on screen; there is nothing here to press back at` 而不是在打仗. `uncovered` 要 `card_groups` 跟 `in_battle` 兩個都成立 (或 `battle_over`) 才寫這行, 所以是其中兩個一起誤判, 或 `battle_over` 誤判: 丟 `ai_coc read` 看是哪一個. 只有 `card_groups` 有東西而 `in_battle` 是 `false` 的面板 (探礦者, 商店 外觀 頁) 走不到這行, 會被按 `back`. `in_battle` 讀 `false` **不代表戰鬥結束**: 彈窗會壓暗那塊紅色, 建築大師基地開場倒數時不畫那顆按鈕.

## 坐船沒坐成

**症狀**: `ai_coc world --go night` 回「畫面還停在不明的畫面」, 或整輪在打錯的村莊.

沒有判讀器找船 (活動會換造型): 相機推到地圖角落夾住, 點固定像素, 再看世界變了沒. `park_camera` 滑到 `view_shift` 連兩次回 (0, 0) 為止; `camera was still moving` 的 warning 要查的是鏡頭, 不是船的座標. 它先 pinch 再滑, 因為拉近時 `view_shift` 沒東西可比而回 None. 在最遠處主村一下就滑到底, 夜世界根本滑不動, 而要連兩次沒動才算停, 所以 `stopped moving after 2 swipe(s)` (主村從別處滑過來是 3) 是正常的.

點歪會打開那裡的建築, 面板會吞掉後面的點擊, 所以每個沒坐成的點後面 (跟第一下滑動之前) 補一次 `uncovered` (畫面讀不出村莊才按 `back`, 最多 `UNCOVER_TRIES` 次). `back` **絕對不按在戰鬥上** (被砍掉的執行會把遊戲留在戰鬥裡, 那會點到 放棄): `uncovered` 先認載入畫面, 再要 `card_groups` 跟 `in_battle` 兩個都成立 (或者是結算畫面) 才算戰鬥, 戰鬥回 `None`. 那個判讀的坑在上一節末尾.

分辨兩個村莊靠頂端面板徽章**的跨度而不是數量** (`AGENTS.md` 搜 `933`): 寶石雨動畫會蓋掉一格.

## 兵力不足, 而且整個 series 停了

看 `army_strength` 跟 `MIN_ARMY_RATIO`. 在付搜尋費之前擋, 不花錢, 不是 bug, 而且**一輪就收掉整個 series**, 不等 `IDLE_REST`: 造兵不花時間, 擋下來就是已儲存的軍隊配置湊不到兵營一半 (2026-09-22 活動結束拿掉活動兵, 剩 20/340), 要人進遊戲改.

所以**看軍隊畫面然後告訴使用者**, 不是查判讀器: 抓一張丟 `ai_coc read` 看 `army`, 跟眼睛比對, 對得上就把圖發給他. 門檻是一半不是九成, 因為兵營升級完忘了改配置的軍隊 (比方 320/360) 照樣打得動.

## 一直跳過對手

`skipped` 每輪幾十, 多半是 `~/.ai_coc/config.json` 的 `thresholds` 對現在的獎盃區間太高. 那是設定不是 bug, 跟使用者講一聲再調.

**先確認真的是跳過**: `skipped` 包含 `_swapped`, 也就是連續 `UNREADABLE_SKIPS` 格讀不到之後迴圈自己按的 `下一個`. `run.log` 有那幾格的紀錄, 調門檻就沒用.

那個警告有兩種原因, log 分得開: `read_scout` 讀不出戰利品, 或畫面還在淡入被擋掉 (`The scout screen is still fading in (panel peak N, drawn is M)`). 峰值離門檻很遠是保護機制在做事; 一批峰值全貼著門檻 (`PANEL_DRAWN_BRIGHTNESS`), 才是門檻要重量.

## 戰利品讀錯

**症狀**: 100 萬讀成 10 萬, 或同一個對手在不同畫面讀出不同數字.

看 `parsers/scout.py` 的 `read_scout` (面板在哪), 再看 `parsers/glyphs.py` 切數字那段 (四個判讀器共用). `AGENTS.md` 搜 `1 047 758`: 中間字元配不上整行放棄, 只丟右端的差配對, 超過 18 px 的區塊要切開而且切點要可信.

## 迴圈走出還在打的戰鬥

**症狀**: 戰鬥還在打卻回報結束, 或回不了家.

`read_scout` 的 `None` 同時是「正在搜尋對手」跟「讀不出來」, 不能當戰鬥結束. 結束看 `_battle_ended` (結果畫面的綠色回營按鈕). `AGENTS.md` 搜 `overloaded`.

## 打完了但回不到村莊

**症狀**: 一輪拖四分鐘左右, `_wait_out_battle` 等到超時 (`BATTLE_TIMEOUT`), 最後靠下一輪 `_open_attack_menu` 交給 `uncovered` 的 `back` 回村莊.

結算畫面靠綠色 回營 認 (`RETURN_HOME_GREEN`, `_leave_result`), 而遊戲畫兩種: 平常的在 `RETURN_HOME_BOX` 讀 0.3283, 打亮的讀 0.2488. 門檻高過打亮版就是這個症狀, 而只有平常版 fixture 的測試照樣綠 (打亮版是 `battle_result_lit.png`). 反方向是活動獎勵頁的綠勾勾讀 0.2009 (見「開不了攻擊選單」). 兩邊夾出 0.2057 到 0.2488, 門檻 0.23 放中間. 被打之後遊戲開場的 首領，歡迎回來 報告, 它唯一的 確定 就在 回營 的位置, 綠色比例照樣過門檻, 所以 `battle_over` 另外靠 `welcome_back` 把它排除; 啟動或切世界停在這張報告上, 先看 `ai_coc read` 的這兩個欄位.

`_leave_result` 點不動時**重讀一次再按 `back`**: 最後一下沒檢查而最可能有效, 剛回村莊的畫面上 `back` 開的是 確定退出遊戲嗎.

## 兵丟不出去

**症狀**: 幾乎沒搶到東西, 或 `_wait_out_battle` 說整場戰利品沒動.

**先看 outcome**, 兩種往相反方向查:

- `nothing_deployed`: 一張卡都出不去, 迴圈的問題, 看這一節
- `no_loot`: 兵下去了, 位置不對, **戰術**的問題, 先讀那一輪的 `plans.jsonl`, 再照 SKILL.md 的調整階梯從戰術檔開始 (實測過: 32%, 卡全清空, 英雄全落地, 打進了沒有倉庫的那一側). 從哪一側打是使用者定的, 見階梯第三層
- `no_loot` 而且那一輪拖滿四分鐘, 戰鬥中每十秒一行 `No battle on screen; nothing here is a card to empty`: 戰鬥畫面不見了而結算畫面也沒出來, 迴圈沒有這個狀態, 只能等 `BATTLE_TIMEOUT` 用完. 最常見的是打到一半登入被搶走了 (使用者在手機或另一台模擬器上開遊戲); 彈出視窗蓋住戰鬥也會這樣. 先看那一輪的畫面, 是被登出就問他是不是在玩, 不要調戰術

依序看:

- `_flank` 不花兵, 只讓開卡片列, 把線彎到紅線上
- `_spread_troops` 在整個 pass 沒消耗時把線往外推 (村莊外圍**不是凸的**, 兩端在外面的線中間仍可能穿過去)
- `push_out` 推到 `DEPLOY_BOUND` 邊緣就不動, 一直推同一點是可能的
- `boundary_reach` 跟 `fitted_line` 把預設側翼彎到真實邊界, 三個錨點的理由 `AGENTS.md` 搜 `chord`

出兵成不成功看 `card_drained`, 不看畫面上有沒有兵 (紅色橫幅不能用, `AGENTS.md` 搜 `你無法在紅線區域內派遣部隊`). 偵察倒數中丟兵會直接開打 (2026-09-26 實測), 所以攻擊不等倒數; 只有 `probe` 跟 `bounds` 還用 `_wait_for_battle` 等.

## 下兵線畫穿村莊

`planned_line` 拒絕中點離螢幕中心太近的線 (`push_out` 推不動中心上的點), 拒絕就掉回它指名的預設側翼. AI 一直畫這種線, 問題在 `src/ai_coc/prompts/attack_plan.md`, 不在迴圈.

## 下方側翼站不下人

看 `_clear_flank` 跟 `FLANK_ROOM`: 卡片列站在下方側翼上, 要先把村莊拖離卡片列再出兵. `AGENTS.md` 搜 `FLANK_ROOM`.

## 整場一隻兵都沒下去

**症狀**: `After the burst: N of N one-off card(s) never landed`, 重試也是 `0 of N retry card(s) landed at`, 而且兵卡都還有貨 (`troop card(s) still hold something`), 連著幾輪.

先在 log 找 `The camera was not at the far zoom`: 所有座標都在最遠的 zoom 量, 鏡頭拉近就全部安靜地落空. **這條警告只在主村問** (高度門檻拿主村量的, 夜世界每張都比它短, 換門檻也分不開), 夜世界沒有它不代表鏡頭沒問題, 直接看下面兩段.

**`launch` 不是嫌疑人** (收尾會 pinch); 沒 pinch 的是 `restart_game` 跟任何在迴圈外動過鏡頭的東西.

遊戲不報 zoom, 所以 `_settle_zoom` 每場先拉遠一次, 順手量村莊高度: 被上下切掉就偏矮, 健康 500 到 572, 出事那輪 411. 寬度不能用, 對手本來就不一樣寬.

手動修法: 停掉迴圈, 跑 `ai_coc view --zoom out`. 偶爾一條不算事, 連著出現才是.

## 座標整個對不上

相機動過而沒被記下. 看 `_settle_camera`, `_pan`, `_panned`, `_onscreen`, `parsers/field.py` 的 `view_shift` 跟 `parsers/boundary.py` 的 `village_box`. `view_shift` 滑兩張畫面比對, 不量兩次村莊 (`village_box` 會被紅色裝飾騙), `parsers/field.py` 開頭的註解搜 `98 px`.

已知沒修的寫在 `AGENTS.md`: `_settle_camera` 拿到的是 scout 畫面, `village_box` 不是為那張校正的.

## 多讀到一張卡

卡片列尾端的虛線空槽在背景亮時會被切成一張卡, 當成多的英雄. 分辨靠等級徽章 (`parsers/scout.py` 的 `_badged`). `ai_coc read` 的 `card_groups` 跟 `selected` 說它切成幾張, 哪張選取中.

`card_groups` 只在**完整**的卡片列上有效, 所以 `_deploy` 只讀一次, 讀的是縮放 (跟置中) 之後自己截的那張, 在任何東西丟下去之前.

## 英雄沒下去

`field_units` 讀卡片上方的血條, 靠藍色分辨血條跟草地. 英雄卡不會清空 (變成技能按鈕), 不能用 `card_drained`. 假陰性有代價: 重試再點那張卡就是放技能. 看 `_drop_singles`, `_landed`, `HERO_SETTLE`.

第一次投放用 plan 那個 `siege` / `hero` 步驟自己的 `at`, 但點若正對著部隊實際那條線、在線的內側 `ONTO_LINE_SLACK` 到 `ONTO_LINE_REACH` 之間 (更遠的當成刻意放在別處, 不動), `onto_line` 先把它搬到線上的垂足 (AI 照抄 prompt 的基準線, 部隊的線卻被 `_flank` 貼到真正的紅線上, 內側的點多半被拒); 線端外面的點不動, 那是清邊英雄的位置. 搬了會有一行 `aimed inside the line`, 所以 `plans.jsonl` 的座標跟畫面對不上時先找這行. 被拒的一律走 `single_spots` 的共用階梯重試, 從線的中點開始 (不是單純再往外, 理由同 `push_out`), 不再用 plan 的點.

## 英雄的技能被提早放掉

**症狀**: 畫面出現 `你已經用過這項英雄技能了`, 或英雄卡在計畫裡它的 `ability` 步驟之前就變灰.

多半是一張已經上場的英雄卡又被點了一次, 那一下就是放技能. `After the burst` 只給數量, 判成 `never landed` 的是哪幾張要看那一輪的 `settled` 畫面; 再看之後的 `before-drop` 畫面 (中間可能夾著推兵線的畫面, 但那張卡沒被再點過) 上它是不是已經有血條: 有, 就是讀太早或拍到落地動畫, 被當成沒下去而重試 (`_settle_drops`, `HERO_SETTLE`). 技能剛發動的那幾秒血條會變亮, 藍色超過 `HERO_BAR_MAX_BLUE` 而讀不到, 那時技能已經放過, 重點只會得到上面那行字, 不算這個 bug.

## 每個英雄的技能都晚一格

**症狀**: 皇后的斗篷開在別人身上, 或某個英雄整場沒開大.

哪張一次性卡片是攻城機具由 plan 有沒有 `siege` 步驟決定 (血條分不出來, 遊戲對攻城戰車也畫血條): `_deploy` 建 `BattleRow` 時有 `siege` 就把第一張當機具, 沒有就整排當英雄. 多寫或少寫一個 `siege`, 整排英雄的落點跟開大秒數就偏一格, **沒有下游會發現**. 對照那輪 `plans.jsonl` 有沒有 `siege` 跟 `--record` 的 `zoomed` 畫面 (置中拖過鏡頭的話是之後那張 `camera`, `_deploy` 讀卡片列的就是它) 上有沒有攻城機具, 對不上就從 `prompts/attack_plan.md` 查.

`siege` 對得上還是晚一格, 看戰鬥中途卡片列前面有沒有多出一張 (活動卡): `BattleRow` 記的是開場讀到的 x, 前面插一張, 後面每張都往右移, `ability` 跟 `_cast` 就點到隔壁那張. 這是 `AGENTS.md` 記著的已知缺陷.

## 法術沒放出去

看 `_cast`, `freeze_cards`, `SPELL_SELECT_DELAY`. 慢的是法術的**選取**, 不是放置; 卡片還有貨時維持選取, 後面幾發是快速連發.

冰凍可以放在紅線內, 所以法術失敗是點被吞掉, 不是位置被拒絕.

## 怒吼丟在空地上

**先分「畫錯」還是「被搬走」.** 在那一輪 `plans.jsonl` 找 `act` 是 `rage` 的步驟, `at` 乘上 (16, 9) 換成像素, 跟畫面上的圈比:

- **對得上, 位置不好**: prompt 的問題. 查 `src/ai_coc/prompts/attack_plan.md` 有沒有要它挑**這一側防禦最密的一團**, 排成**方塊** (四瓶排一橫排比村莊還寬).
- **對不上, 或瓶數比 plan 少**: `spaced` 動過它. 太密的點推到橢圓邊緣, 推不動 (被 `clear_of_controls` 壓回, 或要推超過一個覆蓋範圍) 才丟掉, **不補位**. 從 plan 的座標對回去看是哪一點.

狂暴不追部隊位置 (`_onto_army` 已拿掉, 量完就過了該放的時刻), 靠「兵往防禦建築走」預測; 冰凍瞄防禦建築, 本來就不動.

## 法術下得太晚

**不要用 log 那行判斷**: `Played rage, 18s in` 在 `_cast` 跑完才寫, 包含驗證截圖, 比落地晚約一秒半.

**用遊戲自己的倒數量**: 開 `--shot-every`, `tick_*.png` 寫著 `離戰鬥結束剩下 X分Y秒`, 180 減掉就是戰鬥時鐘. 秒數是相對的: 從 `Played troops` 數到 `Played rage`, 跟計畫裡 `wait` 的 `seconds` 比, 對得上就是執行沒問題.

會讓它變晚的兩處:

- **不要加回絕對時鐘.** `_play_tactic` 的 `opened` 只拿來印那行 log. 從開戰算起的秒數會把讀邊界跟拉相機的時間算到每一瓶頭上.
- **`_cast` 的前置**: 點法術卡後等 `SPELL_SELECT_DELAY` (0.6 秒) 讓範圍圈亮起來. 步驟之間不拍畫面, 只有夠長的 `wait` 裡拍一張看戰鬥還在不在.

步驟照順序跑, `wait` 從上一個動作做完開始數, 所以「AI 的順序沒被尊重」不會是這裡的問題.

## 出兵太慢

整支軍隊該在**兩秒左右**下完, 過程中什麼都不讀 (一次判讀 0.9 秒), 順序全由 plan 決定. 看 `_play_tactic`, `_act`; 落地後那次判讀是 `_settle_drops`, 躲在第一個夠長的 `wait` 裡 (`CHECK_BUDGET`), 截圖之前先讓最後一個投下的英雄等滿 `HERO_SETTLE` 把血條畫出來 (太早讀會把落地的英雄判成沒下去, 重點卡片就是放技能). `DROP_SETTLE` 是放完法術, 推線重下, 夜世界第二階段送預選的機器 (`_send_selected`; 第一階段的機器跟英雄一樣等 `HERO_SETTLE`)跟夜世界每一趟出兵之後等卡片角落重畫的時間, 不在第一波出兵裡 (英雄的重試等的是 `HERO_SETTLE`); `TAP_GAP` 住在 `adapters/adb.py`. 改前改後的秒數在 `AGENTS.md` 搜 `7 seconds`.

## 一張卡的兵全堆在一起, 或者某張卡點個不停

點幾下是**卡片自己的 `xN`** 說的 (`card_count`): 讀到就點那麼多加一, 讀不到退回 `DROPS_PER_PASS`.

- **點個不停**: 這個判讀器**往上失敗** (同一張 x12 讀成 12, None, 121), 所以 `DROPS_PER_PASS` 也是上限; 拿掉上限就是這個症狀.
- **堆在一起**: 一個 pass 走**兩趟線**, 第二趟落在第一趟的點之間, 用 `DROP_STRIDE` 跨著走, 讓沒點完就空掉的卡已下去的兵是散開的 (寶寶龍身邊有同伴就不狂暴). 看 `drop_points`.

`ai_coc read <png>` 的 `counts` 是查這節最快的一步.

## 刷牆停了

**不在打資源這條路上**: 壞了照 SKILL.md 的「管到哪裡為止」記下來, 主線達成以後另開 PR. 但有個回報容易讀反: `WallReport.outcome` 是 **`nothing_bought`** 時**不能直接讀成牆全滿級**, 那是迴圈自己也說不上原因, 最可能是大本營把牆卡住, 但不確定; 買牆時說得出原因的是 `cannot_afford` 跟 `builders_busy`. 不要比對 log 的句子, 那些字會改.

## 夜世界特有的幾種

夜世界共用戰鬥那一半, 上面都適用, 但幾個判讀器在那邊是**反過來**的:

**`card_count` 永遠回 `None`, 刻意的**: 主村寫 `x4`, 那邊寫 `4x` 而且大一號, 4 會比對成 9 而落在 `COUNT_DIGIT_TOLERANCE` 裡. 讀不到只多點幾下, 讀錯會點兩倍以上.

**`live_cards` 不能當「卡片空了」**: 那邊是兵死掉才變灰. `_spread_night` 看這輪有沒有掉東西, **而且要看是不是已經掉過**: 掉過之後不掉是卡空了, 一次都沒掉是線壓在基地上, 往外推.

**卡片列順序反過來**: 機器在最左邊. 用 `xN` 角標分 (`counted_cards`), 不用 group 位置.

**出兵是帶狀** (`night_drops`, `NIGHT_LANES`): 三條線, 每條往外多一個 `PUSH_STEP`, 每一下換一條 lane, 濺射才打不到好幾隻. lane 只往外 (往內是基地, 被拒絕的點不消耗兵). 端點會被 `DEPLOY_BOUND` 夾住, 上面兩條側翼的第一個錨點在 lane 1 跟 2 重疊. 兵還擠成一坨, 先看是不是 lane 被夾掉, 再考慮動 `NIGHT_LANES`.

**機器技能每秒盲按一次** (`ABILITY_TAP`), 每 `ABILITY_POLL` 拍一張看階段結束沒. 驗證就開 `--record` 看機器卡片**上方**的技能條: 洋紅是充飽, 青色由短變長是充能中 (約 14 秒), 都沒有是機器死了或還在卡片裡. 連續好幾張洋紅而 log 有在點, 才是沒按到.

**第二階段開場倖存的機器卡片是選取中的** (白邊, 較寬), 所以點地面不點卡片, 白邊還在就換下一個落點 (`_send_selected`, 最多五個). log 是 `The machine came through from the stage before, preselected` 接 `still in its card after`; 五個都被吞是 `never left its card` 的 WARNING. 又看到 `took nothing` 五次接 `never landed` (點到技能鍵), 或 `0 machine card(s) still alive` 而機器明明活著 (`card_groups` 讀不到選取中的卡, `CARD_SELECTED_SPAN`), 就是這條被改壞了.

只能看 log 的兩件:

- **第二階段**: 第一場打到 100% 才可能有, 不是每次. `_wait_out_night` 看卡片列的數量角被重畫 (`0x` 變回倖存兵數) 就算這階段結束, `_next_stage` 分辨結算還是下一階段. log 依序是 `The card row was repainted; this stage is over` 跟 `共出兵 2 次`.
- **聖水沒入庫不是 bug**: 夜世界聖水進推車, 看 `The loot cart paid N elixir`. 沒那行就看 `CartReport.outcome`: **`locked_holding`** 是車上有東西領不出來 (`held` 是面板數字), 幾乎一定是聖水倉庫滿了; **`locked_empty`** 是車空; **`locked`** 是按鈕灰而數字讀不到. 數字**要先確認面板開著才能讀** (卡片 `xN` 角落壓在同一排). `not_found` 是三個候選點都沒點開, `wrong_world` 是不在夜世界.
