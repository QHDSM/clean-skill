# 磁盘清理 · clean-skill

# 当前状态：只读预览

`clean.py --go` 已停用。旧 Shell 删除接口无法保证操作一定进入回收站；目录指纹也不能验证子孙文件。预览可有限查找 `.git` 标记，但不能代替风险审计或证明不存在 Git 数据。请勿把 dry-run 成功当作安全执行证明。扫描和计划预览仍可使用。


面向 Windows 的 Agent Skill：分析全机磁盘占用，在一次明确授权内自主清理，并默认保留个人数据。

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

重复文件扫描需显式指定 `--duplicates`，会读取候选文件内容。结果中的 `distinct_file_ids_observed` 表示是否观察到不同的非零文件身份；为 `false` 时不能证明路径是独立副本。哈希匹配只是分析线索，不能代替操作前复核。

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
先展示一次授权策略，批准后直接执行范围内的操作，不逐项询问。
保留个人文件、下载、云同步、备份和回收站，排除 D:\Work。
```

```text
使用 $disk-cleanup 分析 D:\Projects 的开发缓存，目标释放 10 GB。
```

授权只对当前任务及明确范围有效。系统提权仍通过正常机制；不会绕过 UAC 或宿主权限。永久删除、关闭应用、重启等必须在授权中有明确选择。用户只要求分析时保持只读。

## 执行清理（`clean.py`）

`clean.py` 只接受 agent 产出的 `plan.json`，**默认 dry-run**：

```powershell
python disk-cleanup/scripts/clean.py --plan plan.json          # 只打印将要做什么
python disk-cleanup/scripts/clean.py --plan plan.json --go     # 当前拒绝执行
python disk-cleanup/scripts/clean.py --plan plan.json --go --json
```

单路径模式需要两次输入同一对象（验证「知道删什么」，不是「会打字」）：

```powershell
python disk-cleanup/scripts/clean.py --path 'D:\tmp\x' --confirm-path 'D:\tmp\x' --go
```

退出码：`0` 全部按要求处理；`2` 计划被门禁拒绝且**零执行**；`3` 计划通过但有目标被拒绝或失败。

每次执行向 `~/.disk-cleanup/audit-YYYYMMDD.jsonl` 追加一条记录，含路径、结果、大小与指纹。日志只存本地，不要提交到仓库。

## 独立使用只读扫描工具

需要 Python 3.10+，只使用标准库。以下命令从仓库根目录运行；扫描本身不会执行清理。

```powershell
python disk-cleanup/scripts/scan.py --list-drives
python disk-cleanup/scripts/scan.py 'C:\' 'D:\' --top 30 --format json
python disk-cleanup/scripts/scan.py 'D:\Projects' --depth 2 --exclude 'D:\Projects\private'
python disk-cleanup/scripts/scan.py 'D:\Downloads' --duplicates --min-size 10MiB
python disk-cleanup/scripts/scan.py 'D:\Projects' --format json --output 'report.json'
```

| 参数 | 含义 |
|---|---|
| 一个或多个目标路径 | 必须显式指定；不接受互相重叠的根目录 |
| `--list-drives` | Windows 固定磁盘及容量，不能与目标路径或输出文件混用 |
| `--depth N` | 根目录为第 0 层；0 只统计根直属文件，默认不限层数 |
| `--top N` | 展示最大的 N 个文件/目录，默认 20；不缩小扫描范围 |
| `--min-size SIZE` | 大项展示和重复候选的最小大小，不改变总统计；支持 MiB/MB 等 |
| `--exclude PATH` | 排除路径及其后代，可重复；相对路径以当前工作目录为准 |
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

测试使用临时目录，不清理真实电脑。共 60 项：

- `test_scan.py`（14 项）：大小统计、范围/深度/排除、只读性质、JSON、报告防覆盖、重复识别、文件变化、访问失败、中断、硬链接、Windows 联接点。
- `test_clean.py`（46 项）：计划门禁全部规则（schema、指纹、路径、action/risk、scope、排除项、Git 闸门）、preflight 保留路径、指纹重验（含 `st_ino` 为 0 的降级路径、读取后不误判为变化）、dry-run 不落地、单路径双输入确认。

实际回收站执行尚不可用；测试不证明回收站行为，也不证明目录内容或 Git 数据安全。

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
