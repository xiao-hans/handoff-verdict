# Spec：Verify 生命周期 Sweep 工程设计规格书（编排基础设施加固 Phase 1）

验收标识：VERIFY-SWEEP-SPEC-P1-20261119
日期：2026-11-19 ｜ 设计方：Claude Code ｜ 实现方：Pi（Phase 2）
性质：仅规格。本文件不改动 handoff.py、tick、agentwebhook.toml、cron 或任何生产文件。

## 0. 实证根因与不变量（全部方案共守）

### 0.1 今日实证（2026-11-19，两条开发主线在交付后核验交接处静默断链）

1. **错 root 笔误永久滞留**：核验器 LLM 把 verify-claim 的 `--root` 误写为 `pi-handoff`（正确为 `pi-handoff/data`）。`handoff.py:3537` `root = os.path.realpath(args.root or default_root())` 后，`_verify_queue_refusal`（handoff.py:2964-2978）里 `load_events(root).get(event_id)` 为空 → 返回与"事件真不存在"同一个 `unknown_event` / rc 1。核验器 prompt 的禁重试规则把一次笔误变成永久滞留。
2. **退出留言无人接收**：核验器的退出留言走 webhook 响应正文；路由器（scripts/agent_handoff_router.py）只看 `_ack`/进程退出，不读响应正文（send 侧 `classify_response` 只用于投递通道的回执判定），留言实际无人消费。
3. **blocked 静默滞留 2 小时**：`cmd_verify_ack`（handoff.py:3030-3083）对 `--verdict blocked` 与 accepted 走同一路径：写 verify-acks.jsonl + 删除队列文档，**不派发、不通知、不产生新事件**。今日 ledger 实证两条 blocked（`evt-575e92d512de40819a9d` at=1795060915，kanban-sync；`evt-4798144dc5de2b535837` at=1795069956，mcp-traj），"等 Router 派返修"没有任何触发器。
4. **同 delivery_id 重发被网关去重吞回**：恢复路径实测——`verify-clear`/`verify-open`/`send-verify` 后，若网关仍持有旧 delivery_id，重发得到 200 duplicate；`classify_response`（handoff.py:1851-1852）把"duplicate + 本事件相关"判为 `accepted`，`send_verify_one`（handoff.py:2146-2148）随之写 `verify_state=sent`——队列在发送方看来完成，实际核验器从未被唤醒。
5. **register --force 清有效收据**：cmd_register 的 --force 分支（handoff.py:2668-2716）无条件删除上一轮 receipt 路径与 `receipt-source/` 下同名文件，把"本轮还没被消费的有效收据"一并删掉。
6. **监督盲区**：补投递守卫只管未投递事件；看板 watchdog 只看卡片状态；tick 守卫（scan_task 一线）只扫非终态任务收据。`verify_state=sent` 被 `run_send`（handoff.py:2191-2197，经 `_verify_queue_open` handoff.py:2068）视为完成（nothing pending），**但 sent ≠ 已核验**。

### 0.2 不变量（继承 spec-webhook-hardening-20261112 §0，本文档不放宽）

- I1 单一写者：每个状态文件只有一个属主进程路径（见 §7 归属表），其余只读。
- I2 幂等：任何命令重跑不产生第二条记录、不重置已获得进度（`open_verify_doc` handoff.py:2945-2950 的"refresh mirror, keep progress"契约保持）。
- I3 锁内读回：所有 read-modify-write 必须在 `with file_lock(root)` 内重新 `read_json` 落盘对象再改（对照 12:43 竞态事故；`send_verify_one` handoff.py:2141-2143 已是范式）。
- I4 零 LLM 监督：一切回收逻辑必须落在 tick 的确定性代码里，不得要求任何会话"记得回来看看"，不引入第二个常驻监督服务（硬边界）。
- I5 回执 ≠ 任务判定：verify webhook 的 accepted 只证明"唤醒成功"；任务级判定唯一来源是 verify-acks ledger 与 claim/ack（handoff.py:1824-1825 原注释）。

## 1. 设计项 1 — sent 超时回收（tick 内零 LLM sweep）

### 1.1 触发条件（全部成立才回收）

在 `data/outbox/verify/*.json` 全量扫描（`list_verify_queue`，handoff.py:2044-2056）中，对每个 vdoc：

- `verify_state == "sent"` 且 `sent_at` 非空；
- `now - int(sent_at) >= VERIFY_SENT_TIMEOUT`；`VERIFY_SENT_TIMEOUT = int(setting("AGENT_HANDOFF_VERIFY_SENT_TIMEOUT") or 900)`（默认 15 min，agentwebhook.toml 可配）；
- 无判定：`not vdoc.get("verdict")` 且 `load_verify_acks(root).get(eid, {}).get("verdict")` 为空；
- 无在途核验：`data/outbox/verify-claims/<eid>.json`（`p_verify_claim`，handoff.py:298）不存在，或存在但 `lease_expires_at` 已过 **且** `pid_exists(claimer_pid) is False`（与 `_verify_claim_locked` handoff.py:2855-2857 的窃取判据完全一致，避免与活着核验器抢文档）。

