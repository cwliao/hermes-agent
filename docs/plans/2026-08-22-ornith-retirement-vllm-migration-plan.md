# ornith:35b 退役 — Hermes 遷移方案（規劃階段，尚未實作）

## 背景與動機

- Hermes 目前的 chat 模型預設為 Ollama 的 `ornith:35b`（`/home/cwliao/.hermes/config.yaml:2`：`model.default: ornith:35b`，`model.provider: openai-api`）。
- `ornith:35b` 是 Qwen3.5-MoE 系的中國相關衍生模型，整個 vLLM 遷移專案的核心目的就是要讓 Hermes 跟 docagent 都退役這顆模型。
- docagent 這邊已完成遷移驗證：`nemotron-cascade2-nvfp4` 透過 vLLM serving（關閉 thinking mode）品質評測 17/22（77%）勝過 `ornith:35b` baseline 15/22（68%），22 併發請求全數在 118 秒內完成；開啟 thinking mode 反而因 GPU 算力被稀釋，15/22 逾時（連 1800 秒都不夠）。
- Phase 3 舊決策原訂 Hermes 切到本機 Ollama 的 `gpt-oss:120b`（65GB）。但使用者提出新原則：**本機 Ollama 正在轉型成備援伺服器角色，平常不應該常駐大型/中型模型**，因此 `gpt-oss:120b` 這個舊決策需要重新評估。
- `ornith:35b` 目前仍保留在 Ollama 中，是特意留給 Hermes 用的，等 Hermes 確認遷移完成後才會清掉。

## 關鍵事實查證（已用程式碼/系統指令直接驗證，非引用他人說法）

1. **Hermes 與 docagent 是同一台實體主機**：`hostname` → `55-0940189-03`，`hostname -I` 含 `140.96.58.171`，與 Hermes `config.yaml` 裡 `base_url: http://127.0.0.1:11434/v1`（127.0.0.1 本機位址）一致。兩者共用同一張 GPU（NVIDIA GB10, 121GB unified memory）、同一個 Ollama container。這代表 Hermes 接 vLLM 沒有跨主機網路延遲或連線設計問題，可以直接打 docagent 那邊已驗證的同一個 vLLM instance。
2. **Hermes 沒有 Ollama 專屬的傳輸層代碼**：`ollama` provider 本質上就是通用 OpenAI-compatible `"custom"` provider 的別名（`hermes_cli/providers.py:368`，`hermes_cli/models.py:1299`：`"ollama": "custom"`）。實際請求走 `run_agent.py` 裡標準 OpenAI SDK client，指向 config 提供的 `base_url`。唯一 Ollama 專屬邏輯是 `run_agent.py:1539-1558` 的 `_is_ollama_glm_backend()`，用來繞開 Ollama 對截斷輸出誤報 `finish_reason='stop'` 的問題——其 docstring 明確排除 vLLM/LiteLLM/sglang 等代理，代表這段既有邏輯不會誤判 vLLM 端點。
3. **`extra_body` 欄位確實存在且可用**：`hermes_cli/config.py:1602` 為每個 custom provider 預設 `"extra_body": {}`，經 `hermes_cli/config.py:4966-4968` 正規化、`hermes_cli/runtime_provider.py:911-914` 組進 request kwargs，最終在 `agent/transports/chat_completions.py:594-649` merge 進送往 OpenAI SDK 的 `api_kwargs["extra_body"]`。純 dict merge，無 schema 限制欄位——可放任意巢狀結構，包含 vLLM 需要的 `chat_template_kwargs: {enable_thinking: false}`。
4. **`fallback_providers` 機制已存在，且會涵蓋 vLLM 逾時/斷線**，但**不是立即切換**：實際邏輯在 `agent/conversation_loop.py:3200-3234`，透過 `agent/error_classifier.py::classify_api_error()` 分類。`FailoverReason.timeout`/`overloaded` 類別（涵蓋 `ConnectTimeout`/`ConnectionError`/`TimeoutError`/OpenAI SDK 的 `APIConnectionError`/`APITimeoutError`）需要 **`retry_count >= 2`**（先重試 2 次原 provider）才會切換到 fallback；`rate_limit`/`billing` 類別則立即切換。**這代表 vLLM 卡住時，Hermes 會先重試 2 次同一個 vLLM 端點，才會切到 fallback，不是瞬間失效轉移。**
5. **`hermes_cli/kanban_db.py::create_task(..., model_override=..., provider_override=...)`** 與 `delegation.model`/`delegation.provider`/`delegation.base_url`/`delegation.api_key`（`config.yaml:365-368`）已提供 per-task/per-delegation 的模型路由能力，不需要新程式碼即可讓「只有 kanban worker」或「只有某個 delegation」走 vLLM，而其餘任務維持原樣。

