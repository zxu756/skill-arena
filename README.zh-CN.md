# SkillArena

**[English](README.md)** | **简体中文**

**受控 Git Worktree 与独立 `HOME` 隔离的编程智能体及 `SKILL.md` A/B 基准评测框架**：提供逐试次独立临时 `HOME` 隔离、基于 `agent-relay`（`relay.sh`）的统一无头智能体调度、基于 `agent-xray` 的流式 HTTP/SSE 与 OpenAI Responses 用量监听、Newcombe-Wilson 95% 置信区间统计检验（差异不显著时直接判定 `看不出差别` / `Inconclusive`）、干净检出确定性单测与 Hallmark 前端工艺判分、受保护测试 SHA-256 防篡改校验，以及支持 **中文 | EN** 实时切换的双轨轨迹与代码补丁回放工作台。

> **版本状态说明**：最新已发布标签版本为 `v0.3.0`。下文中标注 **`v0.3.0` 起** 的入口与行为——包括逐试次独立 `HOME` 隔离（`--pass-env`、`--copy-auth`、`--host-home`、`--allow-unisolated-host-home`）、`agent-relay`（`relay.sh`）调度、单一路径 `agent-xray` 流量计量（流式 TTFT 与 OpenAI `/v1/responses`）、Newcombe-Wilson 95% 置信区间（`看不出差别` / `Inconclusive`）、`--cli simulate`（替代 `--cli mock`）、干净检出防作弊判分、脱离后台进程残留判定、`pytest` 启动前自检与 `./Scripts/test.sh`——均从 `v0.3.0` 起提供（`v0.2.0` 没有）。在用户授权执行付费 `SKILL_ARENA_REAL_AB=1` 全量实测前，仓库不展示任何模拟排行榜或截图。

## 当前已实现能力

