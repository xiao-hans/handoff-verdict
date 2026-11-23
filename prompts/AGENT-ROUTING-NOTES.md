# 来源会话回执：注册约定与 DSH 验证

## 实际改动

- `handoff.py`：新增注册参数 `--agent-name`、`--origin-target`，保存到任务注册表并在注册回显、`status --json` 和文本状态中读回；新增只从注册表解析的 `routing` 状态。`claim` 的结果附带该状态，原有 claim 去重、退出码和 dispatch 含义不变。真实任务缺路由时拒绝 `ack --verdict accepted`，仍允许记录 `blocked`。
- `handoff.py`：`send --event-id` 精确筛选一个事件；不指定时保持批量发送。Webhook 正文仍严格只有 `{schema_version, task_id, event_id, status}`，签名、请求 ID、重试、事件 ID 和投递状态语义不变。
- `tests/test_agent_routing.py`：新增标准库 unittest 合成测试，覆盖元数据注册/读回、地址拒绝、旧注册兼容/阻塞、解析器与展示名独立、四字段及签名、注入地址拒绝、synthetic 不通知、精确发送及其他事件逐字节不变；本轮新增覆盖 `register --force` 换目标后旧事件路由 blocked、verdict 不可覆盖、null/空 source 按 pi 处理、`agent_name` 首尾空白 strip。HTTP 与密钥读取全部 mock，不启服务、不读真实数据。
- `AGENT-RECEIPT-ROUTER-PROMPT.txt`：仅更新项目内路由约定参考文本，明确 claim 去重与路由权限不同、synthetic 不通知、地址来源及缺路由阻塞；没有修改已部署 Cron/Webhook 配置。

Phase 2（webhook 加固，Gap 1–5）新增：

- **Gap 1 双通道独立验收**：验收工单 `outbox/verify/<event_id>.json` 只由脚本通道在路由接管器拿到 dispatch 后写入（`verify-open`，限非 synthetic 的 `report_ready` 且 `route_state=ready`，带 `claim_state=claimed_by_script`），由独立的 `pi-task-handoff-verify` Agent 走 `verify-show` → `verify-claim` → 只读核验产物 → `verify-ack`；验收账本 `outbox/verify-acks.jsonl` 与路由账本 `outbox/acks.jsonl` 分开，`verify-ack` 永不改写事件上的路由 `verdict`（不变量 I3），判定落账后队列文档即被关闭。`send --verify-url` / `send-verify` 只推验收队列，只有相关化的 2xx 收下才置 `sent`，预算耗尽记 `failed`；`verify-clear --reason` 清理未验收积压并保留已验收工单。
- **Gap 2 多 Agent 来源**：`--source` 接受 `pi|claude|receipt|exit|cursor|codex|dsh`；receipt 家族（`receipt|exit|cursor|codex|dsh`）用 `--receipt-mode marker|json|exitcode` 判定完成（**默认 `json`**，`--source exit` 也一样，需要旧语义时显式传 `--receipt-mode exitcode`），默认收件文件落在 `tasks/<task_id>/receipt-source/`（`receipt.json`/`<marker>.done`，默认 `done.done`/`exit.json`），无需 JSONL 会话路径。可选阈值：`--marker-name`、`--exit-code-success`、`--min-report-chars`（50–4000，默认 200）、`--max-retries`。展示名默认 `cursor → Cursor Agent`、`codex → Codex`、`dsh → DSH`；`receipt`/`exit` 无默认名，缺 `--agent-name` 直接返回 2；收据族传 `--session-id` 直接返回 2（本机路由请用 `--origin-target local:<session_id>`）。`register --force` 会清掉上一轮的收据文件：上一轮登记属收据族（`receipt|exit|cursor|codex|dsh`）时删其 `--session-path`/`--exit-code-path` 指向的文件（含 `receipt-source/` 外的绝对路径），并扫本任务 `receipt-source/` 下的默认收据名与上一轮收据文件名；上一轮是 `pi`/`claude` 时其会话 JSONL 一律不删（跨来源改登记成收据族也不例外），`--report-path` 产物也不删——除非该路径就是上一轮的收据文件（收据与产物共用一个文件时按收据删）。
- **Gap 3 桌面本地路由**：`--origin-target` 除 `telegram:<chat_id>[:<thread_id>]` 外，还接受 `local`（回到当前桌面会话）与 `local:<session_id>`（指名本机会话）。路由结果给出 `channel=local`，`notify_telegram` 与 `notify_origin` 同为真（G3-10：ready 路由一定欠来源一条通知，字段名是历史包袱）；接管器用 Hermes 本地会话镜像投递，不发 Telegram。裸 `local` 在运行时依次取任务的 `session_id`、环境变量 `AGENT_HANDOFF_LOCAL_SESSION`（旧名 `PI_HANDOFF_LOCAL_SESSION` 已改名不映射），两者都拿不到就按瞬时失败退避，绝不凭空猜地址，也不启动镜像进程。
- **Gap 4 租约与退避重试**：`claim --lease-seconds` 写租约，租约内或持有进程存活时重复认领返回 3，租约过期且持有 PID 已死才可接管；`release` 主动放锁（已有 verdict 时返回 2）；瞬时失败用 `defer --retry-after` 转入 `retry_pending`，退避窗口 `60/120/240/480/600` 秒、上限 5 次，超限 `defer` 返回 2（`retry_exhausted`）并由接管器升级为 `ack --verdict blocked`；每个重试窗口只发一次 `retry-poke`（`last_retry_poke_at` 守卫，投递 ID 为 `<event_id>-r<retry_count>`）。永久错误（如 `chat not found`）立即 `ack --verdict blocked`，不进入重试。
- **Gap 5 归档与清理**：`archive [--older-than-days 14|--dry-run|--task TASK_ID]` 把已终结、全部事件已 verdict、登记时间超过龄期且无存活进程/无未决投递/无开启重试窗口的任务移出 `registry.json`，写入 `data/archive/registry-archive.json`（synthetic 演练过窗即归档，不再需要额外开关；`--task` 只跳过龄期，其余门禁照旧）；`prune-claims [--dry-run]` 按“是否还挡住东西”删除 claim 标记（`outbox/claims/` 与 `outbox/verify-claims/`）：已 verdict 的、或事件文件已消失且无 verdict 的孤儿标记都删，未 verdict 且事件仍在的标记无论多旧一律保留。事件、收据、账本文件一律不删除。
- `handoff.py` `_atomic_write`：Windows 上 `os.replace` 会因读取方或索引器瞬时占用目标文件抛 `WinError 5`；正文已 fsync 到临时文件，故对 replace 做最多 5 次、`0.02×n` 秒的有界重试，避免把瞬时占用误报成写失败。
- `scripts/agent_handoff_router.py`：Hermes Python 候选改为 `glob` 发现 `<HERMES_HOME>\tools\python-3.*\python.exe`（按版本倒序）再加 `sys.executable`，不再硬编码具体构建号。
- `AGENT-VERIFY-PROMPT.txt`：新增验收 Agent 的订阅提示文本；`tests/test_webhook_hardening.py`：新增 42 项合成单测覆盖 Gap 1–5。

