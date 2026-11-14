# SkillArena

**[English](README.md)** | **简体中文**

**受控 Git Worktree 编程智能体与 `SKILL.md` A/B 基准评测框架**：提供逐轮独立沙盒检出、干净检出确定性单测与 Hallmark 前端工艺判分、受保护测试 SHA-256 防篡改校验、本地 HTTP 回环用量代理，以及支持 **中文 | EN** 实时切换的双轨轨迹与代码补丁回放工作台。

> **版本状态说明**：最新已发布标签版本为 `v0.2.0`。下文中标注 **未发布（`main` / `v0.2.0` 之后）** 的入口与行为——包括 `--cli simulate`（替代 `--cli mock`）、`--allow-unisolated-host-home`、干净检出防作弊判分、脱离后台进程残留判定、`pytest` 启动前自检、`未计量` 标记与 `./Scripts/test.sh`——均位于 `v0.2.0` 之后的当前开发分支。原演示截图已移除，待 `M1` 产出真实智能体 A/B 评测结果后再行补充。

## 当前已实现能力

1. **独立沙盒检出与干净目录判分**（*未发布，`v0.2.0` 之后*） - 每一次 `(任务, 参赛组别, 重复轮次)` 在独立沙盒目录中执行，配备进程组级超时强杀与脱离进程组的后台子孙进程回收；一旦发生超时或发现残留后台进程，该试次均直接判失败。判分在全新干净检出目录中进行，仅搬运允许的非测试源码变更：智能体新增或修改 `tests/*`、`__init__.py`、与标准库/测试器同名的模块（如 `unittest.py`、`pytest.py`、`sitecustomize.py`）或运行了 `0` 个测试均直接判失败，且对 `pytest` 题目会在开跑前自检 `pytest` 是否可用。
2. **工作区级 `SKILL.md` 挂载对照** - 在 **实验组（Treatment）** 工作区的 `.agents/skills/` 与 `.claude/skills/` 中挂载待测技能目录，**对照组（Control）** 工作区不挂载待测技能（关于宿主 `HOME` 全局技能可见性的说明见下方 **当前限制**）。
3. **本地回环用量监听（无数据时显式标记 `未计量`）**（*未发布，`v0.2.0` 之后*） - 对走本地代理的 `claude` 与 `codex` 非流式 `/v1/messages`、`/v1/chat/completions` 请求统计输入/缓存/输出 Token 与花费。对于暂未接入代理的 CLI（`opencode`、`vibe`、`agy`）或未返回 `usage` 的上游响应，统一记录并展示为 **`未计量`（`Unmetered`）**，绝不伪造为 `0` 或 `$0.0000`。
4. **透明的成对比较与经济学指标**（*指标更名，未发布，`v0.2.0` 之后*） - 报告经验单次通过率 `Pass@1`、多轮连续全绿一致性 `Pass^k`（要求单任务 `k/k` 轮全部通过）、摊销失败损耗的 `单次成功成本 Cost-per-Success ($)`、**成对胜率换算的 Elo（双组）**（以对照组 1500 分为基准的双组闭式换算值），以及 **首轮上下文税（Turn-1 Context Tax）**（定义为 *实验组与对照组首轮输入 Token 均值之差*）。
5. **SHA-256 防作弊完整性校验** - 运行后逐字节比对受保护测试文件的 SHA-256 摘要，拦截测试与运行器路径绕过，恢复被改动的测试文件并判定该轮违规失败。
6. **Git 历史提交任务提取（`extract-commit`）**（*加固项未发布，`v0.2.0` 之后*） - 从本地 Git 缺陷修复提交提取评测任务：提示词中不包含提交标题与正文说明，沙盒仅导出修复前父提交的干净文件树快照（不含修复提交的 `.git` 历史），并自动识别 `pytest` 与 `unittest`（无法唯一判定时要求显式传入 `--test-cmd`）。
7. **原生中英双语 CLI 与实时切换看板** - 支持 `--lang {auto,zh,en}`、`SKILL_ARENA_LANG` 与 `LC_ALL` / `LANG`（`zh*`）自动识别；使用 `--cli simulate` 时，终端、数据库与导出 HTML 均显著标注 `[SIMULATED / 模拟数据]`。

## 当前限制

