# Spec: pi-handoff 泛化为 AgentWebhook（2026-11-13）

状态：规划文档，本轮不改代码、不改测试、不改脚本、不跑 git。
目标仓库：`git@github.com:xiao-hans/AgentWebhook.git`（本轮不初始化、不提交、不推送）。
语言：中文为主。

本文只写已核对的代码落点。行号以本轮阅读的工作区文件为准：`handoff.py`（3332 行）、`scripts/agent_handoff_router.py`（585 行）、`scripts/agent_handoff_tick.sh`（12 行）、`tests/test_handoff.py`、`tests/test_agent_routing.py`、`tests/test_webhook_hardening.py`、`AGENT-ROUTING-NOTES.md`。未读 `data/` 与 `.tmp-ref/`。

基线（简报声明，本轮不重跑）：`handoff.py` 约 153.9K，17 个子命令；`scripts/agent_handoff_router.py` 约 26K；`tests/test_agent_routing.py` 15 个 unittest，`tests/test_webhook_hardening.py` 78 个 unittest，合计 93；`tests/test_handoff.py` 为合成场景脚本（简报声明 448 项全绿）。仓库尚无 `.git`。

---

## 1. 命名空间一次切换

不做兼容层。旧名读到即失败，错误文本必须点出对应的新名。测试断言旧名不生效。

### 1.1 环境变量（13 个，后缀不变）

| 旧名 | 新名 | 代码落点 |
|---|---|---|
| `PI_HANDOFF_ROOT` | `AGENT_HANDOFF_ROOT` | `handoff.py:86` `default_root()`；未设时回退到脚本目录下 `data/` |
| `PI_HANDOFF_DATA_ROOT` | `AGENT_HANDOFF_DATA_ROOT` | `scripts/agent_handoff_router.py:35-36` |
| `PI_HANDOFF_SCRIPT` | `AGENT_HANDOFF_SCRIPT` | `scripts/agent_handoff_router.py:37-38` |
| `PI_HANDOFF_HERMES_BIN` | `AGENT_HANDOFF_HERMES_BIN` | `scripts/agent_handoff_router.py:39-41` |
| `PI_HANDOFF_HERMES_AGENT_DIR` | `AGENT_HANDOFF_HERMES_AGENT_DIR` | `scripts/agent_handoff_router.py:43-45` |
| `PI_HANDOFF_LOCAL_SESSION` | `AGENT_HANDOFF_LOCAL_SESSION` | `scripts/agent_handoff_router.py:315`；错误文本 `:320` |
| `PI_HANDOFF_URL` | `AGENT_HANDOFF_URL` | `handoff.py:1990` `run_send`；跳过提示 `:2022` |
| `PI_HANDOFF_VERIFY_URL` | `AGENT_HANDOFF_VERIFY_URL` | `handoff.py:1990` |
| `PI_HANDOFF_SECRET_FILE` | `AGENT_HANDOFF_SECRET_FILE` | `handoff.py:1570` `load_secret` 错误文本；`handoff.py:1990` |
| `PI_HANDOFF_MAX_ATTEMPTS` | `AGENT_HANDOFF_MAX_ATTEMPTS` | `handoff.py:833`、`:1897`、`:1975`、`:2734`，缺省字符串 `"60"` |
| `PI_HANDOFF_STALL_SECONDS` | `AGENT_HANDOFF_STALL_SECONDS` | `handoff.py:857`，缺省 `"3600"` |
| `PI_HANDOFF_MAX_RECORDS` | `AGENT_HANDOFF_MAX_RECORDS` | `handoff.py:1497` |
| `PI_HANDOFF_MAX_BYTES` | `AGENT_HANDOFF_MAX_BYTES` | `handoff.py:3167` tick `--max-bytes` 缺省 `"262144"` |

命中次数（排除 `review/`、`data/`、`tmp/`、`.tmp-ref/`）：`handoff.py` 17，`scripts/agent_handoff_router.py` 7，`scripts/agent_handoff_tick.sh` 3，`tests/test_handoff.py` 6，`tests/test_webhook_hardening.py` 6，以及 `docs/spec-webhook-hardening-20261112.md`、`AGENT-ROUTING-NOTES.md`、`AGENT-RECEIPT-ROUTER-PROMPT.txt`、若干修复简报。

### 1.2 事件、路由、请求号、密钥目录

