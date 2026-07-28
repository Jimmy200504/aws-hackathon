#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "template.yaml"
BUILT_TEMPLATE = ROOT / ".aws-sam" / "build" / "template.yaml"
EVENT = ROOT / "tests" / "fixtures" / "search-event.json"
OUTPUT = ROOT / "reports" / "sam-local-smoke.json"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["SAM_CLI_TELEMETRY"] = "0"
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def parse_lambda_response(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "statusCode" in value:
            return value
    raise ValueError("SAM output did not contain a Lambda proxy response")


def main() -> None:
    validation = run(
        ["sam", "validate", "--lint", "--template-file", str(TEMPLATE)]
    )
    build = run(["sam", "build", "--template-file", str(TEMPLATE)])
    invocation = run(
        [
            "sam",
            "local",
            "invoke",
            "SearchFunction",
            "--event",
            str(EVENT),
            "--template",
            str(BUILT_TEMPLATE),
        ]
    )
    combined = invocation.stdout + "\n" + invocation.stderr
    response = parse_lambda_response(combined)
    body = json.loads(response["body"])
    rows = body.get("result", [])
    ranks = [row.get("rank") for row in rows]
    job_ids = [row.get("job_id") for row in rows]
    paths = [
        path
        for row in rows
        for path in row.get("graph_trace", [])
    ]
    duration_matches = re.findall(r"\bDuration:\s+([0-9.]+)\s+ms", combined)
    checks = {
        "sam_validate_lint": validation.returncode == 0,
        "sam_build": build.returncode == 0,
        "sam_local_invoke": invocation.returncode == 0,
        "http_200": response.get("statusCode") == 200,
        "top_10": len(rows) == 10,
        "contiguous_ranks": ranks == list(range(1, 11)),
        "unique_job_ids": len(job_ids) == len(set(job_ids)),
        "index_version": body.get("meta", {}).get("index_version")
        == "demo-2026.06.05-v1",
        "graph_provenance": bool(paths)
        and all(
            path.get("path")
            and path.get("edges")
            and str(path.get("evidence", "")).strip()
            for path in paths
        ),
    }
    report = {
        "metadata": {
            "schema": "skillweave-sam-local-smoke-v1",
            "verified_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "runtime": "python3.13",
            "architecture": "arm64",
            "event": "tests/fixtures/search-event.json",
            "package": "dist/skillweave-lambda.zip",
        },
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "result_count": len(rows),
            "resolved_skill_count": len(
                body.get("meta", {}).get("resolved_skills", [])
            ),
            "graph_path_count": len(paths),
            "duration_ms": (
                float(duration_matches[-1]) if duration_matches else None
            ),
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
