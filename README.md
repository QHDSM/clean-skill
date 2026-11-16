# 磁盘清理 · clean-skill

## 当前状态：可证明的回收站执行

`clean.py --go` 现在会执行，但只做回收站移动，并且每一件都能证明落点：执行器持有目标句柄跨过删除，再从内核读回对象的最终路径——在 `$RECYCLE.BIN` 下才是成功，在 `$Extend\$Deleted` 下说明被永久删除、按失败上报。逐项 COM 回调还会否决任何未被标记为回收的删除。目录指纹不能验证子孙文件；预览可有限查找 `.git` 标记，但不能代替风险审计。请勿把 dry-run 成功当作安全执行证明，也勿把"文件不见了"当作回收成功。


面向 Windows 的 Agent Skill：分析磁盘占用、比较历史增长、识别 Git 数据风险，生成并校验清理计划，然后在一次授权内执行可恢复的回收站清理并核实结果。

**仓库名：`clean-skill`；技能调用名：`disk-cleanup`；许可证：MIT。**

## 工作方式

1. 查看实际固定磁盘、用户目标和排除项。
2. 展示本次授权范围、清理类型、删除方式和权限要求。
3. 只读扫描并分类，把执行意图写成 `plan.json`（每项带路径、理由、风险与指纹）。
4. 由 `clean.py` 校验计划；先 dry-run，确认后 `--go` 执行回收站移动。
5. 报告每项的 `recycle.status` 与证据、拒绝与跳过原因，以及每盘可用空间变化。

覆盖系统临时文件、应用缓存、开发工具、容器，以及个人文件、备份、云同步和重复文件分析。后几类默认保留；缓存也必须确认用途、位置、可重建性及使用状态。全机扫描默认选择可访问本地固定磁盘；移动、网络和远程 Docker 环境不自动纳入。

## 脚本能力

| 脚本 | 能力 | 是否删除 |
|---|---|---|
| `scripts/scan.py` | 只读扫描、分类、重复文件检测、`--allocation` 实测分配字节、`--streams` 枚举 NTFS 命名数据流、扫描期间目录变动检测 | **无任何删除能力** |
| `scripts/targets.py` | 只读展开候选注册表：存在性、逻辑大小、门禁冲突、占用进程 | 无 |
| `scripts/usn.py` | NTFS 变更日志：存检查点、列出增量变更（需管理员；只给路径与原因，不给大小） | 无 |
| `scripts/plan_by_goal.py` | 按容量目标起草 `plan.json`：目标不足时报缺口，不扩范围、不降风险 | 无 |
| `scripts/clean.py` | 校验计划、dry-run、回收站执行与 `--undo` | 仅回收站移动 |
| `scripts/recycle_backend.py` | 可证明的回收后端（句柄最终路径 + 回调否决 + 卷能力） | 仅回收站移动 |
| `scripts/recycle_bin.py` | 解析 `$I`/`$R`、按卷枚举、还原 | 还原与删索引 |
| `scripts/locks.py` | 占用探针、Restart Manager 进程归因 | 无 |
| `scripts/recycle_selfcheck.py` | 一条命令证明本机确实能回收并还原（临时探针，走完回收→读回→还原→比对） | 仅回收站移动（探针） |
| `scripts/recycle_audit.py` | 只读审计回收站：条目/容量、无法还原的孤儿记录、原路径冲突、某次执行的对象是否还在 | 无 |
| `scripts/git_audit.py` | 只读 Git 风险审计：未推送提交计数、裸仓库/detached 形态、`unknown`（证据缺口）与 `caveats`（范围限制）分开报告 | 无 |
| `scripts/tree_evidence.py` | 有限采集目录树元数据，供计划预览比较 | 无 |
| `scripts/windows_identity.py` | 只读获取 Windows 句柄级文件身份，供计划预览比较 | 无 |
| `scripts/windows_parents.py` | 采集完整父目录身份链，供计划预览比较 | 无 |
| `scripts/inspect_target.py` | 在一次只读检查中组合目标身份与父目录证据 | 无 |
| `scripts/compare_scans.py` | 比较两份已保存的扫描报告，识别可比较的增长 | 无 |

