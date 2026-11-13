# 开发环境和容器

## 共同前提

先识别工具是否安装、版本、项目边界、实际缓存位置及当前任务。不要为了清理运行陌生仓库的构建脚本或安装依赖。只读 `--help`/工具自带统计优先；清理参数以本机版本为准。工具命令可能跨项目/跨卷，必须匹配授权。

全局缓存和项目数据分开。源码、未提交工作、环境配置、密钥、数据库不属于构建缓存。`git clean -xfd`、递归清空工作区、卸载环境不属于默认方案。

## 常见工具

| 工具 | 只读检查/证据 | 清理决策 |
|---|---|---|
| Python/pip | `python -m pip cache dir`、`python -m pip cache info` | `python -m pip cache purge` 会清空该解释器使用的缓存；仅在该全局范围及永久删除已获授权时使用 |
| Python 虚拟环境 | 解释器配置、项目关联和环境描述 | 非默认自动项；环境可能不可复现或被服务使用，需明确范围与重建证据 |
| npm | `npm config get cache`，本机 `npm cache --help` | 缓存删除需核实版本和范围；不能把 npm 的强制参数当作绕过授权 |
| node_modules | package manifest、锁文件、当前任务 | 不只凭目录名删除；需可重建及无活动使用证据；可能有本地链接或修改 |
| Rust/.NET/Java 等 | 工具帮助、项目清单及官方缓存位置 | 只清已核实产物，考虑离线环境、重建时间和共享依赖 |
| IDE | IDE 支持的缓存操作 | 不清工作区设置、插件、历史或未保存状态 |

## Docker/BuildKit/WSL/虚拟机

只读先检查当前 Docker context，避免对远程 daemon 执行操作；`docker system df -v` 可帮助区分镜像、容器、缓存和卷。构建器也可能是远程端，检查目标 builder。

`docker builder prune` / `docker buildx prune` 仅在构建缓存类型、builder、过滤范围与授权匹配且无活动构建时使用。先读本机帮助核实范围，工具确认提示可在已有授权范围内正常处理，但不通过扩大参数来省事。

不把 `docker system prune -a --volumes` 当作日常清理入口。停止的容器和未挂载的卷仍可能包含数据；默认保留镜像、容器、卷及数据库，除非另行明确授权。

WSL、Docker Desktop 和虚拟机磁盘可能在内部删除数据后仍不缩小宿主文件。报告该区别，压缩/关闭服务/重启/注销发行版是独立操作，不能擅自执行。不要手工删除 VHD/VHDX、WSL 发行版目录或虚拟机快照。

## 官方资料

- [Docker 清理对象](https://docs.docker.com/engine/manage-resources/pruning/)
- [Docker builder prune](https://docs.docker.com/reference/cli/docker/builder/prune/)
- [Docker buildx prune](https://docs.docker.com/reference/cli/docker/buildx/prune/)
- [pip cache](https://pip.pypa.io/en/stable/cli/pip_cache/)
- [npm cache](https://docs.npmjs.com/cli/commands/npm-cache/)
