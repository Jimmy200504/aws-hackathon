#!/usr/bin/env python3
"""Run the complete deterministic Skill Graph build with stage-level resume."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from pipeline.deterministic_extract import DEFAULT_CUTOFF


STAGES = ("extract", "resolve", "relations", "export")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True)
class BuildConfig:
    input_path: Path
    duty_map: Path
    seed: Path
    icap: Path | None
    work_root: Path
    run_id: str
    graph_version: str
    cutoff: str = DEFAULT_CUTOFF
    part_size: int = 1000

    @property
    def extraction_root(self) -> Path:
        return self.work_root / "extraction"

    @property
    def resolved_root(self) -> Path:
        return self.work_root / "resolved"

    @property
    def relations_root(self) -> Path:
        return self.work_root / "relations"

    @property
    def release_root(self) -> Path:
        return self.work_root / "release"

    @property
    def state_path(self) -> Path:
        return self.work_root / "pipeline-state.json"


def validate_config(config: BuildConfig) -> None:
    for label, path in (
        ("input", config.input_path),
        ("duty map", config.duty_map),
        ("seed ontology", config.seed),
    ):
        if not path.is_file():
            raise ValueError(f"{label} file does not exist: {path}")
    if config.icap is not None and not config.icap.is_file():
        raise ValueError(f"iCAP vocabulary file does not exist: {config.icap}")
    if config.part_size < 1:
        raise ValueError("part size must be positive")
    for label, value in (("run ID", config.run_id), ("graph version", config.graph_version)):
        if not value or "/" in value or ".." in value:
            raise ValueError(f"invalid {label}: {value!r}")


def stage_commands(config: BuildConfig) -> dict[str, list[str]]:
    executable = sys.executable
    extract = [
        executable,
        "-m",
        "pipeline.deterministic_extract",
        "--input",
        str(config.input_path),
        "--duty-map",
        str(config.duty_map),
        "--seed",
        str(config.seed),
        "--output",
        str(config.extraction_root),
        "--graph-cutoff",
        config.cutoff,
        "--part-size",
        str(config.part_size),
    ]
    if config.icap is not None:
        extract.extend(("--icap", str(config.icap)))
    resolve = [
        executable,
        "-m",
        "pipeline.build_graph",
        "resolve",
        "--input-root",
        str(config.extraction_root),
        "--seed",
        str(config.seed),
        "--output-root",
        str(config.resolved_root),
        "--all-scopes",
    ]
    if config.icap is not None:
        resolve.extend(("--icap", str(config.icap)))
    return {
        "extract": extract,
        "resolve": resolve,
        "relations": [
            executable,
            "-m",
            "pipeline.build_graph",
            "relations",
            "--jobs-root",
            str(config.resolved_root),
            "--output-root",
            str(config.relations_root),
            "--all-scopes",
        ],
        "export": [
            executable,
            "-m",
            "pipeline.build_graph",
            "export",
            "--extraction-root",
            str(config.extraction_root),
            "--resolved-root",
            str(config.resolved_root),
            "--relations-root",
            str(config.relations_root),
            "--output",
            str(config.release_root),
            "--run-id",
            config.run_id,
            "--graph-version",
            config.graph_version,
            "--cutoff",
            config.cutoff,
            "--all-scopes",
        ],
    }


def expected_outputs(config: BuildConfig) -> dict[str, tuple[Path, ...]]:
    scopes = ("evaluation-cutoff", "latest")
    return {
        "extract": (
            config.extraction_root / "checkpoint.json",
            *(config.extraction_root / scope / "manifest.json" for scope in scopes),
        ),
        "resolve": tuple(
            config.resolved_root / scope / name
            for scope in scopes
            for name in ("nodes.jsonl", "job-skill-edges.jsonl", "jobs.jsonl", "resolutions.jsonl")
        ),
        "relations": tuple(
            config.relations_root / scope / name
            for scope in scopes
            for name in ("relation-candidates.jsonl", "relation-edges.jsonl", "relation-rejections.jsonl")
        ),
        "export": tuple(
            config.release_root / "runs" / config.run_id / scope / "manifest.json"
            for scope in scopes
        ),
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "stages": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("stages"), dict):
        raise RuntimeError(f"invalid pipeline state: {path}")
    return value


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_json(state))
    temporary.replace(path)


def _fingerprint(command: Sequence[str], dependency_fingerprint: str) -> str:
    payload = {"command": list(command), "dependency_fingerprint": dependency_fingerprint}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _outputs_complete(stage: str, paths: tuple[Path, ...]) -> bool:
    if not all(path.is_file() for path in paths):
        return False
    if stage != "extract":
        return True
    for path in paths:
        if path.name == "manifest.json":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest.get("complete") is not True:
                return False
    return True


def build_plan(config: BuildConfig) -> list[dict[str, Any]]:
    commands = stage_commands(config)
    outputs = expected_outputs(config)
    dependency = "root"
    plan: list[dict[str, Any]] = []
    for stage in STAGES:
        fingerprint = _fingerprint(commands[stage], dependency)
        plan.append({
            "stage": stage,
            "command": commands[stage],
            "fingerprint": fingerprint,
            "expected_outputs": [str(path) for path in outputs[stage]],
        })
        dependency = fingerprint
    return plan


def run_pipeline(
    config: BuildConfig,
    *,
    stop_after: str = "export",
    force_stage: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[dict[str, str]]:
    validate_config(config)
    if stop_after not in STAGES:
        raise ValueError(f"invalid stop stage: {stop_after}")
    if force_stage is not None and force_stage not in STAGES:
        raise ValueError(f"invalid force stage: {force_stage}")
    plan = build_plan(config)
    state = _read_state(config.state_path)
    outputs = expected_outputs(config)
    results: list[dict[str, str]] = []
    force = False
    for item in plan:
        stage = str(item["stage"])
        force = force or stage == force_stage
        previous = state["stages"].get(stage, {})
        resumable = (
            not force
            and previous.get("status") == "completed"
            and previous.get("fingerprint") == item["fingerprint"]
            and _outputs_complete(stage, outputs[stage])
        )
        if resumable:
            results.append({"stage": stage, "status": "skipped"})
        else:
            state["stages"][stage] = {
                "status": "running",
                "fingerprint": item["fingerprint"],
                "command": item["command"],
            }
            _write_state(config.state_path, state)
            completed = runner(item["command"], check=False)
            if completed.returncode != 0:
                state["stages"][stage]["status"] = "failed"
                state["stages"][stage]["returncode"] = completed.returncode
                _write_state(config.state_path, state)
                raise RuntimeError(f"{stage} failed with exit code {completed.returncode}")
            if not _outputs_complete(stage, outputs[stage]):
                state["stages"][stage]["status"] = "failed"
                state["stages"][stage]["error"] = "expected outputs are incomplete"
                _write_state(config.state_path, state)
                raise RuntimeError(f"{stage} completed without all expected outputs")
            state["stages"][stage] = {
                "status": "completed",
                "fingerprint": item["fingerprint"],
                "command": item["command"],
            }
            _write_state(config.state_path, state)
            results.append({"stage": stage, "status": "completed"})
        if stage == stop_after:
            break
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resumable zero-LLM full Skill Graph build")
    parser.add_argument("--input", type=Path, default=Path("data/dataset/職缺.csv"))
    parser.add_argument("--duty-map", type=Path, default=Path("data/dataset/職務對照表.csv"))
    parser.add_argument("--seed", type=Path, default=Path("config/skill_ontology.seed.json"))
    parser.add_argument("--icap", type=Path, default=Path("config/icap_vocabulary.reviewed.json"))
    parser.add_argument("--work-root", type=Path, default=Path("artifacts/skill-graph-full"))
    parser.add_argument("--run-id", default="deterministic-v1-full")
    parser.add_argument("--graph-version", default="deterministic-v1")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--part-size", type=int, default=1000)
    parser.add_argument("--stop-after", choices=STAGES, default="export")
    parser.add_argument("--force-stage", choices=STAGES)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = BuildConfig(
        input_path=args.input,
        duty_map=args.duty_map,
        seed=args.seed,
        icap=args.icap,
        work_root=args.work_root,
        run_id=args.run_id,
        graph_version=args.graph_version,
        cutoff=args.cutoff,
        part_size=args.part_size,
    )
    validate_config(config)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": build_plan(config)}, ensure_ascii=False, indent=2))
        return
    results = run_pipeline(config, stop_after=args.stop_after, force_stage=args.force_stage)
    print(json.dumps({"work_root": str(config.work_root), "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
