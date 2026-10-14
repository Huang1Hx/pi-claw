"""M7b 精简真实模型对照入口：两个经典任务 × 锚 off/on。

仅做一次小样本对照，产出可复核的真实证据；**不**宣称统计显著性，也不把运行产物写回仓库。
需要真实模型与网络；报告写到 --report 指定路径（默认临时目录）。

用法：
    .venv/bin/python scripts/eval_anchor_compare.py --provider openai --model deepseek-flash \
        --report /tmp/m7b-report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

FIZZ = {
    "files": {
        "fizz.py": (
            "def fizzbuzz(n: int) -> str:\n"
            "    if n % 3 == 0:\n"
            '        return "Fizz"\n'
            "    if n % 5 == 0:\n"
            '        return "Buzz"\n'
            "    return str(n)\n"
        ),
        "test_fizz.py": (
            "import unittest\n\n"
            "from fizz import fizzbuzz\n\n\n"
            "class FizzTest(unittest.TestCase):\n"
            "    def test_three(self):\n"
            '        self.assertEqual(fizzbuzz(3), "Fizz")\n\n'
            "    def test_five(self):\n"
            '        self.assertEqual(fizzbuzz(5), "Buzz")\n\n'
            "    def test_fifteen(self):\n"
            '        self.assertEqual(fizzbuzz(15), "FizzBuzz")\n\n\n'
            'if __name__ == "__main__":\n'
            "    unittest.main()\n"
        ),
    },
    "test_file": "test_fizz.py",
    "goal": "修复 FizzBuzz 对 15 倍数返回错误的问题，并让测试全部通过。",
    "pin": "不要修改 test_fizz.py；只做 fizz.py 的最小改动。",
    "task": "修复 fizz.py，使 `python -m unittest -q test_fizz.py` 全部通过。",
}

STATS = {
    "files": {
        "stats.py": ("def mean(values):\n    return sum(values) / len(values)\n"),
        "test_stats.py": (
            "import unittest\n\n"
            "from stats import mean, median, variance\n\n\n"
            "class StatsTest(unittest.TestCase):\n"
            "    def test_mean_nonempty(self):\n"
            "        self.assertEqual(mean([1, 2, 3]), 2)\n\n"
            "    def test_median_odd(self):\n"
            "        self.assertEqual(median([3, 1, 2]), 2)\n\n"
            "    def test_median_even(self):\n"
            "        self.assertEqual(median([4, 1, 3, 2]), 2.5)\n\n"
            "    def test_variance_population(self):\n"
            "        self.assertAlmostEqual(variance([1, 2, 3]), 2 / 3)\n\n"
            "    def test_empty_raises(self):\n"
            "        for fn in (mean, median, variance):\n"
            "            with self.assertRaises(ValueError):\n"
            "                fn([])\n\n\n"
            'if __name__ == "__main__":\n'
            "    unittest.main()\n"
        ),
    },
    "test_file": "test_stats.py",
    "goal": "为 stats.py 增加 median 与 variance，并让测试全部通过。",
    "pin": "不要修改 test_stats.py；空列表必须抛 ValueError，不要返回 0 或 NaN。",
    "task": (
        "实现 median 与 variance，使 `python -m unittest -q test_stats.py` 全部通过；"
        "mean 对非空输入保持原结果。"
    ),
}

TASKS = {"fizz": FIZZ, "stats": STATS}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _usage(sessions: Path, workspace: Path, backend: str | None) -> dict[str, int] | None:
    try:
        from persistence.factory import open_session_store

        store = open_session_store(sessions, backend=backend)
        info = store.latest(workspace)
        result = store.total_usage(info.session_id, workspace) if info else None
        close = getattr(store, "close", None)
        if callable(close):
            close()
        return result
    except Exception:  # noqa: BLE001 - 指标缺失不应使对照失败
        return None


def run_case(task_id: str, arm: str, repeat: int, args: argparse.Namespace) -> dict[str, object]:
    spec = TASKS[task_id]
    case_dir = args.work_dir / f"{task_id}-{arm}-r{repeat}"
    ws = case_dir / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    sessions = case_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for name, content in spec["files"].items():  # type: ignore[union-attr]
        (ws / name).write_text(content, encoding="utf-8")
    test_path = ws / spec["test_file"]  # type: ignore[index]
    test_before = _sha256(test_path)

    script = f"/goal {spec['goal']}\n/pin {spec['pin']}\n{spec['task']}\n/exit\n"
    env = os.environ.copy()
    env["PI_CLAW_TASK_ANCHOR"] = arm
    env["TERM"] = "dumb"
    argv = [
        sys.executable,
        "-m",
        "interfaces",
        "chat",
        "--provider",
        args.provider,
        "--model",
        args.model,
        "--yes",
        "--workspace",
        str(ws),
        "--data-dir",
        str(sessions),
    ]
    if args.session_backend:
        argv += ["--session-backend", args.session_backend]
    started = time.perf_counter()
    timed_out = False
    stdout = ""
    try:
        proc = subprocess.run(
            argv,
            input=script,
            capture_output=True,
            text=True,
            env=env,
            cwd=args.repo,
            timeout=args.timeout,
        )
        stdout = proc.stdout
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
    duration = round(time.perf_counter() - started, 1)

    check = subprocess.run(
        [sys.executable, "-m", "unittest", "-q", spec["test_file"]],  # type: ignore[index]
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=120,
    )
    tool_calls = sum(1 for line in stdout.splitlines() if line.startswith("tool>"))
    return {
        "task": task_id,
        "arm": arm,
        "repeat": repeat,
        "session_backend": args.session_backend
        or os.environ.get("PI_CLAW_SESSION_BACKEND", "files"),
        "tests_pass": check.returncode == 0,
        "constraint_test_file_unchanged": _sha256(test_path) == test_before,
        "run_reported_incomplete": "运行未完成" in stdout,
        "timed_out": timed_out,
        "duration_s": duration,
        "tool_calls": tool_calls,
        "usage": _usage(sessions, ws, args.session_backend),
        "workspace": str(ws),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="锚 off/on 精简真实模型对照")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--tasks", default="fizz,stats")
    parser.add_argument("--arms", default="on,off")
    parser.add_argument("--timeout", type=int, default=420, help="单次运行墙钟上限（秒）")
    parser.add_argument("--repeat", type=int, default=1, help="每臂重复次数")
    parser.add_argument(
        "--session-backend",
        default=None,
        help="会话后端；默认继承 PI_CLAW_SESSION_BACKEND 或 files",
    )
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/m7b-anchor-compare"))
    parser.add_argument("--report", type=Path, default=Path("/tmp/m7b-anchor-report.json"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.work_dir = args.work_dir.expanduser().resolve()
    args.work_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for task_id in [t.strip() for t in args.tasks.split(",") if t.strip()]:
        for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
            for repeat in range(1, args.repeat + 1):
                result = run_case(task_id, arm, repeat, args)
                results.append(result)
                print(
                    f"{task_id:5} arm={arm:3} r{repeat} tests_pass={result['tests_pass']!s:5} "
                    f"constraint_ok={result['constraint_test_file_unchanged']!s:5} "
                    f"incomplete={result['run_reported_incomplete']!s:5} "
                    f"tools={result['tool_calls']} duration={result['duration_s']}s",
                    flush=True,
                )
    args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