| 项 | 旧 | 新 | 落点 |
|---|---|---|---|
| 事件名 | `pi_task_handoff` | `agent_task_handoff` | `handoff.py:40` `EVENT_TYPE`；`fake_receiver.py:61` 拒绝其他 `X-Event-Type` |
| 主路由 | `/webhooks/pi-task-handoff` | `/webhooks/agent-task-handoff` | `scripts/agent_handoff_tick.sh` 的 `WEBHOOK_URL` 缺省 |
| 校验路由 | `/webhooks/pi-task-handoff-verify` | `/webhooks/agent-task-handoff-verify` | 同上 `VERIFY_URL` |
| 请求号前缀 | `pi-handoff-` | `agent-handoff-` | `handoff.py:1659` `classify_response` 期望集合；`:1709` `send_one` 的 `X-Request-ID`（重试保持稳定）；`:1784` `send_poke` 带后缀；`:1939` `send_verify_one` |
| 密钥目录 | `.../pi-handoff/private.key` | `.../agent-handoff/private.key` | tick.sh 的 `SECRET_FILE` 缺省；不再写死机器路径 |

`handoff.py:1641` 与 `:1646` 的注释同步改掉旧前缀。

### 1.3 失败即报，不做兼容

- 进程环境里出现任一旧名：启动或该路径第一次读配置时退出非零，错误写明“已改名为 `AGENT_HANDOFF_*`，旧名不再被读取”。不要把旧值映射成新值。
- 测试断言：设置 `PI_HANDOFF_ROOT` 而不设 `AGENT_HANDOFF_ROOT` 时，数据根不得落到旧变量指向的目录。同样覆盖 URL、密钥文件、Hermes 路径。
- 旧事件名 `pi_task_handoff` 不再被发送，也不被接收端接受。

### 1.4 测试里必须一起改的旧令牌

- `tests/test_handoff.py:652` `PI_HANDOFF_MAX_RECORDS`
- `tests/test_handoff.py:817` `PI_HANDOFF_URL` / `PI_HANDOFF_SECRET_FILE`
- `tests/test_handoff.py:897-900` 事件名与 `X-Request-ID` 前缀
- `tests/test_handoff.py:989-990` 夹具里的路由名（同处的 cron 号见第 3 章，发布文档用占位符；测试可保留明显是假的 cron 号）
- `tests/test_handoff.py:1005-1006`、`:1067`、`:1102`
- `tests/test_handoff.py:1166-1221` 假接收端的事件名
- `tests/test_handoff.py:1728`、`:1737` `PI_HANDOFF_STALL_SECONDS`
- `tests/test_webhook_hardening.py:1024`、`:1028`、`:1033`、`:1038` `PI_HANDOFF_LOCAL_SESSION`
- `tests/test_agent_routing.py:450` `test_webhook_still_signed_four_fields`，旧名在 `:468`
- `fake_receiver.py:61`

路由器测试通过参数注入 `hermes_bin` / `hermes_python` / `hermes_agent_dir`（例：`tests/test_webhook_hardening.py:1417-1419`）。去掉机器缺省路径不得破坏这条注入缝。

### 1.5 现网网关迁移（发布阶段执行，本轮不做）

现网事实来自既有硬化规格，这里只记操作，不把真实账号写进新规格：

1. 在 Hermes 网关删掉订阅 `pi_task_handoff` 的旧路由（主路由与 `-verify` 路由）。
2. 新增订阅 `agent_task_handoff`，主路由 `/webhooks/agent-task-handoff`，校验路由 `/webhooks/agent-task-handoff-verify`，端口仍为网关端口。
3. 改写现网上的 tick 脚本：现网脚本不在本仓库。本仓库的 `scripts/agent_handoff_tick.sh` 是模板，改完后要覆盖到实际被 cron 调用的那份。
4. 重做一次冒烟：注册 → tick → 签名 POST → 路由器回执 → verify 双通道。旧路由应返回未找到。

`scripts/agent_handoff_router.py:4-6` 写明正式运行的是 Hermes 目录下的分阶副本。改本仓库不等于改那份副本；发布步骤要单独同步它，且不得在本仓库里写它的绝对路径。

---

## 2. 去硬编码：环境变量 → 配置文件 → 失败即报

三层，依次优先。任一层都不得静默落到开发者机器上的路径。

### 2.1 必须拿掉的写死路径

`scripts/agent_handoff_router.py`：

