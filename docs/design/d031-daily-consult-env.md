# D-031 · Daily 生成器换问询环境（骨架级草稿 · 2026-09-03 晚）

> 状态：**草稿**——owner 恢复推送指示后，按此骨架补全为正式短设计再过设计门
> （规则 5），届时本文件随实现合入后归档。动工前置：BUG-002 盘前实弹闭环
> （主线1 最后一块）。

## 背景与目标

D-030（09-01）停推四班后，Daily 生成器（`fin_analyse/operations/
daily_workspace_generator.py` + prepare/delivery 单元）与问询环境（薄 server
八工具 + consult-agent 人格）是两条并行的 LLM 消费链。D-031 目标：**Daily
产物改由问询环境生成**——同一工具面（read_g_context/read_market_*/…）、
同一模型路由（config/llm.yaml）、同一人格纪律，消除双链漂移（BUG-015 类
冻结时钟、BUG-016/017 类交付缺陷不再各修一遍）。

## 方案骨架（二选一，设计门裁决）

- **方案 A · 生成器直连薄 server**：prepare 单元以 stdio/RPC 客户端调
  thin server 拉八工具材料（替代 generator 内部各自 resolve），渲染与
  推送链保留。改动面小；人格纪律仍靠 prompt 复刻。
- **方案 B · consult-agent 会话当生成器**（对齐 D-032 方案 A 的同源化方向）：
  每班以 `finqa-claude`（headless CC；旧名 finqa-x，该名 09-03 改名后已复用为
  codex 交互入口，勿混淆）+ 检查点问题生成正文，交付链只做信封与
  投递。同源化最彻底；成本/时延/bash 权限边界需设计门裁决。

## 不变量

- advisory-only 不变；产物仍过身份门（CHECKOUT_UNSAFE 语义）；审计与
  gap 记账一码一因；两融项删除、不催更新等已裁决纪律全部继承。
- 恢复推送 = 外部消息发送，需 owner 当时明确授权（硬边界 2）；未授权前
  产物只落盘不投递。

## 验收（继承 + 新增）

- BUG-016/017 盘后复验并入本项（NOW 最后#8）；四班 gaps=[]+正文对表；
  B1 盲评 ≥9 口径不变；BUG-002 盘前窗口闭环作为盘前班前置。

## 开放问题（设计门时定）

1. 四班时点与 ZSXQ 采集窗口的对齐（D-030 后 poller 窗口未动）。
2. 生成失败班的降级语义（带伤班照发 + 标注，还是诚实缺班）。
3. 方案 B 的每班 token 成本与 15min TimeoutStartSec 预算。
