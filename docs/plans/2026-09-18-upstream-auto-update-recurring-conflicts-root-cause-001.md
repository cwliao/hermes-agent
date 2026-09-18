# 為什麼 upstream 自動更新每次都撞牆——根因查證 + 改善方向

Status: `OPEN — 已查明根因（真實數據，非猜測），改善方向待共識決定，
未動任何程式碼`
Priority: P2（不是服務故障，但直接影響一個本來設計成「自動、每天默默
跟上 upstream」的機制，現在變成每次都需要人工深度介入——如果不處理，
這個落差只會持續擴大，之後每次要解的衝突只會越來越大）
Date: 2026-09-18
Repository: `~/.hermes/hermes-agent`
Related: `2026-09-15-upstream-sync-arch001-rebase-conflict-001.md`
（第一次 759-commit 大型衝突同步）、
`2026-09-18-upstream-rebase-1356-commits-002.md`（本票查到的根因，
直接導致的第二次、規模更大的衝突，該票負責「這次具體怎麼解」，本票
負責「為什麼會這樣、以後怎麼避免」）

## 使用者原話

> ticket to solve why every time the update was hitting walls and make
> the auto update better

## 查證結果（真實資料，直接讀 git 歷史算出來，不是猜測）

### 根因一：upstream 的 commit 速度在 2026-09-13 前後暴增一個數量級

按日期統計 `upstream/main` 的 commit 數：

```
2026-09-10   10
2026-09-11   31
2026-09-12   31
2026-09-13  100   ← 開始暴增
2026-09-14  546   ← 暴增 17 倍
2026-09-15  482
2026-09-16  635
2026-09-17  178
```

09-13 之前，upstream 大概是「個位數到三十幾 commit/天」的正常步調；
09-13 起直接跳到三位數、甚至單日 635 個 commit。**這個「每天自動同步
一次、預期只有小量差異」的 guard 設計，前提假設是 upstream 步調平緩
——這個前提在 09-13 之後已經不成立**，任何一天沒有人工介入，第二天
要處理的量都是等比級數在長大，不是線性。

**目前尚不確定**這個暴增背後的原因（可能是 NousResearch 那邊開始用
agent swarm 大量自動化提交——`git fetch --all` 時看到不少
`prime-agent-port/...`、`omo-port/...`、`pplx-computer-inspired/...`
這類看起來像自動化 port/review 產生的分支名稱，但這只是旁證，不是
確認）；不管背後原因是什麼，**觀測到的速度變化本身是真的**，這才是
本票要處理的事實基礎。

### 根因二：這個 fork 本身有真實、不小的自訂工程量，不是薄薄一層 patch

上次同步點（`af4a3eba0a`，2026-09-15）之後，我方累積了 **365 個
本地 commit**，全部確認是 `cwliao`／`Claude` 這個 fork 自己寫的
（web_gate、ARCH-001 runtime-state lease、kanban swarm 一系列強化、
DNS-exfil 偵測等），不是不小心夾帶的舊 upstream commit。這代表：
**維護成本不是「upstream 動得快」單一因素造成的，是「upstream 動得快」
乘上「我方也持續在同一個 codebase 上做真實工程」的交叉效應**——兩邊
都在動，才會一直撞。

### 根因三：有明確的「熱點檔案」——雙方都在改同一段邏輯

兩次衝突（今天稍早的 759-commit 同步、本次 1356-commit 同步）**撞到
的都是同一個檔案：`tools/approval_gateway_wait.py`**。查證這次
1356-commit 窗口內，**upstream 自己改了這個檔案 5 次**，我方改了
1 次（`ARCH-001` 那次）。這不是巧合——approval-gateway 這個子系統
顯然是雙方都在積極開發的區塊，我方的 runtime-state lease 邏輯跟
upstream 自己的重構/新機制（例如稍早發現的 `settle` 機制）持續在
同一段程式碼上競爭，只要任一邊再改一次，下次同步大機率又撞在
這裡。

## 改善方向（未實作，列出選項供共識審查，不預設答案）

1. **提高同步頻率（例如從每天一次改成每小時或每 6 小時）**：能讓
   每次要處理的 delta 變小，但**光靠這個不夠**——upstream 現在單日
   可以到 600+ commit，就算縮到每小時一次，尖峰時段單次同步可能還是
   要面對數十個新 commit，衝突機率沒有等比例下降，只是把「一次很大
   的衝突」拆成「很多次中等的衝突」，人力負擔不一定變輕。
2. **把我方的自訂邏輯做成更薄的相容層，減少跟熱點檔案的直接碰撞**：
   例如 ARCH-001 的 runtime-state lease 邏輯，與其直接改
   `approval_gateway_wait.py` 內部函式本體，改成用一層 wrapper／hook
   包住原函式，讓 upstream 對同一個檔案的改動大機率不會碰到我方包
   在外層的邏輯。這是治本方向，但需要重新設計 ARCH-001 這段程式碼，
   工程量不小，需要另外評估。
3. **改變同步模型：從「完整重放歷史的 rebase」改成「vendor 式定期
   釘版本 + 明確 patch queue」**：不再嘗試逐 commit 重放我方 365 筆
   歷史到每次最新的 upstream 之上，而是把 upstream 當成一個外部
   相依套件，定期（例如每週、或人工審查後）把 upstream pin 到某個
   審過的 commit，我方的修改集中維護成一份小而明確的 patch 集
   （例如用 `git format-patch`/`git am`，或專門的 patch-queue 工具），
   套用在那個 pin 上。這樣衝突處理的頻率跟 upstream 實際步調脫鉤，
   代價是失去跟 upstream 逐 commit 對齊的能力、patch 集本身需要
   持續維護。