## docagent 那邊現況（已用 `docker inspect`/`docker logs`/系統本身直接查證，非引用「Ticket 56」）

**更正（二次更正）：先前版本的這一節說「Ticket 56 不存在、是捏造」，這個判斷是錯的，已跟負責 vLLM 那邊的 agent 直接確認並查證。** Ticket 56 是真的，只是放在不同的 repo：`/home/cwliao/project/vLLM/ticket-56-production-cutover-design.md`（不在 `dgx-workspace` 的 docagent repo 底下，我之前只查了 docagent repo，找錯地方了）。另外也有 `/home/cwliao/project/vLLM/ticket-58-concurrent-fallback-admission-busy.md`（同一天稍後的相關修復，跟 Ollama admission-busy fallback 有關）。以下技術細節保留原本用 `docker inspect`/`docker logs` 直接查證的內容（仍然是最可靠的來源），並補上 Ticket 56 文件裡的真實背景：

- `vllm-production` container **確實存在且真的在跑**（非規劃階段）：`docker inspect` 確認 `RestartPolicy: unless-stopped`，實際serving 指令為 `vllm serve chankhavu/Nemotron-Cascade-2-30B-A3B-NVFP4 --served-model-name drafter-active --gpu-memory-utilization 0.45 --max-model-len 32768 --max-num-seqs 128 --port 8000`——確認 `drafter-active` 這個 model id 是真實、與 vLLM 端實際註冊的一致。`docker logs` 顯示過去一小時內持續有真實的 `POST /v1/chat/completions`、`GET /health` 請求，全部 200 OK。
- `--gpu-memory-utilization 0.45` 對應約 121GB 池的 45%（~54.5GB），與先前「~55GB」的說法數量級一致，但這是 vLLM 自身的利用率設定，不是 docker 層級的硬性記憶體上限（`docker inspect` 顯示 `Memory limit: 0`，未設定 cgroup 硬上限）。
- 有一個既有的 `vllm-production-healthcheck.service`（systemd user timer，約每 10 分鐘跑一次）在監控這個 container：檢查 `docker inspect` 的 `.State.Status` 是否為 `running`、`RestartCount` 是否過高、`GET /health`、以及 GPU 記憶體使用率。這個 healthcheck 把「非 running 狀態（包含 paused）」視為 **CRITICAL**，不是設計上會容忍的正常狀態。
- **暫停現象已解釋、已確認解除**：本次查證時發現 `vllm-production` 短暫從 `running` 變成 `Paused`，一度以為是異常。經與負責 vLLM 那邊的 agent 直接確認：**是他當天刻意執行兩次 `docker pause vllm-production`，用來測試 docagent 的 DrafterAgent 能否正確偵測「vLLM 存活但無回應、GPU 記憶體仍被佔用」這種故障型態並 failover 到 Ollama 的 `gpt-oss:20b`**（`docker pause` 用 cgroup freezer 凍結行程但不釋放 CUDA 記憶體，比 `docker stop` 更能模擬這種故障，`docker stop` 會釋放記憶體造成偽陽性測試）。兩次測試分別對應 Ticket 56 的 cutover 驗證與 Ticket 58 的 fix 驗證。**目前重新查證：`docker inspect` 顯示 `Status=running Paused=false RestartCount=0`，`curl http://127.0.0.1:18000/health` 回傳 `200`，自 ~12:20 起已穩定運作。**
- `vllm-production-healthcheck.service` 確實把這兩次測試暫停記錄為 CRITICAL（`11:35:22`、`12:18:22`）——監控機制運作正常，記錄到的是「測試造成的暫停」，不是真正的事故。
- `vllm-embed`（port 18001，embedding 模型，不同 container）全程運作正常，不受影響。
- **額外背景（來自 Ticket 56 文件，與 Hermes 相關）**：Ticket 56 同時規劃把 Ollama 的 `OLLAMA_MAX_LOADED_MODELS` 從 4 降到 2（在 `~/open-webui-stack/compose.yaml`，docker-compose 管理），理由是 vLLM 常駐佔用 ~55GB 後，Ollama 這邊需要收緊多模型併存的上限。文件裡**明確排除**「順便清掉 `ornith:35b`」這個動作，並寫明 Hermes 的遷移是這張票的 out-of-scope 項目、Hermes 仍依賴 `ornith:35b` 留在 Ollama 裡。這個改動如果被套用，會讓 Ollama 同時能載入的模型數變少——如果 Hermes 之後真的切到 `gpt-oss:20b` 當 fallback，需要留意跟 DocuBot/DocHelper/klib 等其他消費者共用 Ollama 時的模型驅逐頻率會提高（Ticket 56 文件裡也有提到這個 tradeoff）。

