# Iteration 024：Docker 命令执行隔离

## 状态

已提交 optimize。提交 `87e0f1a` 已推送 `origin/optimize`，尚未合并 `main`。本轮构建并实际运行了
本地固定镜像，没有调用外部模型；SBOM 和本机镜像 ID 保存在被 Git 忽略的 `runs/sandbox-build/`。

## 目标

Task 3.6 把已通过 Policy/Approval 的命令放入一次性 Linux 容器。Docker 是第二道执行边界，
不能代替应用层授权；若 CLI、daemon、镜像身份或隔离参数不可用，必须 fail closed，绝不回退
Host。

## 固定边界

| 范围 | 实现 |
|---|---|
| 供应链 | Python 与 uv 基础镜像按 digest 固定；依赖按 `uv.lock` 预装；构建脚本输出 image ID 和 SPDX SBOM |
| 镜像身份 | 启动前把 tag 解析为严格的 `sha256:<64 hex>`；每次运行前复核 tag 未漂移；实际 argv 使用 image ID |
| 命令构造 | 只有受信宿主代码构造 Docker argv；模型 argv 只能出现在 image ID 之后，使用绝对 Docker CLI 与 `shell=False` |
| 网络/权限 | `network=none`、只读 rootfs、`cap-drop=ALL`、`no-new-privileges`、非 root UID/GID 65532 |
| 资源 | 512 MiB memory/swap、1 CPU、128 PIDs、64 MiB `/tmp` tmpfs、256 MiB `/workspace` tmpfs |
| 挂载 | 唯一宿主挂载是单次快照到 `/input:ro`；无工作区、用户目录、设备或 Docker socket 挂载 |
| 环境 | 只传固定 HOME/PATH/PYTHONPATH/uv 控制项和受信 cwd；不透传 Key、代理变量或 Docker 配置目录 |
| 工作目录 | 受信 entrypoint 把 `/input` 复制到可写且有容量上限的 `/workspace`，随后切换到批准的相对 cwd |
| 清理 | 不可预测容器名；正常、失败、超时、取消和中断均强制删除并再次确认；清理失败结构化上报 |

Docker CLI 自身使用另一份受信宿主环境白名单，保留 `DOCKER_HOST`、`DOCKER_CONTEXT`、
`DOCKER_CONFIG`、TLS 与 HOME/XDG 配置，使 image inspect、run、rm 和最终 inspect 始终连接同一
daemon；这份环境不通过 `--env` 进入容器。只有最终 inspect 自己明确返回 no-such 才确认清理
成功，并且错误中的容器名必须等于当前不可预测名称；daemon、权限、连接或名称不一致都记为
`cleanup_failed`。相对 Docker 配置/证书/HOME 路径在 Runner 创建时固定为绝对路径。

## RED→GREEN 修复

真实容器首次运行完整测试时，项目源码包无法导入：镜像只有锁定依赖，当前快照源码没有进入模块
搜索路径。先增加 argv/Dockerfile 失败测试，再把固定 `PYTHONPATH=/workspace/src` 同时写入镜像和
运行 argv；实现后两项契约测试转绿。随后又先增加“Docker CLI 失败且清理也失败”的测试，确认
旧实现丢失 `cleanup_failed`，再用结构化异常和 ToolResult metadata 修复。

独立安全审查继续发现跨 daemon、Linux/rootless 权限、清理误报、宿主快照路径回显、常见凭据
遗漏、硬链接绕过、初始化残留和清理窗口不足。对应测试先失败，再统一 Docker client 环境、把只读快照复制成容器私有可写
副本、收紧最终探针、精确脱敏临时路径、扩充固定凭据名、把 Policy cwd 改为工作区相对路径，
并在保持 420 秒父级硬上限的同时把 worker 清理预留从 3 秒改为 8 秒。Popen 等内部异常仅把
固定安全摘要写入 ToolResult/Trace，原异常只作为本地 exception cause 保留；合法绝对 Python
解释器在 Policy metadata、工具结果和 JSONL Trace 中统一显示为 `python`，真实执行路径不变。

## 验证证据

- `scripts/build-sandbox-image.sh` 成功构建 `pi-claw-sandbox:local`，生成不可变 image ID 与 SPDX SBOM。
- 真实 Runner smoke：`uid=65532`、当前源码可读、cwd 为 `/workspace`、`/input` 写入被拒、`/workspace` 可写、
  `network=none`、唯一挂载、退出码 0、`cleanup_failed=false`。
- smoke 完成后按 `pi-claw-*` 过滤无残留容器。
- 真实容器可收集并执行完整 693 项测试；排除下述 6 项宿主专属契约后，其余 687 项全部通过。
  这 6 项因隔离边界有意不满足宿主测试前提：无 `.git`、
  无被排除的 `runs/.env.example`、不安装当前项目 console metadata、最小镜像无 `ps`。没有通过放回
  敏感目录或增加宿主挂载来消除这些失败。
- 宿主完整回归 693 项通过；专项回归、Ruff、格式、wheel/sdist 构建、敏感信息签名扫描和
  diff 检查通过。

## 风险与下一步

Docker 显著加强命令隔离，但仍共享 Linux VM 内核并依赖 daemon、镜像供应链和本机配置，不等于
虚拟机安全边界。Task 3.8 还需用确定性攻击 fixture 验证断网、凭据不可见、原工作区无副作用、
资源限制和 30 次连续清理；在这些硬门槛完成前，阶段 3 不能标记全部完成。
