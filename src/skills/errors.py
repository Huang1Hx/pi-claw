"""Skill 加载、校验与执行错误。

全部错误都表示"拒绝激活"，不会放宽 Policy、Approval 或工作区边界。
"""

from __future__ import annotations


class SkillError(Exception):
    """Skill 相关错误的基类。"""


class ManifestError(SkillError):
    """Manifest 字段缺失、类型错误、取值非法或内容不合规。"""


class PathViolation(SkillError):
    """Skill 目录或提示文件越出内置根目录、含路径穿越或符号链接。"""


class PromptTooLarge(SkillError):
    """提示文件字节数或字符数超过硬上限或 Manifest 声明的上限。"""


class DuplicateSkill(SkillError):
    """出现重复的 Skill ID。"""


class SecretDetected(SkillError):
    """提示或元数据中出现疑似密钥、凭据或带凭据的 URL。"""


class ToolNotAllowed(SkillError):
    """Skill 声明的工具不在当前策略允许集合内。"""