### 1.2 动作（回收 = 重开重发，不是删文档）

锁内（I3）读回后：

- `verify_state = "queued"`、`sent_at = None`；`attempts` **不清零**（预算连续消耗）；
- `history` 追加 `{"at": now, "action": "verify_stale_requeue", "code": None, "detail": "sent timeout after <n>s"}`；
- 若 `attempts >= max_attempts`（含本次回收即耗尽）→ 不重发，直接走 §1.3 终态；
- 本次 tick 的后续 `run_send` 会因 `_verify_queue_open`（handoff.py:2059-2077）重新返回 True 而自然重发，无需 sweep 自己 POST。

### 1.3 重试预算耗尽后的终态与通知

- `verify_state = "failed"`、`failed_at = now`、`last_error = "sent timeout: no claim/verdict within budget"`；
- 立即生成**唤醒失败通知事件**（复用 §3 的通知事件机制，status=`verify_stalled`，见 §3.2），投递 origin-target 并唤醒 Router 附"核验器未接单，请人工查 hermes-verify 会话"；
- `cmd_status`（handoff.py:2750）与看板把 `failed`/`timeout` 计为红项。

### 1.4 挂载点与时序

- 新函数 `sweep_verify_stale(root, now) -> list[str]`（返回回收的事件 id 列表，供打印），在 `cmd_tick` 中于 `run_send`（handoff.py:1726）**之前**调用；`send`/`send-verify` 手动路径同样先 sweep（`run_send` 顶部调用，保证手动重发也自愈）。
- 每 tick 回收上限 `AGENT_HANDOFF_VERIFY_SWEEP_MAX`（默认 20），防风暴；超出部分留下个 tick。
- 输出一行 `sweep: requeued <n> stale verify doc(s)`，让 cron 日志可见（对齐 spec 2.1 的"可见性"要求）。

## 2. 设计项 2 — 去重安全重发（delivery nonce）

### 2.1 现状与缺陷

`verify_delivery_id`（handoff.py:2080-2088）：attempt 0 用 `verify-<event_id>`，之后 `verify-<event_id>-a<attempts>`。序号后缀在正常重发下已经换新 id；**但**任何"文档被 clear/重建使 attempts 归 0"的恢复路径（§0.1 第 4 条实测）都会回到同一个 `verify-<event_id>`，被网关去重；且 `classify_response` 把相关 duplicate 判 accepted，吞回成 sent。

### 2.2 方案：attempts + 持久化 nonce 双后缀

- 新格式：`did = "verify-" + event_id`（首 attempt 保持旧格式，兼容既有对账逻辑与 X-Request-ID 规则 handoff.py:2116）；`attempts >= 1` 时 `did = f"verify-{event_id}-a{attempts}-{nonce}"`；
- `nonce`：8 位十六进制随机串（`secrets.token_hex(4)`），**在生成时立即随 doc 持久化**：`send_verify_one` 发送前把新 did 写入 `cur["last_delivery_id"]` 与 `cur["pending_delivery_id"]`（发出前写、成功后删 pending），进程被杀重启后重发用 `attempts+1` 重新生成，永不复用旧值；
- 兼容：网关按完整 delivery_id 字符串去重，任何新后缀天然绕过；`classify_response` 的相关性期望集合已含本次传入的 `did` 与其 `agent-handoff-<did>` 变体（handoff.py:1836），无需改判据。

### 2.3 duplicate 不再等价于送达（核验通道专用收紧）

- `send_verify_one`（handoff.py:2136-2148）里把分类结果细分：`accepted` 且响应**不是** duplicate marker（复用 `_is_duplicate_marker`，handoff.py:1788）→ `verify_state=sent`；`accepted` 且是 duplicate → **不**标 sent，detail 记 `"gateway deduped this delivery id; not delivered"`，attempts 照常 +1，history 记 `verify_send_duplicate`。下一次重发因 §2.2 的 nonce 必然带新 id，形成"去重 → 换 id 重试 → 真送达"的闭环。
- 说明：这是**核验通道**的判据收紧；origin 投递通道维持 spec-webhook-hardening §1 的"duplicate + correlated = 终止重试"语义不动（那里 duplicate 确实意味着接收方已持有事件，而 verify 通道持有≠核验器被唤醒——唤醒失败正是本次事故本体）。

### 2.4 持久化位置汇总

| 字段 | 位置 | 写入者 | 用途 |
|---|---|---|---|
| `last_delivery_id` | `outbox/verify/<eid>.json` | send_verify_one | 审计/对账 |
| `pending_delivery_id` | 同上 | send_verify_one（发出前置入，回执后删除） | 崩溃恢复时识别在途 id |
| `attempts` | 同上 | 同上 | nonce 前缀序号 |

## 3. 设计项 3 — blocked 事件化（阻断 verdict 变成有触发器的返修闭环）

### 3.1 原则

ledger（verify-acks.jsonl）仍是判定的唯一持久记录，**不新增第二判定源**；blocked 事件只是**通知流量 + Router 唤醒**，其自身永远不构成任务完成/失败判定（I5）。

### 3.2 新事件 status：`verify_blocked` / `verify_stalled`