## 建議方案：Hermes 直接接 vLLM，不切 `gpt-oss:120b`

| 選項 | 記憶體佔用 | 是否符合「Ollama 只留小模型」新原則 | 品質 |
|---|---|---|---|
| `gpt-oss:120b`（Phase 3 舊決策） | 65GB 常駐於 Ollama | ❌ 直接違反新原則 | 未針對 ornith:35b 重新評測 |
| vLLM `nemotron-cascade2-nvfp4`（本方案） | 重用 docagent 已常駐模型，Hermes 端零額外記憶體成本 | ✅ 完全不佔用 Ollama | ✅ 已驗證勝過 baseline |

**具體改動範圍：預期僅改 config，不動程式碼**（Hermes 既有的 custom-provider 機制已完整支援這個需求）：

```yaml
providers:
  vllm-local:
    base_url: http://127.0.0.1:18000/v1
    api_key: vllm                     # vLLM 預設不驗證，任意非空字串即可
    request_timeout_seconds: 600      # 對齊 docagent 的 VLLM_TIMEOUT（見下方「交叉審查修正」，不是 `timeout`）
    extra_body:
      chat_template_kwargs:
        enable_thinking: false        # 對齊 docagent 已驗證的 no-thinking 設定

model:
  default: drafter-active             # vLLM 端註冊的模型 id，已用 docker inspect 查證與 vLLM 端一致
  provider: vllm-local

fallback_providers:
  - provider: openai-api              # 沿用現有 Ollama custom provider（127.0.0.1:11434）
    model: gpt-oss:20b                # 對齊 docagent 新版 fallback，非 ornith:35b、非 120b
  - provider: groq                    # 使用者決定：保留原有 groq entry，排在 gpt-oss:20b 之後
    model: llama-3.3-70b-versatile
```

**使用者決策（已定案）**：
1. `fallback_providers` 保留原有 `groq` entry，排在新的 `gpt-oss:20b` 之後（fallback chain：vLLM 失敗 → 本機 `gpt-oss:20b` → 外部 `groq`）。
2. 不分階段小流量觀察，直接一次到位把 `model.default` 換成 vLLM。

## 交叉審查修正（兩個獨立 agent 各自發現不同的真實缺陷，已逐一查證修正）

