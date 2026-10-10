"""外部评测命令行：拉取元数据、查看筛选、准备环境、判定补丁（含 gold 自检）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from external_eval.agent_adapter import run_agent
from external_eval.docker_env import docker_prepare, docker_tester
from external_eval.harness import (
    DEFAULT_ROOT,
    clone_base,
    create_venv,
    evaluate_patch,
    local_tester,
    save_result,
)
from external_eval.swebench_verified import (
    DEFAULT_CACHE,
    assert_isolated_output,
    fetch_metadata,
    load_selection,
    select_instances,
)


def _load_instance(instance_id: str) -> tuple[dict, dict]:
    selection = load_selection()
    metadata = fetch_metadata(DEFAULT_CACHE)
    rows = {row["instance_id"]: row for row in select_instances(metadata, selection)}
    if instance_id not in rows:
        raise SystemExit(f"实例不在审定清单内：{instance_id}")
    return rows[instance_id], selection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="external_eval")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch", help="拉取并缓存 SWE-bench Verified 元数据")
    sub.add_parser("list", help="打印审定清单与规模")
    runner_parser = sub.add_parser("run", help="跑本项目 Agent 并在外部判定")
    runner_parser.add_argument("--instance", required=True)
    runner_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    runner_parser.add_argument("--docker", action="store_true")
    runner_parser.add_argument("--image")
    runner_parser.add_argument("--config", type=Path, default=Path("pi-claw.toml"))
    runner_parser.add_argument("--max-steps", type=int, default=24)
    runner_parser.add_argument("--deadline", type=int, default=1800)
    runner_parser.add_argument("--no-baseline", action="store_true")
    for name in ("prepare", "gold", "patch"):
        p = sub.add_parser(name, help=f"{name} 一个实例")
        p.add_argument("--instance", required=True)
        p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
        p.add_argument("--python-version", help="用 uv 固定该 Python 版本（如 3.9）")
        p.add_argument("--docker", action="store_true", help="用受限容器固定环境（方案 B）")
        p.add_argument("--image", help="覆盖容器镜像（本地已有镜像或私有源）")
        if name == "patch":
            p.add_argument("--patch-file", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "fetch":
        rows = fetch_metadata(DEFAULT_CACHE)
        print(f"已缓存 {len(rows)} 条实例到 {DEFAULT_CACHE}")
        return 0
    if args.command == "list":
        selection = load_selection()
        rows = select_instances(fetch_metadata(DEFAULT_CACHE), selection)
        print(
            json.dumps(
                {
                    "criteria": selection["criteria"],
                    "instances": [
                        {
                            "instance_id": r["instance_id"],
                            "repo": r["repo"],
                            "difficulty": r.get("difficulty"),
                        }
                        for r in rows
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "run":
        return _run_agent_instance(args)

    instance, _ = _load_instance(args.instance)
    root = assert_isolated_output(args.root)
    repo_dir = clone_base(instance, root)
    if args.docker:
        docker_prepare(instance, repo_dir.parent, image=args.image)
        tester = docker_tester(instance, repo_dir.parent, image=args.image)
    else:
        venv = create_venv(repo_dir, python_version=args.python_version)
        tester = local_tester(venv / "bin" / "python", repo_dir)
    if args.command == "prepare":
        print(f"环境就绪：{repo_dir}")
        return 0

    if args.command == "gold":
        patch = instance["patch"]
        label = "gold"
    else:
        patch = args.patch_file.read_text()
        label = "prediction"
    result = evaluate_patch(instance, repo_dir, tester, patch)
    result["label"] = label
    path = save_result(result, root.parent / "report")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"报告：{path}")
    return 0 if result["resolved"] else 1


def _run_agent_instance(args) -> int:
    """闭环：准备环境 → 跑 Agent 产补丁 → 判定 → 与 gold 基线比较。"""
    instance, _ = _load_instance(args.instance)
    root = assert_isolated_output(args.root)
    repo_dir = clone_base(instance, root)
    report_dir = root.parent / "report"
    inst_dir = repo_dir.parent
    if args.docker:
        docker_prepare(instance, inst_dir, image=args.image)
        tester = docker_tester(instance, inst_dir, image=args.image)
    else:
        venv = create_venv(repo_dir)
        tester = local_tester(venv / "bin" / "python", repo_dir)

    baseline_map = {}
    baseline_path = report_dir / f"{instance['instance_id']}-gold.json"
    if not args.no_baseline:
        baseline = evaluate_patch(instance, repo_dir, tester, instance["patch"])
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2))
        baseline_map = baseline["pass_to_pass"]

    run = run_agent(
        instance,
        repo_dir,
        config_path=args.config,
        max_steps=args.max_steps,
        deadline_seconds=args.deadline,
        trace_path=inst_dir / "trace-agent.jsonl",
    )
    result = evaluate_patch(instance, repo_dir, tester, run["patch"])
    regressions = sorted(
        k for k, ok in result["pass_to_pass"].items() if baseline_map.get(k) and not ok
    )
    ftp_ok = all(result["fail_to_pass"].values())
    # 本机环境的 PASS_TO_PASS 噪声会拉低官方 resolved；相对口径 = 目标用例全过且无相对 gold 回归。
    relative_resolved = ftp_ok and not regressions
    report = {
        "instance_id": instance["instance_id"],
        "repo": instance["repo"],
        "agent": {k: run[k] for k in ("status", "steps", "usage", "final_text")},
        "evaluation": result,
        "patch_bytes": len(run["patch"]),
        "gold_baseline_ptp": sum(baseline_map.values()) if baseline_map else None,
        "ptp_regressions": regressions,
        "ftp_all_passed": ftp_ok,
        "official_resolved": result["resolved"],
        "relative_resolved": relative_resolved,
    }
    path = save_result(report, report_dir)
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("instance_id", "patch_bytes", "ptp_regressions", "relative_resolved")
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"报告：{path}")
    return 0 if report["relative_resolved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