- 复用既有事件管线 `create_event`/`save_event`（handoff.py:949-1002）：新增 status 常量 `VERIFY_NOTIFY_STATUSES = ("verify_blocked", "verify_stalled")`。**不进** `PROTOCOL_STATUSES`（handoff.py:46），`scan_task` 永远不会自己产出它们，收据/回执语义零改动；
- 事件内容全部来自本地 ledger（registry + vdoc + ack note），不从任何远端正文取值（对齐 `open_verify_doc` handoff.py:2886-2889 的可信度声明）；
- `verify_blocked`：由 `cmd_verify_ack --verdict blocked` 在写 ledger 后同步生成，body 附 `返修要求 = ack note[:500] + verifier + event_id + task_id`；
- `verify_stalled`：由 §1.3 预算耗尽路径生成。

### 3.3 幂等键

`make_event_id(task_id, status, reason, report_fp=source_event_id)`（handoff.py:894）：reason 固定取 `"verify verdict blocked"` / `"verify budget exhausted"`，report_fp 取被核验的原 event_id。同一原事件重复触发 → 同一 event_id → `create_event` 的 existing 分支（handoff.py:955-959）幂等吞掉，双写不可能。blocked ack 本身已拒覆盖（handoff.py:3037-3039、3049-3051），双保险。

### 3.4 路由与 Router 唤醒

- 通知事件带 `--state pending` 走**origin 通道**（`AGENT_HANDOFF_URL`），`route_event`（router.py:606+）按注册表 routing 投 origin-target（telegram 或 local 镜像，`deliver_local` router.py:427-440 即 bot-chat 等效注入）；
- Router 会话唤醒：新增 setting `AGENT_HANDOFF_ROUTER_TARGET`（形如 `local:<router-session-id>`，只看结构不读凭据）。通知事件生成后，`run_send` 对其额外做一次 `deliver` 目标 = ROUTER_TARGET 的本地注入（同 router 的 mirror snippet 机制），注入文本含 `verify-blocked <eid> task=<tid> → 请派发返修`；ROUTER_TARGET 未配置时降级：仅 origin 通知 + `cmd_status` 红项，不报错（渐进启用）；
- 与 events/verify-acks 的关系：events 目录多出的只是通知事件；`cmd_archive`/prune 按既有非终态规则处理；verify-acks 条目不因通知事件而改写。

### 3.5 响应正文断链的收口（§0.1 第 2 条）

- 核验器 prompt 契约修订（§4.4）：所有必须送达的留言一律写 `verify-ack --note` / `verify-nack --reason`，**禁止**以 webhook 响应正文、stdout 作为交接面；
- 发送侧兜底：`send_verify_one` 已把响应 detail 截 200 入 history（handoff.py:2159-2162），补充：detail 非空时同步 `append_jsonl(p_journal(root), {"action":"verify_response_detail", ...})`，使人工排障可在 journal 单文件看完。

## 4. 设计项 4 — claim 错误细分（错 root / 文档缺失 / 真未知 各自可辨识）

### 4.1 root 合法性前置判据（新增 rc 4）

`main`（handoff.py:3537）解析 root 后不猜路径；改由 `_verify_queue_refusal` 与 `cmd_verify_claim/show/open` 共用新函数：

```python
def classify_missing(root, event_id):  # -> (result, rc, hint)
    if not os.path.isdir(os.path.join(root, "outbox")):
        cand = os.path.join(root, "data")
        if os.path.isdir(os.path.join(cand, "outbox")):
            return "invalid_root", 4, f"--root {root} is not a state root (no outbox/); did you mean {cand}?"
        return "invalid_root", 4, f"--root {root} is not a state root (no outbox/)"
    ...既有 already_verified(3)/unknown_event(1)/synthetic_not_queued(3)/not_queued(3) 分支...
```

- **rc 语义表**：1=事件在此 root 确实不存在（真未知）；3=无你的活（已核验/合成/未入队，禁重试成立）；4=root 本身不对（**必须改参数重试**，禁重试规则不适用于 4）；2=用法错误（既有）。
- 只提示不自动切换 root（单一写者与可审计性：静默换 root 会让一次笔误写到另一套账本）。

### 4.2 队列文档缺失细分

- doc 缺失 + ledger 有 verdict → `already_verified` rc3（既有）；
- doc 缺失 + 事件存在 + 存在 `p_claim(root, eid)` 且 holder==`SCRIPT_CHANNEL_CLAIMER`（hermes-webhook，handoff.py:71、2910-2914）→ 新 `doc_missing_reopen` rc3，hint=`run verify-open <eid>`（这正是 §1 sweep 与人工恢复的断点）；
- doc 存在但 `verify_state in ("acked",...)` → 既有拒绝路径。

### 4.3 「按 event_id 自动解析 root」可行性评估 — **否决（Phase 1）**

