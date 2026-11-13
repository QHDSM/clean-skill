# 计划格式（disk-cleanup-plan/v1）

**当前只能预览，`--go` 一律拒绝执行。** 目录指纹不覆盖子孙内容；预览仅有限地发现 `.git` 标记，Git 风险证据需要另行审计。预览结果不是安全执行的保证。

每项预览的 `git_discovery` 包含 `markers`、`complete`、`issues` 和 `entries_checked`。搜索不跟随重解析点，每项目最多检查 5000 个条目、约 0.5 秒（单次文件系统调用可能超时）；权限错误、跳过的链接或触及上限会标记不完整。裸仓库、外部 Git 目录及扫描期间的并发变化不一定被发现。即使 `complete` 为 true，也不代表没有 Git 风险。

`clean.py` 的唯一输入。字段缺失或类型不符会导致**整份计划被拒绝**，一个对象都不会删除。

## 顶层

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema` | string | ✅ | 必须是 `disk-cleanup-plan/v1` |
| `generated_unix` | integer | 建议 | 计划生成时间（Unix 秒），用于判断计划是否过旧 |
| `authorization` | object | ✅ | 见下 |
| `targets` | array | ✅ | 非空；单份计划最多 200 项 |
| `note` | string | ❌ | 给用户的说明，不参与校验 |

## authorization

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `scope` | string[] | ✅ | 非空；每项必须是带盘符的绝对路径（如 `C:\`、`D:\Projects`） |
| `excluded` | string[] | ❌ | 与排除根有祖先或后代关系的目标均拒绝；优先于 `scope` |
| `permanent_delete` | boolean | ❌ | 必须为 `false` 或省略。本执行器不做永久删除 |
| `git_acknowledged` | boolean | ❌ | 见「Git 风险」一节 |

## targets[]

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | ✅ | 带盘符的绝对路径 |
| `action` | string | ✅ | `trash` / `keep` / `report` |
| `risk` | string | ✅ | `safe` / `caution` / `never` |
| `reason` | string | ✅ | 非空。写清「为什么可删」，不要只写「缓存」 |
| `evidence.size_bytes` | integer | ❌ | 扫描时的逻辑大小；用于报告估算收益 |
| `evidence.fingerprint` | integer[5] | ✅ | `[st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns]`；比对时只用前四项 |
| `evidence.git` | object\|null | ❌ | `git_audit.py` 的结论 |
| `evidence.tree_metadata` | object\|null | ❌ | `tree_evidence.py` 对同一目录生成的完整结果；用于预览时比较 |

`scan.py --format json` 返回的 `largest_files`、`largest_directories` 每项含扫描时取得的 `fingerprint`，可直接复制到对应路径的 `evidence.fingerprint`。目录指纹记录的是目录自身在遍历开始时的状态，不代表递归内容快照，也不保证聚合大小与该指纹同时成立。扫描不是原子快照，创建计划时重新取值不能声称是扫描时证据。

对需要比较内部变化的目录，可先运行 `python scripts/tree_evidence.py '<目录绝对路径>'`，将完整 JSON 对象放入该目标的 `evidence.tree_metadata`。预览会重新采集并比较；采集不完整或摘要不一致时跳过该目标。采集只读取相对路径、类型、设备、文件标识、逻辑大小和修改时间，不读取文件内容；默认上限 100000 项、约 30 秒（单次系统调用可能超过）。遇到链接、重解析点、跨文件系统、权限错误或上限会返回 `complete: false`，不能作为证据。该比较不是原子快照，不保证识别保留元数据的内容修改，也无法消除并发变化或路径替换；匹配不意味着安全、授权或可执行。

### action 语义

| 值 | 行为 |
|---|---|
| `trash` | 走完整流程：门禁 → preflight → 指纹重验 → 回收站 |
| `keep` | 不执行，仅在结果中记录（用于向用户说明「这项保留」） |
| `report` | 不执行，仅在结果中记录（用于只读项） |

### risk 语义

| 值 | 行为 |
|---|---|
| `safe` | 预览时标记为低风险，不证明安全 |
| `caution` | 预览时标为需注意 |
| `never` | **绝不执行**。即使写进计划也会被 preflight 拒绝并计入 `refused` |

## Git 风险

计划包含 Git 仓库（或其内部路径）时，先跑 `scripts/git_audit.py <路径>`，把结论放进 `evidence.git`：

```json
"evidence": {
  "size_bytes": 812345678,
  "fingerprint": [1234, 5678, 812345678, 1730000000000, 1729000000000],
  "git": {"verdict": "LOCAL_ONLY_DATA_PRESENT", "all_content_synced": false,
          "risks": ["untracked worktree entries are not in Git"]}
}
```

闸门规则：`risk` 为 `safe` 但 `verdict` 不是 `SAFE_TO_CONSIDER` 时，必须同时设置
`authorization.git_acknowledged: true`，否则整份计划被拒绝。此检查仅在提供了 Git 证据时生效，不能自动识别仓库，也不能独立防止误删。

`verdict` 取值：

| verdict | 含义 | 建议 |
|---|---|---|
| `SAFE_TO_CONSIDER` | 审计结果满足其判定条件（不证明远端实时状态） | 可按 `safe` 处理 |
| `LOCAL_ONLY_DATA_PRESENT` | 有未推送提交或本地独有引用 | 先向用户说明；确认后设 `git_acknowledged` |
| `LOCAL_MODIFICATIONS` | 有未提交改动/暂存/冲突 | 同上 |
| `UNVERIFIED` | 证据不足（无 remote 或检查失败） | 不能按安全处理 |

## 门禁规则（任一不过 → 退出码 2，零执行）

1. `schema` 必须精确匹配。
2. `authorization.scope` 非空，且每项带盘符。
3. `authorization.permanent_delete` 不得为 true。
4. `targets` 非空且不超过 200 项。
5. 每项 `path` 必须是带盘符的绝对路径。
6. 每项 `action`、`risk` 必须在枚举内。
7. 每项 `reason` 非空。
8. 每项 `evidence.fingerprint` 必须是 5 个整数。
9. 每项 `path` 必须落在某个 `scope` 内。
10. 每项 `path` 不得与任何 `excluded` 重叠（包括目标为排除项的父目录）。
11. Git 风险闸门（见上）。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部按要求处理（dry-run 或全部成功） |
| 2 | 计划被门禁拒绝，**没有执行任何操作** |
| 3 | 计划通过但部分目标被拒绝或失败（详见 `results[]`） |

## 平台路径规则（preflight）

以下判断在门禁之后、执行之前逐项进行：

- 目标不存在 → 拒绝（`does not exist`）
- 路径链上任意一级是符号链接/重解析点/离线或召回对象 → 拒绝
- 盘根 → 拒绝
- `C:\Windows` 及其**整个子树** → 拒绝
- `C:\$Recycle.Bin`、`C:\System Volume Information`、`C:\Recovery`、`C:\Boot` 整个子树 → 拒绝
- `C:\ProgramData`、`C:\Program Files`、`C:\Users` **目录本身** → 拒绝；其下被明确点名的缓存子目录**允许**（例：`C:\ProgramData\Package Cache`）
- 用户配置根 `C:\Users\<name>` → 拒绝
- 用户目录顶层 `Documents / Desktop / Downloads / Pictures / Music / Videos / OneDrive / AppData` → 拒绝
- 路径中出现 `.git` 目录名 → 拒绝

## 示例：合法计划

```json
{
  "schema": "disk-cleanup-plan/v1",
  "generated_unix": 1730000000,
  "authorization": {
    "scope": ["C:\\"],
    "excluded": ["C:\\Users\\me\\Documents", "C:\\Users\\me\\OneDrive"],
    "permanent_delete": false
  },
  "targets": [
    {
      "path": "C:\\Users\\me\\AppData\\Local\\Temp\\build-cache",
      "action": "trash",
      "risk": "safe",
      "reason": "构建缓存，mtime 45 天，无进程占用，可重建",
      "evidence": {
        "size_bytes": 1234567,
        "fingerprint": [1234567, 89, 1234567, 1730000000000, 1729000000000]
      }
    },
    {
      "path": "C:\\ProgramData\\Package Cache",
      "action": "trash",
      "risk": "caution",
      "reason": "补丁安装缓存，QFE 已应用，可重新下载",
      "evidence": {
        "size_bytes": 2000000000,
        "fingerprint": [1234567, 90, 2000000000, 1729000000000, 1728000000000]
      }
    }
  ]
}
```

## 示例：会被拒绝的计划

```json
{"schema": "disk-cleanup-plan/v1",
 "authorization": {"scope": ["C:\\"], "excluded": [], "permanent_delete": true},
 "targets": [{"path": "C:\\Windows\\Temp", "action": "trash", "risk": "safe",
              "reason": "临时", "evidence": {"fingerprint": [1, 2, 3, 4, 5]}}]}
```

三个独立问题：`permanent_delete` 为 true（规则 3）、`C:\Windows\Temp` 在受保护子树内（preflight）、`reason` 过于笼统（不拦但报告质量差）。

## 与 `--path` 单路径模式的关系

需要立即处理单个对象、不写计划时：

```powershell
python scripts/clean.py --path 'C:\temp\x' --confirm-path 'C:\temp\x' --go
```

两个参数规范化后的路径字符串必须相同；这不证明文件身份。
单路径模式仍经过同一套 preflight 与指纹校验（指纹取当前状态，不做历史比对）。
