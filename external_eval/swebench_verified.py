"""SWE-bench Verified 元数据读取、实例筛选与冻结评测隔离断言。"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

DATASET = "princeton-nlp/SWE-bench_Verified"
_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=princeton-nlp%2FSWE-bench_Verified&config=default&split=test"
    "&offset={offset}&length=100"
)
PAGE = 100
TOTAL = 500

# 冻结的内部评测产物前缀：外部评测绝不能写入这些批次。
FROZEN_RUN_PREFIXES = ("stage3-", "agent-modes-")
DEFAULT_CACHE = Path("runs/swebench-verified/metadata.json")
DEFAULT_SELECTION = Path(__file__).with_name("selection.json")


def fetch_metadata(cache: Path = DEFAULT_CACHE, *, opener=None) -> list[dict]:
    """分页拉取 Verified 元数据并缓存；缓存已存在时直接复用，避免重复联网。"""
    cache = Path(cache)
    if cache.is_file():
        return json.loads(cache.read_text())
    open_url = opener or urllib.request.urlopen
    rows: list[dict] = []
    for offset in range(0, TOTAL, PAGE):
        with open_url(_ROWS_URL.format(offset=offset), timeout=60) as response:
            payload = json.load(response)
        rows.extend(item["row"] for item in payload["rows"])
    if len(rows) != TOTAL:
        raise ValueError(f"期望 {TOTAL} 条实例，实际 {len(rows)}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows, ensure_ascii=False))
    return rows


def _as_list(value) -> list:
    """SWE-bench 的测试清单可能是 JSON 字符串或原生列表。"""
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


def instance_tests(instance: dict) -> dict:
    """返回 FAIL_TO_PASS / PASS_TO_PASS 的规范列表。"""
    return {
        "FAIL_TO_PASS": _as_list(instance["FAIL_TO_PASS"]),
        "PASS_TO_PASS": _as_list(instance["PASS_TO_PASS"]),
    }


def load_selection(path: Path = DEFAULT_SELECTION) -> dict:
    """读取人工审定过的实例清单与筛选标准。"""
    selection = json.loads(Path(path).read_text())
    instances = selection.get("instances")
    if not isinstance(instances, list) or not instances:
        raise ValueError("selection.instances 必须是非空列表")
    if len(set(instances)) != len(instances):
        raise ValueError("selection.instances 含重复实例")
    return selection


def select_instances(metadata: list[dict], selection: dict) -> list[dict]:
    """按清单顺序返回元数据，缺少或多出即失败，保证可复现。"""
    by_id = {row["instance_id"]: row for row in metadata}
    missing = [i for i in selection["instances"] if i not in by_id]
    if missing:
        raise ValueError(f"清单实例不在数据集中：{missing}")
    allowed = set(selection.get("repos") or [])
    rows = [by_id[i] for i in selection["instances"]]
    if allowed:
        off_repo = [row["instance_id"] for row in rows if row["repo"] not in allowed]
        if off_repo:
            raise ValueError(f"清单实例不在允许仓库内：{off_repo}")
    return rows


def assert_isolated_output(path: Path) -> Path:
    """拒绝把外部评测产物写进冻结批次目录，避免污染既有评测。"""
    resolved = Path(path).resolve()
    for prefix in FROZEN_RUN_PREFIXES:
        if resolved.name.startswith(prefix):
            raise ValueError(f"外部评测产物不得写入冻结批次：{resolved.name}")
    if resolved.name != "swebench-verified" and "swebench-verified" not in resolved.parts:
        raise ValueError("外部评测产物必须位于 swebench-verified 目录下")
    return resolved


def frozen_task_ids() -> set[str]:
    """读取冻结评测的任务 id，仅用于证明外部评测与其无重叠（只读）。"""
    from evaluation.mode_holdout_tasks import ALL_TASKS

    return set(ALL_TASKS)