重复文件扫描需显式指定 `--duplicates`，会读取候选文件内容。结果中的 `distinct_file_ids_observed` 表示是否观察到不同的非零文件身份；为 `false` 时不能证明路径是独立副本。哈希匹配只是分析线索，不能代替操作前复核。

Windows 上可加 `--allocation` 查询文件系统报告的文件分配量。`allocation_observation` 按句柄级文件 ID 去重硬链接，并报告覆盖完整性；它排除目录和文件系统元数据开销，不代表可释放空间。默认扫描不做额外分配查询。

`clean.py` 的安全边界（在代码里，不在文档里）：

- 计划模式读取计划，不重放扫描结果；执行只做回收站移动。
- 门禁校验 schema、指纹完整性、路径越权与排除项；任一项不过则**零执行**。
- 预览时重验对象自身指纹的前四项；目录内的深层变化不一定能发现。
- 执行前探占用：`--go` 用 Restart Manager 报出进程名，dry-run 只做轻量探针；被占用即拒绝并说明原因。
- 持句柄重验：拿到句柄后、调用 Shell 前，用句柄本身重读计划证据（文件 ID、大小、mtime）；路径在检查与移动之间被替换时拒绝并说明 `the object changed after the plan was checked`，Shell 不被调用；句柄不跟随联接点，路径被换成链接时同样拒绝并说明，绝不顺着链接动它的目标。
- 执行后读内核返回的最终路径：只有落在 `$RECYCLE.BIN` 下才记为成功；`PERMANENTLY_DELETED`、`UNKNOWN` 一律按失败上报，且一旦观测到永久删除，执行器立即中止整批（`aborted_after`），剩余目标记为 `skipped`。
- 卷能力先验证（NTFS/ReFS、`$RECYCLE.BIN` 可写、容量够、`NukeOnDelete` 未开），不满足即拒绝而不是降级。
- 同时只跑一个执行器：`--go` 与 `--undo` 先取具名互斥量，第二个进程 `exit 2` 并报 `another cleanup run is in progress`；dry-run 不占锁。两个执行交错会把同一对象当成还在，各自预留容量并重复报告移动。
- 计划内不得重复或嵌套目标：同一对象出现两次、或目标落在另一个目标里面，整份计划被拒绝（`repeats an earlier target` / `is inside another target`），避免重复移动与重复计入容量。
- 容量按卷、按整批算：先读该卷回收站已占用字节，再按批内顺序扣减，会推过 `MaxCapacity` 的目标提前 `refused`；回收站占用读不全时预算记为 unknown（`bin_usage_complete=false`），不当成空。
- 预览即预演：dry-run 逐项给出 `bin_fit`（是否放得进回收站、卷能否被证明可回收），并汇总 `capacity_warnings`/`volume_warnings`，不用等执行才发现整批都装不下。
- 卷能力闸门：`NoRecycleFiles=1`（组策略）、`MaxCapacity=0`、`NukeOnDelete=1`、非 NTFS/ReFS、缺当前用户回收站目录，任一命中即 `verified_capable=False`，`--go` 拒绝并给出具体原因——这些设置下删除不会进回收站。
- 审计日志一次执行一个文件（`~/.disk-cleanup/runs/`），同时是 `--undo` 的撤销清单，独占创建不覆盖已有文件。
- 日志边执行边追加并 `fsync`：每个对象移动前先写一条 intent 记录，移动后写带 `$I`/`$R` 的记录。进程被杀或
  断电后 `--undo` 仍能还原已完成的项；只有 intent 记录、没有成对记录的对象，按 intent 时间戳在原路径上
  以 ±5 秒窗口从回收站找回（报告 `recovered`），找不到就如实报 `skipped`。