1. **逐试次独立临时 `HOME` 与子进程环境变量白名单隔离**（*`v0.3.0` 起*） - 每一次 `(任务, 参赛组别, 重复轮次)` 均在独立沙盒目录中运行，子进程环境变量改用严格系统白名单构造（**默认不透传任何一家的宿主 API key 环境变量**，仅当用户通过 `--pass-env <VAR>` 显式点名时才传入被点名的变量；阻断 `SSH_AUTH_SOCK`、`CLAUDE_CODE_*` 及宿主机无关会话令牌；未传 `--copy-auth` 时向隔离 `HOME` 复制 `0` 份 CLI 凭据文件），并配发全新创建的临时 `HOME` 与 `TMPDIR`（统一重指 `XDG_CONFIG_HOME`、`XDG_DATA_HOME`、`XDG_STATE_HOME`、`XDG_CACHE_HOME`、`CLAUDE_CONFIG_DIR`、`CODEX_HOME`、`VIBE_HOME`），试次结束后自动清理。**对照组（Control）** 全局与工作区均为 `0` 个技能（自动剥离仓库内预置技能目录）；**实验组（Treatment）** 在环境变量指向的目录中仅挂载本次指定的候选 `--skill`（注：宿主机模式仅隔离环境变量与工作区 worktree，边界见下方*当前限制*）。
2. **统一通过 `agent-relay`（`relay.sh`）调度真实智能体与实验全局 `--budget-usd` 累计预算上限**（*`v0.3.0` 起*） - 已移除内部散落的各家 CLI 参数拼装代码，`claude`、`codex`、`agy`、`vibe` 统一交由 `agent-relay`（`relay.sh <vendor> <workdir> <prompt-file>`）管理模型参数与 `RELAY_TIMEOUT` 超时，解析 `.relay/LOG.md` 中的状态与步骤花费，并在防作弊与 Diff 判分前清理 `.relay/`。`--budget-usd` 为**整场 A/B 实验的全局累计花费上限**：框架跨试次累计已发生费用，向 `relay.sh` 传递剩余可用预算 `RELAY_BUDGET_USD`，在下一试次预计会超出总预算前提前停止，并以专用非零退出码 **`3`**（`EXIT_BUDGET_EXCEEDED = 3`）退出（注：当 `--budget-usd` 小于单次试验花费时，第 1 次试验仍会先执行并在首试次超支后立即停止，详见下方*当前限制*）。
3. **单一路径 `agent-xray` 流式代理计量（无数据时显式标记 `未计量`）**（*`v0.3.0` 起*） - 已删除重复的自建缓冲代理，全部 HTTP/SSE 监听统一复用 `agent-xray` 的 `start_background_proxy(...)`（锁定 `agent-xray` 提交 `3cd9b14dcbae54505c56f982d13fae862d606e5a`（agent-xray v0.3.0）；若从无 `.git` 的拷贝或安装包加载导致无法读取提交号，会向 `stderr` 显式输出 `agent-xray 提交号没核对` 告警），支持流式首字延迟 `ttft_ms`、Anthropic `/v1/messages`、OpenAI `/v1/chat/completions` 与 OpenAI `/v1/responses`（流式 SSE 与非流式 JSON 的输入、缓存、输出 Token）。当智能体未走 Anthropic/OpenAI 协议（如 `agy`、`vibe`）或上游未返回 `usage` 时，统一显示 **`未计量`（`Unmetered`）**，绝不伪造为 `0` 或 `$0.0000`。
4. **统计不确定性（按题配对的 95% 置信区间）与经济学指标**（*`v0.3.0` 起；按题配对和超时判无效在 `v0.3.0` 之后加入*） - 对两组 `Pass@1` 之差计算 95% 置信区间，**以题目为分析单位**（Miller 2024，*Adding Error Bars to Evals*，arXiv 2411.00640）：每道题的 `k` 次重复先折成该题的通过率，`d_i = p_T,i - p_C,i`，标准误取题间估计 `sqrt(Σ(d_i - d̄)² / (n(n-1)))` 与题内 Agresti-Caffo 估计中较大的一个，临界值用自由度 `n - 1` 的 t 分布。报告同时给出 80% 把握下的**最小可检测差别**，题目少时事先就说明这次实验最多能看出多大的差别。当每组试验数少于 `5` 次或 95% 置信区间跨越 `0` 时，报告结论直接输出 **`看不出差别`（`Inconclusive`）** 且不排定名次。任一组超过 **10%** 的试验被时限打断时，报告 **`实验无效`（`Invalid run`）**、不排定名次：先花时间做计划或写测试的 skill，否则会只因被提前掐断而显得更差。每题默认 `timeout_sec` 为 **900 秒**（原为 90 秒；内置题目 agent 大约需要 2 分钟）。同时报告连续全绿一致性 `Pass^k`、摊销失败损耗的 `单次成功成本 Cost-per-Success ($)`、**成对胜率换算的 Elo（双组）** 以及 **首轮上下文税（Turn-1 Context Tax）**（定义为 *实验组与对照组首轮输入 Token 均值之差*）。
5. **干净检出判分与 SHA-256 防作弊校验**（*加固项，`v0.3.0` 起*） - 运行后逐字节比对受保护测试文件的 SHA-256 摘要，拦截智能体修改或删除 `tests/` 下的文件、在 `tests/` 下新增辅助模块、`conftest.py`、`__init__.py` 及标准库/测试器同名覆盖模块（`unittest.py`、`pytest.py`、`sitecustomize.py`）；从下一版起，智能体可以**新增**自己写的 `tests/**/test_*.py` 测试模块（例如在测试驱动开发类 skill 下），不算作弊，这些文件也不会被复制进判分用的干净检出目录，拒绝 `0` 测试空跑，对超时或残留后台进程直接判失败，并在仅含合法源码变更的全新干净检出目录中执行判分。
6. **Git 历史提交任务提取（`extract-commit`）**（*加固项，`v0.3.0` 起*） - 从本地 Git 缺陷修复提交提取评测任务：提示词中不包含提交标题与正文说明，沙盒仅导出修复前父提交的干净文件树快照（不含修复提交的 `.git` 历史），并自动识别 `pytest` 与 `unittest`（无法唯一判定时要求显式传入 `--test-cmd`）。
7. **原生中英双语 CLI 与实时切换看板** - 支持 `--lang {auto,zh,en}`、`SKILL_ARENA_LANG` 与 `LC_ALL` / `LANG`（`zh*`）自动识别；使用 `--cli simulate` 时，终端、数据库与导出 HTML 均显著标注 `[SIMULATED / 模拟数据]`。
8. **为大规模 A/B 设计的实验功能**（*`v0.3.0` 之后加入*） - `pool-export` 把题库（`task.json`、`start/`、`tests/`、`reference/`、`mutants/<名字>/`）转成 TaskSpec JSON，分**可见测试**版（测试放进工作区并受保护）和**隐藏测试**版（工作区里没有测试，判分时才把测试覆盖进干净检出目录；agent 在隐藏测试路径上写的文件会被覆盖，不算作弊），题号加 `@visible` / `@hidden` 后缀。`--arm 名字=路径[,路径...]`（可重复）让多个 skill 分别和始终存在的无技能对照组比较：每组比较给出按题配对的置信区间、按题差值的符号翻转置换检验 p 值（非零差值的题不超过 16 道时精确枚举，否则固定种子抽样 20000 次）和 Holm 校正后的 p 值。试次按随机分块顺序运行（每个 `(题目, 轮次)` 块包含所有组；`--seed`），用 `--workers N` 个线程并行，并记录启动序号和开始、结束时间。每个判分结果带失败原因（`passed`、`wrong`、`interrupted`、`anti_cheat`、`craft`、`infra`、`grader_error`）和完整判分输出（最多 200 KB）；Hallmark 的 `sloplint.mjs` 已随包附带（MIT 许可，见 `graders/vendor/hallmark/NOTICE.txt`；可用 `SKILL_ARENA_SLOPLINT` 换成别的副本），判分结果记录用的是哪一份。报告按组新增：时限曲线 `Pass@T`（通过**且**在 T = 60/120/180/300/600/900 秒和时限内结束）、agent 自写测试的变异得分（在参考实现和每个变异体上运行）、行为指标（跑测试次数、是否先写测试、是否读了 `SKILL.md`；只有 Mistral `vibe` 经 `agent-relay` 运行时留有工具调用记录，其他 CLI 显示 n/a），以及分类别（和分可见性）的按题配对差值。
9. **仓库与自报确定性验收器（`skill-arena verify`）**（*`v0.3.0` 之后加入*） - 在单次 `mkdtemp` 临时根目录（含 `repo/`、`home/`、`tmp/`、`bin/`）中通过 `git clone --no-local` 创建干净克隆，所有子进程套入 macOS `sandbox-exec`（内核级阻断外网连接并拦写真实家目录，仅放行共用的 `~/.cache/uv`），自动探测构建与基线测试（`0` 条测试判失败）、逐条原样回放根目录 `README` 命令、按固定顺序执行 Python AST / Swift 行级单点破坏性变异反证，并对里程碑自报（`--self-report`）核查绝对化用语是否有同条命令与输出证据；同一仓库同一提交连续运行两次，去掉 `timing_meta` 后的 JSON 报告逐字节相同。

