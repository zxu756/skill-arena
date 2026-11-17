# SkillArena

**[English](README.md)** | **简体中文**

**受控 Git Worktree 与独立 `HOME` 隔离的编程智能体及 `SKILL.md` A/B 基准评测框架**：提供逐试次独立临时 `HOME` 隔离、基于 `agent-relay`（`relay.sh`）的统一无头智能体调度、基于 `agent-xray` 的流式 HTTP/SSE 与 OpenAI Responses 用量监听、Newcombe-Wilson 95% 置信区间统计检验（差异不显著时直接判定 `看不出差别` / `Inconclusive`）、干净检出确定性单测与 Hallmark 前端工艺判分、受保护测试 SHA-256 防篡改校验，以及支持 **中文 | EN** 实时切换的双轨轨迹与代码补丁回放工作台。

> **版本状态说明**：最新已发布标签版本为 `v0.3.0`。下文中标注 **`v0.3.0` 起** 的入口与行为——包括逐试次独立 `HOME` 隔离（`--pass-env`、`--copy-auth`、`--host-home`、`--allow-unisolated-host-home`）、`agent-relay`（`relay.sh`）调度、单一路径 `agent-xray` 流量计量（流式 TTFT 与 OpenAI `/v1/responses`）、Newcombe-Wilson 95% 置信区间（`看不出差别` / `Inconclusive`）、`--cli simulate`（替代 `--cli mock`）、干净检出防作弊判分、脱离后台进程残留判定、`pytest` 启动前自检与 `./Scripts/test.sh`——均从 `v0.3.0` 起提供（`v0.2.0` 没有）。在用户授权执行付费 `SKILL_ARENA_REAL_AB=1` 全量实测前，仓库不展示任何模拟排行榜或截图。

## 当前已实现能力