- 还原后核对身份：`--undo` 用清单里记录的 128 位 FileId 与大小复核回来的对象，不符报 `RESTORED BUT NOT THE SAME OBJECT` 并给出原因（身份不符 / 大小变化）——路径回来了不等于原来的东西回来了；清单没记身份的旧记录 `verified` 为 `null`（未知），不当成通过，`summary.verified` 单独统计。
- 受保护树（`C:\Windows`、`C:\$Recycle.Bin`、`C:\Recovery` 等）整棵拒绝；`C:\ProgramData` 等只拒绝目录本身，其下被点名的缓存子目录可处理。
- 计划格式与全部规则见 [`disk-cleanup/references/plan-schema.md`](disk-cleanup/references/plan-schema.md)。

## 安装到 Codex

将仓库中的 `disk-cleanup` 文件夹复制到 `$CODEX_HOME/skills/`；未设置该环境变量时使用 `$HOME/.codex/skills/`。例如在克隆后的仓库根目录运行：

```powershell
$skillBase = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $HOME '.codex/skills' }
$skillTarget = Join-Path $skillBase 'disk-cleanup'
if (Test-Path -LiteralPath $skillTarget) { throw '技能目录已存在，请先审阅差异，避免覆盖本地修改。' }
New-Item -ItemType Directory -Force -Path $skillBase | Out-Null
Copy-Item -LiteralPath '.\disk-cleanup' -Destination $skillTarget -Recurse
```

按宿主的技能发现方式重新加载或开始新对话。目录布局和元数据经过 Codex 技能校验；不同宿主/版本的实际加载行为需核实，未验证其他 Agent 的兼容性。

## 对话示例

```text
使用 $disk-cleanup 只分析 C 盘，不修改任何文件。
```

```text
使用 $disk-cleanup 清理整台电脑。
先展示候选、证据和授权策略，生成清理计划供预览。
保留个人文件、下载、云同步、备份和回收站，排除 D:\Work。
```

```text
使用 $disk-cleanup 分析 D:\Projects 的开发缓存，目标释放 10 GB。
```

授权只对当前任务及明确范围有效。系统提权仍通过正常机制；不会绕过 UAC 或宿主权限。永久删除、关闭应用、重启等必须在授权中有明确选择。用户只要求分析时保持只读。

## 校验与预览计划（`clean.py`）

```powershell
python disk-cleanup/scripts/clean.py --plan plan.json --json
python disk-cleanup/scripts/clean.py --path 'D:\tmp\x' --confirm-path 'D:\tmp\x'
python disk-cleanup/scripts/clean.py --path 'D:\tmp\x' --confirm-path 'D:\tmp\x' --go
python disk-cleanup/scripts/clean.py --undo 'C:\Users\me\.disk-cleanup\runs\audit-20260101-120000-abcdef.jsonl'
python disk-cleanup/scripts/clean.py --undo-last
```

计划模式验证范围、排除项、指纹和所提供的证据。单路径模式比较规范化后的路径字符串，不证明文件身份。不带 `--go` 时只预览；带 `--go` 时只做回收站移动，每一项都要有最终路径证据，见到 `PERMANENTLY_DELETED` 会中止整批，并把剩余目标记为 `skipped`。

退出码：`0` 表示预览完成，不代表目标安全或已经清理；`2` 表示参数/计划被拒绝；`3` 表示有目标在预览中被拒绝。结果含实际校验字节的完整 `plan_digest`，摘要不代表用户授权。

## 独立使用只读扫描工具

需要 Python 3.10+，只使用标准库。以下命令从仓库根目录运行；扫描本身不会执行清理。

```powershell
python disk-cleanup/scripts/scan.py --list-drives
python disk-cleanup/scripts/scan.py 'C:\' 'D:\' --top 30 --format json
python disk-cleanup/scripts/scan.py 'D:\Projects' --depth 2 --exclude 'D:\Projects\private'
python disk-cleanup/scripts/scan.py 'D:\Downloads' --duplicates --min-size 10MiB
New-Item -ItemType Directory -Force reports | Out-Null
python disk-cleanup/scripts/scan.py 'D:\Projects' --format json --output 'reports/baseline.json'
# 之后保存同范围的新扫描，再比较
python disk-cleanup/scripts/scan.py 'D:\Projects' --format json --output 'reports/current.json'
python disk-cleanup/scripts/compare_scans.py reports/baseline.json reports/current.json
```