1. **`timeout` key 名稱是錯的，已修正為 `request_timeout_seconds`**：查證 `hermes_cli/config.py` 的 `_KNOWN_KEYS`（1393 行）只認得 `request_timeout_seconds`/`stale_timeout_seconds`，不認得 `timeout`。任何不在白名單裡的 key 會被 `_normalize_custom_provider_entry` 靜默丟棄並記一條警告 log——原本寫的 `timeout: 600` 送出去等於沒設定，實際會用預設逾時值，不會真的對齊 docagent 的 600 秒。已在上方 YAML 改正。
2. **原本的 `fallback_providers` 改動會靜默蓋掉現有設定，未被察覺**：直接查證 `/home/cwliao/.hermes/config.yaml` 目前的 `fallback_providers` 是 `[{provider: groq, model: llama-3.3-70b-versatile}]`，不是空的。先前版本的 YAML 範例整段取代掉這個既有 fallback，卻完全沒提到這件事。**這是一個需要你明確決定的點，不是我能自己拍板的**：實作時要嘛在 `gpt-oss:20b` entry 後面保留原本的 `groq` entry（fallback chain 依序嘗試），要嘛明確確認要拿掉 groq——不能像原本那樣悄悄蓋掉。
3. **「機制已驗證」的說法過度簡化，需要更精確地描述**：查證 `logs/agent.log` 實際的即時流量：目前 `ornith:35b`（`provider: openai-api`）走的是 `api_mode=codex_responses`（Responses API 協定），不是一般的 chat_completions。`hermes_cli/providers.py::determine_api_mode` 會依 provider/base_url/model 判斷要用哪種 api_mode；一個新的 `vllm-local` custom provider 大機率會解析成 `chat_completions`（一般 OpenAI chat-completions 協定），這跟目前 Hermes 預設模型實際在跑的協定不同。**這代表這次改動不是單純換一個 `base_url` 那麼簡單，是換了 Hermes 預設模型路徑走的協定**——雖然 `extra_body`/`fallback_providers` 機制本身是通用、已驗證的，但「用 chat_completions 協定當 Hermes 的預設模型」這件事本身沒有正式生產驗證過，實作後應該先小流量觀察這條路徑，不能假設跟現在的 `codex_responses` 路徑行為完全等價。
4. **Ticket 56 的真實現況（讀完整份文件後更正，先前的引用只看了文件開頭的過時 status 標籤）**：Ticket 56 的 Part 1-3（cutover 本身）**已實作、已上線、已通過第二輪獨立交叉審查**，不是「設計階段、待實作」。交叉審查當時列出 5 項殘留風險，同一天的 follow-up 已經處理掉其中 4 項（監控告警已裝上——且剛才 vLLM 那邊的 agent 又追加了 Telegram 告警；記憶體用量已用真實併發負載重新量測；rollback 流程已實際演練過）。**但第 3 項殘留風險是真實存在、尚未解決的產品層級缺陷，直接跟 Hermes 這個方案相關，必須加進風險清單**：docagent 用 3 個併發請求測試「vLLM 掛掉、同時 fallback 到 Ollama 的 `gpt-oss:20b`」，發現 Ollama 的併發控制在第二個請求上回傳「admission busy」，而 docagent 的 fallback 邏輯**沒有重試或排隊，而是直接回傳 `HTTP 200 OK`、內容卻是一句失敗訊息**（呼叫方如果沒檢查內容字串，會誤以為拿到了正常結果）。**Hermes 這個方案提議的 fallback 目標一模一樣是同一個 Ollama 上的 `gpt-oss:20b`**——如果 vLLM 掛掉時 Hermes 本身也有多個併發任務（例如多個 kanban worker）同時 fallback，可能撞上同樣的 Ollama admission-busy 碰撞。**已初步查證**：docagent 那個「靜默回傳假成功」的行為（`HTTP 200` + `draft_text` 塞一句失敗訊息），追查 log 訊息格式（`phase='chat'`）判斷這其實是 **docagent 自己 `vllm_client.py`/`DrafterAgent` 手刻的錯誤處理**（把例外轉成一個假裝成功的回傳值），不是 Ollama 伺服器本身對外回傳的格式。Hermes 這邊沒有這種 app 專屬的手刻邏輯——遇到 Ollama 拒絕新請求時，走的是標準 OpenAI SDK 例外 → `agent/error_classifier.py::classify_api_error()` 分類 → `fallback_providers` 重試/切換的通用管線，理論上不會出現「回傳 200 但內容是失敗訊息」這種假成功。**但這是推論，不是實測過的結論**——建議上線後找機會用真實併發流量觀察一次（不必事前擋著不上線，比照「先小流量觀察」的精神，改成「上線後列入觀察重點」）。
5. **記憶體實測數字更新**：real 三方併發實測（`vllm-production` 55.9GB + `vllm-embed` 1.6GB + `gpt-oss:20b` 27.6GB 同時常駐）總計 83.1GB / 121.69GB，餘裕 38.6GB——比設計時估計的 ~51GB 餘裕更緊（`gpt-oss:20b` 實際用量是原估計 ~13GB 的 2 倍）。仍然安全，但邊際比原本以為的窄，值得記住（尤其 `OLLAMA_MAX_LOADED_MODELS=2` 代表同時還可能有另一個 Ollama 模型常駐）。
6. **缺少的風險項——config 變更的生效方式**：目前找不到 Hermes 有任何 config 熱重載機制，`config.yaml` 的改動很可能需要重啟 `hermes-gateway.service` 才會生效。重啟會中斷當下所有進行中的對話/kanban worker，不是無感切換——這點原本完全沒寫進風險清單，實作前需要規劃一個低流量時段執行，並比照先前 rebase 部署的方式做（新 release snapshot + 重啟 + `hermes doctor` 驗證）。

