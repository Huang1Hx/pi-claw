from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "alpha.txt").write_text("alpha\nneedle one\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "beta.txt").write_text("beta\nneedle two\n", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def block_real_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("测试禁止真实网络访问")

    monkeypatch.setattr(urllib.request, "urlopen", blocked)
