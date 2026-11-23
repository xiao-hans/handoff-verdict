# handoff-verdict

**给编码 Agent 用的带签名回调（signed webhook）交付层**：注册一个正在跑的任务，
读取它的会话文件，判断它是完成了、失败了还是卡住了，然后把这件事作为一条事件（event）
签名后投递到接收端的 webhook，并在接收端回执（receipt）之后做二次校验。

A signed-webhook delivery layer for coding agents — register a running task, read its
session file, judge whether it finished, failed or stalled, deliver that as a signed
event to a webhook, and re-verify it after the receiver's receipt.

它解决的唯一问题是：**“我以为它还在跑”和“它其实已经停下”不能靠猜**。所有判断都来自
会话文件里可读的记录，所有投递都有 HMAC 签名和回执可对账。

The one problem it solves: **"I thought it was still running" vs "it had already
stopped" should never be a guess.** Every verdict comes from readable records in the
session file, and every delivery carries an HMAC signature with a receipt to reconcile.

## 特点 Features

- **文件队列是唯一事实源**：HTTP 只是运输方式，先落盘再投递，事件、回执、裁决、审计流水都有据可查
- **签名投递，回执可对账**：HMAC 签名（`X-Hub-Signature-256`），接收端回执后做二次校验
- **裁决不可变**：verdict 一经写入不可改，要改只能走 `verify-clear`
- **回环限定**：投递地址只接受 `127.0.0.1` / `localhost`，公网地址会被拒绝——这是设计约束，不是缺省值
- **零第三方依赖**：纯 Python 标准库，Python 3.11+（用到 `tomllib`）
- **适配器可选**：Hermes 的 telegram / local 通道都没配也能跑，事件照常到达 webhook 与文件队列
- **19 个子命令**：登记、判定、投递、认领、回执、校验、归档全套都在 `handoff.py` 里

## 架构 Architecture

handoff-verdict 是 Hermes 多 Agent 接力架构中的事件运输层。完整运行链路是：

```text
register -> tick/judge -> signed event -> router -> origin session
                                                     |
                                                     v
                                              verify-drain
                                                     |
                                                     v
                                           kanban projection
                                                     |
                                                     v
                                      archive tombstone + watchdog
```

1. `register` 记录任务、执行 Agent、工作目录和需要被唤醒的原会话。
2. `tick` 从会话、退出码和活动证据判定 running、完成、失败或卡住，并先把事件落盘。
3. `router` 校验签名后，把结果送回唯一原会话；无法唯一识别时拒绝投递，不新开会话。
4. `verify-drain` 在投递后独立验收。投递成功只代表消息送达，不代表任务被接受。
5. 看板同步在验收之后运行。它只读 handoff 台账，按幂等键投影卡片；失败不影响 handoff 结果。
6. `archived` 是人工接管后的墓碑，后续同步不再重建、转列、刷新或评论。watchdog 忽略 `handoff-tracker` 镜像卡，避免把投影卡当成真实派工。

同一条主线可以在原会话中迭代。规划、开发、测试、审阅分别登记阶段；一个阶段验收后唤醒原会话，由原会话决定下一阶段，而不是由 webhook 或看板调度。

```text
register  ->  tick  ->  signed send  ->  webhook  ->  router  ->  verify
(登记任务)   (读会话、   (HMAC 签名       (接收端      (按注册表    (二次校验、
             生成事件)    POST 投递)      路由)         路由投递)    独立记账)
```

- **文件队列是唯一事实源**，HTTP 只是运输方式。数据根下的布局：`registry.json` 存任务，
  `tasks/<task-id>/state.json` 存读取进度，`tasks/<task-id>/receipts/` 存回执原文，
  `outbox/events/<event-id>.json` 是事件权威记录（先落盘再投递），
  `outbox/claims/<event-id>` 是并发投递的独占标记，`outbox/acks.jsonl` 存主通道裁决，
  `outbox/journal.jsonl` 是审计流水；校验通道另有 `outbox/verify/`、
  `outbox/verify-claims/`、`outbox/verify-acks.jsonl`，与主通道分账。