## 当前限制

- **各家真实 CLI 在隔离 `HOME` 下仅带凭据登录尚未实跑验证**：虽然独立临时 `HOME` 与最小凭据只读复制（`--copy-auth`：`.claude/.credentials.json`、`.codex/auth.json`、`.vibe/.env`）已实现并通过替身 CLI + 真实 `relay.sh` 端到端验证，但真实付费 `claude`、`codex`、`agy`、`vibe` 在替换 `HOME` 后能否仅凭复制凭据完成登录与执行**本轮未实跑（`没跑`）**（需用户授权调用真实外部 CLI）；内置 8 题的真实付费智能体全量 A/B（`SKILL_ARENA_REAL_AB=1`）默认在离线验收中跳过（`没跑`），以免产生未授权账单。
- **宿主机模式仅提供环境变量与工作区 worktree 层面的隔离（`KR4`、`KR6`）**：默认情况下，SkillArena 以严格系统白名单构造被测智能体的子进程环境变量（重指独立的 `HOME`、`TMPDIR`、`XDG_*`、`CLAUDE_CONFIG_DIR`、`CODEX_HOME`、`VIBE_HOME`，阻断 `SSH_AUTH_SOCK`、`CLAUDE_CODE_*` 及会话令牌），**默认不透传任何一家厂商的宿主 API key 环境变量**（如 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`MISTRAL_API_KEY`、`GOOGLE_API_KEY`、`GEMINI_API_KEY` 等，仅当用户通过 `--pass-env <VAR>` 显式点名时才传入被点名的变量），且未传 `--copy-auth` 时向隔离 `HOME` 复制 `0` 份宿主 CLI 登录凭据文件。但宿主机模式没有创建独立的操作系统用户或文件系统命名空间：被测智能体仍以当前 OS 用户身份运行，`pwd.getpwuid(os.getuid()).pw_dir` 仍返回真实家目录，进程主动读写工作区以外（outside the worktree）的路径依然能够成功；文件系统与操作系统级强隔离需在 `AgentVM` 中运行。
- **预算截断专用退出码（`3`）与首试次超支边界（`KR2`、`KR8`）**：当 `--budget-usd` 在全部计划试次完成前触发截断（或累计花费超出 `--budget-usd`）时，`skill-arena run` 会保存已完成试次、输出对照报告，并以专用非零退出码 **`3`**（`EXIT_BUDGET_EXCEEDED = 3`）退出。由于累计花费只能在试次之间核对，当 `--budget-usd` 小于单次试验的实际花费时，第 `1` 次试验仍会先执行完毕后才能检出累计超支——因此第 `1` 次试验可能会超出 `--budget-usd`，随后框架在第 `2` 次试验启动前立即停止并返回退出码 `3`。
- **关闭 `HOME` 隔离需显式传入风险确认参数（`--host-home`，`K2`）**：默认启用逐试次独立 `HOME` 隔离；若显式传入 `--host-home` 要求在真实宿主 `HOME` 下运行，必须同时传入 `--allow-unisolated-host-home`，否则直接拒绝启动。
- **CLI 与代理协议覆盖范围（`K7`–`K9`）**：真实 CLI 统一通过 `agent-relay` 调度（支持 `claude`、`codex`、`agy`、`vibe`，已移除独立 `opencode` 拼装入口）。`agent-xray` 监听 Anthropic（`/v1/messages`）与 OpenAI（`/v1/chat/completions`、`/v1/responses`）协议；不走上述 HTTP 接口的 CLI（如 `agy`、`vibe`）在 Token 列显示 `未计量`，同时保留 `relay.sh` 记录的步骤状态与花费元数据。
- **macOS `SIP` 系统二进制与清空环境变量的脱离守护进程边界（`K13`、`K16`）**：每次试次会注入唯一环境变量 `SKILL_ARENA_TRIAL_TOKEN`，结束时联合继承 `flock` 文件锁、试次沙盒 `cwd` 与打开文件（`lsof +D`）扫描以及 `ps eww` 检索强杀逃逸进程，在试次结果 `leaked_processes` 中记录残留进程并将该试次判失败。但由于 macOS 缺乏非特权 PID 命名空间/`cgroup`，且对当前用户全局进程做试次前后差分会误伤宿主机上的并发任务，若进程脱离会话（`setsid` / 双 `fork`）、切出沙盒目录（`chdir`）并关闭全部工作区与锁文件描述符后，同时满足以下任一情形：（a）执行受系统完整性保护（`SIP`）保护的 Apple 平台二进制（如 `/bin/sleep`、`/bin/sh`，其对普通权限调用方的 `ps eww` 隐藏环境变量），或（b）主动清空或移除环境变量中的 `SKILL_ARENA_TRIAL_TOKEN`（如 `execve(..., env={})`、`os.environ.pop` 或 `env -i`），宿主机模式下**不保证**能检测或回收该守护进程（强隔离需依赖虚拟机）。
- **`extract-commit` 提示词中的实现文件名与 `repo_path`**：`extract-commit` 虽然剥离了提交标题/正文并导出了独立文件树快照，但当前生成的提示词仍列出了该提交修改的实现文件名，且 `TaskSpec.repo_path` 指向磁盘上的源仓库路径（宿主机进程若主动越出工作目录按该路径读取源仓库仍可访问历史）。
- **重复运行不是独立样本（`KR5`）**：`v0.3.0` 及以前用的是 Newcombe-Wilson 置信区间，按每组总试次数 `题目数 * k_reps` 汇总，把同一道题在 `--reps > 1` 下的多次重复当作独立样本，算出的区间偏窄。现在改为上面所说的按题计算。题目少时区间会故意很宽：8 道题 × 3 遍只能看出大约 30 个百分点的差别；增加题目比增加 `--reps` 有效得多。
- **并行与预算**：用 `--workers N` 时，头 `N` 个试次在还不知道任何花费时就会启动，所以最多可能比 `--budget-usd` 多花 `N` 个试次；之后每次检查都会预留正在运行的试次的花费。`agent-relay` 会给 `vibe` 传 `--time-limit`，这是客户端到点打断，不会告诉模型（vibe 3.1.0 的提示词和会话记录里都没有这个时限）。
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

