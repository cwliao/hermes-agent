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

## docagent 那邊現況（Ticket 54 / 56，尚未部署）

- Ticket 56（設計完成、待 cross-review）規劃把 vLLM 從「docagent opt-in、手動切換」升級為常駐的 `vllm-production` container（`--restart unless-stopped`，固定佔用 ~55GB，port 18000）。
- docagent 自己的 `DRAFTER_MODEL_FALLBACK` 屆時會訂為 `gpt-oss:20b`（**不是** `gpt-oss:120b`）——理由：vLLM 逾時/出錯時記憶體不會釋放，`120b` 會直接撐爆 121GB 記憶體池；且同樣考量「Ollama 只留小模型」的新原則。
- 資源預算（Ticket 56 已估算）：`vllm-production` 55GB + `vllm-embed` 12GB + Ollama 既有 2 個小模型 ~18GB + OS 2.5GB ≈ 87GB，121.69GB 池中還有 ~34.5GB 餘裕。
- **這是「重用同一個常駐 vLLM instance」，不是「另起一個」**——Hermes 加入只會增加併發請求負載（GPU 算力競爭），不會增加額外記憶體佔用，因為模型權重已經常駐一份。
- **docagent 的 `vllm-production` container 目前尚未實際部署**（仍在等自己的 cross-review），這是本方案時序上的前置依賴。

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
    timeout: 600                      # 對齊 docagent 的 VLLM_TIMEOUT
    extra_body:
      chat_template_kwargs:
        enable_thinking: false        # 對齊 docagent 已驗證的 no-thinking 設定

model:
  default: drafter-active             # vLLM 端註冊的模型 id，需與 docagent 那邊一致
  provider: vllm-local

fallback_providers:
  - provider: openai-api              # 沿用現有 Ollama custom provider（127.0.0.1:11434）
    model: gpt-oss:20b                # 對齊 docagent 新版 fallback，非 ornith:35b、非 120b
```

## 待確認、必須在 cross-review 中處理的風險點

1. **併發負載疊加未實測**：docagent 自己的併發測試（22 併發、關 thinking）滿載也才 118 秒完成；Hermes 加入後兩邊共用同一個 vLLM instance 的總併發量會更高，目前沒有實測數據。建議上線後先小流量觀察，不能假設沒問題。
2. **時序依賴（已由使用者定案）**：docagent Ticket 56 的 `vllm-production` 常駐 container 目前尚未部署。**使用者已確認：不要求兩邊同步 cutover（推翻 Phase 3 舊原則），Hermes 等 docagent 那邊 vllm-production 實際上線、觀察過併發負載後再切換。** 本方案的實作與 config 變更不應在 docagent 的常駐 container 穩定運作前執行。
3. **Failover 不是瞬間**：如上述查證第 4 點，vLLM 卡住時 Hermes 會先重試 2 次才切換到 `gpt-oss:20b` fallback，需要確認這個延遲在實際使用情境（尤其 Telegram 互動）下是否可接受。
4. **模型 id 一致性**：`model.default: drafter-active` 這個字串必須與 docagent/vLLM 那邊實際註冊的模型 id 完全一致，需要在實作前跟 docagent 那邊核對，不能憑空假設。
5. **`ornith:35b` 保留至確認遷移完成**：本方案完全上線、觀察穩定後，才能通知可以清除 `ornith:35b`。

## 尚未執行的動作

- 尚未修改任何 Hermes 檔案。
- 下一步：待使用者確認上述風險點（尤其第 2 點時序依賴、是否要求同步 cutover）後，交付獨立 cross-review，通過後才實作 config 變更並驗證。