- `:35-36` 数据根缺省 `<HANDOFF_ROOT>/data`
- `:37-38` 脚本缺省 `<HANDOFF_ROOT>/handoff.py`
- `:39-41` Hermes 可执行文件缺省到当前用户的 `hermes.exe`
- `:43-45` Hermes agent 目录同样写死
- `:49-54` Python 候选：先 glob 当前用户的 `hermes/tools/python-3.*/python.exe`，再加 `sys.executable`
- `:365` `_resolve_hermes_bin` 在文件不存在时回退到字面量 `"hermes"`

`scripts/agent_handoff_tick.sh` 全文写死了：

- `HANDOFF_SCRIPT` 与 `DATA_ROOT` 指向 `<HANDOFF_ROOT>/`
- `WEBHOOK_URL` 缺省 `http://127.0.0.1:8644/webhooks/pi-task-handoff`
- `VERIFY_URL` 缺省对应的 `-verify`
- `SECRET_FILE` 缺省到当前用户的 `hermes/pi-handoff/private.key`

改法：这亘08 处的缺省改为“读环境变量，否则读配置文件，再否则退出并打印缺哪一项”。tick.sh 不再内嵌任何机器路径或真实用户目录。

### 2.2 协议缺省可以留

下列是协议数字，不是机器路径，保持现值并写进 README：

- 最大尝试 60（`handoff.py:833` 等）
- 停滞阈值 3600 秒（`handoff.py:857`）
- tick 最大字节 262144（`handoff.py:3167`）
- 路由重试退避 `RETRY_BACKOFF_SCHEDULE = (60, 120, 240, 480, 600)`（`scripts/agent_handoff_router.py:67`）
- 租约 120 秒、锁过期 300 秒、发送超时 45 秒、镜像超时 60 秒（同文件常量区）

路径类缺省不在此列。

### 2.3 配置文件

仓库根放 `config.example.toml`（或 `.env.example`，二选一，推荐 toml，与现有结构化配置习惯一致）。每一个 `AGENT_HANDOFF_*` 都有一行，带注释说明必填还是可选。真实配置文件名写进 `.gitignore`，不入库。

读取顺序：进程环境变量盖过配置文件；配置文件缺项则该项失败即报（协议数字除外，用第 2.2 节的缺省）。

### 2.4 Hermes 降为可选适配器

核心不依赖 Hermes，以下在零 Hermes 环境必须仍能跑通：注册、注销、tick、状态、领取、确认、延迟、释放、清理、归档、回执校验的领取/确认/查看/打开/清除、剪掉过期领取。对应 `handoff.py:3292` 的 `HANDLERS`。

发送是适配器：

- 已配置 `AGENT_HANDOFF_HERMES_BIN` 与 `AGENT_HANDOFF_HERMES_AGENT_DIR`：保持现行行为。telegram 走 `hermes send --to <origin> --subject <subject>`（`scripts/agent_handoff_router.py:278` `deliver_telegram`）；本地镜像走 `hermes` 的 Python 执行 `MIRROR_SNIPPET`，调 `gateway.mirror.mirror_to_session`（`:84-90`、`:323` `deliver_local`）。
- 未配置：不得调用 `hermes`，不得回退到字面量 `"hermes"`。telegram 与 local 分发失败即报并给出安装提示；事件本身已经 POST 到 webhook 或留在文件队列，这一步仍算成功。不要把“没有 Hermes”报成业务失败。

现行失败分类保留：`deliver_telegram` 里 rc==2 为永久失败（用法错误），超时与 rc==1 为瞬时失败；`deliver_local` 的失败全部视为瞬时。去掉 Hermes 之后，“未配置适配器”是配置错误，不进入这个重试梯子。

`route_event`（`:464`）只在 `route_state=ready` 且 `notify_origin` 时分发 telegram 或 local，否则确认为阻塞。这条不变。

`handoff.py` 文档字符串（`:2`）与 `build_parser` 描述（`:3105`）现在写“pi → Hermes”，改为 AgentWebhook；`--root` 帮助（`:3109`）、tick 的 `--url` / `--secret-file` 帮助（`:3172-3173`）改指新环境变量。`send` 子命令的 `--url` 仍是 `required=True`（`:3177`），它本身不读环境变量，不动。

源选择 `SOURCE_CHOICES = ("pi", "claude", "receipt", "exit", "cursor", "codex", "dsh")`（`handoff.py:50`）保留。`task_routing`（`:303`）不从 `session_id` 推断目的地；未知 source 阻塞且 `agent_name=None`（`:337-343`）；ready 时 `notify_telegram` 与 `notify_origin` 都为真。这些是路由合同，不随命名空间改动。

载荷只有 `schema_version`、`task_id`、`event_id`、`status`。不扩字段。

---

