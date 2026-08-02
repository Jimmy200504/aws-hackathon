"""Fargate stage entry point with an explicit, versioned S3 run specification.

The image intentionally keeps orchestration thin. Each stage executable and its
arguments are supplied by the immutable run spec instead of being hidden in the
Step Functions definition.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse


ALLOWED_COMMANDS = {
    "extract": "pipeline.deterministic_extract",
    "resolve": "pipeline.build_graph",
    "relations": "pipeline.build_graph",
    "export": "pipeline.build_graph",
}


def load_run_spec(uri: str, destination: Path) -> dict:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError("RUN_SPEC_S3_URI must be an s3:// bucket/key URI")
    import boto3
    boto3.client("s3").download_file(parsed.netloc, parsed.path.lstrip("/"), str(destination))
    value = json.loads(destination.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("run spec must be an object")
    return value


def sync_inputs(client: object, downloads: list[dict]) -> None:
    for item in downloads:
        parsed = urlparse(str(item.get("uri", "")))
        path = Path(str(item.get("path", "")))
        if parsed.scheme != "s3" or not parsed.netloc or not path.is_absolute():
            raise ValueError("downloads require an S3 URI and absolute local path")
        path.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(parsed.netloc, parsed.path.lstrip("/"), str(path))


def sync_outputs(client: object, uploads: list[dict]) -> None:
    for item in uploads:
        parsed = urlparse(str(item.get("uri", "")))
        path = Path(str(item.get("path", "")))
        if parsed.scheme != "s3" or not parsed.netloc or not path.is_absolute():
            raise ValueError("uploads require an S3 URI and absolute local path")
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for source in (file for file in files if file.is_file()):
            suffix = source.relative_to(path).as_posix() if path.is_dir() else source.name
            key = parsed.path.lstrip("/").rstrip("/") + "/" + suffix
            client.upload_file(str(source), parsed.netloc, key)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=sorted(ALLOWED_COMMANDS))
    args = parser.parse_args()
    spec_uri = os.getenv("RUN_SPEC_S3_URI", "")
    if not spec_uri:
        raise SystemExit("RUN_SPEC_S3_URI is required")
    spec = load_run_spec(spec_uri, Path("/tmp/run-spec.json"))
    stages = spec.get("stages", {})
    stage_spec = stages.get(args.stage)
    if isinstance(stage_spec, list):
        stage_spec = {"args": stage_spec}
    if not isinstance(stage_spec, dict):
        raise SystemExit(f"run spec has no valid {args.stage} stage")
    stage_args = stage_spec.get("args")
    if not isinstance(stage_args, list) or not all(isinstance(item, str) for item in stage_args):
        raise SystemExit(f"run spec has no valid {args.stage} argument list")
    import boto3
    s3 = boto3.client("s3")
    sync_inputs(s3, stage_spec.get("downloads", []))
    command = [os.sys.executable, "-m", ALLOWED_COMMANDS[args.stage]]
    if args.stage != "extract":
        command.append(args.stage)
    command.extend(stage_args)
    completed = subprocess.run(command, check=False)
    if completed.returncode == 0:
        sync_outputs(s3, stage_spec.get("uploads", []))
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