- 方案：全局索引（如 `~/.hermes/event-index.jsonl` 记 eid→root）。成本：索引写入点散布在 create_event/ack/archive/register 之后，任何漏写点即成错误路由源，直接违反 I1（给 root 解析引入第二写者）；多 root（多 workspace 并行主线）时同名 task_id 冲突需再定义归属；索引损坏/陈旧的失败模式恰好复现本次事故（找回了 root 但找回的是旧账本）。
- 收益被 §4.1 的 rc4 + 明确提示覆盖 95% 笔误面（错 root 的形态几乎总是"项目根 vs data 子目录"一级差）。
- 结论：不做；若 Phase 2 数据显示 rc4 仍高频，重评只读版索引（tick 末尾重建、claim 端只读、永不作为唯一路由依据）。

### 4.4 核验器 prompt 契约同步修订点（交付给 Router/prompt 维护方，非本仓代码）

1. 只把 rc0 当"可开工"；rc3=干净退出且**不留言给响应正文**；rc4=改正 `--root .../data` 后**允许且必须**重试一次；rc1=真未知，写 nack（§6）后退出。
2. 任何要给人看的话一律进 `verify-ack --note` 或 `verify-nack --reason`。
3. claim 后先 `verify-show` 核对 `route_state/claim_state`，不符即 nack。

## 5. 设计项 5 — register --force 有效收据保留

### 5.1 "有效"判据（新函数 `_receipt_is_valid(root, entry, path) -> bool`）

同时满足：

- 文件存在且 `mtime >= entry.registered_at`（本轮注册之后写的）；
- 收据内 `task_id` 与 entry.task_id 一致（JSON 收据）；marker 模式按 `_receipt_is_stale` 既有指纹路径校验；
- **未被消费**：以 `receipt_path` 建过的终态事件（`load_events`）不存在携带相同 report_fp/exit 判定的 verdict 记录——即它尚未变成某条已投递事件的证据；
- 不落入 `_reset_incremental_state`（handoff.py:2670）之后任何已存在 state 游标的已读偏移。

### 5.2 行为改动（cmd_register --force 分支 handoff.py:2681-2716）

- 删除集合从"上一轮路径 ∪ 同名 sweep"改为"**仅 stale**"：`_receipt_is_valid` 为 True 的文件**保留**，并打印 `KEEP: <name> (valid receipt not yet consumed; --force will not delete it)`；stale 判定沿用现行删除逻辑；
- 保留路径下不重扫（重扫=让 tick 立即消费旧收据，等价于另一种污染）；
- 删除失败仍按现行 WARN 显名（handoff.py:2709-2714），新增计数入 stdout JSON echo（`stale_cleared`/`kept`），供路由器脚本断言。
- 兼容：跨 source（pi/claude→receipt family）时现行"绝不动旧 session_path 日志"的门槛（handoff.py:2676-2680 注释）保持原样。

## 6. 设计项 6 — verify-nack 留痕（准入失败不再是无声退出）

### 6.1 新子命令

```
handoff.py verify-nack --root R --event-id EID --reason TEXT [--claimer C]
```

- 前置：ledger 已有 verdict → rc2 拒绝（与 ack 同律）；doc 缺失 → §4 细分结果；
- 写回 vdoc：`verify_state="nacked"`、`nacked_at=now`、`nack_reason=reason[:500]`、`nack_count += 1`、history 追加 `verify_nacked`；**文档保留在队列**（与 ack 的"关闭即删"相反——nack 是待回收态不是终态）；
- 写 journal：`{"action":"verify_nacked", ...}`；ledger（verify-acks）**不写**，它仍只存判定。

### 6.2 状态机迁移与 Router 回收触发

- `sent → nacked`（verifier 唯一写点）；`nacked → queued`（仅 §1 sweep 属主）：`now - nacked_at >= AGENT_HANDOFF_VERIFY_NACK_TIMEOUT`（默认 300s，给一次改正参数重试的窗口）且 `nack_count < NACK_MAX`（默认 3）→ 重发（§2 nonce 保证网关不再吞）；
- `nack_count >= NACK_MAX` → 与 §1.3 同路径：`verify_state="failed"` + `verify_stalled` 通知事件（reason 附最后一条 nack_reason）→ Router 被唤醒处置；
- `_verify_queue_open`（handoff.py:2068）的关闭集合纳入 `nacked`（未到回收窗口前不重复 POST）。

## 7. 设计项 7 — 状态机全图与写者归属（对照 12:43 事故）

### 7.1 状态集合

`queued / sent / claimed* / nacked / timeout** / acked / blocked** / failed`
（* `claimed` 不是 verify_state 字段值，是由 `verify-claims/<eid>.json` marker（lease+pid）派生的正交标记，判据同 §1.1；** `timeout/blocked` 在文档字段上分别落地为 `queued+history=verify_stale_requeue`（瞬时）与终态 ledger 记录（acked 之外 blocked 仅是 ack note 语义 + 通知事件，见 §3）——图中列出以对齐验收口径。）

### 7.2 合法迁移表（表外迁移 = 实现必须拒绝并返回 rc2）

