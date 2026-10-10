"""批量驱动：先在公开集上做环境体检（prepare+gold），再对健康实例跑 Agent。

- sweep：逐条 prepare+gold，记录"能否建环境、gold 是否解决目标用例"，产出体检表；
- batch：只对体检通过的实例跑本项目 Agent，汇总相对口径结果；
- 全部产物落在 runs/swebench-verified/ 下，不触碰冻结的内部评测。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from external_eval.docker_env import docker_prepare, docker_tester
from external_eval.harness import DEFAULT_ROOT, clone_base, evaluate_patch
from external_eval.swebench_verified import (
    DEFAULT_CACHE,
    assert_isolated_output,
    select_instances,
)

_PRINT_LOCK = threading.Lock()


def _log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


def health_check(instance: dict, root: Path, *, image: str | None = None) -> dict:
    """prepare + gold：返回该实例的环境健康度摘要。"""
    iid = instance["instance_id"]
    started = time.monotonic()
    report_dir = root.parent / "report"
    record: dict = {"instance_id": iid, "repo": instance["repo"]}
    try:
        repo_dir = clone_base(instance, root)
        docker_prepare(instance, repo_dir.parent, image=image)
        tester = docker_tester(instance, repo_dir.parent, image=image)
        result = evaluate_patch(instance, repo_dir, tester, instance["patch"])
        (report_dir / f"{iid}-gold.json").write_text(
            json.dumps({**result, "label": "gold"}, ensure_ascii=False, indent=2)
        )
    except Exception as exc:  # noqa: BLE001 - 体检要记录任意失败原因
        record.update({"ok": False, "stage": "build_or_gold", "error": str(exc)[:400]})
    else:
        ftp = result["fail_to_pass"]
        record.update(
            {
                "ok": all(ftp.values()),
                "stage": "done",
                "ftp": f"{sum(ftp.values())}/{len(ftp)}",
                "ptp": f"{sum(result['pass_to_pass'].values())}/{len(result['pass_to_pass'])}",
                "resolved": result["resolved"],
            }
        )
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


def sweep(
    *,
    root: Path = DEFAULT_ROOT,
    image: str | None = None,
    workers: int = 3,
    only: list[str] | None = None,
) -> list[dict]:
    root = assert_isolated_output(root)
    selection = json.loads((Path(__file__).with_name("selection.json")).read_text())
    rows = select_instances(json.loads(_metadata_path(root).read_text()), selection)
    if only:
        wanted = set(only)
        rows = [r for r in rows if r["instance_id"] in wanted]
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(health_check, r, root, image=image): r["instance_id"] for r in rows}
        for fut in as_completed(futures):
            rec = fut.result()
            records.append(rec)
            _log(
                f"[sweep] {rec['instance_id']:<40} ok={str(rec.get('ok')):<5} "
                f"{rec.get('ftp', '-'):>5} {rec.get('ptp', '-'):>9} {rec.get('seconds', '?')}s "
                f"{rec.get('error', '')[:80]}"
            )
    records.sort(key=lambda r: r["instance_id"])
    out = root.parent / "sweep.json"
    if out.is_file():
        # 与既有的体检表合并，支持只补扫失败项而不丢全表。
        merged = {r["instance_id"]: r for r in json.loads(out.read_text())}
        merged.update({r["instance_id"]: r for r in records})
        records = sorted(merged.values(), key=lambda r: r["instance_id"])
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2))
    _log(f"[sweep] 体检表：{out}")
    return records


def _metadata_path(root: Path) -> Path:
    return DEFAULT_CACHE


def run_instance(instance: dict, root: Path, *, image, config, max_steps, deadline) -> dict:
    from types import SimpleNamespace

    from external_eval.__main__ import _run_agent_instance

    args = SimpleNamespace(
        instance=instance["instance_id"],
        root=root,
        docker=True,
        image=image,
        config=config,
        max_steps=max_steps,
        deadline=deadline,
        no_baseline=False,
    )
    started = time.monotonic()
    try:
        rc = _run_agent_instance(args)
    except Exception as exc:  # noqa: BLE001
        return {"instance_id": instance["instance_id"], "error": str(exc)[:400], "rc": None}
    report = root.parent / "report" / f"{instance['instance_id']}.json"
    data = json.loads(report.read_text())
    return {
        "instance_id": instance["instance_id"],
        "repo": instance["repo"],
        "status": data["agent"]["status"],
        "steps": data["agent"]["steps"],
        "tokens": data["agent"]["usage"]["total_tokens"],
        "ftp_all_passed": data["ftp_all_passed"],
        "official_resolved": data["official_resolved"],
        "relative_resolved": data["relative_resolved"],
        "ptp_regressions": len(data["ptp_regressions"]),
        "seconds": round(time.monotonic() - started, 1),
        "rc": rc,
    }


def batch(
    *,
    root: Path = DEFAULT_ROOT,
    image: str | None = None,
    config: Path = Path("pi-claw.toml"),
    max_steps: int = 30,
    deadline: int = 2400,
    workers: int = 2,
    only: list[str] | None = None,
    health: list[dict] | None = None,
) -> list[dict]:
    root = assert_isolated_output(root)
    if health is None:
        health = json.loads((root.parent / "sweep.json").read_text())
    healthy = {r["instance_id"] for r in health if r.get("ok")}
    selection = json.loads((Path(__file__).with_name("selection.json")).read_text())
    rows = select_instances(json.loads(_metadata_path(root).read_text()), selection)
    rows = [r for r in rows if r["instance_id"] in healthy]
    if only:
        wanted = set(only)
        rows = [r for r in rows if r["instance_id"] in wanted]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                run_instance,
                r,
                root,
                image=image,
                config=config,
                max_steps=max_steps,
                deadline=deadline,
            ): r["instance_id"]
            for r in rows
        }
        for fut in as_completed(futures):
            rec = fut.result()
            results.append(rec)
            _log(
                f"[batch] {rec['instance_id']:<40} rel={str(rec.get('relative_resolved')):<5} "
                f"steps={rec.get('steps', '-')} tok={rec.get('tokens', '-')} "
                f"{rec.get('seconds', '?')}s {rec.get('error', '')[:70]}"
            )
    results.sort(key=lambda r: r["instance_id"])
    out = root.parent / "batch.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    _log(f"[batch] 结果表：{out}")
    return results