## 字段语义与边界

- `source=pi|claude` 只选择日志解析器；即使展示名是 `Claude Code`，`source=pi` 仍按 Pi JSONL 解析。不同 Agent 若输出兼容格式，使用对应的 `source` 并独立填写真实展示名。
- 新真实任务的发起方必须显式传入真实 `--agent-name` 和当前发起聊天的 `--origin-target`。本工具不猜测当前聊天，也不从 Webhook、日志正文、收据或环境变量提取目标。
- 地址仅接受 ASCII 十进制的 `telegram:<chat_id>` 或 `telegram:<chat_id>:<thread_id>`。十进制 chat_id，可带正负号，数字部分首位必须为 1–9；thread_id 必须为不带符号的正十进制整数，首位必须为 1–9。拒绝空值、空白/控制字符、非数字、额外冒号/字段、零 chat_id、前导零，以及零、负数或带符号的 thread_id，不会自动去空白修复地址。
- 本地地址仅接受 `local` 或 `local:<session_id>`。`session_id` 必须是 8–64 位 ASCII，首字符为字母或数字，其余可含点、下划线、连字符；拒绝空 session 段、过短段、含空白或 `../` 的形式。`local` 不带 ID 表示“回到当前桌面会话”，由接管器在运行时解析，注册表不保存猜测出的会话 ID。
- `--source` 只决定判定方式：`pi`/`claude` 解析会话日志（需 `--session-path`），receipt 家族读 `tasks/<task_id>/receipt-source/` 下的判定文件（`marker` 看 `exit=<int>` + `report_fp=<sha256>` 两行、`json` 看结构化收据并校验 `schema_version`/`task_id`/`status`/`exit_code`/`report_fp`/`finished_at`/正文长度、`exitcode` 看 `exit.json` 对象而非裸整数，`exitcode`/`json` 可配 `--report-path` 指向真实产物）。任一校验不通过只把 `state.json` 的 `bad_receipts` 加一，不发事件、不设终态，下一轮仍然合格；判定文件不存在且来源进程仍存活时保持等待，绝不伪造终态；来源进程已死且没有判定文件记为 `stalled`。
- 展示名必须非空且不含控制字符；不将其当命令。执行外部投递的一方必须以独立参数或正确引用的值传递动态字段，不拼接执行不可信文本。
- 日志关联用的 `session_id`（Claude 必填）与通知地址用的 `local:<session_id>` 是两个不相干的字段：前者只定位本机日志文件，后者只指名要镜像回哪个桌面会话；二者相等也不代表可以从日志反推地址，仍须由发起方显式传入。`telegram:<chat_id>[:<thread_id>]` 只指 Telegram chat/topic，`local` 只指本机会话镜像，两者都不恢复原 Hermes 内部对话状态。

