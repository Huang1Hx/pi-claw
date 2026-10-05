# Pi-Claw Docker 命令镜像

该镜像只服务于 `DockerCommandRunner`，不是通用开发容器。基础 Python 与 uv 镜像都按 OCI digest
固定；pytest、Ruff 和项目运行依赖在构建时安装到 `/opt/venv`，运行时设置 `UV_OFFLINE=1`、
`UV_NO_SYNC=1` 和 `UV_NO_ENV_FILE=1`。

容器以 UID/GID `65532:65532` 运行。唯一宿主挂载是一次性输入快照 `/input:ro`；入口脚本把它
复制到 `/workspace` tmpfs 后才执行已审批 argv。因此测试缓存和临时文件不会回写原工作区。
宿主 Runner 另外固定关闭网络、只读根文件系统、移除 capability、限制 CPU/内存/PID，并在
每条退出路径强制删除容器。

从仓库根目录构建：

```bash
scripts/build-sandbox-image.sh
```

默认生成本地 tag `pi-claw-sandbox:local`，并在被 Git 忽略的 `runs/sandbox-build/` 写入 image ID
和 SPDX SBOM。Docker/Scout 不可用或 SBOM 生成失败时脚本返回非零；不能把部分构建当成已发布
镜像。Docker 容器加强了命令隔离，但不等于虚拟机，仍依赖 Docker daemon、宿主内核与固定镜像
供应链的安全性。
