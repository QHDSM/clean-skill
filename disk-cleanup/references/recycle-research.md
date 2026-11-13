# Windows 可恢复执行：研究记录

当前 `clean.py --go` 停用。研究候选不等于已验证可用，不得据此开启执行。

`scripts/recycle_experimental.py` 是隔离原型，没有 CLI，也没有接入 `clean.py`。模拟测试覆盖接口调用顺序、执行失败与中止查询；即使 Shell 返回成功，原型也仅返回 `UNKNOWN`，不声称可恢复。该原型未完成实机验证，不应用于用户文件。

- [Microsoft IFileOperation 标志文档](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags) 描述 `FOFX_RECYCLEONDELETE` 在 Windows 8 引入，用于将删除项送往回收站；`FOF_ALLOWUNDO` 仅在可能时保留撤销信息。文档还描述了 `FOFX_EARLYFAILURE` 与 `FOF_NOERRORUI` 的组合，但本项目尚未验证不可回收位置、容量不足和系统策略等条件。
- [Tuzfucius/disk-cleanup-skills 的回收后端](https://github.com/Tuzfucius/disk-cleanup-skills/blob/main/src/disk_cleanup/cleaner/recycle.py) 使用 `IFileOperation`、`FOFX_RECYCLEONDELETE` 和 `FOFX_EARLYFAILURE`，并检查中止状态。[执行器](https://github.com/Tuzfucius/disk-cleanup-skills/blob/main/src/disk_cleanup/cleaner/executor.py) 在调用前隔离并重验对象。可借鉴隔离、验证和结果分级，但其[可选实机测试](https://github.com/Tuzfucius/disk-cleanup-skills/blob/main/tests/test_windows_recycle_integration.py)只断言原路径消失，不能单独证明文件可恢复或失败时没有永久删除。
- [Microsoft 中止状态文档](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-getanyoperationsaborted) 要求无论 `PerformOperations` 返回成功或失败，都检查中止状态；返回成功也可能表示操作已被取消。

恢复执行前需要独立验证：使用隔离的测试文件和测试环境，检查回收站中实际可恢复对象；覆盖不可回收卷或策略、容量限制、取消与失败、并发替换、目录内部变化、进程中断与回滚；正确区分成功、拒绝、失败、未知和恢复失败。计划授权、目标身份、路径和排除项也须在操作时绑定并复核。任何未知结果不得报告为成功；未经验证不得重新启用 `--go`。