| 参数 | 含义 |
|---|---|
| 一个或多个目标路径 | 必须显式指定；不接受互相重叠的根目录 |
| `--list-drives` | Windows 固定磁盘及容量，不能与目标路径或输出文件混用 |
| `--depth N` | 根目录为第 0 层；0 只统计根直属文件，默认不限层数 |
| `--top N` | 展示最大的 N 个文件/目录，默认 20；不缩小扫描范围 |
| `--min-size SIZE` | 大项展示和重复候选的最小大小，不改变总统计；支持 MiB/MB 等 |
| `--exclude PATH` | 排除路径及其后代，可重复；相对路径以当前工作目录为准 |
| `--allocation` | Windows 文件分配量观察，按文件 ID 去重硬链接；增加查询和内存开销 |
| `--duplicates` | 显式读取候选内容并用 SHA-256 分组；可能耗时和占用较多内存 |
| `--format text/json` | 默认文本；JSON 包含范围、统计、错误和完整性标记 |
| `--output PATH` | 仅显式指定才写报告，父目录必须已存在，拒绝覆盖已有文件 |
| `--no-progress` | 关闭 stderr 进度提示 |

退出码：0 表示完整扫描；2 表示部分覆盖/跳过/错误，或命令行参数错误；130 表示用户中断并返回部分结果。参数错误输出到 stderr，扫描结果为 JSON 时用 `complete`、`error_count`、`skipped_count` 等区分状态。Ctrl+C 可中止遍历和哈希阶段。

### 统计与隐私

- 目录累计大小互相包含，不能相加。逻辑大小可能重复计算硬链接；物理占用及可释放空间未测量，JSON 的 `allocated_bytes` 为 null。
- 显式排除、深度限制、权限不足、重解析点等会导致 `complete=false`；错误和跳过明细最多保留各 200 条，计数保留完整数量。
- 默认不跟随符号链接、联接点、挂载点、离线/召回属性；这些限制也会使某些有效目录不被统计。
- 扫描不是文件系统快照。文件变化会影响统计，重复哈希前后会核对文件标识和时间等元数据；不能保证在敌对并发修改或系统驱动异常下实现文件系统隔离。每走完一个目录会重读它的 mtime，成员增删计入 `concurrent_change`；目录内文件原地改写不改变目录 mtime，看不到。
- 普通扫描只读元数据；重复分析读取候选内容，可能由操作系统更新访问时间。不会上传结果或自动安装软件。
- 报告包含路径和文件名，分享前请检查隐私。不要将真实扫描报告提交到仓库；`reports/` 已加入忽略项。
- Windows 为主要支持平台；其他系统的目录扫描可用于开发测试，不能据此声称清理流程完整兼容。

## 验证

```powershell
python -m unittest discover -s disk-cleanup/tests -v
```

默认测试使用自建临时目录；覆盖扫描、报告比较、计划门禁、Git 审计、Windows 身份和目录链、隔离、回滚、撤销与崩溃恢复、日志中断检查。Windows 实际回收测试默认跳过，符号链接等平台能力不足时也会跳过；以当前运行输出为准。

可选实机测试需要 Windows，且只操作测试自行创建的文件及目录；成功后恢复、校验并清理测试对象，失败时会话保留测试对象供检查（不会放宽断言去掩盖失败）：

```powershell
$env:DISK_CLEANUP_LIVE_RECYCLE_TEST = '1'
$env:DISK_CLEANUP_LIVE_TEST_PARENT = '<已存在的测试父目录>'
python -m unittest discover -s disk-cleanup/tests -v
Remove-Item Env:DISK_CLEANUP_LIVE_RECYCLE_TEST
Remove-Item Env:DISK_CLEANUP_LIVE_TEST_PARENT
```