## 用题库跑一次设计好的实验

```bash
# 导出题库里每道题的两个版本（题库格式见其 FORMAT.md）
uv run skill-arena pool-export \
  --pool /path/to/pool \
  --variant visible \
  --variant hidden \
  --timeout-sec 900 \
  --out suites/pool-ab.json

# 两个 skill 分别对比无技能对照组，随机分块顺序，同时跑 4 个试次
uv run skill-arena run \
  --lang zh \
  --suite suites/pool-ab.json \
  --cli vibe \
  --model deepseek-v4-1-flash-1017 \
  --arm tdd=/path/to/tdd-skill \
  --arm review=/path/to/review-skill \
  --reps 3 \
  --seed 20261118 \
  --workers 4 \
  --budget-usd 30 \
  --html-out .skill-arena/pool-ab.html
```

## 从自有 Git 历史提取回归评测任务

```bash
uv run skill-arena extract-commit \
  --lang zh \
  --repo /path/to/your/repo \
  --commit HEAD \
  --out tasks/my-regression.json
```

## 验收器（skill-arena verify）

`skill-arena verify` 在干净隔离的临时克隆中对目标 Git 仓库及里程碑自报做确定性核查，不采信口头自述：

- **命令与参数**：
  `uv run skill-arena verify [--lang {auto,zh,en}] [--repo <路径>] [--ref <提交|标签|分支>] [--build-cmd <命令>] [--test-cmd <命令>] [--mutate | --no-mutate] [--max-mutations <N>] [--self-report <md>] [--skip-cmd <模式>] [--timeout-sec <秒>] [--md-out <路径>] [--json-out <路径>]`。仅传 `--self-report` 不传 `--repo` 时单独核查自报文档。