## 新任务注册示例（只作说明，勿原样注册真实任务）

由发起方从受信的当前 Telegram 上下文填写 chat ID；主题消息还须填写该主题 thread ID，不能遗漏后改投群主会话。

```text
python handoff.py --root <任务状态根目录> register --task-id <新任务ID> --session-path <专用JSONL绝对路径> --workdir <工程绝对目录> --goal <任务目标> --source pi --agent-name "Pi" --origin-target "telegram:<当前chat_id>[:<当前thread_id>]"

python handoff.py --root <任务状态根目录> register --task-id <新任务ID> --session-path <专用stream-json绝对路径> --workdir <工程绝对目录> --goal <任务目标> --source claude --session-id <本机Claude会话ID> --agent-name "Claude Code" --origin-target "telegram:<当前chat_id>[:<当前thread_id>]"

python handoff.py --root <任务状态根目录> register --task-id <新任务ID> --workdir <工程绝对目录> --goal <任务目标> --source cursor --receipt-mode marker --origin-target "local:<本机会话ID>"

python handoff.py --root <任务状态根目录> register --task-id <新任务ID> --workdir <工程绝对目录> --goal <任务目标> --source exit --agent-name <真实展示名> --receipt-mode exitcode --report-path <报告绝对路径> --origin-target "local"
```

上述尖括号和方括号是占位说明，不是合法地址。纯合成地址示例：私聊 `telegram:12345`、群 `telegram:-10012345`、群主题 `telegram:-10012345:27`、本机 `local:abcd1234efgh`。不提供任何真实聊天地址。`receipt`/`exit` 来源没有默认展示名，必须显式给 `--agent-name`，否则注册返回 2。

## 旧记录与接管流程

下述规则按 `routing.channel` 生效：`telegram` 走 `hermes send`，`local` 走 Hermes 本地会话镜像，两者都要求先 claim 成功、都只在真正投递成功后才允许 `ack --verdict accepted`。