| from | to | 属主/触发 | 条件 |
|---|---|---|---|
| （无 doc） | queued | verify-open（脚本通道侧） | open_verify_doc 全部前置通过（handoff.py:2891-2914） |
| queued | sent | send_verify_one（tick/run_send） | 2xx 且 classify accepted 且非 duplicate（§2.3） |
| queued | queued(+attempts) | send_verify_one | 2xx duplicate / 非 2xx；未耗尽预算 |
| queued | failed | send_verify_one | attempts>=max_attempts（handoff.py:2155-2158 现行） |
| sent | queued | §1 sweep（tick 属主） | 超龄 + 无 verdict + 无活 claim |
| sent | nacked | verify-nack（verifier 属主） | 准入失败 |
| sent | acked | verify-ack（verifier 属主） | verdict∈{accepted,blocked}；blocked 同时产出 §3 事件 |
| nacked | queued | §1 sweep | 过 NACK 窗口且未超 NACK_MAX |
| nacked | failed | sweep | NACK_MAX 耗尽 + verify_stalled 通知 |
| acked | （doc 删除） | verify-ack | 现行 handoff.py:3072-3079 |
| failed | queued | 仅人工 verify-open 重开 | open 保留 progress 契约（handoff.py:2945-2950）不变；预算须显式重设 |

### 7.3 单一写者归属（I1）

- `outbox/verify/*.json`：tick 系（run_send/send_verify_one/sweep_verify_stale）与 verifier 系（verify-ack/verify-nack）**都**只在自己的表内行迁移，且全部在 `file_lock(root)` 内先读回最新盘态（I3，范式=handoff.py:2141-2143）；
- `verify-claims/`：O_EXCL 创建（handoff.py:2843），窃取只认 lease+pid 双条件；
- `verify-acks.jsonl`：append-only，唯一写者 verify-ack；
- `registry.json` / 事件文件：维持现行（tick 扫描与接收侧 ack），本次不动。

### 7.4 竞态防护（12:43 事故的针对性约束）

- sweep 与 ack 赛跑：sweep 回收前锁内重读，若发现 `verdict` 已出现（ack 先落）→ 放弃回收并记 history `sweep_aborted_verdict`；反之 ack 路径已天然拒覆盖（ledger 双查 handoff.py:3037+3049）；
- sweep 与 verifier claim 赛跑：§1.1 活 claim 判据；
- 双 tick 赛跑：file_lock 全局串行 + sweep 幂等（同 doc 二次回收因 sent_at 已 None 而不再命中）；
- register --force 与 sweep 无共享文件，不构成竞态；与 receipt 消费赛跑由 §5.1 mtime>=registered_at 判据挡住未写收据被删。

## 8. 设计项 8 — 验收清单（Phase 2 Pi 单测；每条 = 当前实现下必然红的证伪用例，全部离线、零真实网络）

用例矩阵风格沿用 spec-webhook-hardening §6（tmp root + monkeypatch opener/时钟）。

| # | 用例 | 步骤 | 当前实现为何红 | 期望新行为 |
|---|---|---|---|---|
| T1 | 错 root 笔误 | `verify-claim --root <project>`（无 outbox/）任意 eid | `_verify_queue_refusal` 返回 unknown_event rc1（与真未知同码） | rc4 invalid_root + `did you mean .../data?` |
| T2 | 队列文档缺失可重开 | 事件存在、script claim 存在、verify doc 手工删除 → verify-claim | 返回 not_queued rc3 无提示 | rc3 `doc_missing_reopen` + hint verify-open |
| T3 | sent 超龄回收 | doc `sent/sent_at=now-901`、无 verdict、无 claim → tick | `run_send` 打印 nothing pending，永不再发 | sweep 置 queued，同 tick 重发，attempts+1 |
| T4 | 活 claim 不抢 | 同 T3 但 claim marker lease 未过期且 pid 存活 | （现行同样不抢，但无测试锁行为） | sweep 跳过 + history sweep_aborted |
| T5 | 回收预算耗尽 | T3 且 attempts=max-1 | 无超时通道，failed 只可能来自发送失败 | queued→failed + verify_stalled 事件生成 |
| T6 | blocked 无通知 | `verify-ack --verdict blocked` | ledger 写毕即删 doc，无事件、无人被唤醒（今日两条实证） | 同步产出幂等 verify_blocked 事件进 pending |
| T7 | blocked 幂等 | T6 后手工重放同 status 事件生成 | —（无此路径） | create_event existing 分支吞掉，仅一条事件 |
| T8 | --force 删有效收据 | 注册 receipt 任务→写有效未消费收据→`register --force` | 无条件 os.remove（handoff.py:2706-2715），红 | 文件保留 + stdout `kept` 计数 |
| T9 | --force 清 stale | 上一轮旧 mtime 收据 → `--force` | 现行正确 | 仍删（防回归 T8 的过度保留） |
| T10 | 同 delivery_id 重发被吞 | 首次 sent 后 clear→open（attempts 归 0）→重发；测试 opener 返回 duplicate-correlated 200 | `verify_state` 被再次置 sent（handoff.py:2146-2148 + classify duplicate=accepted） | duplicate 不置 sent；且第二次 did 携 nonce ≠ 首次 did |
| T11 | nack 留痕 | `verify-nack` 后读 doc | 子命令不存在 | nacked/nack_count/history/journal 齐 |
| T12 | nack 回收 | T11 后过 NACK 窗口 → tick | — | nacked→queued 重发；第三次 nack 后 failed + 通知 |
| T13 | sweep 与 ack 赛跑 | 并发脚本：先 sweep 读、后 ack 写 verdict，再 sweep 写 | —（sweep 不存在） | sweep 锁内重读发现 verdict → 放弃，doc 保持 acked |
| T14 | 真未知仍 rc1 | root 合法、事件不存在 | rc1 | rc1 且 hint 不含"did you mean"（与 T1 可区分） |
| T15 | needs_input 阻断可回收（§11） | 写一条 `verify-ack --verdict blocked` 且 note 含 §11.2 的 `needs_input` 结构化标记 → 推进时钟过超龄判据 → `tick` | blocked ack 只写 ledger 并删队列文档（§0.1 第 3 条实证：两条 blocked 静默滞留 2 小时），note 无字段约定，sweep 不扫 blocked，无人被唤醒 | ledger 行不被改写；产出**一条** status=`verify_blocked`、reason=`"needs_input escalation"`、report_fp=原 event_id 的通知事件（pending，body 含原任务与缺口三要素）；同 tick 重放不产生第二条；`AGENT_HANDOFF_ROUTER_TARGET` 未配置时降级为 origin 通知 + `cmd_status` 红项且不抛错 |

