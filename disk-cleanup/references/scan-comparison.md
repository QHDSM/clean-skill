# 扫描基线与增长分析

需要回答“空间为什么又满了”时，保存同范围扫描的 JSON，再比较：

```powershell
python scripts/scan.py 'D:\Projects' --format json --output baseline.json
# 以后按相同 roots、排除项和深度扫描，使用新文件名；扫描器不覆盖已有报告
python scripts/scan.py 'D:\Projects' --format json --output current.json
python scripts/compare_scans.py baseline.json current.json
```

比较命令只读取报告并输出 JSON，不访问报告内的目标路径，不产生清理计划，也不执行删除。报告含个人路径，应留在本地。

- `comparable` 为 true 时，`logical_bytes_delta` 和 `files_delta` 表示两次观察的总量差，不证明实际释放或同一批文件发生变化。
- roots、排除项、深度不一致，任一报告不完整，或 current 时间早于 baseline 时，`comparable` 为 false，`reasons` 解释原因，所有大小差值为 null。
- 两份 Top 列表都出现的路径才有 `observed_size_delta`，按增长量降序排列。路径相同不证明对象身份相同。
- 只出现在一份 Top 列表的条目为 `only_in_current_top` 或 `only_in_baseline_top`，缺席侧大小和差值为 null。可能只是名次、筛选或 Top 数量改变，不应报告成创建或删除。
- 目录行相互嵌套，不能相加。扫描不是原子快照，后台活动、硬链接和重新挂载等都会限制解释范围；差值不证明清理因果或底层物理空间变化。

实现思路参考了 [vhaozheng/windows-disk-cleanup](https://github.com/vhaozheng/windows-disk-cleanup/blob/main/SKILL.md) 的保存基线和增长对比流程。[其比较脚本](https://github.com/vhaozheng/windows-disk-cleanup/blob/main/scripts/compare_disk_baseline.ps1)对缺席路径使用零；本项目的扫描只保留 Top 列表，因此采用未知值，并核对范围和覆盖情况后再给出差值。
