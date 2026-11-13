# Windows 可恢复执行：研究记录

当前 `clean.py --go` 停用。研究候选不等于已验证可用，不得据此开启执行。

`scripts/recycle_experimental.py` 是隔离原型，没有 CLI，也没有接入 `clean.py`。模拟测试覆盖接口调用顺序、执行失败与中止查询；即使 Shell 返回成功，原型也仅返回 `UNKNOWN`，不声称可恢复。该原型未完成实机验证，不应用于用户文件。

`scripts/stage_experimental.py` 是另一项未接入执行器的研究原型：使用已打开句柄核对卷序列号和 128 位文件 ID，再通过 `SetFileInformationByHandle` 在同一目录重命名，不允许覆盖目标。本机临时文件测试覆盖了文件、目录、同名冲突和打开前的路径替换。原型没有历史父目录身份绑定、崩溃恢复或失败回滚，也没有授权门禁；不能用于用户清理。即使通过身份检查，文件内容和目录内部仍可能变化。

`scripts/transaction_experimental.py` 在自建测试文件上演练隔离后的回滚：后端返回或抛出异常后，如果隔离路径仍存在，会按文件身份尝试恢复原名，且不覆盖新出现的原路径；隔离对象消失、恢复失败或恢复后身份无法确认均单独报告。该实验使用注入的模拟后端，未连接真实回收；没有自动崩溃恢复或用户授权，不得用于实际清理。

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

父目录句柄和身份采集已抽取到只读模块 `windows_parents.py`，实验隔离与正式预览共用实现。预览只在检查期间持有句柄，结束即释放；正式清理仍停用。
