"""有界解析 .env 的 KEY=VALUE 子集，不执行 shell 或变量替换。"""

import re
from pathlib import Path


def read_env(path: Path, *, required: bool = False) -> dict[str, str]:
    """读取本机指定文件；错误仅含行号，不回显可能含密钥的行。"""
    if not path.exists():
        if required:
            raise ValueError(f"环境文件不存在：{path}")
        return {}
    if path.is_symlink():
        raise ValueError("环境文件不能是符号链接")
    with path.open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("环境文件超过 64 KiB")
    values = {}
    for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"环境文件第 {number} 行格式无效")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"环境文件第 {number} 行引号未闭合")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if key in values:
            raise ValueError(f"环境文件第 {number} 行存在重复变量")
        values[key] = value
    return values