1. 不自动迁移、补写或删除旧注册。缺 `agent_name` 时，仅兼容映射 `pi → Pi`、`claude → Claude Code`，旧版缺省 source 仍为 pi；未知来源不能猜名称。
2. 为兼容旧调用，缺省 `--origin-target` 仍可登记，但真实任务立即给出路由阻塞警告。`status`/`claim` 的 `routing` 会显示 `route_state=blocked`、`notify_telegram=false` 和缺信息原因；本机 session ID 不会成为回退地址。旧任务仍可扫描、形成收据和发送四字段本地事件，便于接收端显式记录阻塞。
3. 只有首次 `claim` 的 `result=claimed` 或路由器自己拥有的重试窗口才进入处理流程；重复认领返回 `already_claimed`、`dispatch=false`、退出码 3，但 `retry_pending` 且窗口已开、标记 `claimer=hermes-webhook` 时视为自己拥有，接管器照投递（Spec §4.5 分支 A）。`dispatch=true` 只说明首次认领，不意味着允许通知。
4. 真实通知要求 `routing.route_state=ready`，此时 `notify_telegram` 与 `notify_origin` 必为同一个值（G3-10）：两通道都欠来源一条通知，实际走哪条由 `channel` 决定。缺任务注册、目标缺失/格式无效、展示名无法确定或 `source` 是本构建未知的值时禁止路由；事件 `created_at` 早于当前任务的 `registered_at` 时，`claim.routing` 同样返回 `route_state=blocked`、`notify_telegram=false`、`notify_origin=false`，原因是“事件早于当前注册”。上述阻塞情况下，`ack --verdict accepted` 返回 2 且不写 claim/ack/verdict；接管器应以明确原因记录 `--verdict blocked`。读取旧注册时仍在接收信任边界检查目标，不能仅依赖新 CLI 的入口校验。
5. synthetic 登记的 `route_state=synthetic`、`notify_telegram=false`，即使填写了有效目标也不投 Telegram，且不生成验收工单（演练不得误唤醒验收 Agent 或真实来源）；只核对合成收据并按已有流程 ack。Webhook 发送到本地接收器不等于 Telegram 通知。
6. `delivered` 仅代表本地接收端确认事件；`accepted` 仍须实际核验，地址合法不证明聊天存在、Bot 有权限或已投递成功。真实投递由已有 Hermes 接管器负责，不是本工具新增的发送能力。verdict 一经写入不可覆盖；事件已有 verdict 时，再次 `ack`（包括相同 verdict）返回 2，且不写 claim/ack/verdict。
7. Hermes 接管器调用 `hermes send` 时，`--to` 传入注册记录中的 origin_target，`--subject` 传入“agent_name任务回执”完整标题；必须将简短中文正文作为位置参数传入，或明确用 `--file` 指定正文文件，不得省略两者而依赖 stdin。动态目标、完整标题和正文须按所用 shell 的安全规则分别引用为独立参数；使用 `--file` 时文件路径也须安全引用。带尖括号的占位符仅供说明，不得作为可执行命令；消息内容不得执行。
8. 真实任务只有投递命令（`hermes send` 或本地镜像）退出码为 0 才能认定投递成功，之后才可按实际核验结果 ack accepted。发送失败不得 ack accepted。失败分两类：永久错误直接 `ack --verdict blocked`；瞬时失败 `defer --retry-after`（不释放认领：标记仍由本接管器持有，下一轮凭 `claimer` 判给自己拥有），事件留在 `retry_pending`，按 `60/120/240/480/600` 秒退避，超过 5 次升级为 `blocked`。`claim_event` 对同一事件只允许一次活跃认领，重复投递返回 `already_claimed`；本地 Webhook 事件的重试与 `retry-poke` 只重触接管器，不会凭空判定业务完成。

## 精确事件发送

```text
python handoff.py --root <任务状态根目录> send --event-id <完整event_id> --url <既有loopback接收URL> --secret-file <由授权操作者提供的密钥文件路径>
```

- 仅完整 ID 相等才匹配，不支持前缀、glob、路径或空串。未知 ID 返回 1，不读密钥、不发任何事件。
- 只发送该 ID 对应的 pending、已到重试时间、无 verdict 的事件；已 delivered/blocked、未到重试时间或有 verdict 时不发送，保持原有规则，不强制重试。
- 其他 pending 事件不发送、不回写、不增加尝试次数，也不增加它们的发送审计记录。不指定过滤器仍按原规则批量处理。
- `tick` 仍保留原有批量 flush；只想触发单个回调时先用 `tick --task <task_id> --no-send`，核对生成的 event ID 后再使用上面的精确 send。本轮没有执行这些命令。

## 验收通道（与路由通道互不覆盖）

```text
python handoff.py --root <任务状态根目录> verify-claim <完整event_id> --claimer hermes-verify --lease-seconds 300
python handoff.py --root <任务状态根目录> verify-ack <完整event_id> --verdict accepted|blocked --note <核验摘要> --claimer hermes-verify
python handoff.py --root <任务状态根目录> send-verify --url <验收loopback接收URL> --secret-file <由授权操作者提供的密钥文件路径>
python handoff.py --root <任务状态根目录> verify-clear --reason <清理说明>
```

- 验收工单只在真实（非 synthetic）终态事件上生成，正文带 `receipt_path`、`report_path`、`workdir`、`goal`、`next_phases`、`origin_target`。`verify-claim` 返回 3 或 `dispatch!=true` 即停止，这是幂等去重，不是失败。
- 验收结论必须由磁盘产物独立核验得出：进程退出码 0、Agent 自述文本、路由通道的 `delivered` 都不算证据。
- 两条通道身份分离：路由 claim 用 `--claimer hermes-webhook`（与 `AGENT-RECEIPT-ROUTER-PROMPT.txt` 一致），验收用 `--claimer hermes-verify`；`verify-ack` 只写 `outbox/verify-acks.jsonl`，绝不改写事件上已有的路由 verdict，反之亦然。
- 重试窗口归路由接管器所有：`retry_pending` 且窗口已开时，标记的 `claimer=hermes-webhook` 就是自己拥有，`claim` 仍返回 3（`already_claimed`）但接管器照投递（Spec §4.5 分支 A）；send 侧的 retry-poke 只有在收到相关化 2xx 后才写 `last_retry_poke_at`，poke 失败不烧窗口。

## 失败分级：永久错误与瞬时失败