通过标准：T1–T15 全绿 + 既有 `tests/test_webhook_hardening.py`、`tests/test_handoff.py`、`tests/test_agent_routing.py` 全绿零修改断言（verify 通道内既有断言若因 §2.3 duplicate 语义收紧需调整，单独标注 diff 送设计方复核，不允许静默改测试）。

## 9. 配置新增汇总（agentwebhook.toml / 环境变量同名）

| key | 默认 | 用途 |
|---|---|---|
| AGENT_HANDOFF_VERIFY_SENT_TIMEOUT | 900 | §1 sent 超龄秒数 |
| AGENT_HANDOFF_VERIFY_SWEEP_MAX | 20 | 每 tick 回收上限 |
| AGENT_HANDOFF_VERIFY_NACK_TIMEOUT | 300 | §6 nack 回收窗口 |
| AGENT_HANDOFF_VERIFY_NACK_MAX | 3 | §6 nack 次数封顶 |
| AGENT_HANDOFF_ROUTER_TARGET | （空=降级） | §3.4 Router 会话注入目标 |

读取一律走既有 `setting()`（handoff.py:164），env → toml → 缺省，不新增加载器（对齐 spec-webhook-hardening §2.1）。

## 10. 未做与显式不做（边界声明）

- 不引入第二个常驻监督进程；全部回收逻辑内嵌 `handoff.py tick`（I4）。
- 不做全局 event_id→root 索引（§4.3 否决及重评条件）。
- 不改 origin 投递通道的 duplicate=终止 语义（§2.3 仅收紧 verify 通道）。
- 不改 verify-acks.jsonl 既有行格式；blocked 的"返修要求"只进新通知事件与 ack note。
- 不动 cross-agent-session-mcp、kanban-handoff-sync 两条在跑主线的任何文件。
- agentwebhook.toml 仅引用键名，不读取/转述任何 secret 值。
- 核验器 prompt 本体不在本仓：§4.4 是交付给 prompt 维护方的契约修订点清单。

## 11. 设计项 9 — 无人值守核验会话无人工输入依赖

性质：规格增补（Phase 2 由 Pi 实现）。本项只补"核验会话在无人值守通道里遇到授权/决策缺口时如何收场"这一条链路，不改动 §1–§8 的既有设计；§9 配置表与 §10 边界声明**保持原样不动**（本项刻意不新增任何配置键，见 §11.4）。

### 11.1 现状缺口（为什么 §3 还不完）

§3 已让 blocked 从"静默滞留"变成"有事件、有唤醒"，但它只覆盖**核验器自己就能给出阻断判定**的形态（判定=blocked，返修要求写在 note 里）。另一类缺口 §3 不解决：核验器**无权决定下一步**——需要授权（扩大文件访问范围、允许写生产配置、允许跑高成本重跑）、需要在两个以上处理方案之间取舍、或需要业务口径确认。这类缺口在 webhook/cron 通道里没有任何人会回答：

- 核验会话由 webhook 唤醒或 cron 拉起，stdin 无终端、回复无人接收；它若调用 `clarify` / `ask_user` 之类等待人工输入的机制，结果就是**整会话挂起或空转到超时**，文档停在 `sent`/`claimed`，比 blocked 更糟——连 ledger 里那一行判定都不会出现（§0.1 第 3 条的两条滞留实证正是这条路径的上游形态）；
- 即便它侥幸退出，"退出留言"走 webhook 响应正文，同样无人消费（§0.1 第 2 条）。

因此本项的不变量补充：**无人值守核验会话不得以"等待人工输入"作为任何分支的出口**；缺口只能以结构化 blocked 判定落盘，由确定性 sweep 交给前台 Router 处置（延续 I4 零 LLM 监督）。

### 11.2 核验器侧契约（prompt 契约，交付给 prompt 维护方，非本仓代码）

