"""实时显示 compare_modes 批次的进度看板（只读扫描，不干扰评测）。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _hm(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def render(root: Path) -> str:
    manifest = _load(root / "manifest.json")
    modes = manifest.get("modes", [])
    tasks = manifest.get("tasks", [])
    trials = int(manifest.get("trials_per_task", 1))
    deadline = manifest.get("deadline_seconds")
    workers = manifest.get("workers")
    total = len(modes) * len(tasks) * trials

    done = passed = failed = 0
    running: list[tuple[str, str, float]] = []
    recent: list[tuple[float, str, bool, float]] = []
    per_mode = {mode: [0, 0, 0] for mode in modes}
    mtimes: list[float] = []
    now = time.time()
    for mode in modes:
        mode_dir = root / mode
        if not mode_dir.is_dir():
            continue
        for trial_dir in mode_dir.iterdir():
            if not trial_dir.is_dir():
                continue
            stat = trial_dir.stat().st_mtime
            mtimes.append(stat)
            grade = _load(trial_dir / "grade.json")
            name = f"{mode}/{trial_dir.name}"
            if "passed" in grade:
                done += 1
                ok = bool(grade["passed"])
                passed += int(ok)
                failed += int(not ok)
                per_mode[mode][0] += 1
                per_mode[mode][1] += int(ok)
                per_mode[mode][2] += int(not ok)
                recent.append((stat, name, ok, float(grade.get("duration_seconds", 0))))
            else:
                running.append((mode, trial_dir.name, now - stat))
    recent.sort(reverse=True)
    if not mtimes:
        return f"[进度] {root.name}: 尚未开始"
    start = min(mtimes)
    elapsed = now - start
    throughput = elapsed / done if done else 0.0
    eta = throughput * (total - done) if done else 0.0
    pct = (done / total * 100) if total else 0.0
    lines = [
        f"[进度] {root.name}  workers={workers} deadline={deadline}s",
        f"  完成 {done}/{total} ({pct:.1f}%)  通过 {passed}  失败 {failed}  "
        f"运行中 {len(running)}  耗时 {_hm(elapsed)}  {throughput:.0f}s/trial  ETA ~{_hm(eta)}",
    ]
    if running:
        lines.append(
            "  运行中： "
            + "  ".join(
                f"{m}/{n}({_hm(e)})" for m, n, e in sorted(running, key=lambda x: -x[2])[:6]
            )
        )
    mode_txt = " | ".join(
        f"{m} {v[0]}/{len(tasks) * trials}(+{v[1]}/-{v[2]})" for m, v in per_mode.items()
    )
    lines.append("  按模式： " + mode_txt)
    for _, name, ok, dur in recent[:5]:
        lines.append(f"  最近： {'PASS' if ok else 'FAIL'} {name} {dur:.0f}s")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="compare_modes 批次进度看板")
    parser.add_argument("root", type=Path, help="批次输出目录")
    parser.add_argument("--watch", type=float, default=0, help="每 N 秒刷新一次，0 只打印一次")
    args = parser.parse_args()
    while True:
        if args.watch:
            sys.stdout.write("\033[2J\033[H")
        print(render(args.root), flush=True)
        print(f"  刷新时间 {datetime.now().strftime('%H:%M:%S')}", flush=True)
        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