1. **逐试次独立临时 `HOME` 与子进程环境变量白名单隔离**（*`v0.3.0` 起*） - 每一次 `(任务, 参赛组别, 重复轮次)` 均在独立沙盒目录中运行，子进程环境变量改用严格系统白名单构造（**默认不透传任何一家的宿主 API key 环境变量**，仅当用户通过 `--pass-env <VAR>` 显式点名时才传入被点名的变量；阻断 `SSH_AUTH_SOCK`、`CLAUDE_CODE_*` 及宿主机无关会话令牌；未传 `--copy-auth` 时向隔离 `HOME` 复制 `0` 份 CLI 凭据文件），并配发全新创建的临时 `HOME` 与 `TMPDIR`（统一重指 `XDG_CONFIG_HOME`、`XDG_DATA_HOME`、`XDG_STATE_HOME`、`XDG_CACHE_HOME`、`CLAUDE_CONFIG_DIR`、`CODEX_HOME`、`VIBE_HOME`），试次结束后自动清理。**对照组（Control）** 全局与工作区均为 `0` 个技能（自动剥离仓库内预置技能目录）；**实验组（Treatment）** 在环境变量指向的目录中仅挂载本次指定的候选 `--skill`（注：宿主机模式仅隔离环境变量与工作区 worktree，边界见下方*当前限制*）。
2. **统一通过 `agent-relay`（`relay.sh`）调度真实智能体与实验全局 `--budget-usd` 累计预算上限**（*`v0.3.0` 起*） - 已移除内部散落的各家 CLI 参数拼装代码，`claude`、`codex`、`agy`、`vibe` 统一交由 `agent-relay`（`relay.sh <vendor> <workdir> <prompt-file>`）管理模型参数与 `RELAY_TIMEOUT` 超时，解析 `.relay/LOG.md` 中的状态与步骤花费，并在防作弊与 Diff 判分前清理 `.relay/`。`--budget-usd` 为**整场 A/B 实验的全局累计花费上限**：框架跨试次累计已发生费用，向 `relay.sh` 传递剩余可用预算 `RELAY_BUDGET_USD`，在下一试次预计会超出总预算前提前停止，并以专用非零退出码 **`3`**（`EXIT_BUDGET_EXCEEDED = 3`）退出（注：当 `--budget-usd` 小于单次试验花费时，第 1 次试验仍会先执行并在首试次超支后立即停止，详见下方*当前限制*）。
3. **单一路径 `agent-xray` 流式代理计量（无数据时显式标记 `未计量`）**（*`v0.3.0` 起*） - 已删除重复的自建缓冲代理，全部 HTTP/SSE 监听统一复用 `agent-xray` 的 `start_background_proxy(...)`（锁定 `agent-xray` 提交 `3cd9b14dcbae54505c56f982d13fae862d606e5a`（agent-xray v0.3.0）；若从无 `.git` 的拷贝或安装包加载导致无法读取提交号，会向 `stderr` 显式输出 `agent-xray 提交号没核对` 告警），支持流式首字延迟 `ttft_ms`、Anthropic `/v1/messages`、OpenAI `/v1/chat/completions` 与 OpenAI `/v1/responses`（流式 SSE 与非流式 JSON 的输入、缓存、输出 Token）。当智能体未走 Anthropic/OpenAI 协议（如 `agy`、`vibe`）或上游未返回 `usage` 时，统一显示 **`未计量`（`Unmetered`）**，绝不伪造为 `0` 或 `$0.0000`。
4. **统计不确定性（Newcombe-Wilson 95% 置信区间）与经济学指标**（*`v0.3.0` 起*） - 对两组 `Pass@1` 之差计算闭式 Newcombe-Wilson 95% 置信区间：当每组试验数少于 `5` 次或 95% 置信区间跨越 `0` 时，报告结论直接输出 **`看不出差别`（`Inconclusive`）** 且不排定名次；同时报告连续全绿一致性 `Pass^k`、摊销失败损耗的 `单次成功成本 Cost-per-Success ($)`、**成对胜率换算的 Elo（双组）** 以及 **首轮上下文税（Turn-1 Context Tax）**（定义为 *实验组与对照组首轮输入 Token 均值之差*）。
5. **干净检出判分与 SHA-256 防作弊校验**（*加固项，`v0.3.0` 起*） - 运行后逐字节比对受保护测试文件的 SHA-256 摘要，拦截智能体新增或修改 `tests/*`、`__init__.py` 及标准库/测试器同名覆盖模块（`unittest.py`、`pytest.py`、`sitecustomize.py`），拒绝 `0` 测试空跑，对超时或残留后台进程直接判失败，并在仅含合法源码变更的全新干净检出目录中执行判分。
6. **Git 历史提交任务提取（`extract-commit`）**（*加固项，`v0.3.0` 起*） - 从本地 Git 缺陷修复提交提取评测任务：提示词中不包含提交标题与正文说明，沙盒仅导出修复前父提交的干净文件树快照（不含修复提交的 `.git` 历史），并自动识别 `pytest` 与 `unittest`（无法唯一判定时要求显式传入 `--test-cmd`）。
7. **原生中英双语 CLI 与实时切换看板** - 支持 `--lang {auto,zh,en}`、`SKILL_ARENA_LANG` 与 `LC_ALL` / `LANG`（`zh*`）自动识别；使用 `--cli simulate` 时，终端、数据库与导出 HTML 均显著标注 `[SIMULATED / 模拟数据]`。

## 当前限制

