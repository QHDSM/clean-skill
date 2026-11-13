# Windows 可恢复执行：研究记录

当前 `clean.py --go` 停用。研究候选不等于已验证可用，不得据此开启执行。

`scripts/recycle_experimental.py` 是隔离原型，没有 CLI，也没有接入 `clean.py`。模拟测试覆盖接口调用顺序、执行失败与中止查询；即使 Shell 返回成功，原型也仅返回 `UNKNOWN`，不声称可恢复。该原型未完成实机验证，不应用于用户文件。

Windows 实机测试已覆盖 COM 初始化、接口创建、设置标志、创建临时测试文件的 Shell 项并排队操作；不调用 `PerformOperations`，退出后测试文件内容保持不变。还检查了不存在的测试路径在执行前失败、接口释放可重复调用。这些测试不验证实际回收、取消后的状态或文件可恢复性。

一次 Windows 实机实验使用随机命名的自建临时文件，调用原型后，通过回收站中唯一的文件名及原目录匹配定位该文件，再通过 Shell 还原并校验随机内容，成功后仅移除该测试文件。`tests/test_recycle_live.py` 提供同样的可选测试；默认跳过，需要 Windows、pywin32，以及显式设置 `DISK_CLEANUP_LIVE_RECYCLE_TEST=1`。测试失败时保留测试目录供检查，不清空回收站。这只验证当前机器上的一个普通文件，不能推断其他卷、容量、策略、文件类型或失败情况下的行为。

- [Microsoft IFileOperation 标志文档](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags) 描述 `FOFX_RECYCLEONDELETE` 在 Windows 8 引入，用于将删除项送往回收站；`FOF_ALLOWUNDO` 仅在可能时保留撤销信息。文档还描述了 `FOFX_EARLYFAILURE` 与 `FOF_NOERRORUI` 的组合，但本项目尚未验证不可回收位置、容量不足和系统策略等条件。
- [Tuzfucius/disk-cleanup-skills 的回收后端](https://github.com/Tuzfucius/disk-cleanup-skills/blob/main/src/disk_cleanup/cleaner/recycle.py) 使用 `IFileOperation`、`FOFX_RECYCLEONDELETE` 和 `FOFX_EARLYFAILURE`，并检查中止状态。[执行器](https://github.com/Tuzfucius/disk-cleanup-skills/blob/main/src/disk_cleanup/cleaner/executor.py) 在调用前隔离并重验对象。可借鉴隔离、验证和结果分级，但其[可选实机测试](https://github.com/Tuzfucius/disk-cleanup-skills/blob/main/tests/test_windows_recycle_integration.py)只断言原路径消失，不能单独证明文件可恢复或失败时没有永久删除。
- [Microsoft 中止状态文档](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-getanyoperationsaborted) 要求无论 `PerformOperations` 返回成功或失败，都检查中止状态；返回成功也可能表示操作已被取消。

恢复执行前需要独立验证：使用隔离的测试文件和测试环境，检查回收站中实际可恢复对象；覆盖不可回收卷或策略、容量限制、取消与失败、并发替换、目录内部变化、进程中断与回滚；正确区分成功、拒绝、失败、未知和恢复失败。计划授权、目标身份、路径和排除项也须在操作时绑定并复核。任何未知结果不得报告为成功；未经验证不得重新启用 `--go`。