- 永久（契约/配置）错误：目标缺失、地址非法、`chat not found`、Bot 被屏蔽、`peer_id_invalid`、`hermes send` 参数被拒（退出码 2）——立即 `ack --verdict blocked` 并写明确原因，不消耗重试预算。
- 瞬时（投递）失败：连接被拒、超时、5xx、镜像脚本无法启动、本机镜像回执 `HANDOFF_MIRROR=0`（含指名会话不存在）、裸 `local` 一时解析不出 session_id——`defer --retry-after <窗口>` 转 `retry_pending`，按 `60/120/240/480/600` 秒退避，5 次用尽后升级成 `blocked`。区别的意义：重试不会修好一个错误的地址，也不会因为一次抖动就永久判死；但会话可能只是还没起来，所以判死只放在重试预算用尽之后。

## 归档与积压清理

```text
python handoff.py --root <任务状态根目录> archive --older-than-days 14 --dry-run
python handoff.py --root <任务状态根目录> archive --older-than-days 14 --task <已终结任务ID>
python handoff.py --root <任务状态根目录> prune-claims --dry-run
```

- 两者都必须先 `--dry-run` 看清将被移动/删除的条目，再执行真实调用；归档只移出注册表，事件、收据、账本全部留在原处，审计链不断。
- 仍在运行的进程、未 verdict 的事件、未决投递、开启中的重试窗口、仍在龄期内的活动，任一条命中即拒绝归档该任务。

## 建议 DSH 命令（本轮未运行）

在 `<HANDOFF_ROOT>` 中，用既有 Python 环境执行，无需安装依赖：

```text
python -m unittest discover -s tests -p test_*.py -v
python tests/test_handoff.py
python -m py_compile handoff.py scripts/agent_handoff_router.py tests/test_webhook_hardening.py
```

第一条跑全部合成单测（`tests/test_agent_routing.py` 路由契约 + `tests/test_webhook_hardening.py` Gap 1–5）；第二条是项目原有完整合成回归（会生成合成密钥、启用 loopback 假接收器并创建测试子进程）；第三条只确认可编译，不执行任何逻辑。DSH 应按既有隔离约定清除真实 `AGENT_HANDOFF_URL` / `AGENT_HANDOFF_SECRET_FILE` 等部署环境变量，只用合成临时目录、测试密钥和本地假服务，不注册真实任务或投递 Telegram。不要用真实任务的 `tick`/`send` 代替回归。

## 验证状态与未解决限制

- 验证状态（已更新）：Phase 2 三条命令均通过——`python -m unittest discover -s tests -p "test_*.py" -v` 56/56 OK（`tests/test_webhook_hardening.py` 42 项 + `tests/test_agent_routing.py` 14 项）；`python tests/test_handoff.py` 448 项合成检查 PASS；`python -m py_compile handoff.py scripts/agent_handoff_router.py tests/test_webhook_hardening.py` 无错误。全部单测只用临时目录、mock 的 HTTP 与子进程，不触真实 `data/`、不开真实套接字。
- 早期记录：`tests/test_agent_routing.py` 14 项与 `tests/test_handoff.py` 主回归均已通过——DSH 沙箱内主回归 PASS；unit 套件经调度方在同哈希副本上独立验证 14/14 OK。DSH 沙箱内 unit 套件的 28 个 ERROR 为 tempfile 清理时 `chmod` 被 workspace-write 沙箱拦截的环境伪影，非代码缺陷。
- CodeGraph 调用未获权限，改用工作区内定向文件读取。没有读取工作区外文件、真实会话正文、注册数据或密钥；没有发消息、安装依赖、改 Cron/Webhook 或清理旧记录。
- 本目录不是 Git 仓库；没有 Git diff、提交或部署。
- 本轮仅落盘注册与路由守卫，以及接管器参考约定；未重新部署已安装接管器、未改工作区外 tick 包装脚本。真实端是否遵守来源目标、Telegram 权限和发送结果仍须授权后的部署核验，不能从合成/mock 单测推断。
- Phase 2 本轮同样只落盘代码、文档与合成测试：未安装或订阅 `pi-task-handoff-verify` Agent、未改动已部署的 Cron/Webhook 配置、未向真实 Telegram 会话或本机会话投递过任何消息，也未对真实 `data/` 执行 `archive`/`prune-claims`。双通道、本地路由、退避重试与归档的真实行为仍需授权后的部署核验。
- 旧任务缺来源信息不会被本轮自动修复；需要任务发起方/授权操作者明确提供信息后另行处理，不应靠猜测、回退 home DM 或盲目 `register --force` 修复（force 会重置任务读取状态）。
