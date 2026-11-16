# Windows 可恢复执行：研究记录

## 现状：已落地可证明后端

`scripts/recycle_backend.py` 已接入 `clean.py --go`，执行只做回收站移动。它不再依赖"原路径不存在"这种无法区分回收与永久删除的观察，而是：

1. 删除前用 `CreateFileW` 以 `DELETE|FILE_READ_ATTRIBUTES`、共享 `READ|DELETE` 打开目标并**持有句柄**跨过整个 `IFileOperation`。持 DELETE 访问在别处有独占者时返回 `ERROR_SHARING_VIOLATION(32)`，所以开句柄本身就是占用探针。
2. 删除后对同一句柄调用 `GetFinalPathNameByHandleW`：落在 `\$RECYCLE.BIN` 下判 `RECYCLED`，落在 `\$Extend\$Deleted` 下判 `PERMANENTLY_DELETED`，路径未变判 `NOT_DELETED`，其余判 `UNKNOWN`。文件 ID 前后一致，说明看的是同一个对象。
3. 逐项 COM 回调 `PreDeleteItem` 检查 `0x80` 位（"将进入回收站"）；没有该位直接返回 `E_ABORT`，实测可挡住 SUBST 盘上的强制永久删除。`PostDeleteItem` 记录 Shell 是否给出了回收项目。
4. 卷能力先验证：只信 NTFS/ReFS，检查当前 SID 的 `\$RECYCLE.BIN` 可写、`MaxCapacity` 足以容纳目标、`NukeOnDelete` 未开启。超出容量时 Windows 会静默转永久删除，所以这一步是拒绝而不是警告。
5. `recycle.status` 只有 `RECYCLED` 算成功。`PERMANENTLY_DELETED`、`NOT_DELETED`、`UNKNOWN`、`REFUSED` 都按失败上报。

实机验证：普通文件与嵌套目录均返回 `RECYCLED`，`proof.final_path` 指向 `$R` 对象、`recycle_bin.original_path_matches` 为真；被占用文件在开句柄阶段即 `REFUSED` 且原对象幸存；端到端 dry-run → `--go` → `--undo` 往返后内容逐字节一致。

`tests/test_recycle_backend.py` 的 3 个 live 测试默认跳过，需要 `DISK_CLEANUP_LIVE_RECYCLE_TEST=1` 和现有父目录 `DISK_CLEANUP_LIVE_TEST_PARENT`。跨卷目标与“回收站被组策略禁用”现在都有明确答复：回收站按卷生效，目标总落在自己的卷上；组策略 `NoRecycleFiles=1`、`MaxCapacity=0`、`NukeOnDelete=1` 会被 `volume_profile` 读出来并判为`verified_capable=False`，`--go` 拒绝执行。仍未覆盖：并发替换目录内部。断电与进程被杀现在覆盖到"可恢复"：执行器在移动前写 intent 记录并 `fsync`，移动后写成对记录；`clean.py --undo` 对只剩 intent 的对象，用 intent 时间戳在原路径上以 ±5 秒窗口从回收站找回，报 `recovered`，实测（人为 kill 后追加半行）`restored 2 | recovered 1 | failed 0` 且文件全部回位。仍不覆盖：磁盘写入缓存未刷盘、日志文件本身被删。计划证据在持句柄期间被重验：文件 ID、大小、mtime 由句柄读回，与计划不一致则拒绝、Shell 不被调用；实测把路径换成同大小同 mtime 的另一个对象、并让 preflight 与 `reverify` 返回旧结论后，仍被拦下（对象幸存）。 句柄用 `FILE_FLAG_OPEN_REPARSE_POINT` 开在对象本身：路径在执行时变成联接点/符号链接，同样被拒（实测把目录换成指向另一个目录的 junction，status=REFUSED，目标目录与其内容完好，Shell 未被调用）。回收站条目被第三方清空不能阻止，但现在可被发现：`recycle_audit.py --journal <该次日志>` 按 `$I` 身份交叉核对，报 `present`/`missing`，并同时给出 `orphans` 分类与原路径冲突；`missing` 只表示“现在不在回收站里”，撤销过和被别人清空两种情况审计分不出。以下为研究过程记录，保留其原貌（"仍停用"等表述属于当时的结论）。