- `handoff.py` 是登记与投递端，19 个子命令：`register / unregister / tick / send /
  status / claim / ack / ack-pending / clean / send-verify / release / defer /
  verify-claim / verify-ack / verify-show / verify-open / verify-clear / archive /
  prune-claims`。
- `scripts/agent_handoff_router.py` 是接收端分发器：它**只读注册表**决定事件该去哪里，
  请求体永远只有 `schema_version / task_id / event_id / status` 四个字段。
- `scripts/agent_handoff_tick.sh` 是给调度器（cron 之类）用的包装：先 `tick` 再 `send`，
  配置缺失就以非零退出，不吞错；投递地址、校验地址、密钥文件都在环境或配置文件里。
- 仓库根目录的 `fake_receiver.py` 是一个只用于自验的假接收端（`--secret-file` 必需，
  `--mode ok|500|badsig|redirect`），它同样只接受 `X-Event-Type: agent_task_handoff`。
- 事件类型 `agent_task_handoff`；接收端路由 `/webhooks/agent-task-handoff` 与
  `/webhooks/agent-task-handoff-verify`。签名头：`X-Event-Type`、`X-GitHub-Event`、
  `X-Request-ID`（重试期间保持不变）、`X-Hub-Signature-256`。
- 裁决（verdict）一经写入不可变；verify 通道单独记账，不与主通道混用。
- 协议默认值是契约的一部分，不随机器改变：投递上限 60 次（`AGENT_HANDOFF_MAX_ATTEMPTS`），
  停滞判定 3600 秒（`AGENT_HANDOFF_STALL_SECONDS`），单任务单轮最多读 262144 字节
  （`AGENT_HANDOFF_MAX_BYTES`）与不限条数（`AGENT_HANDOFF_MAX_RECORDS` 缺省 0）；
  分发器侧的重试退避 60/120/240/480/600 秒、独占租约 120 秒、陈旧锁 300 秒、
  telegram 发送超时 45 秒、本地镜像超时 60 秒，都是编译期常量。

## 快速开始 Quick start

前置：Python 3.11+，不需要装任何第三方包。

1. 复制配置样例，填自己的值（两个样例等价，环境变量优先于配置文件）：

   ```bash
   cp config.example.toml agentwebhook.toml   # 或：source .env.example 后改值
   ```

   必填四项：`AGENT_HANDOFF_ROOT`（文件队列目录）、`AGENT_HANDOFF_URL`、
   `AGENT_HANDOFF_VERIFY_URL`、`AGENT_HANDOFF_SECRET_FILE`（HMAC 密钥文件）。
   配置解析顺序是**环境变量 → 配置文件 → 非零退出并报出缺哪个键**；任何一层都不会
   落到某个开发者机器的路径上。配置文件默认名 `agentwebhook.toml`，放在 `handoff.py`
   同目录，或用 `AGENT_HANDOFF_CONFIG` 指到别处。

2. 登记一个正在跑的任务，并跑一轮判定与投递：

   ```bash
   python handoff.py register --task-id <task-id> \
     --session-path <session-file> --workdir <project-dir> \
     --goal "<这个任务要达成什么>" --pid <running-pid>
   python handoff.py tick --max-bytes 262144
   ```

   `tick` 默认判完就投递，`--no-send` 只判定不入队投递；调度器一般直接跑
   `scripts/agent_handoff_tick.sh`（它把 `tick` 与 `send` 串起来）。

   只想看一眼整条链路，可以先起假接收端：

   ```bash
   python fake_receiver.py --port 8644 --secret-file <secret-file> --mode ok
   ```

3. 接收端分发：接收端先把路由配好（事件类型 `agent_task_handoff`，只接受回环地址），
   然后用事件号跑分发器；不带参数就是全量分发：

   ```bash
   python scripts/agent_handoff_router.py <event-id>
   ```

4. 校验链路（可选，需要 `AGENT_HANDOFF_VERIFY_URL`）：

   ```bash
   python handoff.py verify-open <event-id>     # 看还欠哪些校验
   python handoff.py send-verify --url <verify-url> --secret-file <secret-file>
   ```

投递目标是**回环限定**的：`AGENT_HANDOFF_URL` / `AGENT_HANDOFF_VERIFY_URL` 只接受
`127.0.0.1` 或 `localhost`，公网地址会被拒绝——这是设计约束，不是缺省值。