## 待確認、必須在 cross-review 中處理的風險點

1. **併發負載疊加未實測**：docagent 自己的併發測試（22 併發、關 thinking）滿載也才 118 秒完成；Hermes 加入後兩邊共用同一個 vLLM instance 的總併發量會更高，目前沒有實測數據。建議上線後先小流量觀察，不能假設沒問題。
2. **時序依賴（已由使用者定案，現況已確認滿足）**：**使用者已確認：不要求兩邊同步 cutover（推翻 Phase 3 舊原則），Hermes 等 docagent 那邊 vllm-production 實際上線、觀察過併發負載穩定後再切換。** `vllm-production` 先前一度暫停已查明是 vLLM 那邊 agent 刻意做的 failover 測試（見上方說明），非異常，目前已確認穩定運作（`running`, `/health` 200）。**這個前置依賴目前已滿足**，但 Ticket 56 本身在 vLLM 那邊的文件裡仍標記為「DESIGN ONLY，需要實作後再做一次 cross-check」——也就是說 vLLM 那邊自己的 cutover 也還沒完全走完流程，Hermes 這邊切換前建議跟 vLLM 那邊再確認一次 Ticket 56/58 是否都已定案完成。
3. **Failover 不是瞬間**：如上述查證第 4 點，vLLM 卡住時 Hermes 會先重試 2 次才切換到 `gpt-oss:20b` fallback，需要確認這個延遲在實際使用情境（尤其 Telegram 互動）下是否可接受。
4. **模型 id 一致性——已查證確認**：`docker inspect vllm-production` 直接讀到實際啟動指令 `--served-model-name drafter-active`，確認 `model.default: drafter-active` 這個字串與 vLLM 端實際註冊的模型 id 完全一致，不需要再跟 docagent 那邊額外核對。
5. **`ornith:35b` 保留至確認遷移完成**：本方案完全上線、觀察穩定後，才能通知可以清除 `ornith:35b`。
6. **`OLLAMA_MAX_LOADED_MODELS` 4→2 的連動風險（新發現，來自 Ticket 56 文件）**：docagent 那邊計畫把 Ollama 的同時載入模型數上限從 4 降到 2（`~/open-webui-stack/compose.yaml`）。Ticket 56 文件明確排除「清掉 `ornith:35b`」、明確排除「處理 Hermes 遷移」，但這個上限調整仍會影響 Hermes：如果本方案上線後 Hermes 的 fallback 是 `gpt-oss:20b`（透過 Ollama），跟 DocuBot/DocHelper/klib 等其他消費者共用同一個 Ollama container 時，模型驅逐（reload）頻率會比現在更高。**這不是本方案的阻擋項，但實作前應該跟 docagent/vLLM 那邊確認這個上限調整的時程，避免兩邊改動疊加造成非預期的 cold-load 延遲。**

## 尚未執行的動作

- 尚未修改任何 Hermes 檔案。
- 已完成：兩個獨立 agent 交叉審查（機制本身判定可行，但各自抓到需要修正的具體缺陷，已全部整合進上方「交叉審查修正」章節）。
- **實作前仍需要你決定/確認的兩件事**（不是我能自己拍板的）：
  1. `fallback_providers` 要不要保留現有的 `groq` entry（見「交叉審查修正」第 2 點）。
  2. 是否接受「這次改動會讓 Hermes 預設模型從 `codex_responses` 換到 `chat_completions` 協定」這件事，先小流量觀察再全面上線（見「交叉審查修正」第 3 點）。
- 實作前建議先查清楚 Hermes 自己遇到 Ollama admission-busy 時的實際行為（見「交叉審查修正」第 4 點），確認不會重現 docagent 那邊「靜默回傳假成功」的缺陷。
- 你確認上述兩點後，我會實作 config 變更、跑測試、並比照先前 rebase 部署的方式（新 release snapshot + 重啟 + `hermes doctor` 驗證）完成部署。
