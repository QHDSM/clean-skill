# 交接文档：disk-cleanup skill（2026-11-16）

> 接手者请先完整读一遍本文档，再动手。文档里带 ⚠️ 的段落是**踩过坑之后写下的纪律**，不是建议。
> 文档中所有事实都经过实测核对，数字以本文档记录的时刻为准（`2026-11-16 17:24`）。

---

## 0. 一分钟摘要

| 项 | 值 |
|---|---|
| 项目名 | `disk-cleanup`（Windows 磁盘清理 agent skill） |
| 交付仓库 | `QHDSM/clean-skill`（公开，MIT） |
| 远端 HEAD | `3f6e740` |
| 本地 HEAD | `eda4b74`，**80 个提交**，62 个跟踪文件 |
| 测试 | `Ran 423, OK (skipped=18)` |
| CI | `Validate scanner` ✅ / `End-to-end recycle` ✅（均为绿） |
| 运行时 | Python 3.10+，**纯标准库**，无第三方依赖 |
| 平台 | Windows（NTFS / ReFS）。其他平台仅供开发测试 |
| 许可证 | MIT，Copyright (c) 2026 QHDSM |

**它做什么**：扫描磁盘 → 产出候选清单 → 生成带证据的清理计划 → 预览 → 执行回收站移动 → 每项给出内核级证据 → 可撤销。

**它不做什么**：不永久删除、不清空回收站、不强杀进程、不改 ACL、不跟随联接点、不上传任何数据、不自己发明目标值。

---

## 1. 工作区布局

```
Z:\磁盘清理-skill\
├── 00-交付物\                    ← 交付物与离线产物
│   ├── disk-cleanup-src\         ← ★ 真正的 git 仓库（工作目录）
│   ├── push_via_api.py           ← 推送脚本（不在仓库内，见 §7.2）
│   ├── clean-skill.bundle        ← 全量历史打包，280KB
│   ├── 0001-0002-cleanup-executor.patch
│   ├── 0003-0080-provable-executor.patch   ← 第 3–80 个提交，78 个
│   └── 推送说明.md               ← 同步状态快照
├── 01-竞品研究\                  ← 24 个竞品的分析与源码
│   ├── 01-市场测算.md / 02-深度学习-12个L5.md / 04-首轮竞品全景.md
│   └── 03-竞品源码\  （01-Mole … 12-AvdLee-xcode-disk-cleanup）
├── 02-逆向工程\                  ← 对 std-microblock 的 REA 逆向
│   ├── 01-REA逆向报告.md / 02-反编译证据 / 03-REA证据 / 04-工具链
├── 03-设计方案\                  ← ★ 设计决策与实施记录，动手前必读
│   ├── 01-改造方案.md
│   ├── 02-实施记录.md
│   ├── 03-实施记录-可证明回收.md
│   ├── 04-实施记录-注册表增量与目标模式.md
│   └── 05-实施记录-USN实机与托管runner.md
└── .workbuddy\memory\            ← 工作区记忆
    └── 2026-11-16.md
```

**动手前必读的顺序**：`03-设计方案/05` → `03-设计方案/04` → `03-设计方案/03` → `README.md` → `disk-cleanup/SKILL.md`。

⚠️ **注意路径的两层含义**：本仓库（`disk-cleanup-src/`）是 GitHub 上的交付物，但 `01-竞品研究`、`02-逆向工程`、`03-设计方案` 三个目录**不在仓库里**——它们是本地工作区的研究资料，不对外发布。如果你只拿到这个仓库，下面的设计决策记录就读不到；那就以本文档本身为准，它已经把关键决策和理由都摘出来了。如果能访问整台机器，这些目录在 `Z:\磁盘清理-skill\` 下。

`references/recycle-research.md` 在仓库内，包含回收站实现研究、与 freedesktop Trash 规范的逐条对照，以及托管 runner 暴露的两个缺陷的完整证据——**这些是最难重建的部分，优先读它**。

---

## 2. 仓库内部结构

```
disk-cleanup-src\
├── README.md                    ← 用户面向的完整说明（12,714 字符）
├── LICENSE                      ← MIT
├── .gitattributes               ← `* text=auto eol=lf`
├── .gitignore                   ← 排除 plan*.json / audit-*.jsonl / reports/
├── .github\workflows\
│   ├── test.yml                 ← 单元测试，Python 3.10 + 3.12 矩阵
│   └── e2e.yml                  ← 真实回收→撤销，每次 push 都跑
└── disk-cleanup\                ← ★ skill 本体
    ├── SKILL.md                 ← agent 行为契约（10,697 字符）
    ├── references\
    │   ├── plan-schema.md       ← ★ 计划格式、门禁规则、退出码、路径保护
    │   ├── targets.json         ← ★ 声明式候选注册表（16 条）
    │   ├── recycle-research.md  ← 回收站实现研究与 freedesktop 规范对照
    │   ├── windows.md / cleanup-categories.md / development-tools.md
    │   └── scan-comparison.md
    ├── scripts\  (24 个, 6,874 行)
    └── tests\    (22 个, 5,049 行)
