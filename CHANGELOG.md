# 变更记录

按发布记录**可观察到的行为变化**。每一条都写清楚『对使用者意味着什么』，而不是『改了哪些文件』。

## 0.1.0 — 2026-11-16

第一个带版本号的发布。此前仓库只有提交历史：没有版本号、没有 tag，报告和审计日志里也看不出是哪个版本写的。

### 新增

- **版本化**（disk-cleanup/scripts/version.py）：TOOL_VERSION = 0.1.0，另有四个 schema 常量（plan / goal / report / audit）。落在三处：clean.py --version 打印 disk-cleanup 0.1.0；执行报告与撤销报告带 schema 与 tool_version 字段；审计日志每一行带 tool_version。以后一份报告或一行日志会自己说明它出自哪个版本，不必猜。
- **doctor.py：一条命令看清这台机器能不能用。** 只读体检六段：环境（Python 版本、平台、临时目录可写）、回收后端可用性、每卷能力（文件系统、只读、SID、回收站目录、NukeOnDelete、容量上限）与回收站占用、USN 是否需提权、历史审计日志（份数、最后一份行数、坏行数）、git 是否可用。输出 READY / READY WITH WARNINGS / NOT READY，退出码 0 / 1 / 2。任何一段探测抛异常都只变成该段的一条 fail 行，不掩盖其它段；--format json 给机器读。
- **CI 覆盖率门槛**：.coveragerc 只统计五个承载安全契约的模块（clean.py、recycle_backend.py、tree_evidence.py、scan.py、recycle_bin.py），branch 统计，低于 65% 时 CI 变红。门槛故意压低：本机实测线覆盖约 82%，branch 统计更低，65 只在『整块测试消失』时才触发，不对每个防御分支唠叨。

### 修复

- recycle_selfcheck.py --help 以前会直接崩掉：argparse 的 help 字符串里有裸 %T，被当成格式化占位符，抛 ValueError: unsupported format character 'T' 并以 1 退出。现在 help 正常渲染，并加了回归测试。

### 兼容性

- 报告与审计行只是**新增**字段，读取方忽略未知字段即可；没有字段被删除或改名。
- schema 字符串仍是 disk-cleanup-plan/v1 等原值，0.1.0 不改变任何 JSON 结构。