- **各家真实 CLI 在隔离 `HOME` 下仅带凭据登录尚未实跑验证**：虽然独立临时 `HOME` 与最小凭据只读复制（`--copy-auth`：`.claude/.credentials.json`、`.codex/auth.json`、`.vibe/.env`）已实现并通过替身 CLI + 真实 `relay.sh` 端到端验证，但真实付费 `claude`、`codex`、`agy`、`vibe` 在替换 `HOME` 后能否仅凭复制凭据完成登录与执行**本轮未实跑（`没跑`）**（需用户授权调用真实外部 CLI）；内置 8 题的真实付费智能体全量 A/B（`SKILL_ARENA_REAL_AB=1`）默认在离线验收中跳过（`没跑`），以免产生未授权账单。
- **宿主机模式仅提供环境变量与工作区 worktree 层面的隔离（`KR4`、`KR6`）**：默认情况下，SkillArena 以严格系统白名单构造被测智能体的子进程环境变量（重指独立的 `HOME`、`TMPDIR`、`XDG_*`、`CLAUDE_CONFIG_DIR`、`CODEX_HOME`、`VIBE_HOME`，阻断 `SSH_AUTH_SOCK`、`CLAUDE_CODE_*` 及会话令牌），**默认不透传任何一家厂商的宿主 API key 环境变量**（如 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`MISTRAL_API_KEY`、`GOOGLE_API_KEY`、`GEMINI_API_KEY` 等，仅当用户通过 `--pass-env <VAR>` 显式点名时才传入被点名的变量），且未传 `--copy-auth` 时向隔离 `HOME` 复制 `0` 份宿主 CLI 登录凭据文件。但宿主机模式没有创建独立的操作系统用户或文件系统命名空间：被测智能体仍以当前 OS 用户身份运行，`pwd.getpwuid(os.getuid()).pw_dir` 仍返回真实家目录，进程主动读写工作区以外（outside the worktree）的路径依然能够成功；文件系统与操作系统级强隔离需在 `AgentVM` 中运行。
- **预算截断专用退出码（`3`）与首试次超支边界（`KR2`、`KR8`）**：当 `--budget-usd` 在全部计划试次完成前触发截断（或累计花费超出 `--budget-usd`）时，`skill-arena run` 会保存已完成试次、输出对照报告，并以专用非零退出码 **`3`**（`EXIT_BUDGET_EXCEEDED = 3`）退出。由于累计花费只能在试次之间核对，当 `--budget-usd` 小于单次试验的实际花费时，第 `1` 次试验仍会先执行完毕后才能检出累计超支——因此第 `1` 次试验可能会超出 `--budget-usd`，随后框架在第 `2` 次试验启动前立即停止并返回退出码 `3`。
- **关闭 `HOME` 隔离需显式传入风险确认参数（`--host-home`，`K2`）**：默认启用逐试次独立 `HOME` 隔离；若显式传入 `--host-home` 要求在真实宿主 `HOME` 下运行，必须同时传入 `--allow-unisolated-host-home`，否则直接拒绝启动。
- **CLI 与代理协议覆盖范围（`K7`–`K9`）**：真实 CLI 统一通过 `agent-relay` 调度（支持 `claude`、`codex`、`agy`、`vibe`，已移除独立 `opencode` 拼装入口）。`agent-xray` 监听 Anthropic（`/v1/messages`）与 OpenAI（`/v1/chat/completions`、`/v1/responses`）协议；不走上述 HTTP 接口的 CLI（如 `agy`、`vibe`）在 Token 列显示 `未计量`，同时保留 `relay.sh` 记录的步骤状态与花费元数据。
- **macOS `SIP` 系统二进制与清空环境变量的脱离守护进程边界（`K13`、`K16`）**：每次试次会注入唯一环境变量 `SKILL_ARENA_TRIAL_TOKEN`，结束时联合继承 `flock` 文件锁、试次沙盒 `cwd` 与打开文件（`lsof +D`）扫描以及 `ps eww` 检索强杀逃逸进程，在试次结果 `leaked_processes` 中记录残留进程并将该试次判失败。但由于 macOS 缺乏非特权 PID 命名空间/`cgroup`，且对当前用户全局进程做试次前后差分会误伤宿主机上的并发任务，若进程脱离会话（`setsid` / 双 `fork`）、切出沙盒目录（`chdir`）并关闭全部工作区与锁文件描述符后，同时满足以下任一情形：（a）执行受系统完整性保护（`SIP`）保护的 Apple 平台二进制（如 `/bin/sleep`、`/bin/sh`，其对普通权限调用方的 `ps eww` 隐藏环境变量），或（b）主动清空或移除环境变量中的 `SKILL_ARENA_TRIAL_TOKEN`（如 `execve(..., env={})`、`os.environ.pop` 或 `env -i`），宿主机模式下**不保证**能检测或回收该守护进程（强隔离需依赖虚拟机）。
- **`extract-commit` 提示词中的实现文件名与 `repo_path`**：`extract-commit` 虽然剥离了提交标题/正文并导出了独立文件树快照，但当前生成的提示词仍列出了该提交修改的实现文件名，且 `TaskSpec.repo_path` 指向磁盘上的源仓库路径（宿主机进程若主动越出工作目录按该路径读取源仓库仍可访问历史）。
- **Newcombe-Wilson 置信区间在 `--reps > 1` 下的独立样本假设（`KR5`）**：当前的闭式 95% Newcombe-Wilson 置信区间按每组总试次数 `题目数 * k_reps` 汇总，把同一道题在 `--reps > 1` 下的多次重复运行当作相互独立的伯努利独立样本（independent Bernoulli trials）处理；由于同一题目的重复试次之间存在相关性，当 `--reps > 1` 时计算出的名义 95% 置信区间会比按题目聚类的区间偏窄。
- **合成模拟器边界（`K3`、`KR5`，`v0.3.0` 起）**：`--cli simulate` 仅用固定规则与合成公式离线自检评测管线，所有输出（包括最终统计结论行）强制携带 `[SIMULATED | 模拟]` 标记，且拒绝接收真实模型名称或 `--skill` 技能路径。