## 测试 Tests

`tests/` 下 5 个测试文件：`test_handoff.py`（pytest）、`test_agent_routing.py`、
`test_webhook_hardening.py` 与 `test_kanban_watchdog.py`（unittest）、
`test_kanban_handoff_sync.py`（unittest，覆盖看板投影同步）。跑全部测试：

```bash
python -m pytest tests/ -q   # 需先安装 pytest；pytest 会把两个 unittest 文件一起跑
```

## Bot 唤醒机制 Bot wake-up chain

签名事件送达接收端只是投递，**真正让 Agent 干活的是 bot 唤醒链**。整条链由三层组成，
全部配置与提示词都在本仓库内（密钥、真实账号、会话号一律是占位符）：

```text
handoff.py send ──HMAC POST──> Hermes gateway webhook 平台 (127.0.0.1:8644)
                                      |  订阅规则匹配 event: agent_task_handoff
                                      v
                              唤醒 cron 任务 (deliver=log)
                                      |  注入唤醒 prompt（只含 task_id / event_id / status）
                                      v
                     路由 Agent ——按 prompts/AGENT-RECEIPT-ROUTER-PROMPT.txt 行事
                                      |  claim -> hermes send -> ack -> verify-open
                                      v
                     验收 Agent ——按 prompts/AGENT-VERIFY-PROMPT.txt 行事
                                        verify-show -> verify-claim -> 只读产物核验 -> verify-ack
```

1. **webhook 订阅**（Hermes gateway 侧）：`platforms.webhook` 监听 `127.0.0.1:8644`；
   一条订阅把事件名 `agent_task_handoff` 绑定到 `/webhooks/agent-task-handoff`，
   校验 `X-Hub-Signature-256`，命中后以模板
   `接管已注册pi任务：task_id={task_id} event_id={event_id} status={status}` 唤醒一个
   cron 任务（`deliver=log`）。模板**刻意只含三个标识字段**——载荷里的路径与目标地址
   一概不进 prompt，路由 Agent 必须回本地注册表取权威数据，通知不等于验收。
   校验通道同理走 `/webhooks/agent-task-handoff-verify`。
2. **路由 Agent**（`prompts/AGENT-RECEIPT-ROUTER-PROMPT.txt`）：被唤醒后按固定流程
   `claim --claimer hermes-webhook --lease-seconds 120` 领取事件，只信注册表；按回执
   目标 `telegram:<chat_id>[:<thread_id>]` / `local[:<session_id>]` 分通道投递，投前
   `verify-open` 确认还有没有可活；成功 `ack --verdict accepted`，无法唯一寻址
   `ack --verdict blocked`，瞬态失败按 60/120/240/480/600 秒退避 `defer`。
3. **验收 Agent**（`prompts/AGENT-VERIFY-PROMPT.txt`）：独立于路由 Agent 的第二双眼睛，
   `verify-claim --claimer hermes-verify --lease-seconds 300` 后**只读磁盘上的任务产物**
   做比对，验收结论写独立台账 `outbox/verify-acks.jsonl`，永不 `hermes send`。
4. **补投递守卫**：`scripts/agent_handoff_tick.sh` 由每分钟 cron 触发，兜住漏投的
   回调；`AGENT-ROUTING-NOTES.md` 记录 `--agent-name` / `--origin-target` 约定、
   `source=pi|claude|receipt|exit|cursor|codex` 判定与地址语法。

## 提示词与规格 prompts/ docs/

- `prompts/AGENT-RECEIPT-ROUTER-PROMPT.txt` — 路由 Agent 的唤醒 prompt（可原文部署）。
- `prompts/AGENT-VERIFY-PROMPT.txt` — 验收 Agent 的唤醒 prompt。
- `prompts/AGENT-ROUTING-NOTES.md` — 路由与寻址约定的权威笔记。
- `docs/AGENTWEBHOOK-PLAN.md` — 总设计：从单 Agent 补投递到多 Agent 路由的演进计划。
- `docs/spec-webhook-hardening-20261112.md` — webhook 加固规格（幂等、租约、签名、回落）。
- `docs/spec-agentwebhook-generalization-20261113.md` — pi→通用 Agent 泛化规格（改名与兼容）。
- `docs/verify-lifecycle-sweep-20261119.md` — 验收通道生命周期清扫规格。