- **三类仓库检查与自报用语检查**：
  1. **干净拷贝构建与基线测试**：单次 `mkdtemp` 根目录下建立 `repo/`、`home/`、`tmp/`、`bin/`，以 `git clone --no-local` 克隆指定提交并禁用 `origin` 推送；运行前后各取一次源仓库只读指纹（`HEAD`、`.git/index`、`git status --porcelain=v1 --ignored=matching`、`show-ref`），按项目文件自动探测构建与测试（`uv.lock` -> `uv sync --offline --frozen` + `uv run pytest`；`Package.swift` -> `swift build --disable-sandbox` + `swift test --disable-sandbox`），运行 `0` 条测试判失败，收尾 `finally` 修复只读权限后删除临时目录并回收残留后台进程。
  2. **根级 `README` 命令逐条回放**：只读取仓库根目录直属 `README*.md`（不递归子目录夹具），双语 `README.md` / `README.zh-CN.md` 并存时校验命令序列一致；可离线执行的命令原样执行并记录退出码与摘要；需网络、密钥、交互会话、常驻服务、Docker、占位符、写用户目录或发版推送的命令标为 `UNVERIFIABLE`（无法验证）并写明原因；未声明前置条件而在干净拷贝中失败的命令判 `FAIL`（可通过 `--skip-cmd` 显式豁免并在报告列出）。
  3. **单点破坏性变异反证**：按 `(路径, 行, 列)` 固定顺序在非测试源码中生成单点变异（Python `ast` 算子与先通过编译校验的 Swift 行级算子；默认 `1` 处，最多 `--max-mutations` 处），仅当测试以真实断言失败退出才计为 `KILLED`（语法错误、导入错误、收集错误与编译失败均归为 `INVALID`，不算杀死）；变异存活判 `FAIL`，结束后还原原字节并校验测试恢复通过且 `git status --porcelain` 为空。
  4. **自报用语检查（`--self-report`）**：扫描 10 个绝对化词表（`彻底`、`全部`、`零`、`已修`、`绝不`、`无任何`、`100%`、`never`、`zero`、`completely`），要求同一逻辑条目同时具备可执行命令与输出/退出码证据（`SUPPORTED`），否则标为 `UNSUPPORTED`（无证据，退出码 `1`），并内置 4 条确定性去噪规则（排除 `非零` / `non-zero`、章节标题与 `:` 结尾的表格/列表导语、`不在范围` / `没验证` 小节免责声明、以及引号内被删除/撤回的旧文案）。
- **断网与家目录隔离规则（含 `~/.cache/uv` 例外）**：
  验收器启动的每个子进程均包裹在 macOS `/usr/bin/sandbox-exec` 与白名单离线环境中（`HOME`、`TMPDIR`、`XDG_*` 全部指入临时目录，`UV_OFFLINE=1`、`PIP_NO_INDEX=1`，禁用 `http`/`https` Git 传输）。Seatbelt 在内核层拦截外网连接（`deny network*`，仅放行本机 `localhost` 回环与 Unix 域套接字），并拦截对真实账户家目录（`pwd.getpwuid(os.getuid()).pw_dir` 及 `~/Library`）的一切写入——**唯一例外是共用 `<USER_HOME>/.cache/uv`**（避免每次离线 `uv sync` 拷贝十余 GB 缓存），运行前后新增的缓存条目数会统计写入报告（`timing_meta.uv_cache_new_entries`）。
