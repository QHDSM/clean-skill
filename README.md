# 磁盘清理 · clean-skill

## 当前状态：扫描与计划预览

`clean.py --go` 已停用。旧 Shell 删除接口无法保证操作一定进入回收站；目录指纹也不能验证子孙文件。预览可有限查找 `.git` 标记，但不能代替风险审计或证明不存在 Git 数据。请勿把 dry-run 成功当作安全执行证明。扫描和计划预览仍可使用。


面向 Windows 的 Agent Skill：分析磁盘占用、比较历史增长、识别 Git 数据风险，并生成和校验清理计划。真实执行正在实验验证，当前交付入口只做预览。

**仓库名：`clean-skill`；技能调用名：`disk-cleanup`；许可证：MIT。**

## 工作方式

1. 查看实际固定磁盘、用户目标和排除项。
2. 展示本次授权范围、清理类型、删除方式和权限要求。
3. 只读扫描并分类，把执行意图写成 `plan.json`（每项带路径、理由、风险与指纹）。
4. 由 `clean.py` 校验并预览计划；真实执行目前停用。
5. 报告实际处理结果、拒绝与跳过原因，以及每盘可用空间变化。

覆盖系统临时文件、应用缓存、开发工具、容器，以及个人文件、备份、云同步和重复文件分析。后几类默认保留；缓存也必须确认用途、位置、可重建性及使用状态。全机扫描默认选择可访问本地固定磁盘；移动、网络和远程 Docker 环境不自动纳入。

## 脚本能力

| 脚本 | 能力 | 是否删除 |
|---|---|---|
| `scripts/scan.py` | 只读扫描、分类、重复文件检测 | **无任何删除能力** |
| `scripts/clean.py` | 校验并预览 `plan.json` | 执行已停用 |
| `scripts/git_audit.py` | 只读 Git 风险审计 | 无 |
| `scripts/tree_evidence.py` | 有限采集目录树元数据，供计划预览比较 | 无 |
| `scripts/windows_identity.py` | 只读获取 Windows 句柄级文件身份，供计划预览比较 | 无 |
| `scripts/windows_parents.py` | 采集完整父目录身份链，供计划预览比较 | 无 |
| `scripts/compare_scans.py` | 比较两份已保存的扫描报告，识别可比较的增长 | 无 |

重复文件扫描需显式指定 `--duplicates`，会读取候选文件内容。结果中的 `distinct_file_ids_observed` 表示是否观察到不同的非零文件身份；为 `false` 时不能证明路径是独立副本。哈希匹配只是分析线索，不能代替操作前复核。

Windows 上可加 `--allocation` 查询文件系统报告的文件分配量。`allocation_observation` 按句柄级文件 ID 去重硬链接，并报告覆盖完整性；它排除目录和文件系统元数据开销，不代表可释放空间。默认扫描不做额外分配查询。

`clean.py` 的安全边界（在代码里，不在文档里）：

- 计划模式读取计划，不重放扫描结果；执行功能当前停用。
- 门禁校验 schema、指纹完整性、路径越权与排除项；任一项不过则**零执行**。
- 预览时重验对象自身指纹的前四项；目录内的深层变化不一定能发现。
- 当前任何 `--go` 都会被拒绝。
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
```

计划模式验证范围、排除项、指纹和所提供的证据。单路径模式只预览；双输入比较规范化后的路径字符串，不证明文件身份。`--go` 当前一律以退出码 2 拒绝，且不会写执行审计日志。实验模块未连接该入口。

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
- 扫描不是文件系统快照。文件变化会影响统计，重复哈希前后会核对文件标识和时间等元数据；不能保证在敌对并发修改或系统驱动异常下实现文件系统隔离。
- 普通扫描只读元数据；重复分析读取候选内容，可能由操作系统更新访问时间。不会上传结果或自动安装软件。
- 报告包含路径和文件名，分享前请检查隐私。不要将真实扫描报告提交到仓库；`reports/` 已加入忽略项。
- Windows 为主要支持平台；其他系统的目录扫描可用于开发测试，不能据此声称清理流程完整兼容。

## 验证

```powershell
python -m unittest discover -s disk-cleanup/tests -v
```

默认测试使用自建临时目录；覆盖扫描、报告比较、计划门禁、Git 审计、Windows 身份和目录链、隔离、回滚及日志中断检查。Windows 实际回收测试默认跳过，符号链接等平台能力不足时也会跳过；以当前运行输出为准。

可选实机测试需要 Windows 和已安装的 pywin32，只操作测试自行创建的文件及目录；成功后恢复、校验并清理测试对象，失败时可能保留测试目录供检查：

```powershell
$env:DISK_CLEANUP_LIVE_RECYCLE_TEST = '1'
python -m unittest discover -s disk-cleanup/tests -p test_recycle_live.py -v
Remove-Item Env:DISK_CLEANUP_LIVE_RECYCLE_TEST
```

`DISK_CLEANUP_LIVE_TEST_PARENT` 可指定测试对象的现有父目录。测试不修改回收站设置或处理已有回收站项目。实机覆盖普通文件、嵌套目录、独占占用、逐项回调及程序主动取消；容量、策略、界面取消和断电等尚未完整验证。成功测试不等于正式清理能力已可用，详见[执行研究与缺口](disk-cleanup/references/recycle-research.md)。

[Agent 行为场景](disk-cleanup/tests/behavior-scenarios.md) 是独立演练清单，不代表所有宿主 Agent 已完成行为验收。

## 目录与维护

- [核心技能](disk-cleanup/SKILL.md)：触发条件、一次授权、计划格式、执行边界与结果报告。
- [计划格式](disk-cleanup/references/plan-schema.md)：`plan.json` 全部字段、门禁规则、退出码与路径保护规则。
- `disk-cleanup/references/`：按场景加载的 Windows、分类和开发工具指南，附官方资料。
- `disk-cleanup/scripts/scan.py`：只读扫描，无删除能力。
- `disk-cleanup/scripts/clean.py`：计划校验与 dry-run；执行已停用。
- `disk-cleanup/scripts/git_audit.py`：只读 Git 风险审计。
- `disk-cleanup/tests/`：扫描与执行器测试、行为演练场景。

工具版本和系统入口变化时，先检查本机帮助及官方资料，再更新对应参考。贡献修改时运行相关测试，并核实是否改变授权范围或数据保留规则。

## 许可证

[MIT](LICENSE)，Copyright (c) 2026 QHDSM。
