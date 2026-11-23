# AgentWebhook 通用化：落地状态与后续分工（Phase 2 之后）

依据：`docs/spec-agentwebhook-generalization-20261113.md`（唯一事实源）。
代码基线：本仓库工作区根目录（未初始化 git，发布动作属 spec §7.4）。
逐条改动的行号与自验输出见 `tmp/pi_gen_result_20261113.md`。

## 1. 已落地（spec 第 1、2 章 + 第 3、4 章交付物）

- **第 1 章 命名切换**：13 个 `PI_HANDOFF_*` 全部换成 `AGENT_HANDOFF_*`，事件名
  `agent_task_handoff`，路由 `agent-task-handoff(-verify)`，请求号前缀
  `agent-handoff-`，`classify_response` 期望集同步，`fake_receiver.py` 的事件名校验同步。
  旧名**不做兼容映射**：只要旧名已设而同名新项未设（环境或配置文件都算），
  任何子命令非零退出并提示对应新名；`handoff.py`、分发器、cron 包装三条入口都覆盖。
- **第 1.3 节 测试同步**：三份测试逐行改名，断言一条没删；新增合成场景
  `t13_renamed_settings()`，它遍历 `handoff.py` 的 `ENV_KEYS`（15 项）逐个验证旧名被拒、
  错误提示指向新名，并验证配置文件里的旧键同样被拒、分发器不会退回机器默认路径。
  `ENV_KEYS` 因此是配置面清单的唯一来源，样例文件与它同批校验。
- **第 2 章 去硬编码**：分发器的 6 处机器默认（数据根、脚本路径、hermes.exe、agent 目录、
  Python 候选 glob、`_resolve_hermes_bin` 的 `"hermes"` 兜底）全部删除；cron 包装
  `scripts/agent_handoff_tick.sh` 重写为环境变量 → 配置文件 → 非零退出并报缺哪个键，
  文件内不含机器路径。解析顺序在 `handoff.py` 与分发器各实现一次，cron 复用
  `handoff.py` 的解析结果，不在 shell 里读 TOML。
- **第 2.4 节 Hermes 变可选适配器**：`AGENT_HANDOFF_HERMES_BIN / _PYTHON / _AGENT_DIR`
  未配置时 telegram 与 local 通道按受阻语义记账并写明缺哪个键，核心 19 个子命令照常可用；
  rc 2 永久、超时/rc 1 瞬时的判定不变。
- **第 3 章 个人数据与发布集**：新增 `.gitignore`（`data/`、`tmp/`、`.tmp-ref/`、
  `review/`、`agentwebhook.toml`、`.env`、`receipt*.json`、`*.key`、缓存目录）。
- **第 4 章 样例与说明**：`config.example.toml` 与 `.env.example` 逐键注释必填/可选/缺失行为，
  值全为占位符；`README.md` 按五节结构（是什么/架构/快速开始/可选 Hermes 适配器/排错）；
  `LICENSE` 为 MIT。
- **第 5 章 结构**：仍是扁平布局，入口文件名 `handoff.py` 不改（spec 未决事项默认不改），
  内部模块名 `pi_handoff_lib` → `agent_handoff_lib`。

## 2. 待办分工

- **DSH（spec §7.2）回归**：干净环境跑 `python -m unittest tests.test_agent_routing
  tests.test_webhook_hardening` 与 `python tests/test_handoff.py`；另需验证空环境下
  `tick` 与分发器确实因缺配置而失败、只有旧名时被拒；并跑第 3 章文件集的个人数据扫描。
  本轮交付说明里的扫描结果可以直接复核。
- **Cursor（spec §7.3）复核三件事**：机器路径残留、真实账号/cron/密钥路径残留、契约漂移。
  明确不要改代码。需要它裁决的两点写在交付说明"偏差"一节：第 6.3 节字面扫描与
  第 1.3 节失败即报机制的冲突、测试里 telegram 语法夹具与第 3.3 节正则的冲突。
- **Hermes（spec §7.4）发布**：网关迁移（删旧路由、订阅 `agent_task_handoff`、覆盖 live
  cron 脚本、重新冒烟）只在这一段做；然后 `git init` 与 push。本轮不动 live cron、
  不动网关订阅、不执行任何 git 命令。

## 3. 本轮明确未做

- live cron 与网关订阅未迁移，因此真实投递在 §7.4 之前不会恢复：`handoff.py` 已按新
  事件名 `agent_task_handoff` 签名发出，而线上网关仍只订阅改名前的旧事件名，新路由也未建立。
- `data/`、`tmp/`、`review/`、根目录 `receipt*.json` 未改动；`data/` 的 mtime 变化来自
  live cron 每两分钟一次的心跳（见交付说明）。
- 未新增 `agentwebhook.py` 包目录，未拆 `src/`，未改任何路由与重试契约的语义。