4. **重新評估要不要繼續逐 commit 追 `main`**：如果 upstream 的暴增是
   永久性的（agent swarm 常態化），持續每天全量追蹤可能本來就不是
   划算的策略，可以考慮改成只追 tagged release，或拉長到每月評估
   一次要不要同步，犧牲即時性換取穩定的人力成本。

**這四個方向互不排斥，也可能是組合方案**——本票不預設要選哪個，
交給三審共識討論。

## 熱點檔案判斷的實測驗證（2026-09-18，來自
`2026-09-18-upstream-rebase-1356-commits-002.md` 的真實執行）

使用者裁示（見該票「Go」後續發展）暫停逐一硬解衝突，優先回來處理本票
的策略問題——先把當時蒐集到的真實證據記錄下來：

- 實際執行那次 rebase，306/367 個 commit 重放成功，**7 個真實衝突全部
  集中在 9 個檔案**（`tools/approval_gateway_wait.py`、
  `plugins/platforms/telegram/adapter.py`、`gateway/run_turn.py`、
  `gateway/run_turn_runner.py`、`gateway/kanban_watchers.py`、
  `hermes_cli/kanban.py`、`hermes_cli/kanban_db.py`、
  `hermes_cli/kanban_db_dispatch.py`，外加牽連到的測試檔）
- 這 9 個檔案總共承接了我方 **365 個本地 commit 裡的 93 個（約
  25%）**——不是零星巧合，是這個 fork 真正投入工程心力的核心區塊
  （approval-gateway 生命週期、kanban 多代理派工/監工）恰好也是
  upstream 自己密集開發的區塊
- 這**印證**了原本「熱點檔案」的判斷，而且範圍比原本以為的（單一
  `approval_gateway_wait.py`）更廣——是一整個 kanban／approval 子系統
  等級的碰撞，不是單一檔案的偶然

## 共識結果（2026-09-18，`hermes-upstream-sync-strategy-2026q3-v1`，
quorum=2）

```
status: consensus
approved_by: [claude, grok, agy]
rejected_by: []
failed_or_timeout: [native_hermes]
approved_count: 3 / quorum 2
```

**誠實說明**：`reviewer_dispatch.py` 的 verdict 只有 APPROVE/REJECT
二元選項，沒辦法直接「投票選哪個方向」——這次共識通過的是「用這份
證據（衝突集中在 9 個熱點檔案、佔本地 commit 25%）做決策依據，往下
選方向」這個**做法**本身站得住腳，不是某個具體方向本身經過三方
獨立驗證選出來的。**下面的方向選擇是我自己的工程判斷，不是共識
直接產出**，這點要對使用者說清楚，不能包裝成「共識選了方向 2」。

## 判斷（非共識直接產出，供使用者確認或推翻）

四個方向裡，**方向 2（把自訂邏輯做成薄相容層）最直接對應到這次的
實測證據**——衝突具體發生在「雙方都改同一段函式內部邏輯」，薄相容層
（wrapper／hook 包住原函式，不直接改內部）能直接降低這種碰撞。方向 1
（提高頻率）已經被根因分析否證效益有限；方向 3（vendor/patch-queue）
更治本但工程量大得多，屬於架構級改動；方向 4（要不要繼續追 main）是
產品/治理層級的決定，不是我該自己選的。

**建議排序（不是決定，等使用者確認）**：
1. 近期：針對這次證實的 9 個熱點檔案，評估方向 2（薄相容層）的具體
   做法，另開實作票，走三審共識
2. 中期：方向 4（要不要繼續逐 commit 追 upstream main，還是改追
   tagged release／拉長週期）需要使用者親自決定，不是工程判斷能
   代替的
3. 方向 3（vendor/patch-queue 模式）列為長期選項，先不投入，除非
   方向 2 實測後發現熱點碰撞問題沒有真正緩解

## 待做

- [x] 用 `2026-09-18-upstream-rebase-1356-commits-002.md` 的實際執行
      結果驗證熱點檔案判斷——已驗證，範圍比原判斷更廣（整個 kanban／
      approval 子系統，不只單一檔案）
- [x] 針對方向選擇走真實 instamem 共識——完成，但共識驗證的是「用
      這份證據做決策」這個做法，不是直接票選出方向 2，上面已誠實
      標註
- [ ] 使用者確認上面「建議排序」是否採用，或指定別的方向
- [ ] 方向確定後另開實作票，走三審共識 + 部署流程，不在本票範圍內
      直接動手
- [ ] `2026-09-18-upstream-rebase-1356-commits-002.md` 那個暫停中的
      rebase（306/367，worktree 仍在）要不要繼續、還是等策略票定案
      後重新評估，留給使用者決定

## 驗收標準

- [ ] 使用者確認要採用的改善方向（近期／中期／長期如上「建議排序」，
      或使用者自訂組合）
- [ ] 若涉及程式碼變更（方向 2、3），走既有的三審共識 + 部署流程
- [ ] 若只涉及排程/流程變更（方向 1、4），至少過一次共識確認排程
      改動不會造成非預期副作用（例如過於頻繁觸發 preflight 造成
      資源浪費）

## Blast radius

本票全程唯讀——只用 `git log`/`git merge-base --is-ancestor`/
`git fetch` 統計數據，沒有修改任何檔案、沒有觸碰任何排程設定
（`~/.hermes/cron/jobs.json` 目前的 `30 4 * * *` 排程維持不動）、
沒有安裝或移除任何機制。所有改善方向都只是列出選項，未經共識前不會
實作任何一項。