- **比较前的确定性规整规则**：
  易变的真实耗时与 `~/.cache/uv` 缓存条目增量单独归入顶层 `timing_meta`；其余报告字段由 `PathScrubber` 与 `summarize_command_output` 剥离 ANSI 转义序列、脱敏临时目录与源仓/用户路径（`<TMP_REPO>`、`<TMP_HOME>`、`<TMPDIR>`、`<TMP_ROOT>`、`<REPO_ROOT>`、`<USER_HOME>`）、将命令输出摘要中的内联耗时（`in 9.87s`、`in 102.31s (0:01:42)`、`in 9.87 seconds`、`Ran N tests in 0.003s`、`finished in 0.01s`、`after 0.004 seconds`、`in 0.01 (0.02) seconds`）替换为 `<TIME>`，并在摘要长度截断前把 `pytest` 按行宽补齐等号的最终汇总行统一规整为两侧各三个等号的固定形式（`=== <汇总> in <TIME> ===`），同时保持测试名、点号进度行、分节标题与失败报错信息原样不动。
- **退出码**：
  全部已启用检查项通过返回 `0`；任一验证项失败（构建/测试失败、`0` 条测试、未写前置条件的 `README` 命令失败、变异存活、残留后台进程、自报存在无证据绝对词）返回 `1`；参数非法、自报文件不存在、目标不是 Git 仓库或源仓库前后指纹不一致返回 `2`。

```bash
# 1. 在隔离离线克隆中验收 Git 仓库（构建、测试、根级 README 回放与单点变异反证）
uv run skill-arena verify \
  --lang zh \
  --repo /path/to/your/repo \
  --md-out .skill-arena/verify-report.md \
  --json-out .skill-arena/verify-report.json

# 2. 离线核查里程碑自报中的无证据绝对化用语（使用仓库自带脱敏样本）
uv run skill-arena verify \
  --lang zh \
  --self-report tests/fixtures/verify_sample_self_report.md \
  --json-out .skill-arena/self-report-audit.json
```

### `diy-harness` 实测验收报告摘要（2026-11-21）

测量方法与日期：2026-11-21 由 `./Scripts/acceptance/m2-acceptance.sh`（脱敏记录见 `reports/m2-acceptance.txt`）对 `<USER_HOME>/Projects/diy-harness`（提交 `52207fd803d4`）连续两次执行 `uv run skill-arena --lang zh verify --repo <USER_HOME>/Projects/diy-harness`：

- **退出码与两次运行确定性**：两次均返回退出码 `1`（结论 `FAIL`），去掉 `timing_meta` 后的两份 JSON 逐字节相同（`36,031` 字节，`sha256=db7a3bd1ad5a6420...`），源仓库只读指纹前后一致，临时目录已删除，残留进程数为 `0`。
- **构建与基线测试**：`uv sync --offline --frozen` 退出码 `0`（**PASS**）；`uv run pytest` 退出码 `0`（**PASS**，`1154 passed, 4 skipped, 2 deselected`）。
- **根级 `README.md` 回放**：共 `18` 个代码块（Shell 块 `11`、非命令块 `7`），拆出 `58` 条命令——**回放通过 `11` 条**，**回放失败 `2` 条**（仅 `uv run python scripts/sync_prompts.py --check` 退出码 `2` 与 `bash scripts/ci_sim.sh` 退出码 `1`，均因隐含依赖外部 `~/deepagents` 或联网而在干净拷贝中失败并写明原因），**无法验证 `45` 条**（含 `uv tool install --editable .` 标为离线无法验证）。
- **破坏性变异反证**：**PASS** —— 第 1 处候选 `src/diy_harness/acp.py:L214`（`compare_negate`: `<=` -> `>`）由 `uv run pytest tests/unit_tests/test_acp.py -q` 以断言失败杀死（**KILLED**，退出码 `1`，`3 failed, 57 passed`），还原后测试通过且工作树干净。

```text
[verify] diy-harness @ 52207fd803d4 -> FAIL (build=PASS, test=PASS, readme=11 pass / 2 fail / 45 unverifiable, mutate=PASS)
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
./Scripts/test.sh scope verifier
./Scripts/test.sh milestone 0
./Scripts/test.sh milestone 1
./Scripts/test.sh milestone 2
```

## 开源许可

MIT License。详见 [LICENSE](LICENSE).