```

### 2.1 脚本职责与规模

| 脚本 | 行数 | 职责 | 写权限 |
|---|---|---|---|
| `clean.py` | 1268 | **门禁 + 预览 + 执行 + 撤销**（核心） | 仅回收站移动 |
| `recycle_backend.py` | 841 | 可证明的回收后端（句柄最终路径 + 回调否决 + 卷能力） | 仅回收站移动 |
| `recycle_bin.py` | 540 | `$I`/`$R` 解析、按卷枚举、还原、路径规范化 | 还原与删索引 |
| `usn.py` | 529 | NTFS USN 变更日志：检查点 / 增量 | 无 |
| `scan.py` | 504 | 只读扫描、分类、重复检测、命名流枚举 | **无任何删除能力** |
| `locks.py` | 412 | 占用探针、Restart Manager 进程归因、具名互斥量 | 无 |
| `targets.py` | 401 | 只读展开候选注册表 | 无 |
| `plan_by_goal.py` | 377 | 按容量目标起草 `plan.json` | 无 |
| `git_audit.py` | 356 | 只读 Git 风险审计 | 无 |
| `e2e_check.py` | 220 | 全链路验收（临时目录内） | 仅自建文件 |
| `recycle_selfcheck.py` | 188 | 证明本机能回收并还原 | 仅临时探针 |
| `recycle_audit.py` | 180 | 只读审计回收站与日志交叉核对 | 无 |
| 其余 12 个 | 66–149 | 证据采集、对比、5 个 `*_experimental.py` 研究原型 | 见 `recycle-research.md` |

> `*_experimental.py` 是历史研究原型，**未接入执行器，不得用于用户文件**，但仍被 `test_recycle_live.py` 覆盖。保留是因为它们记录了技术探索过程。

### 2.2 核心数据流

```
scan.py --format json  ──►  scan.json（largest_files / largest_directories / fingerprint）
                                        │
targets.py list --stat ─┐                │
                        ▼                ▼
                  plan_by_goal.py ──►  plan.json（schema: disk-cleanup-plan/v1）
                                        │
                                        ▼
                             clean.py --plan plan.json        ← 预览（dry-run）
                                        │  门禁 12 条规则 + preflight
                                        ▼
                             clean.py --plan plan.json --go   ← 执行
                                        │  句柄持有 → Shell → 读回最终路径
                                        ▼
                        recycle.status=RECYCLED + proof.final_path 落在 $RECYCLE.BIN
                                        │
                                        ▼
              ~/.disk-cleanup/runs/audit-*.jsonl ──► clean.py --undo <日志>
```

---

## 3. 不可动摇的设计纪律

这些是整个项目的价值所在。**改动时若触碰了其中任何一条，必须在 commit message 里说明理由，并同步更新 `references/plan-schema.md`。**

### 3.1 执行只做回收站移动，且每项必须有证据

- `recycle.status` **只有 `RECYCLED` 是成功**。`PERMANENTLY_DELETED` / `NOT_DELETED` / `UNKNOWN` / `REFUSED` 一律按失败上报，**不降级为"大概没事"**。
- 成功判据是 `recycle.proof.final_path` 落在 `$RECYCLE.BIN` 下——这是**从内核读回的对象最终路径**，不是"文件不见了"。
- 一旦观测到 `PERMANENTLY_DELETED`，执行器**立即中止整批**（报告 `aborted_after`，其余记为 `skipped`）。
- `authorization.permanent_delete` 不得为 `true`。本执行器不做永久删除。

### 3.2 硬保护清单（`clean.py:is_denied`）

两级保护，刻意不同：

```python
DENY_TREE = ('c:\\windows', 'c:\\$recycle.bin', 'c:\\system volume information',
             'c:\\recovery', 'c:\\boot', 'c:\\perflogs', 'c:\\config.msi')
DENY_SELF  = {'c:\\program files', 'c:\\program files (x86)', 'c:\\programdata',
              'c:\\users', 'c:\\users\\public', 'c:\\users\\default'}