## 快速上手

环境要求：**Python 3.11+**、`git`，以及用于真实 CLI 模式的同级或已安装 `agent-xray` 与 `agent-relay`（对使用 `pytest` 的题目判分时需当前 `python3` 环境已安装 `pytest`）。注：下方 `--cli simulate`、`--pass-env`、`--copy-auth`、`--budget-usd` 与 `./Scripts/test.sh` 均为 **`v0.3.0` 起** 入口。

```bash
# 1. 查看内置 8 项校准基准任务（简体中文与英文输出）
uv run skill-arena --lang zh list-suites
uv run skill-arena --lang en list-suites

# 2. 运行离线合成管线自检（--cli simulate 的终端、数据库与 HTML 均带 SIMULATED 模拟数据标记）
uv run skill-arena run \
  --lang zh \
  --cli simulate \
  --suite concurrency \
  --reps 1 \
  --db .skill-arena/sim-check.sqlite \
  --html-out .skill-arena/sim-check.html

uv run skill-arena compare --lang zh --db .skill-arena/sim-check.sqlite
uv run skill-arena export-html --lang zh --db .skill-arena/sim-check.sqlite --out .skill-arena/sim-export.html
```

### 通过 `agent-relay` 与独立隔离 `HOME` 运行真实无头智能体（`v0.3.0` 起）

```bash
# 每个试次在独立临时 HOME 下通过 agent-relay (`relay.sh`) 与 agent-xray 执行：
uv run skill-arena run \
  --lang zh \
  --suite concurrency \
  --cli claude \
  --model claude-sonnet-4-5 \
  --skill /path/to/candidate-skill \
  --reps 3 \
  --budget-usd 2.0 \
  --copy-auth

# 启动本地交互式 Web Studio（排行榜 + 双轨轨迹与 Git Diff 同步回放）
uv run skill-arena studio --lang zh --db .skill-arena/arena.sqlite --port 8765
```

## 从自有 Git 历史提取回归评测任务

```bash
uv run skill-arena extract-commit \
  --lang zh \
  --repo /path/to/your/repo \
  --commit HEAD \
  --out tasks/my-regression.json
```

## 内置 8 项工程校准基准任务集（`builtin-8`）

| 任务 ID | 所属领域 | 核心工程不变量考核点 |
|---|---|---|
| `conc-01-token-bucket-race` | `concurrency`（并发控制） | 16 线程高并发争用下 `TokenBucket` 原子补充与防超卖 |
| `conc-02-async-flight-coalescer` | `concurrency`（并发控制） | 异步 `SingleFlight` 并发请求合并与上游异常防死锁恢复 |
| `ui-01-billing-ledger-slop` | `frontend_craft`（前端工艺） | 重构 AI 塑料感账单台账 HTML 至严格 4px 栅格与零 Hallmark 告警 |
| `ui-02-incident-status-board` | `frontend_craft`（前端工艺） | 构建支持 `tabular-nums` 等宽数字排版与零工艺告警的区域状态看板 |
| `refac-01-cron-expression-parser` | `refactor_test`（重构测试） | 5 段式 Cron 范围/步长/列表展开与严格越界异常校验 |
| `refac-02-jsonl-ring-compactor` | `refactor_test`（重构测试） | 记录数与字节数双上限下容忍损坏行的 JSONL 环形日志压缩 |
| `sec-01-dsn-credential-redactor` | `security_hygiene`（安全加固） | 结构化日志中的 PostgreSQL DSN 密码、`Bearer` 令牌与 `sk-*` 密钥脱敏 |
| `sec-02-zip-slip-tar-extractor` | `security_hygiene`（安全加固） | 阻断 Tar 归档解压中的 `../` Zip-Slip 路径穿越与符号链接逃逸 |

## 开发与分级测试验证（`v0.3.0` 起）

```bash
./Scripts/test.sh scope graders
./Scripts/test.sh scope sandbox
./Scripts/test.sh scope proxy
./Scripts/test.sh scope extractor
./Scripts/test.sh scope store
./Scripts/test.sh scope cli
./Scripts/test.sh milestone 0
./Scripts/test.sh milestone 1
```

## 开源许可

MIT License。详见 [LICENSE](LICENSE).
