# Iteration 025：Host 文件工具竞态加固

## 状态

已提交 optimize。功能提交 `e3d082a` 已推送 `origin/optimize`；Task 3.7 的实现、独立安全复审
和离线验证已经完成。本轮不调用外部模型，保持 Iteration 021 的 `78/90` 真实评测基线冻结不变。

## 基线与目标

Task 3.6 已把命令执行放入一次性 Docker 容器，但 Host 文件工具仍存在经典 TOCTOU 窗口：先用
`Path.resolve/stat` 校验，再按路径读取或替换时，另一个进程可能换入 symlink、不同 inode 或另一
个父目录。Task 3.7 的目标是在不改变工具协议的前提下，把读取、搜索、预览、审批和最终提交统一
绑定到 root directory fd 与稳定文件版本。

## 主要修改

- 新增 `src/security/secure_fs.py`：固定工作区 root 的设备号/inode，所有路径从 root fd 开始逐段
  `O_NOFOLLOW` 打开；普通文件同时核对 inode、类型、链接数、大小、纳秒时间和 SHA256。
- `SecureFileVersion` 对存在文件绑定目标与父目录身份；对缺失目标绑定“最深已存在目录
  dev/inode + 剩余缺失路径”，避免预览后整体替换普通父目录仍可创建文件。
- 审批 diff、SHA 和 prepared version 来自同一稳定快照。审批后、handler 首读、最终打开以及
  原子提交前分别复核同一版本；字符串错误哨兵永远不能授权副作用 handler。
- 创建使用绑定父目录内的独占临时文件与硬链接，保持旧行为的 `0600`；替换保留原 mode，在同一
  目录 fsync 后 `os.replace`。目标 symlink、特殊节点和多重硬链接均拒绝。
- `read_file`、`write_or_replace`、`apply_patch` 和 `search_text` 统一使用安全文件层；`list_files`
  与搜索改为 `os.listdir(fd)` 的有界 walker，返回前复核访问目录仍映射到原 inode，并继续过滤
  任意层级的 `.env`、凭据、密钥及既有忽略目录。
- 平台缺少 `dir_fd`、`O_NOFOLLOW` 或 fd 枚举能力时，Host 文件发现、读取和写入明确 fail closed；
  独立 Docker 命令 Runner 不受 Host 文件能力标志影响。
- 目录打开后的 `fstat`、身份校验或递归异常均在移交 fd 所有权前关闭 child fd；临时文件也在所有
  失败路径清理。

## RED→GREEN 证据

实现前先增加并观察失败，再逐项修复：

- 中间/末级 symlink、外部硬链接和父目录换成 symlink；
- 审批期间同内容 inode 替换、普通父目录替换、审批复核后至 handler 首读之间换入未展示正文；
- 预览 SHA 与 diff 来自不同 preimage、最终打开换入超预算文件；
- `search_text` 在 stat/read 之间读取外部 symlink；
- 缺失父目录安全创建、`0600` 权限兼容、敏感文件任意层级过滤；
- 安全能力不足时 list/search/write 明确失败，而 Docker command path 仍可用；
- 三条目录 `open → fstat` 故障注入均验证 fd 已关闭。

独立复审先后发现审批快照不一致、missing 父目录未绑定、search Path 读取、handler 首读未绑定、
错误哨兵授权、安全 walker 能力边界、创建权限放宽和异常 fd 泄漏。每项均以回归测试复现并修复；
最终复审结论为 `Ready`，Critical/Important/Minor 均为 0。

## 验证证据

- 专项安全、工具、工作区、Policy 与 Trace 回归：118 项通过。
- 宿主完整回归：719 项全部通过。
- 真实 Docker Runner 兼容回归：713 项通过，6 项宿主专属契约按既有清单排除；
  `timed_out=false`、`cleanup_failed=false`，运行后 `pi-claw-*` 残留容器为 0。
- Ruff、格式检查和 `git diff --check` 通过。
- wheel 与 sdist 构建通过；`.env` 未被跟踪且被 ignore，私有 Key/内部 Base URL 实值未出现在
  Git 跟踪内容或构建产物中。

## 安全边界与下一步

这些措施把路径解析、审批版本和最终提交绑定到 fd/inode，并显著缩小 TOCTOU 窗口，但 POSIX 没有
可移植的“比较版本并 rename”单一 CAS；它不能控制同机特权恶意进程，也不替代 Docker/VM 隔离。
目录映射在提交前再次复核，提交本身仍依赖内核原子 rename/link 语义。

下一项是 Task 3.8：用确定性恶意 fixture 和至少 30 次连续 trial 量化泄漏、网络、宿主副作用、
逃逸、资源限制和清理率；所有离线硬门槛通过后，才允许进行冻结协议的真实模型诊断复测。