在 §4.4 三条之外新增契约条目（本节自成编号，交付时与 §4.4 合并进同一份 prompt 修订清单）：

1. **禁止**在 webhook/cron 等无人值守核验会话中调用 `clarify` / `ask_user` 或任何依赖人工回复才能继续的机制；禁止以"等小主回答"作为分支出口。
2. 遇到授权缺口或决策缺口时，**唯一**合规出口是：`handoff.py verify-ack --root <state>/data --event-id EID --verdict blocked --note <结构化 note>`，随后**正常退出**（rc0），不等待任何输入。
3. 结构化 note 的字段约定（三行起，逐行 `key: value`，值内不换行）：
   - `needs_input: yes` — 本项缺口属于"需要人（小主/Router）决策"，不是核验器可自证的返修要求；
   - `gap: <一句话缺口>` — 缺什么授权 / 要在什么之间取舍 / 需要确认什么口径；
   - `options: <可选项1 | 可选项2 | ...>` — 供前台决策参考的候选处理项，**只列不改选、不代表已选**。
4. 一次 blocked ack 只承载**一个**原 event_id 的缺口（ack 幂等键即原 event_id，见 §3.3）；多个缺口合并进同一条 note 的 `gap`/`options`，不开第二条 ack（ledger 首判优先、拒覆盖，§3.3）。
5. note 总长仍受既有 `ack note[:500]` 截断约束（§3.2）：字段名短、值精炼，超限截断不得吃掉 `needs_input:` 首行（契约要求该行必须出现在开头）。

### 11.3 sweep 侧回收（超龄 needs_input → 通知 Router + origin/source 群）

判定与动作全部落在 `sweep_verify_stale`（§1.4 的同一属主函数、同一 tick 挂载点）之内，不新建常驻进程、不新建第二条 sweep：

- **扫描对象**：`load_verify_acks(root)` 中 `verdict == "blocked"` 且 note 含 `needs_input:` 标记的行。**只读**（I1：verify-acks.jsonl 唯一写者仍是 verify-ack，本项不新增任何 ack 字段、不回写、不改写既有行格式——§10 第 3 条继续成立）。
- **超龄判据**：`now - ack.at >= AGENT_HANDOFF_VERIFY_SENT_TIMEOUT`（§9 既有键，默认 900s）。**刻意不新增配置键**：needs_input 的升级延迟与 sent 回收预算语义相同（"这段时间内没有活着的处理者"），复用既避免配置面扩大，也避免两个阈值互相矛盾。
- **升级动作**：生成一条通知事件并进入 pending 队列，字段与 §3 完全同构：
  - `status = "verify_blocked"`（复用既有常量，**不新增**状态；`VERIFY_NOTIFY_STATUSES` 不变）；
  - 幂等键 `make_event_id(task_id, status, reason, report_fp=原 event_id)`（§3.3），但 `reason` 取**新固定串** `"needs_input escalation"`——与 §3.2/§3.3 的 `"verify verdict blocked"`（ack 当场生成）区分开，二者 report_fp 相同而 reason 不同 → event_id 不同，因此两条事件可以共存（一条"阻断已发生"、一条"阻断超龄无人处置"），互不吞掉；同一原事件的升级事件重放仍被 `create_event` existing 分支幂等吞掉；
  - body 三要素：原任务（`task_id` + registry 里的 `description`/`target` 摘要，取自本地 ledger+registry，不从远端正文取值，对齐 §3.2）、缺口（ack note 的 `gap` 值）、可选处理项（`options` 值），并附 `blocked_at` 与超龄秒数。
- **投递与唤醒**：与 §3.4 同一机制——origin 通道投 origin-target（即 origin/source 群或 local 镜像），`AGENT_HANDOFF_ROUTER_TARGET` 配置时对 Router 会话做一次本地注入，注入文本含 `needs-input <原eid> task=<tid> 已超龄 → 请前台决策`；ROUTER_TARGET 未配置时降级为 origin 通知 + `cmd_status` 红项，不报错（渐进启用，语义同 §3.4）。
- **升级去重（同一缺口不反复轰炸）**：sweep 在 `data/outbox/verify-escalations.jsonl`（新文件，**唯一写者 = sweep 属主路径**，append-only）记一行 `{"event_id", "escalated_at", "notify_event_id"}`；命中已记录的原 eid 即跳过。选择新增独立 append-only 文件而不是给 ledger 加 `escalated` 字段，理由：后者等于给 verify-acks.jsonl 引入第二个写者（违反 I1）并改写既有行格式（违反 §10）；前者失败模式只是"多一条待去重记录"，永远不会误判任务状态（I5）。
- **预算边界**：升级是**一次性**通知，不是重试通道——不消耗 `attempts`、不重开队列文档、不把 blocked 变回 queued（§7.2 迁移表因此无需增行：blocked 在原 event 上仍是终态）。升级后仍无人处置怎么办？答案是**不自动处置**：不二次升级、不 escalate-to-failed，红项留在 `cmd_status` 与问题卡片上由人收口（§11.5），避免监督逻辑退化成无限提醒。

