"""本机跨进程锁与乐观序号校验；锁不承担权限授权。"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class LockingMixin:
    """SessionStore 内部协作组件，共享会话路径和读取预算。"""

    @contextmanager
    def _session_write_lock(self, session_dir: Path):
        """跨进程串行化 journal/state/checkpoint 的读改写提交。

        session_dir 决定锁粒度：同会话串行，不同会话互不占用此锁。
        这是本机文件系统上的协作式排他锁；只保护持久化临界区，不是分布式锁，
        不授予工具权限，也不应跨越模型请求或等待人工审批的全过程。
        """

        lock_path = session_dir / ".session.lock"
        self._reject_symlink(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            # with 内的代码才持有锁；无论正常退出还是异常，都在 finally 中解锁。
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _check_expected_seq(expected_seq: int | None, current_seq: int) -> None:
        """比较调用方快照版本与当前日志序号；不匹配拒绝写入，None 表示未启用版本检查。"""
        if expected_seq is not None and expected_seq != current_seq:
            raise RuntimeError("会话已被另一个进程更新；为避免覆盖新消息，请重新加载 session")