## 可选的 Hermes 适配器 Optional Hermes adapters

接收端分发器可以把结果送到两个通道，两个通道都由 Hermes 提供，**因此都是可选的**：

- **telegram**：需要 `AGENT_HANDOFF_HERMES_BIN`，用 `hermes send --to <origin> ...` 投递，
  目标来自登记时的 `--origin-target`，形如 `telegram:1234567890` 或带话题号
  `telegram:1234567890:1`（示例都是占位值，不是真实账号）。
- **local**：需要 `AGENT_HANDOFF_HERMES_PYTHON` 与 `AGENT_HANDOFF_HERMES_AGENT_DIR`，
  把消息镜像回发起会话；`local:<会话号>` 直接用，裸 `local` 需要
  `AGENT_HANDOFF_LOCAL_SESSION` 给出会话号。

没有配置适配器时**不会崩溃，也不会被当成业务失败**：事件已经到达 webhook 与文件队列，
这一步仍然算成功；分发器会把该事件记为 `blocked` 并写明缺哪个键，核心命令
（`register / unregister / tick / status / claim / ack / defer / release / clean /
archive / verify-* / prune-claims`）照常可用。

通道语义保持不变：`hermes send` 返回 rc 2 是永久错误，立即 `blocked`；超时和 rc 1 是
瞬时错误，按 60/120/240/480/600 秒退避，五次之后 `blocked`。

## 排错 Troubleshooting

- **`ERROR: PI_HANDOFF_URL is set but AGENT_HANDOFF_URL is not ... renamed`**
  旧的 `PI_HANDOFF_*` 变量名已经全部改成 `AGENT_HANDOFF_*`，并且**不做兼容映射**。
  只要环境或配置文件里出现旧名而同名新名未设，任何命令都会非零退出并报出对应的新名。
  按提示改名字即可，不要指望旧值生效。
- **`no secret file given (--secret-file / $AGENT_HANDOFF_SECRET_FILE); refusing to
  fall back to any default credential`**：签名密钥文件必须显式给出，程序不会猜测密钥。
- **`send skipped: no receiver configured ($AGENT_HANDOFF_URL or --url)`**：没有接收端
  配置，事件留在 outbox 里等待，`tick` 本身仍然成功。
- **`{"result": "misconfigured", ...}`**：分发器缺 `AGENT_HANDOFF_DATA_ROOT` 或
  `AGENT_HANDOFF_SCRIPT`，按上面补齐即可；它不会去扫描磁盘猜路径。
- **`{"result": "blocked", "note": "hermes adapter is not configured; ...", ...}`**：
  通道需要的 `AGENT_HANDOFF_HERMES_BIN` / `AGENT_HANDOFF_HERMES_PYTHON` /
  `AGENT_HANDOFF_HERMES_AGENT_DIR` 没配，补齐后事件仍会按退避表重试。
- **路由找不到 / 事件被拒**：确认接收端订阅的是 `agent_task_handoff`，路由名是
  `/webhooks/agent-task-handoff`（校验通道是 `...-verify`），并且 `fake_receiver.py`
  之类的手工接收端也按新事件名校验 `X-Event-Type`。
- **lock busy**：分发器拿不到 `router.lock` 时输出 `"result": "lock_busy"` 并返回退出码
  `0`——已有另一个分发在跑，这不是失败，调度器不应重试告警。
- **裁决不会变**：已经写入 `verdict` 的事件再裁决会返回 rc 2，这是不可变契约；
  要改只能走 `verify-clear`。

## 许可 License

MIT（见 `LICENSE`）。`data/`、`tmp/`、`review/`、真实配置文件 `agentwebhook.toml`、
`.env`、`receipt*.json` 与 `*.key` 都在 `.gitignore` 里：这个仓库发布的是机制，不含任何
真实账号、会话、密钥或机器路径。配置样例与本文一律使用占位值
（`<data-root>`、`<secret-file>`、`telegram:1234567890`）。
