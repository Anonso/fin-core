# 设计稿：cognition 空坍塌定修（推理预算耗尽 + 哨兵误判）

> 规则 5 短设计（核心链路：LLM backend 公共入口 + cognition 提取管线）。
> 合入后删除，Git 即归档。触发：owner 08-30「空坍塌机制调查」→「排查修复」。

## 现象与根因（2026-08-30 晚实弹实证）

现象：晚间窗口 glm53/deepseek/qwen 全返回字面 `[]`（raw_len=2），opencode 网关
deepseek_flash 正常出活。交接稿原判「模型自主选择最短合法 JSON」被证伪。

原始 HTTP 探针（`raw_http_probe.py`，直接客户端抓 response 元数据）：

- glm53 / deepseek / qwen 三者一致：`finish_reason='length'`、`content_len=0`、
  `reasoning_len≈9500–10000` 字符、`completion_tokens=4096`（预算顶满）。
- 即：**隐藏推理吃光 `max_tokens=4096` 全部预算，可见答案被截成空**。模型完全
  可用（推理链 1 万字符），是预算耗尽，不是限流、不是能力差、不是模型选空。

两级放大缺陷：

1. `OpenAICompatibleBackend._response_text()` 只读 `message.content`，空即抛
   `ValueError`，在 `finish_reason=='length'` 的 `max_tokens` 倍增恢复路径
   （4096→8192→16384，代码已有）**之前**短路——恢复逻辑永远走不到。
2. 重试耗尽后 `complete()/complete_bounded()` 返回字面 `"[]"` 哨兵；
   `CognitionLLM.complete_json` 把它 parse 成合法空数组 → 提取层判「bare-empty
   模型输出」，先浪费一次同 backend nudge，链升级后若仍全败，落警告
   `LLM found no extractable units`——**不可重试**（retryable 正则只认
   `LLM extraction failed/error`）→ 排空不再补做，产物空壳进 durable store。

次要路径：glm53 偶发 HTTP 400 内容过滤（`系统检测到输入或生成内容可能包含
不安全或敏感内容`，规则 4 买卖纪律措辞触发），同样吞成 `[]` 哨兵。

## 修复方案（四刀，全部最小外科）

1. **截断恢复可达性**（`fin_analyse/claims/openai_backend.py`，complete 与
   complete_bounded 两处对称）：响应拿到后先读 `finish_reason`；
   `== 'length'` 时容忍空 content（跳过 `_response_text` 的硬校验）进倍增分支。
   分级：答案非空截断 → 全档倍增 4096→8192→16384；content 空（推理耗尽
   签名，实证 glm53/deepseek 推理随预算线性增长三档全空）→ 只倍增一次
   （4096→8192，qwen 实测此档救回）即终态 `LLMResponseTruncated`。
   非 length 的空 content 保持既有失败语义不变。`before_attempt`/deadline/
   熔断记录语义不动。
2. **哨兵与真空区分**（`fin_analyse/cognition/thesis_extractor.py` 主提取
   升级链）：parse 出空 units 且 `llm.backend.last_failure is not None` →
   判硬失败（服务端故障，nudge 无效），跳过同 backend 重试指令，换链下一
   backend；链耗尽产出 `LLM extraction failed: backend failure (<error_type>
   [http=N])`——命中 retryable 正则，补做语义恢复。fake/test backend 无
   `last_failure` → 行为不变；真模型产 `[]`（last_failure=None）→ 既有
   bare-empty 升级语义保留。
3. **GLM 400 内容过滤**：被刀 2 自动归为 retryable 硬失败 + 链升级兜底
   （deepseek_flash 独立池实测活）。**不改**规则 4 措辞（有明确产品语义，
   owner 08-30 已裁定纪律必须按原文提取），不预做 prompt 手术。
4. **DS opencode 端点降级**（owner 08-30 追加指令）：deepseek_flash 配
   `endpoints` 两段——主键 `${OPENCODE_GO_API_KEY}`（llm.env）+ 降级键
   `${AUTHJSON:opencode-go}`（运行时 owner-only 读
   `~/.local/share/opencode/auth.json`）。主端点失败自动换键重试；config_loader
   新增 `${AUTHJSON:<entry>}` 解析（与 dotenv 同级 owner-only 边界：非符号
   链接/属主/0600/前后 fstat 一致，任何不符返回空串、值不入日志）。密钥不入
   git/日志，auth.json 保持唯一权威源。

## 不改什么

- `complete()` 失败返回 `"[]"` 的既有哨兵契约（众多调用方/测试依赖它）。
- llm.yaml 全局 `max_tokens` 上调（只缓解不修复，且扩大所有消费者成本）。
- 读 `reasoning_content` 当答案（推理不是答案，会污染 evidence 校验域）。
- bare-empty 升级 / empty_reason 成本护栏语义。

## 验证

- 单测：openai_backend（length+空 content → 倍增后成功；三档耗尽 → 终态截断
  失败）；thesis_extractor（last_failure 哨兵 → 跳 nudge 换 backend、全链失败
  retryable 警告；无 last_failure 的 `[]` 行为不变）；config_loader（AUTHJSON
  正常解析 + 符号链接/组他可读/缺条目拒绝）；openai_backend（主端点 401 →
  降级端点出活）。
- 实弹：`probe_with_failure.py` 复跑 → glm53/deepseek/qwen 预期从 `[]` 变为
  真实内容（或 `last_failure=LLMResponseTruncated`，不再伪装合法空）。
- focused 全量：`pytest tests/claims tests/cognition` 绿。