- **对照组仍可见宿主 `HOME` 下的全局技能（`K1`，计划在 `M1` 隔离）**：虽然工作区内的 `.agents/skills/` 与 `.claude/skills/` 已按组隔离，但当前尚未替换子进程的 `HOME` 目录，两组仍可看到 `~/.claude/skills` 与 `~/.agents/skills` 中的宿主全局技能。按轮次隔离 `HOME` 与凭据目录将在 `M1` 实现。
- **运行真实智能体需显式传入风险确认参数（`K2`，未发布，`v0.2.0` 之后）**：由于当前在真实用户 `HOME` 下以跳过权限确认方式（`--dangerously-skip-permissions` / `--full-auto`）启动无头智能体，未传入 `--allow-unisolated-host-home` 时 `skill-arena run` 会直接拒绝执行。
- **macOS `SIP` 系统二进制与清空环境变量的脱离守护进程边界（`K13`、`K16`）**：每次试次会注入唯一环境变量 `SKILL_ARENA_TRIAL_TOKEN`，结束时联合继承 `flock` 文件锁、试次沙盒 `cwd` 与打开文件（`lsof +D`）扫描以及 `ps eww` 检索强杀逃逸进程，在试次结果 `leaked_processes` 中记录残留进程并将该试次判失败。但由于 macOS 缺乏非特权 PID 命名空间/`cgroup`，且对当前用户全局进程做试次前后差分会误伤宿主机上的并发任务，若进程脱离会话（`setsid` / 双 `fork`）、切出沙盒目录（`chdir`）并关闭全部工作区与锁文件描述符后，同时满足以下任一情形：（a）执行受系统完整性保护（`SIP`）保护的 Apple 平台二进制（如 `/bin/sleep`、`/bin/sh`，其对普通权限调用方的 `ps eww` 隐藏环境变量），或（b）主动清空或移除环境变量中的 `SKILL_ARENA_TRIAL_TOKEN`（如 `execve(..., env={})`、`os.environ.pop` 或 `env -i`），宿主机模式下**不保证**能检测或回收该守护进程（强隔离需依赖虚拟机）。
- **`extract-commit` 提示词中的实现文件名与 `repo_path`**：`extract-commit` 虽然剥离了提交标题/正文并导出了独立文件树快照，但当前生成的提示词仍列出了该提交修改的实现文件名，且 `TaskSpec.repo_path` 指向磁盘上的源仓库路径（未隔离宿主模式下智能体若主动按该路径访问源仓库仍可读到历史）。
- **计量代理覆盖范围（`K7`–`K9`，计划在 `M1` 统一）**：`opencode`、`vibe`、`agy` 尚未接入 `agent-relay` 统一计量，指标显示为 `未计量`；当前代理对流式响应采用整包缓冲转发（流式首字延迟与 OpenAI Responses 接口计量将在 `M1` 收敛）。
- **合成模拟器边界（`K3`，未发布，`v0.2.0` 之后）**：`--cli simulate` 仅用固定规则与合成公式离线自检评测管线，所有输出强制携带 `SIMULATED` 标记，且拒绝接收真实模型名称或 `--skill` 技能路径。

## 快速上手

环境要求：**Python 3.11+** 与 `git`（零第三方运行时依赖；对使用 `pytest` 的题目判分时需当前 `python3` 环境已安装 `pytest`）。注：下方 `--cli simulate` 与 `--allow-unisolated-host-home` 为 **未发布（`main` / `v0.2.0` 之后）** 入口。

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

### 运行真实无头编程智能体（`--allow-unisolated-host-home`，未发布，`v0.2.0` 之后）

```bash
# 需本机已安装对应 CLI，并显式确认知晓在宿主 HOME 下运行：
uv run skill-arena run \
  --lang zh \
  --suite concurrency \
  --cli claude \
  --model claude-sonnet-4-5 \
  --skill /path/to/candidate-skill \
  --reps 3 \
  --allow-unisolated-host-home

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

## 开发与分级测试验证（未发布，`v0.2.0` 之后）

```bash
./Scripts/test.sh scope graders
./Scripts/test.sh scope sandbox
./Scripts/test.sh scope proxy
./Scripts/test.sh scope extractor
./Scripts/test.sh scope store
./Scripts/test.sh scope cli
./Scripts/test.sh milestone 0
```

## 开源许可

MIT License。详见 [LICENSE](LICENSE).
