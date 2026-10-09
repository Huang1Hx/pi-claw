"""把修复账本叠加到来源报告，物化出完整 90 行报告根，供剩余 provider_error 复测接续。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evaluation.mode_reports import trial_identity

KEEP = (
    "modes",
    "tasks",
    "trials_per_task",
    "workers",
    "deadline_seconds",
    "max_steps",
    "models",
    "premium_model",
    "grader_version",
    "source_sha256",
    "transport",
)


def main() -> int:
    baseline_root = Path(sys.argv[1])
    repair_root = Path(sys.argv[2])
    out = Path(sys.argv[3])
    baseline = json.loads((baseline_root / "report.json").read_text())
    baseline_rows = baseline["trials"]
    identities = [trial_identity(row) for row in baseline_rows]
    if len(identities) != len(set(identities)):
        raise SystemExit("baseline 包含重复 trial 身份")
    ledger = json.loads((repair_root / "repair-ledger.json").read_text())["entries"]
    replacements = {}
    for entry in ledger:
        identity = trial_identity(entry["identity"])
        if identity in replacements:
            raise SystemExit(f"修复账本身份重复：{identity}")
        replacements[identity] = entry["rounds"][-1]
    unknown = set(replacements) - set(identities)
    if unknown:
        raise SystemExit(f"修复账本含不属于 baseline 的身份：{sorted(unknown)}")
    rows = [
        dict(replacements.get(identity, row)) for identity, row in zip(identities, baseline_rows)
    ]
    provider_errors = [
        trial_identity(row) for row in rows if row.get("leader_status") == "provider_error"
    ]
    out.mkdir(parents=True, exist_ok=False)
    report = dict(baseline, trials=rows)
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "manifest.json").write_text(
        json.dumps({k: baseline[k] for k in KEEP if k in baseline}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"materialized trials={len(rows)} pass={sum(r.get('passed') is True for r in rows)} "
        f"provider_error={provider_errors} -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
