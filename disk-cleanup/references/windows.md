# Windows：存储、权限和执行

## 先检查

使用扫描器 `--list-drives` 获取固定磁盘。盘符不代表物理磁盘；网络映射盘和可移动盘不默认加入。读取用户排除项，检查当前权限；不要为只读统计自动提升权限。

系统设置入口可通过 `Start-Process 'ms-settings:storagesense'` 打开。界面随 Windows 版本变化，应核对实际项目再操作。打开界面不等于已经清理，不应报告成功。

Windows 存储设置及“临时文件”/“清理建议”用于核实候选。Storage Sense 可涉及回收站、下载和云文件；检查选项，不启用会改变未来行为的计划清理，不把整个功能默认配置视为本次授权。系统盘分析不能代替其他磁盘分析。

## 需要区别的内容

| 内容 | 默认判断 |
|---|---|
| 当前用户临时目录 | 仅候选；临时目录也可能含未保存状态/正在运行的安装程序数据 |
| Windows 临时/更新文件 | 优先系统支持入口，访问不足就跳过；不手工清空 SoftwareDistribution |
| Windows.old、恢复分区、还原点 | 保留；删除可能失去回退或恢复能力 |
| WinSxS、Installer、System32、驱动仓库 | 不手工删除；如需维护必须另行核实官方支持方式及授权 |
| pagefile、hiberfil、交换/转储文件 | 不作为普通缓存；停用功能或改变配置需独立明确授权 |
| 回收站 | 可分析，清空需明确选择，不借助全局命令扩大到未授权盘 |

## 操作前核对

解析绝对路径后检查每层祖先及目标的重解析属性，并再次检查卷和用户排除项。只用 `Resolve-Path` 或字符串前缀比较不够：`C:\Cache2` 并非 `C:\Cache` 的子目录。路径、目标类型和授权范围不符时跳过。

清理统一经 `scripts/clean.py` 执行，不要用 `Remove-Item` 手工拼命令：

- `clean.py` 当前仅预览，`--go` 一律拒绝。旧 Shell 接口的 `FOF_ALLOWUNDO` 只在可能时保留撤销信息，不能保证只进回收站；不要绕过限制。
- 它在执行前重新 `lstat` 每个目标，与计划中的 `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)` 比对；不一致就跳过。这正是为了避开「扫描时是缓存、执行时已变成别的东西」。
- 受保护树（`C:\Windows`、`C:\$Recycle.Bin`、`C:\System Volume Information`、`C:\Recovery`、`C:\Boot`）整棵拒绝；`C:\ProgramData`、`C:\Program Files`、`C:\Users` 只拒绝目录本身，其下被明确点名的缓存子目录仍可处理。
- `Remove-Item` 通常永久删除，不等于回收站；`-WhatIf` 只是预览，不等于安全性证明。需要手工处理时先确认 `clean.py` 为何拒绝，而不是绕开它。

锁定文件、活动日志、更新和安装期间产生的文件跳过。修改 ACL、夺取所有权、关闭安全软件、强杀进程、重启并不属于默认清理。若系统提权被拒，记录该项并继续其他已授权工作。

## 收益统计

同一目录大小可能包含下级目录，禁止重复计入预计收益。NTFS 压缩、稀疏文件、硬链接、云占位符使逻辑大小与物理占用不同。删除到回收站不会立即释放同等空间。操作前后读取每盘可用空间，并说明后台写入等因素影响差值。

## 官方资料（实施时核实版本）

- [Windows 存储设置](https://support.microsoft.com/en-us/windows/experience/storage-filemanagement/storage-settings-in-windows)
- [Storage Sense](https://support.microsoft.com/en-us/windows/experience/storage-filemanagement/manage-drive-space-with-storage-sense)
- [释放磁盘空间](https://support.microsoft.com/en-gb/windows/experience/storage-filemanagement/free-up-drive-space-in-windows)