`scripts/recycle_experimental.py` 是隔离原型，没有 CLI，也没有接入 `clean.py`。模拟测试覆盖接口调用顺序、执行失败与中止查询；即使 Shell 返回成功，原型也仅返回 `UNKNOWN`，不声称可恢复。该原型未完成实机验证，不应用于用户文件。

`scripts/stage_experimental.py` 是另一项未接入执行器的研究原型：使用已打开句柄核对卷序列号和 128 位文件 ID，再通过 `SetFileInformationByHandle` 在同一目录重命名，不允许覆盖目标。本机临时文件测试覆盖了文件、目录、同名冲突和打开前的路径替换。原型没有历史父目录身份绑定、崩溃恢复或失败回滚，也没有授权门禁；不能用于用户清理。即使通过身份检查，文件内容和目录内部仍可能变化。

`scripts/transaction_experimental.py` 在自建测试文件上演练隔离后的回滚：后端返回或抛出异常后，如果隔离路径仍存在，会按文件身份尝试恢复原名，且不覆盖新出现的原路径；隔离对象消失、恢复失败或恢复后身份无法确认均单独报告。该实验使用注入的后端，没有自动崩溃恢复或用户授权，不得用于实际清理。

实验事务会独占创建 JSONL 日志，在隔离前记录原路径、隔离路径和身份，在调用后端或尝试回滚前写入意图，并 flush/fsync。同步失败时不开始对应操作。日志写在测试目录，含本地路径，不能提交到仓库。`python scripts/journal_experimental.py '<日志>'` 只读报告最后事件和路径当前身份，不自动恢复。子进程强制退出测试验证了隔离后仍能定位对象；截断、格式错误或序号不连续时报告证据不完整。日志未经认证，不能证明授权或排除篡改；fsync 也不是所有硬件和断电情形下的持久性保证。

Windows 实机测试已覆盖 COM 初始化、接口创建、设置标志、创建临时测试文件的 Shell 项并排队操作；不调用 `PerformOperations`，退出后测试文件内容保持不变。还检查了不存在的测试路径在执行前失败、接口释放可重复调用。这些测试不验证实际回收、取消后的状态或文件可恢复性。

一次 Windows 实机实验使用随机命名的自建临时文件，调用原型后，通过回收站中唯一的文件名及原目录匹配定位该文件，再通过 Shell 还原并校验随机内容，成功后仅移除该测试文件。`tests/test_recycle_live.py` 提供同样的可选测试；默认跳过，需要 Windows、pywin32，以及显式设置 `DISK_CLEANUP_LIVE_RECYCLE_TEST=1`。测试失败时保留测试目录供检查，不清空回收站。这只验证当前机器上的一个普通文件，不能推断其他卷、容量、策略、文件类型或失败情况下的行为。

本机还分别在 C 盘临时目录和 D 盘自建目录测试：普通文件及带嵌套文件的目录均能恢复且内容一致；独占打开的测试文件在尝试后仍存在且内容一致。可通过 `DISK_CLEANUP_LIVE_TEST_PARENT` 为测试指定现有父目录。测试只清理自身成功恢复并校验的对象；不修改回收站设置。这些结果仍不覆盖不可回收卷、回收站容量、系统策略、取消或并发替换。