PERSONAL_TOP = {'documents','desktop','downloads','pictures','music','videos','movies',
                'onedrive','appdata','favorites','contacts','links','saved games','searches'}
```

- `DENY_TREE`：**整棵子树**禁（系统内部、启动、恢复）。
- `DENY_SELF`：**只有目录本身**禁，其下被点名的缓存子目录**允许**。`C:\ProgramData` 本身保护，但 `C:\ProgramData\Package Cache` 是正常清理目标——这是两级保护分开的唯一原因。
- 用户目录顶层（Documents / Desktop / Downloads / …）禁。
- 路径任意一级出现 `.git` 目录名即禁。

⚠️ **不要把这些改成"更严格"。** `ProgramData` 那类"父目录保护、子目录放行"的模式是刻意设计的，收紧会让正常缓存清不掉。

### 3.3 卷能力闸门（`recycle_backend.py:_capability_issues`）

以下任一命中 → `verified_capable=False`，`--go` 拒绝并写明原因：

| 条件 | 来源 |
|---|---|
| 卷以只读方式挂载 | `GetVolumeInformationW` 的 `FILE_READ_ONLY_VOLUME` 标志 |
| 文件系统不是 NTFS/ReFS | `GetVolumeInformationW` |
| 解析不到当前用户 SID | `GetTokenInformation` |
| 无当前用户的回收站目录 | 文件系统 |
| 组策略 `NoRecycleFiles=1` | 注册表 |
| `NukeOnDelete=1` | 注册表 |
| `MaxCapacity=0` | 注册表 |

### 3.4 执行后必须核对身份，不只核对路径

`--undo` 还原后用清单里记录的 **128 位 FileId + 大小**复核对象身份：
- 不符 → `RESTORED BUT NOT THE SAME OBJECT`，**不得对用户说"已完整还原"**。
- 旧记录没记身份 → `verified` 为 `null`（**未知**，不是通过）。

### 3.5 审计日志即撤销清单

- 路径：`~/.disk-cleanup/runs/audit-YYYYmmdd-HHMMSS-<随机>.jsonl`
- **一次执行一个文件**（不是一天一个文件），因为 `--undo` 必须只还原某一次执行的对象。
- **边执行边写并 `fsync`**：每个对象移动前先写 intent 记录。崩溃/断电后已进回收站的项仍可撤销。
- 截断的末行由读取器跳过，不视为致命错误。
- ⚠️ **日志含真实路径，永不提交仓库**（`.gitignore` 已排除 `audit-*.jsonl`）。

### 3.6 并发互斥

`--go` 与 `--undo` 先取具名互斥量，第二个进程 `exit 2` 并报 `another cleanup run is in progress`。dry-run 不占锁。**两个执行交错会把同一对象当成还在**，各自预留容量并重复报告移动。

### 3.7 计划门禁 12 条（`references/plan-schema.md` 全文为准）

任一不过 → **退出码 2，零执行**。字段缺失或类型不符会让**整份计划**被拒绝。

关键几条：`scope` 非空且带盘符；`permanent_delete` 不得为 true；`targets` 非空且 ≤200；每项 `reason` 非空；`evidence.fingerprint` 必须是 5 个整数；目标必须在 `scope` 内；不得与 `excluded` 重叠；**不得重复同一对象，也不得嵌套**（`repeats an earlier target` / `is inside another target`）。

### 3.8 Git 闸门

所有 `trash` 项都受约束，**与 `safe` / `caution` 标签无关**。只有 `verdict=SAFE_TO_CONSIDER` 且 `all_content_synced: true`、`unknown: []`、`risks: []` 同时满足才免确认，否则必须 `authorization.git_acknowledged: true`。

当前审计**不 fetch**，因此 `unknown` 始终非空、`all_content_synced` 不会为 true——**这是设计，不是 bug**。

---

## 4. ⚠️ 六个必须知道的坑

这些是本项目花掉最多时间的地方，写在这里是为了让接手者不要重走一遍。

### 坑 1：Windows 上同一个路径不止一种写法

托管机的 TEMP 是短名 `C:\Users\RUNNER~1\...`，Shell 写进 `$I` 的是长名 `C:\Users\runneradmin\...`。**同一个目录，两种拼写。**

四处裸字符串相等全中招，其中一处最严重：撤销时"回收站记录与日志必须一致"的检查会**把就在回收站里的对象报告成不可恢复**。

**修法**：`recycle_bin.canonical_path()` —— 去 `\\?\` 前缀、**逐段**展开 8.3 短名、归一分隔符与大小写；`same_path()` 做比较。

⚠️ **逐段展开是必需的**：文件移进回收站后原路径已不存在，整条路径的 `GetLongPathNameW` 必然失败，只有父目录还能解析。而短名恰恰在父目录那一层。

⚠️ **凡是判断"这两串说的是同一个东西"的地方，都不能用字符串相等。** 已修的四处：`recycle_selfcheck.py` 的 metadata 检查、`clean.py` 的"记录与日志必须一致"、`clean.py` 的已恢复集合、`recycle_backend.py` 的"对象仍在原路径"判定。

### 坑 2：GitHub 会静默作废含裸冒号的 workflow

```yaml
run: python .../e2e_check.py --drive C:    # ❌ 整个 workflow 文件被判无效
run: python .../e2e_check.py --drive "C:"  # ✅ 正常注册
```

**表现是完全静默的**：不报错、不建作业、显示名回落成文件路径、`workflow_dispatch` 答 422。

⚠️ 排查"文件明明合法却不执行"，**必须逐字符二分内容**。靠读文档或猜触发器写法都会绕远路——为此建过 20 多个探针 workflow。排查时排除了触发器写法（块式/流式）、`schedule`、`workflow_dispatch`、`continue-on-error`、BOM、CRLF、文件字节，**全部无关**。

### 坑 3：用定义，不要用观测到的形态

USN 的卷根识别错了两次：

| 判据 | 为什么错 |
|---|---|
| "记录 5 是根" | 假设根记录存在——实测枚举里 `low48 == 5` 的记录**0 条** |
| "自父引用的记录是根" | 假设根记录形态固定——实测自父引用**0 条** |
| **"低 48 位 MFT 索引 == 5"** ✅ | 用了定义 |

改用掩码判据后，**934,436 / 934,444** 父可解析。

### 坑 4：`push_via_api.py` 推 workflow 必须走 Contents API

用 tree/commit/ref 推 `.github/workflows/*.yml`，GitHub **不会重新解析它**：文件进了仓库，但 workflow 条目的显示名保持成路径、作业不创建、每次 run 都失败。

Contents API（`PUT /contents/...`）才会触发注册。脚本已分流处理。

⚠️ 另一个坑：Contents API **自行提交**，所以其余文件必须提交在它产生的结果之上，否则非快进。

### 坑 5："verified"必须有校验垫底

`push_via_api.py` 原先在只有 workflow 变更时提前 `return 0`，**跳过了最终的树比对**——它会打印"已推送"却不曾验证过。已修：即使只有 workflow 变更也走完整校验。

**推广到所有交付物**：状态文档里的数字（提交数、文件数、测试数）必须与实际核对过。交接时发现文档写"63 个提交"、实际是 79，产物 patch 因此也叫错了名字。

### 坑 6：USN 输出没人能读

AMD 驱动不停写 dump 日志，**每 304 字节产生一条 USN 记录**，同一条路径刷出几百行。报告里 90% 是同一行。

改成按路径聚合：同路径合并成一条（带 `events` 计数）；改名配成一条（靠 **frn** 配对，不是路径——改名前后路径本来就不同），带 `renamed_from`。

语义细节：**创建后跟写入仍是 `created`；写入后跟删除仍是 `deleted`**。"先创建再写入"不是"修改一个本来不存在的东西"。

---

## 5. 关键契约

### 5.1 `plan.json`（`references/plan-schema.md` 是唯一权威）

```json
{
  "schema": "disk-cleanup-plan/v1",
  "generated_unix": 1730000000,
  "authorization": {
    "scope": ["C:\\"],
    "excluded": ["C:\\Users\\me\\Documents"],
    "permanent_delete": false,
    "git_acknowledged": false
  },
  "goal": {
    "schema": "disk-cleanup-goal/v1",
    "kind": "move_bytes",
    "requested_bytes": 21474836480,
    "estimated_bytes": 24000000000,
    "shortfall_bytes": 0,
    "met": true
  },
  "targets": [{
    "path": "C:\\Users\\me\\AppData\\Local\\Temp\\build-cache",
    "action": "trash",
    "risk": "safe",
    "reason": "构建缓存，mtime 45 天，无进程占用，可重建",
    "evidence": {
      "size_bytes": 1234567,
      "fingerprint": [1234, 5678, 1234567, 1730000000000, 1729000000000]
    }
  }],
  "note": "给用户的说明"
}
```

- `evidence.fingerprint` 是 `[st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns]`，**比对时只用前四项**（`ctime` 在 Windows 上读一下就变）。
- `action`：`trash` / `keep` / `report`。`risk`：`safe` / `caution` / `never`（`never` 绝不执行）。
- **目录没有自己的 `size_bytes`**——目录递归大小不等于目录自身元数据长度，不能写进 `evidence.size_bytes`。

### 5.2 `goal` 是移动目标，不是释放目标

⚠️ **这是最容易讲错的一点。**

所有动作只进回收站，清空前**不释放空间**。所以：

- `kind` 只有 `move_bytes`；
- 报告里 `requested_bytes` / `estimated_bytes` / `reclaimed_bytes` 三个数分开，**`reclaimed_bytes` 恒为 0**；
- `clean.py` **重算**缺口与达成状态，不信任写入的布尔值；自相矛盾整份拒绝；
- 凑不够时**照写计划并报出缺口**（"目标 20 GiB / 候选仅 12 GiB / 缺口 8 GiB"），**不扩大扫描范围、不降低风险标签、不提高 `--max-targets`**——那些都得由用户决定。

### 5.3 `targets.json` 候选注册表

```json
{
  "id": "user-temp",
  "label": "当前用户临时目录",
  "category": "system",
  "status": "release",
  "risk": "safe",
  "action": "delete",
  "regenerates": "按需重建",
  "paths": ["%LOCALAPPDATA%\\Temp"],
  "exclude": [],
  "running": [],
  "requires_admin": false,
  "note": "正在被进程占用的文件应在执行时被拒绝，不要强杀进程"
}
```

- 16 条，`categories` = system / browser / development / application / protection。
- **`status: pending`（Docker）不作为候选；`risk: never`（downloads、recycle-bin）永不作为候选。**
- ⚠️ **被 `clean.py` 拒绝的路径照实报告并写明原因，不静默丢掉**——`C:\Windows\Temp` 是真实存在的缓存，只是被门禁挡住，藏起来反而会教错。
- 结构借自 BleachBit CleanerML（cleaner/option/action + running/var 分层），但**定义全部原创**（CleanerML 是 GPL，混进 MIT 仓库有授权问题）。
- `running` 字段驱动进程探针；**进程在跑是提示，不是否决**——缓存里有几个被占用的文件，其余仍可能达标，整类拒掉只会把缺口推给无关候选。

### 5.4 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部按要求处理（dry-run 完成或全部成功） |
| 2 | 计划被门禁拒绝，**没有执行任何操作**；或参数错误 |
| 3 | 计划通过但部分目标被拒绝或失败（看 `results[]`） |
| 130 | 用户中断（`scan.py` 返回部分结果） |

---

## 6. 环境与命令

### 6.1 本机环境

| 项 | 值 |
|---|---|
| Python | 3.13（CI 测 3.10 + 3.12） |
| 管理员 | **否**（`usn.py` 需要提权；托管 runner 默认是管理员） |
| 依赖 | 纯标准库，**不需要 pywin32** |
| 网络 | `github.com:443` 双向不通（见 §7.1） |

### 6.2 常用命令

```powershell
cd Z:\磁盘清理-skill\00-交付物\disk-cleanup-src

# 只读：列出卷
python disk-cleanup/scripts/scan.py --list-drives

# 只读：扫描并保存报告
python disk-cleanup/scripts/scan.py 'D:\Projects' --format json --output 'reports/scan.json'

# 只读：候选注册表
python disk-cleanup/scripts/targets.py list --stat

# 只读：按目标起草计划
python disk-cleanup/scripts/plan_by_goal.py --scan 'reports/scan.json' --target 20GiB --git-audit --out 'reports/plan.json'

# 只读：预览（不带 --go）
python disk-cleanup/scripts/clean.py --plan 'reports/plan.json'

# 唯一有写权限的命令
python disk-cleanup/scripts/clean.py --plan 'reports/plan.json' --go

# 撤销
python disk-cleanup/scripts/clean.py --undo-last

# 自检（首次在某机器上执行前必跑）
python disk-cleanup/scripts/recycle_selfcheck.py --drive C:

# 全链路验收（只碰自建文件）
python disk-cleanup/scripts/e2e_check.py --drive C:

# 测试
python -m unittest discover -s disk-cleanup/tests          # 423 通过
DISK_CLEANUP_E2E=1 python -m unittest disk-cleanup.tests.test_e2e -v
```

### 6.3 USN（需管理员）

```powershell
python disk-cleanup/scripts/usn.py probe --drive C:      # 只读探针
python disk-cleanup/scripts/usn.py baseline --drive C:    # 建检查点（约 1–2 分钟，93 万条）
python disk-cleanup/scripts/usn.py since --drive C: --json
```

⚠️ 无提权时**退出 3 并说明原因**，绝不退回目录遍历冒充增量。
⚠️ `since` 的输出约定：同路径已聚合；父目录缺失的条目在独立的 `unresolved[]` 里（名字已知、绝对路径未知），此时 `complete` 为 false。

---

## 7. 同步到 GitHub（重点，容易踩坑）

### 7.1 网络现实

| 端点 | 结果 |
|---|---|
| `github.com:443` 直连 | ❌ 15s 超时 |
| `github.com:443` 经代理 | ❌ `CONNECT tunnel failed, response 502` |
| `github.com:22` / `ssh.github.com:443` | ✅ 握手成功，但本机无 `~/.ssh` |
| `api.github.com:443` | ✅ 200 |

**结论：`git push` 走不通，只能走 REST API。**

### 7.2 为什么不能直接 `git push`

⚠️ **本地与远端的历史不是同一条链。** 本地是 80 个真实提交，远端是历次快照合成的提交；两者**树**逐字节一致，但**提交拓扑**不同。`git push` 在这个仓库结构上不可用，只能做快进提交。

### 7.3 推送流程

```powershell
cd Z:\磁盘清理-skill\00-交付物

python push_via_api.py --dry-run        # 先看会推什么

$env:GITHUB_TOKEN = 'ghp_xxx'          # ⚠️ 不要写进任何文件
python push_via_api.py --message-file msg.txt
```

脚本行为：
- 按远端 tree 比对 blob SHA，**只传变化的文件**；
- 内容取自 `git cat-file blob`（**不读工作区**——工作区 CRLF、索引 LF，读盘会造成漂移）；
- 每个上传的 blob 都回读校验 sha；
- **workflow 文件走 Contents API**（见坑 4），其余走 tree/commit/ref；
- 远端有、本地无的文件以 `sha: null` 删除；
- 推完做**全量树比对**，打印 `verified: remote tree matches HEAD (62 files, byte-identical)`。

### 7.4 同步后必须重建离线产物

⚠️ **产物按提交数命名，重建后必须同步改文件名和状态文档里的数字。**

```powershell
cd Z:\磁盘清理-skill\00-交付物
git -C disk-cleanup-src bundle create clean-skill.bundle --all
git -C disk-cleanup-src format-patch --binary f7422a6..HEAD --stdout > "0003-0080-provable-executor.patch"
```

然后更新 `推送说明.md` 的：同步状态段、本地 HEAD 与提交数、产物表、核对命令、历史表。

### 7.5 ⚠️ 凭据

- 交接时对话记录里出现过 classic PAT（`repo` 全权限），**必须撤销**：<https://github.com/settings/tokens>
- 若要长期同步，建 **fine-grained token**，只授权 `QHDSM/clean-skill`，权限 `Contents: Read and write`。
- 或生成 SSH 密钥贴到 <https://github.com/settings/keys>，之后 `git push` 直接可用（但 `github.com:22` 需本机有 `~/.ssh`）。
- **不要把 token 持久化到磁盘**（`setx`）除非用户明确同意——那意味着凭据对该用户所有进程可见。

---

## 8. 测试与 CI

### 8.1 本地

```powershell
cd Z:\磁盘清理-skill\00-交付物\disk-cleanup-src
python -m unittest discover -s disk-cleanup/tests
# Ran 423 tests, OK (skipped=18)
```

18 个跳过项分类：

| 数量 | 条件 |
|---|---|
| 13 | 需 `DISK_CLEANUP_LIVE_RECYCLE_TEST=1`（实机回收测试） |
| 3 | 需管理员 shell（USN live 测试） |
| 1 | 需 `DISK_CLEANUP_E2E=1` |
| 1 | 符号链接不可用的平台限制 |

⚠️ **跳过项不等于通过。** 这 18 项在托管 runner 上是**真跑**的（runner 默认管理员），`End-to-end recycle` 作业正是为此存在。

### 8.2 CI

| Workflow | 触发 | 内容 |
|---|---|---|
| `Validate scanner` | push + PR | `unittest discover`，Python 3.10 + 3.12 矩阵 |
| `End-to-end recycle` | push | 真实回收→撤销→字节比对，**失败即失败** |

⚠️ `e2e.yml` 的 `run:` 里 `--drive "C:"` **必须带引号**（坑 2）。文件里有注释说明，别"清理"掉。

### 8.3 行为验收表（尚未被真实 agent 验证）

`disk-cleanup/tests/behavior-scenarios.md`（49 行）是一张 Agent 行为验收用例表：给测试者 SKILL.md 与请求，不预告答案，核对其实际操作。

⚠️ **这张表一次都没有被真实 agent 会话跑过。** 文件里明确写着"不声称所有 Agent 已通过"。工具层测透了，**服从性一次没测过**——要关这个缺口只能拿干净上下文逐场景演练并记录。

---

## 9. 未决事项（按价值排序）

| # | 事项 | 状态 | 谁来做 |
|---|---|---|---|
| 1 | **USN 聚合逻辑真机复验** | 根判据与路径拼接已被用户的实机运行验证（跑出绝对路径）；**聚合**（合并同路径、改名配对）只过了单测 | 需一次提权 `since` |
| 2 | **发布卫生** | **0 个 tag、无 CHANGELOG、README 未声明支持的 Windows 版本**。仓库是公开的，每个访客看到的都是无版本的一坨 | 接手者，约半小时 |
| 3 | **`SKILL.md` 瘦身** | 10,697 字符。竞品研究把"提示预算 5.4KB 全场最低"列为差异化优势，现在翻了一倍。提示预算按会话次数相乘——**这是全项目唯一"越写越亏"的文件** | 接手者 |
| 4 | **Docker 注册表条目定论** | 唯一的 `pending`。需一台装有 Docker Desktop 的机器做只读测量 | 需用户资源 |
| 5 | **回收站目录写权限被改** | 明确**不覆盖**（见下） | 保持开放 |
| 6 | **"真正释放"（清空回收站）** | 明确**不做**（见下） | 保持开放 |
| 7 | **注册表覆盖面** | 16 条只覆盖系统/浏览器/开发工具的常见位置。中文 Windows 机器最大的可再生占用（微信/QQ/钉钉）**没有**——微信必须进 `pending` 且路径钉死在缓存子目录，聊天记录是个人数据 | 接手者，需谨慎 |
| 8 | **行为验收表从未被真实 agent 跑过** | 见 §8.3 | 需干净上下文演练 |
| 9 | **本地历史有 2 个诊断性提交** | `71be826` / `e8edee8`（二分 workflow 注册问题用），远端树里没有对应文件。**清理需重写历史 + 重推远端，风险大于收益** | 建议留着 |

### 9.1 明确**不要**做的三件事

1. **不要实现清空回收站。** 不可逆，且一旦 goal 驱动的流程能碰回收站，"凑不够 20GB"就变成"清空凑数"——**激励结构必然作恶**。要做也必须是独立命令、独立确认、永不被目标流程触达。

2. **不要在 `$RECYCLE.BIN` 里写可写性探针。** 残留的零字节文件会被 `recycle_audit.py` 归为 `r_without_i` 孤儿——**用一个更坏的失败去堵一个很窄的时间窗**（自检之后、执行之前权限被改）。这一半缺口明确保持"未覆盖"，写在 `references/recycle-research.md` 里，不假装解决。

3. **不要再往 `SKILL.md` 加散文，也不要在 README 写没实现的能力。** SKILL.md 已从 62 行长到 140+ 行，靠提示词维持的约束越多，实际生效的越少；README 写没实现的能力是**过度承诺**——历史上"目标释放 10 GB"这条示例就坑过一次。

---

## 10. 提交与文档惯例

### 10.1 commit message 风格

看 `git log` 的既有风格：**英文正文 + 解释"为什么"而非"改了什么"**，段落之间空行分隔。近期实例：

```
Compare Recycle Bin paths canonically, not by string equality

The hosted runner proved that the path the Shell records in the $I file is not
always the path the caller has. The self-check trashed a probe, read it back
and reported a mismatch on a path that looked identical, and every undo lookup
compares the same two strings the same way.

canonical_path() strips the \\?\ long-path prefix, resolves an 8.3 short name
through GetLongPathNameW, drops a trailing separator, and normalises case and
separators. ...
```

风格要点：第一行是祈使句、说明**行为变化**而非文件；正文解释**为什么**这样改、**代价**是什么、**什么没做**；不写"Fix bug"。

### 10.2 改动的连带义务

改任何能力都要同步四处，缺一不可：

| 位置 | 内容 |
|---|---|
| `disk-cleanup/SKILL.md` | Agent 行为契约 |
| `README.md` | 用户面向的说明与脚本表 |
| `references/plan-schema.md` | 计划格式与门禁（只在契约变化时） |
| `disk-cleanup/tests/behavior-scenarios.md` | 行为验收用例 |

⚠️ 改 `clean.py` 的撤销逻辑时，**没有任何机器会告诉你坏了**——这是本项目最脆弱的地方，改完必须跑 `e2e_check.py`。

### 10.3 文档纪律

- 状态文档里的每个数字都要**实际核对过**（提交数、文件数、测试数、产物大小）。
- 产物文件名带提交数，重建后**同步改文件名和文档数字**。
- 写"未验证"的东西时，说清楚**未验证的是什么**、**为什么本机验证不了**。

---

## 11. 已知的环境陷阱

| 陷阱 | 说明 |
|---|---|
| GitHub Actions 裸冒号 | `run:` 里的 `--drive C:` 让整个 workflow 文件静默失效（坑 2） |
| Actions 触发器注册 | 带 `schedule` 的新 workflow 有时不被触发系统正确登记。解法：加 `push` 触发器让它真正跑一次 |
| Actions `workflow_dispatch` 422 | 报错 "Workflow does not have 'workflow_dispatch' trigger" 可能意味着该 workflow 根本没被解析，不是触发器写错 |
| Actions 作业列表偶发 0 | `/runs/{id}/jobs` 会返回 `total_count: 0`，但作业其实存在。**换一个 run ID 或稍等重试**——当时因此误判了很久 |
| CRLF | `.gitattributes` 已设 `* text=auto eol=lf`。**内容取自 `git cat-file blob` 而不是读工作区** |
| 令牌拼写 | 手工敲 token 容易错一位。错误表现为 404/403，容易误判成权限问题 |

---

## 12. 设计决策的来源

想理解"为什么这样设计"，去读这几份：

| 文件 | 内容 |
|---|---|
| `03-设计方案/03-实施记录-可证明回收.md` | 为什么执行器要持有句柄跨过删除、为什么以最终路径为成功判据 |
| `03-设计方案/04-实施记录-注册表增量与目标模式.md` | 为什么用 CleanerML 结构而不抄实现、为什么做 USN 而不做裸 `$MFT`、为什么目标是移动量 |
| `03-设计方案/05-实施记录-USN实机与托管runner.md` | 托管 runner 暴露的两个缺陷 + 六条教训 |
| `01-竞品研究/02-深度学习-12个L5.md` | 12 个 L5 级竞品的深度分析 |
| `02-逆向工程/01-REA逆向报告.md` | 对 std-microblock 的逆向：它直接解析裸 `$MFT` + `FSCTL_GET_NTFS_FILE_RECORD` 兜底 |
| `disk-cleanup-src/disk-cleanup/references/recycle-research.md` | 回收站实现研究 + 与 freedesktop Trash 规范的逐条对照 |

**一条重要的判断**：逆向显示竞品（std-microblock）用裸 `$MFT` 解析换取 WizTree 级速度，**但我们不做**。理由：那是它自己的一套文件系统实现，在无提权时无法验证，而它换来的**大小**数据 `scan.py` 已经能算；我们真正缺的是**增量**。USN 记录根本没有 size 字段，所以 `usn.py` 明确只回答"哪些路径变了"，字节数让调用方单独 stat。

---

## 13. 接手后建议的第一周

| 顺序 | 事项 |
|---|---|
| 1 | 跑一遍全量测试 + `e2e_check.py`，确认本机状态与本文档一致 |
| 2 | 读 `03-设计方案/05`（最近一轮的教训）与 `SKILL.md` |
| 3 | **用新代码重建 USN 基线再跑一次 `since`**，确认聚合输出可读（关掉遗留 #1） |
| 4 | 打 tag `v0.1.0` + 写 CHANGELOG + README 声明支持的 Windows 版本（遗留 #2） |
| 5 | `SKILL.md` 瘦身：把 USN 注意事项、goal 语义、targets 用法的细节移到 `references/`，正文只做路由（遗留 #3） |
| 6 | 补注册表：yarn / pnpm / gradle / maven / HuggingFace / torch；微信/QQ/钉钉**必须**进 `pending` 且路径钉死在缓存子目录 |
| 7 | 拿一个干净上下文逐场景演练 `behavior-scenarios.md`，记录结果（遗留 #8） |

---

## 14. 一句话总结

这个项目的价值不在"能删文件"，而在**每一步都能证明自己做了什么、没做什么**——删掉的每个对象都有内核读回的最终路径作证，还原回来的每个对象都用 128 位 FileId 复核过身份。

**改动时优先保住这个性质，而不是功能数量。** 一个不能自证的清理工具，比没有清理工具更危险。