### 11.4 「执行器工具集不含 clarify」的可行性评估 — 本 Phase **不做**（如实标注：这是 prompt 契约，不是系统级隔离）

更强的做法确实是让无人值守通道**拿不到** `clarify` / `ask_user` 这类工具，而不是靠提示词禁止。评估结论：

- **可行面**：核验会话由 webhook/cron 执行器拉起，执行器侧的工具集/权限配置若能对"非交互来源"单独收敛（去掉需要人工回答的工具），就能把 §11.2 第 1 条从约定变成结构约束；harness 若支持"无终端输入时该工具直接不可见/直接失败"，属同类效果且更彻底。
- **限制与不做的理由**：
  1. 执行器配置与工具注册表不在本仓（本仓只有 `handoff.py` 与 `scripts/agent_handoff_*`；§10 第 7 条同样口径：prompt 本体与执行器编排属外部交付物）；改动面属生产配置，超出本 spec 边界；
  2. 无法确认现用执行器支持"按来源区分工具集"的粒度；若只能全局移除 `clarify`，会连带影响**交互式**核验会话里合法的澄清用法，属回归风险；
  3. 提示词注入面的绕过风险不因此消失：即便工具被移除，模型仍可能以"输出问题后结束回复"的方式变相等待人工输入——所以无论工具集是否收紧，§11.2 第 1/2 条都必须存在。
- **口径要求（写给后续实现与验收）**：Phase 1 该约束的**唯一强制力是 prompt 契约 + sweep 兜底回收**，实现与文档一律不得表述为"系统级已隔离/无法调用"。若 Phase 2 之后外部执行器确证支持工具集收敛，作为**加固**单独提案，仍不得反向放宽 §11.2。

### 11.5 残留问题卡片的处置边界

blocked/needs_input 事件升级后，任务侧（看板或任务卡）常留下一张"待决策"的残留卡片。硬规则：

- **不自动选择推荐项**：卡片里的 `options` 只是候选，任何自动"取第一个/取推荐项"都等于把授权或业务决策交给 LLM 监督逻辑，违反 I4 的确定性要求与本项的决策归属（小主）；
- **不唤醒旧核验会话**：原核验会话已按 §11.2 正常退出；对它做 resume/续跑会带来两个后果——旧上下文里的授权假设已过期，以及同一任务被"旧会话 + 新派发"双跑（判定源冲突，违反 I5 的唯一判定来源约束，且 ledger 首判优先会把后到的那一半静默吃掉）；
- **保留给人处理**：卡片保持未终态 + `cmd_status` 红项，Router 在前台拿到 §11.3 注入后向小主呈现代价，处置路径（授权 / 选项 / 改派 / 放弃）由人决定；人决定后如需再核验，走 §7.2 表内既有路径（人工 `verify-open` 重开，预算显式重设），不是复活旧会话。

### 11.6 本项可验收清单（区分静态契约核验与真实运行行为）

**A. 静态契约核验**（本 spec 与交付清单层面即可完成，不需要代码；Phase 1 交付时逐条勾选）：

| # | 检查 | 通过判据 |
|---|---|---|
| S1 | §11.2 清单可交付 | 条目编号接续 §4.4，字段名/键名/rc 全部是本文件既有名字，无自造配置键 |
| S2 | 强制力口径如实 | 文档任何位置不得出现"执行器已禁止 clarify""系统级隔离"表述（§11.4 明确写成 prompt 契约） |
| S3 | 与 §3 状态机一致 | 不新增 status 常量、不改 `VERIFY_NOTIFY_STATUSES`、不改 §7.1/§7.2、不改 ledger 行格式、§9 表零新增行 |
| S4 | 幂等与写者 | 升级事件 reason 固定串 `"needs_input escalation"` 与 §3.3 的 `"verify verdict blocked"` 并存不互吞；`verify-escalations.jsonl` 单一写者为 sweep |
| S5 | §8 表增行 | T15 已追加，通过标准区间更新为 T1–T15 |

**B. 真实运行行为核验**（Phase 2 Pi 单测，风格沿用 §8：tmp root + monkeypatch opener/时钟，全离线零真实网络）：T15（见 §8 表末行）覆盖"超龄 needs_input → 生成一条可区分的升级通知事件、幂等不重放、ROUTER_TARGET 缺省时降级不抛错"，即 §11.3 判定与动作的证伪用例；§11.2 的"核验器不等待输入"属 prompt 行为，本仓单测无法证伪，只作为交付给 prompt 维护方的验收项（人工在真实 webhook/cron 会话核验一次：缺口场景下 rc0 退出 + ledger 出现合规 note，记入 Phase 2 交付说明，**不以单测冒充**）。

**C. 上线口径**：截至本文件本次修订，§11.3 的 sweep 分支、`verify-escalations.jsonl`、§11.2 的 note 字段约定**均未实现、未部署**；§3 的 blocked 事件化本身在现网 `handoff.py` 也仍是待实现项（§0.1 第 3 条实证）。任何状态汇报不得称本项能力已上线；本项完成判据 = S1–S5 全勾 + T15 绿 + Phase 2 交付说明含 B 项的人工核验记录。