## 3. 去掉个人数据

新仓库只含代码、测试、文档。

### 3.1 不入库

`.gitignore` 覆盖：

- `data/`（约 60 个真实任务，本轮未读）
- `tmp/`（600 以上目录，本轮未读）
- `.tmp-ref/`（Hermes 源码副本，本轮未读，不上传）
- `review/`
- `__pycache__/`、`*.bak`
- `repair-receipt-*.json`、`runtime-repair-receipt-*.json`（根目录已有 `repair-receipt-781532de.json`、`repair-receipt-pi-2026-10-31.json`、`runtime-repair-receipt-81b689ba.json`）
- 真实配置文件与 `*.key`

### 3.2 占位符

发布文档、示例、README 里的账号与机器路径全部换成占位符：

- Telegram：`telegram:1234567890`。不要出现 `telegram:<telegram-chat-id>`（现在在 `docs/spec-webhook-hardening-20261112.md:114`）。
- cron 号：用 `cron:example` 或同等明显虚构的值。不要出现 `cron-job-verify`、`cron-job-tick`、`cron-job-gate`。
- 路径：用 `<data-root>`、`<hermes-bin>`、`<hermes-agent-dir>`。不要出现 `<HOME>` 或 `<HANDOFF_ROOT>`。

历史规格与修复简报（`docs/spec-webhook-hardening-20261112.md`、`IMPLEMENTATION-BRIEF.md`、`REPAIR-BRIEF.md`、`WEBHOOK-CONTRACT-REPAIR-RESULT.md`）不进新仓库；若要留摘要，先清掉上述标识再放。

若随仓库一起发布，以下提示词也要换令牌：`AGENT-ROUTING-NOTES.md`、`AGENT-RECEIPT-ROUTER-PROMPT.txt`、`AGENT-VERIFY-PROMPT.txt`。

### 3.3 发布前扫描

在准备入库的文件集上，下列必须为 0 命中：

- `<HOME>`、`<HOME>`
- `telegram:` 后跟 6 位以上数字（占位符 `1234567890` 除外，它只出现在示例里）
- 上文三个真实 cron 号
- `<HANDOFF_ROOT>`

---

## 4. README 与配置示例

仓库根新增 `README.md`，中文为主，英文段落可选。章节：

1. 是什么。任务从注册方交给接收方的签名 webhook，带回执校验。
2. 架构。一条链：注册 → tick → 签名发送 → webhook → 路由器 → 双通道校验。说明文件队列是事实来源，HTTP 只是传输。
3. 快速开始。复制配置示例，填数据根、主 URL、校验 URL、密钥文件，跑一次 `register` 再跑一次 `tick`。给出占位符命令，不给真实路径。
4. 可选的 Hermes 适配器。配了才有 telegram 与本地镜像；没配则事件停在 webhook 与文件队列，核心命令仍可用。
5. 排错。旧环境变量名导致的失败即报、缺密钥文件、未配置适配器、路由返回未找到。每条给出对应的错误文本大意，不粘贴日志。

`config.example.toml` 逐项对应第 1.1 节的 13 个变量。必填：数据根、主 URL、校验 URL、密钥文件。可选：两个 Hermes 路径、本地会话、四个协议数字、脚本路径。

---

## 5. 目录布局

保持扁平，不搬到 `src/`。

```
handoff.py
scripts/agent_handoff_router.py
scripts/agent_handoff_tick.sh
scripts/fake_receiver.py
tests/test_handoff.py
tests/test_agent_routing.py
tests/test_webhook_hardening.py
docs/
README.md
LICENSE
.gitignore
config.example.toml
```

`handoff.py` 留在仓库根，不改名。理由：`default_root()`（`handoff.py:86`）、测试与 tick 脚本都按“脚本所在目录”定位数据根与脚本路径。改文件名不带来命名空间收益，却要改这三处定位，本轮不做。

`LICENSE` 用 MIT。

`scripts/agent_handoff_router.py:181` 用 `importlib` 把 `handoff.py` 加载为模块名 `pi_handoff_lib`。这个模块名是内部的，随命名空间改为 `agent_handoff_lib`，避免发布物里留下 `pi_` 前缀。

声明方保留：`SCRIPT_CHANNEL_CLAIMER = "hermes-webhook"`（`handoff.py:68`）与 `ROUTER_CLAIMER = "hermes-webhook"`（`scripts/agent_handoff_router.py:63`）。它是通道声明方的协议值，不是机器路径。改它会让已有回执对不上，本次不改。若以后要改，单独开一轮，并同时改双方与全部回执夹具。