`recycle_backend.py` 与 `clean.py` 的回收路径是纯标准库 ctypes，实机测试不需要 pywin32。覆盖：普通文件回收、嵌套目录回收、被占用文件拒绝且幸存、`$I`/`$R` 往返还原、进程被杀后按 intent 记录恢复、组策略/容量/`NukeOnDelete` 拒绝路径（单元测试）。跨卷目标、真实断电（目前只模拟到清单半行截断）、需要管理员的卷尚未实测，详见[执行研究与缺口](disk-cleanup/references/recycle-research.md)。

`--allocation` 的 `allocated_bytes` 是**上层口径而不是承诺**：按 128 位 FileId 去重（硬链接只算一次），排除目录与文件系统元数据，并且是删除后可释放空间的**上限**；`reclaimable_bytes` 恒为 `unknown`，观察不完整时 `allocated_bytes` 为 `null`。本机实测（5 个路径含 1 个硬链接）：逻辑 8,388,608B，逐路径分配之和 8,388,608B，按 FileId 去重后 7,340,032B，报告值与去重值一致，硬链接那 1,048,576B 只计一次。

扫描性能实测（本机 `Z:`，447k 文件 / 218 GiB 逻辑大小，缓存预热后）：`scan.py` 全盘 **5.5s**，约 8 万文件/秒；同一棵树的朴素 `os.walk` 遍历 2.1s，但不含每文件身份、类型分类与大文件堆，且会把联接点目标一起走（本盘 `node_modules` 联接点下另有 8.5k 文件，`scan.py` 按设计不进入、不计入）。冷缓存首轮约 26s，属磁盘读放大，不是算法开销。

命名数据流实机（本机 NTFS）：一个 5 字节文件挂 `:hidden` 与 `:Zone.Identifier` 两个流后，`--streams` 报 `files_with_streams: 1`、`alternate_bytes: 26`，同时 `logical_bytes` 仍只算可见内容——流里的字节既不进逻辑字节也不进分配字节，只被单独报告。不加 `--streams` 时该字段为 `null`。

[Agent 行为场景](disk-cleanup/tests/behavior-scenarios.md) 是独立演练清单，不代表所有宿主 Agent 已完成行为验收。

## 目录与维护

- [核心技能](disk-cleanup/SKILL.md)：触发条件、一次授权、计划格式、执行边界与结果报告。
- [计划格式](disk-cleanup/references/plan-schema.md)：`plan.json` 全部字段、门禁规则、退出码与路径保护规则。
- `disk-cleanup/references/`：按场景加载的 Windows、分类和开发工具指南，附官方资料。
- `disk-cleanup/scripts/scan.py`：只读扫描，无删除能力。
- `disk-cleanup/scripts/clean.py`：计划校验、dry-run、回收站执行与 `--undo`。
- `disk-cleanup/scripts/recycle_backend.py`：可证明的回收后端，纯标准库。
- `disk-cleanup/scripts/recycle_bin.py`：`$I`/`$R` 解析、按卷枚举与还原。
- `disk-cleanup/scripts/locks.py`：占用探针与 Restart Manager 进程归因。
- `disk-cleanup/scripts/recycle_selfcheck.py`：本机回收/还原能力的端到端自检，不碰用户路径。
- `disk-cleanup/scripts/recycle_audit.py`：只读审计回收站与执行日志的交叉核对，不 prune、不还原。
- `disk-cleanup/scripts/git_audit.py`：只读 Git 风险审计。
- `disk-cleanup/tests/`：扫描与执行器测试、行为演练场景。

工具版本和系统入口变化时，先检查本机帮助及官方资料，再更新对应参考。贡献修改时运行相关测试，并核实是否改变授权范围或数据保留规则。

## 许可证

[MIT](LICENSE)，Copyright (c) 2026 QHDSM。
