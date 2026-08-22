# Doctor 測試套件的排列順序脆弱性（發現於 upstream rebase 驗證，與 rebase 本身無關）

## 現象

合併執行以下測試檔時，`tests/hermes_cli/test_doctor.py` 與
`tests/hermes_cli/test_doctor_gateway_release_drift.py` 裡部分測試會不穩定地失敗：

```
tests/hermes_cli/test_kanban_db.py
tests/hermes_cli/test_kanban_swarm.py
tests/hermes_cli/test_kanban_cli.py
tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py
tests/hermes_cli/test_kanban_boards.py
tests/hermes_cli/test_kanban_core_functionality.py
tests/hermes_cli/test_doctor.py
tests/hermes_cli/test_doctor_gateway_release_drift.py
tests/hermes_cli/test_config.py
```

單獨執行 `test_doctor.py` / `test_doctor_gateway_release_drift.py` 時 100% 通過。

## 根因（已查證）

1. `tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py` 的 `isolated_kanban_home`
   fixture（`hermes_cli/kanban_cli_dispatch_passthrough.py:19-27`，早於本次 upstream
   rebase 就存在，源自 commit `69b74c15a3`）會執行：

   ```python
   for mod in list(sys.modules.keys()):
       if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
           del sys.modules[mod]
   ```

   這會把 `hermes_cli.gateway` 從 `sys.modules` 整個移除，逼下一次 `import
   hermes_cli.gateway` 建立一個全新的模組物件。

2. `hermes_cli/doctor.py::_check_gateway_release_drift`（388 行起）與
   `_check_gateway_service_linger`（441 行起，會呼叫前者）都用函式內
   lazy import：`from hermes_cli.gateway import _probe_systemd_service_running, ...`。

3. `tests/hermes_cli/test_doctor.py` 在檔案頂端 `import hermes_cli.gateway as
   gateway_cli`（模組匯入時就綁定，只執行一次）。測試裡
   `monkeypatch.setattr(gateway_cli, "_probe_systemd_service_running", ...)`
   patch 的是**當下**那個模組物件。

4. 如果在同一個 pytest process 裡，`test_kanban_cli_dispatch_passthrough.py`
   的 fixture 先跑過、刪除了 `sys.modules['hermes_cli.gateway']`，之後
   `doctor.py` 內的 lazy import 會拿到一個**全新**的模組物件——而
   `test_doctor.py` 頂端綁定的 `gateway_cli` 仍是**舊**的那個。此時
   `monkeypatch.setattr(gateway_cli, ...)` patch 的是舊模組，doctor.py
   實際呼叫的卻是新模組裡未被 patch 的**真正** `_probe_systemd_service_running`
   / `_read_systemd_unit_environment`，讀到這台主機上真實的 systemd 狀態
   （例如真的讀到 `HERMES_RELEASE_SHA=56400b36a7`），導致斷言失敗。

## 查證方式

- 用 `git -C /home/cwliao/.hermes/hermes-agent` 檢查 `isolated_kanban_home`
  fixture 的引入 commit（`69b74c15a3`），確認早於本次 upstream rebase。
- 在 **目前 production `main`**（`/home/cwliao/.hermes/hermes-agent`）上用完全相同的檔案組合重跑，
  一樣重現失敗（`5 failed, 1048 passed`）——證實與 upstream rebase 無關。
- 重複執行同一組合，失敗的測試集合不完全相同（例如
  `test_config.py::test_migrate_reports_normalized_line_formatting`
  第二次重跑就通過了）——證實這是非決定性的順序/時序脆弱性，不是穩定
  可重現的迴歸。

## 影響範圍

只在「同一個 pytest process 內，`test_kanban_cli_dispatch_passthrough.py`
先於 `test_doctor.py`/`test_doctor_gateway_release_drift.py` 執行」時才會
觸發。個別執行、或用 `pytest-xdist` 分行程執行都不受影響。不影響正式環境
（正式環境不會有測試專用的 `sys.modules` 刪除邏輯）。

## 建議修法（尚未實作，僅記錄，不阻擋目前的 upstream rebase 合併決策）

擇一：
1. `isolated_kanban_home` 改用更精準的隔離手段（例如只 patch 需要重讀
   config 的少數模組屬性，而非整批刪除 `sys.modules`），或改用
   `importlib.reload()` 針對特定模組。
2. `test_doctor.py` / `test_doctor_gateway_release_drift.py` 改成在每個測試
   內用 `import hermes_cli.gateway as gateway_cli`（區域匯入，測試執行當下
   才綁定），而非依賴檔案頂端一次性匯入的模組參照。
3. 幫這類會刪除 `sys.modules` 的 fixture 加上明確的 pytest 標記或文件警告，
   提醒其他測試作者這個副作用範圍。

## 狀態

- 尚未實作任何修復。
- 不阻擋本次 upstream rebase（`rebase/upstream-2026-08-22`）的合併決策——
  此問題與 rebase 完全獨立存在，rebase 前後行為一致。