---

## 6. 行为合同与门禁

改名之后以下必须仍然全绿。本轮不跑。

### 6.1 命令

在干净环境（无 Hermes、无真实数据目录）执行：

```
python -m unittest tests.test_agent_routing tests.test_webhook_hardening
python tests/test_handoff.py
```

期望：93 个 unittest 通过；合成脚本打出全部 PASS（基线为 448 项，改名不得减少场景）。

`tests/test_handoff.py` 不是 unittest：`check()` 被调用 346 次，场景函数列表 `ALL` 在 `tests/test_handoff.py:1846-1848`，临时目录在 `:51`。不要把它改成 unittest。

### 6.2 必须保持的行为

- 签名头仍是四个：事件名、传送类型、请求号、签名本身。请求号在重试间保持稳定（`handoff.py:1709`）。
- `classify_response` 的期望集合改用新前缀，匹配规则不变（`handoff.py:1622`、`:1659`）。
- `cmd_clean` 仍拒绝清理等于 `default_root()` 实路径的目标（`handoff.py:3277`）。
- 未知 source 仍阻塞，不从 `session_id` 推断目的地。
- 路由器锁忙时 `main` 仍返回 0（`scripts/agent_handoff_router.py:571` 一带），打印 JSON 摘要。
- 注入式测试仍可以不启动真实 Hermes 跑通路由。

### 6.3 旧名扫描门禁

代码、测试、发布文档中，下列为 0 命中，除一个例外：

- `PI_HANDOFF_`
- `pi_task_handoff`
- `pi-task-handoff`
- `pi-handoff-`

例外：专门测试“旧名会失败即报”的那一例，断言里可以出现旧名，且必须断言它导致错误而不是被接受。

再加第 3.3 节的个人数据扫描。

### 6.4 CI

GitHub Actions 只跑第 6.1 节的两条命令。不安装 Hermes，不访问外网，不读秘密。失败即红。

---

## 7. 分阶段工单

每一阶段做完再进下一阶段。本轮一项都不执行。

### 7.1 Pi：改文件

- `handoff.py`：第 1 章的全部落点；文档字符串与帮助文本；旧名失败即报。
- `scripts/agent_handoff_router.py`：去掉六处写死路径；模块名改为 `agent_handoff_lib`；未配置 Hermes 时不调用它。
- `scripts/agent_handoff_tick.sh`：改成读环境变量或配置，缺项则退出。
- `scripts/fake_receiver.py:61`：新事件名。
- 三个测试文件：第 1.4 节列出的行，加上旧名失败即报的断言。
- 新增 `README.md`、`LICENSE`、`.gitignore`、`config.example.toml`。
- 若发布提示词：同步换令牌。

不改：载荷字段、源选择、路由合同、声明方常量、协议数字缺省、`cmd_clean` 的拒绝条件。

### 7.2 DSH：全量回归

- 跑第 6.1 节两条命令，记录通过数。
- 空环境（不设任何 `AGENT_HANDOFF_*`、不放配置文件）跑 `tick` 与路由器，确认失败即报且提示缺哪一项，不落到任何用户目录。
- 只设旧名、不设新名，确认被拒绝。
- 按第 6.3 节与第 3.3 节扫描，命中数为 0（那一例失败即报测试除外）。

### 7.3 Cursor：审查

只查三件事：

1. 还有没有写死的机器路径或用户目录。
2. 还有没有真实账号、cron 号、秘密路径。
3. 行为合同有没有漂：签名四头、请求号稳定、未知 source 阻塞、清理拒绝、路由只在 ready 时分发。

不改代码。有问题则退回 7.1。

### 7.4 Hermes：发布

顺序固定，不得跳：

1. 再扫一遍第 3.3 节与第 6.3 节。
2. 按第 1.5 节改现网网关：删旧路由、订新事件、覆盖现网 tick 脚本、重做冒烟。
3. 同步分阶副本到 Hermes 脚本目录（路径以那台机器的实际安装为准，不写进仓库）。
4. 在准备发布的副本上 `git init`，只加第 5 章列出的路径。
5. 提交。
6. 推送到 `git@github.com:xiao-hans/AgentWebhook.git`。

本工作区的 `data/`、`tmp/`、`.tmp-ref/`、回执文件与历史简报不进这一步。

---

## 本轮边界

只写本文件。不改 `handoff.py`、脚本、测试。不跑 git。不读 `data/` 与 `.tmp-ref/`。不把那两处的内容写进本文。