- [Microsoft IFileOperation 标志文档](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags) 描述 `FOFX_RECYCLEONDELETE` 在 Windows 8 引入，用于将删除项送往回收站；`FOF_ALLOWUNDO` 仅在可能时保留撤销信息。文档还描述了 `FOFX_EARLYFAILURE` 与 `FOF_NOERRORUI` 的组合，但本项目尚未验证不可回收位置、容量不足和系统策略等条件。
- [Tuzfucius/disk-cleanup-skills 的回收后端](https://github.com/Tuzfucius/disk-cleanup-skills/blob/main/src/disk_cleanup/cleaner/recycle.py) 使用 `IFileOperation`、`FOFX_RECYCLEONDELETE` 和 `FOFX_EARLYFAILURE`，并检查中止状态。[执行器](https://github.com/Tuzfucius/disk-cleanup-skills/blob/main/src/disk_cleanup/cleaner/executor.py) 在调用前隔离并重验对象。可借鉴隔离、验证和结果分级，但其[可选实机测试](https://github.com/Tuzfucius/disk-cleanup-skills/blob/main/tests/test_windows_recycle_integration.py)只断言原路径消失，不能单独证明文件可恢复或失败时没有永久删除。
- [Microsoft 中止状态文档](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-getanyoperationsaborted) 要求无论 `PerformOperations` 返回成功或失败，都检查中止状态；返回成功也可能表示操作已被取消。
- [Microsoft FILE_RENAME_INFO 文档](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info) 描述句柄重命名结构及禁止覆盖标志；不能由此推断父路径不会并发变化。

恢复执行前需要独立验证：使用隔离的测试文件和测试环境，检查回收站中实际可恢复对象；覆盖不可回收卷或策略、容量限制、取消与失败、并发替换、目录内部变化、进程中断与回滚；正确区分成功、拒绝、失败、未知和恢复失败。计划授权、目标身份、路径和排除项也须在操作时绑定并复核。任何未知结果不得报告为成功；未经验证不得重新启用 `--go`。

当前隔离调用会从卷根到直接父目录逐级持有句柄；实验事务在后端调用和回滚期间也保持这些句柄。句柄请求目录列表及属性读取，且不共享删除。Windows 临时目录测试验证持有期间祖先和直接父目录改名失败、释放后成功，以及准备失败时释放已打开的句柄。属性读取权限单独使用时曾未能阻止直接父目录改名，因此已保留行为测试。此机制本身不防止目录内部文件内容变化；其他文件系统和更广泛的竞争行为尚待验证。

实验事务现要求调用者提供此前采集的完整父目录链，使用 `capture_parent_chain(source)` 在持有句柄时记录每级路径、卷序列号和 128 位文件 ID。事务再次打开并持有整条链，从同一批句柄读取身份后逐项比较；缺失、截短、改变或不可读取时不隔离文件，日志意图包含实际验证的父目录链。临时对象测试验证：文件 ID 保持不变但父目录被替换时仍拒绝操作。此证据已通过 windows_parent_chain 字段接入正式计划预览；没有授权或认证，不证明文件内容不变或完整执行流程安全。

父目录句柄和身份采集已抽取到只读模块 `windows_parents.py`，实验隔离与正式预览共用实现。预览只在检查期间持有句柄，结束即释放；正式清理现已改由 `recycle_backend.py` 持有目标句柄（不再持有整条父目录链）。

可选实机测试现将事务与真实回收后端组合：在父目录链保持句柄的情况下隔离自建文件或嵌套目录，调用回收原型，通过唯一名称与原目录找到回收项目并恢复到隔离路径，再由事务按身份回滚到原名。测试要求内容、文件 ID 和最终日志状态都匹配，且另外确认测试回调确实完成回收恢复，避免后端异常被事务回滚掩盖。本机 C、D 两块磁盘均通过该流程；这是测试内的回收后立即恢复，不等于正式清理成功，也不覆盖容量限制、不可回收策略或自动崩溃恢复。测试默认关闭。（当时结论"正式 --go 仍停用"已被 `recycle_backend.py` 的句柄最终路径判据取代。）

`recycle_sink_experimental.py` 为原型提供可选的逐项 COM 回调观察。根据 [Microsoft PostDeleteItem 文档](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperationprogresssink-postdeleteitem)，回调包含实际删除 HRESULT，非空的新项目指针表示回收站中的项目；原型只记录是否提供该指针，不在回调结束后使用借用指针。HRESULT 按失败位判断，不能仅接受零值。本机普通文件和嵌套目录实测收到非零成功值及非空回收项目，随后仍独立执行恢复和内容验证。观察器在删除前缺少回收请求标志时拒绝，但该标志仅表示尽可能回收，不能证明未来不会降级；当时后端因此仍报告 UNKNOWN、正式执行停用。现在的 `recycle_backend.py` 用删除后内核返回的最终路径消除这一不确定性，不再依赖该标志本身作结论。回调检查在测试对象恢复清理后断言，避免证据断言阻断测试恢复。

原型新增显式拒绝删除的回调模式：`NativeAdapter(permit_delete=False)` 会强制附加观察器，即使未请求观察；`PreDeleteItem` 返回失败以取消操作。C、D 两块磁盘的自建文件和嵌套目录测试确认，回调拒绝后隔离对象仍可按身份回滚到原名，随机内容和文件 ID 保持，且没有成功回收项目的回调证据。此测试仅覆盖程序主动拒绝，不覆盖用户界面取消、系统终止或断电。当时它没有改变执行入口的停用状态；该模式（`0x80` 位检查 + `E_ABORT`）现已整合进 `recycle_backend.py` 的正式路径。

---

# 与 freedesktop.org Trash 规范对照（2026-11-16）

本节不是移植计划。Linux 的 Trash 规范（`$XDG_DATA_HOME/Trash`、跨挂载点的 `.Trash/$uid` 或 `.Trash-$uid`、`info/*.trashinfo` 里的 `Path=` 与 `DeletionDate=`）与 Windows 的 `$Recycle.Bin\<SID>\` + `$I`/`$R` 是两套独立实现。对照的价值在于**别人把边界写成了条文**，可以拿来逐条核对我们有没有漏。

| 规范要求 | 我们的做法 | 结论 |
|---|---|---|
| 回收站按用户隔离 | 按 SID 分目录；别的 SID 读不到时整盘标 `INCOMPLETE` | 一致，但我们明确区分"证据不足"与"回收站为空" |
| 回收站必须在文件所在挂载点上，不得跨挂载点搬运 | `volume_profile` 按目标所在卷读回收站与策略 | 一致；跨卷目标在预览阶段就被拒 |
| 无法建立/使用回收站时**不得删除** | `verified_capable=False` 即拒绝；回调否决任何未标记为回收的删除 | 一致，且我们有第二道守卫 |
| 还原时目标已存在不得静默覆盖 | 报冲突、保留回收站副本、绝不覆盖 | 一致 |
| 文件名冲突需保证唯一 | 交给 Windows 生成 `$R` 名；撤销只认 `$I`/`$R` 配对，不靠改名 | 一致，路径依赖更少 |
| 元数据可被外部修改/损坏 | `$I` 二进制解析失败、命名为目录等情况归入 `invalid_i` / `i_named_directory` | 规范未要求；我们更严 |
| 元数据缺失但载荷存在（或反之） | `orphans` 四分类：`i_without_r`、`r_without_i`、`invalid_i`、`i_named_directory` | 规范未定义；我们更严 |
| 还原后如何证明"回来的是原来那个东西" | 用清单里的 128 位 FileId 与大小复核，不符报 `RESTORED BUT NOT THE SAME OBJECT` | 规范与 Windows 都不要求；这是我们的净增量 |
| 并发操作 | 规范不论；我们持每用户互斥量，执行与撤销互斥 | 我们更严 |
| 保留期/容量上限 | 规范不论；我们读 `MaxCapacity`、`NukeOnDelete`、`NoRecycleFiles` | 我们跟随系统策略 |

## 对照后仍存的差距

1. **只读卷与不可写回收站的前置检查**：`recycle_selfcheck.py` 在临时目录证明"本机能回收并还原"，但那是自检时刻、自检盘符。执行批次时如果某卷在自检之后变为只读或回收站目录被改权限，目前靠执行期逐项失败发现，而不是预览阶段一次性报出。**2026-11-16 已补一半**：`volume_profile` 现在读 `GetVolumeInformationW` 的 `FILE_READ_ONLY_VOLUME` 标志，只读卷在预览阶段就 `verified_capable=False`、`fits=false`，且不需要写任何探针文件。剩下的一半是**回收站目录的写权限在执行前被改**——这仍然只能逐项发现，因为写探针会在 `$RECYCLE.BIN` 里留下可能被审计误判为 `r_without_i` 的对象，代价高于收益。
2. **多用户回收站**：别的 SID 无权限时我们只能标 `INCOMPLETE`，无法像 per-uid 目录那样按权限逐个读取。这是 Windows ACL 的限制，不是实现选择，但报告里必须继续说"证据不足"，不能说"没有条目"。
3. **可移动介质**：FAT/exFAT 卷可能根本没有回收站。规范对此有"顶层 `.Trash` 不存在则不得删除"的明确要求；我们靠 `verified_capable` 拒绝，但没有针对可移动介质的专门提示。
4. **`DeletionDate` 的等价物**：`$I` 记录删除时间，但精度与来源（本地时间 vs UTC）在不同 Windows 版本上不一致。`--undo` 的 ±5 秒窗口已考虑这一点，仍未做跨版本验证。

第 1 条是唯一会改变执行路径的缺口，其余三条是报告措辞与覆盖面问题。
