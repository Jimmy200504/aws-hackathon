#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.graph_validation import validate_extraction
PROMPT = ROOT / "config" / "bedrock-skill-prompt.txt"
SKILL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "surface": {"type": "string"},
                    "canonical_skill": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": [
                            "language",
                            "framework",
                            "library",
                            "tool",
                            "platform",
                            "database",
                            "method",
                            "certification",
                            "domain",
                            "soft_skill",
                        ],
                    },
                    "level": {
                        "type": "string",
                        "enum": ["required", "preferred", "mentioned"],
                    },
                    "evidence": {"type": "string"},
                    "evidence_field": {
                        "type": "string",
                        "enum": ["職務名稱", "職務內容", "電腦技能資料", "工作技能", "專業證照"],
                    },
                    "confidence": {"type": "number"},
                },
                "required": [
                    "surface",
                    "canonical_skill",
                    "type",
                    "level",
                    "evidence",
                    "evidence_field",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["RELATED_TO", "PREREQUISITE_OF", "SPECIALIZATION_OF"],
                    },
                    "target": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["source", "type", "target", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["mentions", "relations"],
    "additionalProperties": False,
}


def extract_with_bedrock(
    client: Any,
    model_id: str,
    system_prompt: str,
    job: dict[str, Any],
    max_tokens: int = 3000,
) -> dict[str, Any]:
    document = {
        "job_id": job["job_id"],
        "職務名稱": job.get("職務名稱", ""),
        "職務內容": job.get("職務內容", ""),
        "電腦技能資料": job.get("電腦技能資料", ""),
        "工作技能": job.get("工作技能", ""),
        "專業證照": job.get("專業證照", ""),
        "職務分類": job.get("職務分類", []),
    }
    response = client.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": "Extract only grounded skill graph proposals from this JSON:\n"
                        + json.dumps(document, ensure_ascii=False)
                    }
                ],
            }
        ],
        outputConfig={
            "textFormat": {
                "type": "json_schema",
                "structure": {
                    "jsonSchema": {
                        "schema": json.dumps(SKILL_OUTPUT_SCHEMA, ensure_ascii=False),
                        "name": "skill_graph_extraction",
                        "description": "Grounded skill mentions and proposed skill relations",
                    }
                },
            }
        },
        inferenceConfig={"temperature": 0, "maxTokens": max_tokens},
    )
    text = response["output"]["message"]["content"][0]["text"]
    proposal = json.loads(text)
    proposal["__bedrock_usage"] = response.get("usage", {})
    proposal["__bedrock_metrics"] = response.get("metrics", {})
    return proposal


def main() -> None:
    parser = argparse.ArgumentParser(description="Bedrock JD skill extraction batch worker")
    parser.add_argument("--input", type=Path, required=True, help="Train-only JSONL jobs")
    parser.add_argument("--output", type=Path, required=True, help="Validated JSONL edges")
    parser.add_argument("--quarantine", type=Path, required=True, help="Rejected JSONL records")
    parser.add_argument("--graph-cutoff", required=True)
    parser.add_argument("--model-id", default=os.getenv("BEDROCK_MODEL_ID"))
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-northeast-1"))
    parser.add_argument(
        "--max-records",
        type=int,
        default=0,
        help="Optional bounded pilot size; 0 processes the complete input",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Bounded parallel Converse requests with adaptive SDK retries",
    )
    parser.add_argument("--max-tokens", type=int, default=3000)
    args = parser.parse_args()
    if not args.model_id:
        raise SystemExit("--model-id or BEDROCK_MODEL_ID is required")

    try:
        import boto3
    except ImportError as exc:
        raise SystemExit(
            "boto3 is required for this production worker; install requirements-production.lock"
        ) from exc
    from botocore.config import Config

    client = boto3.client(
        "bedrock-runtime",
        region_name=args.region,
        config=Config(
            retries={"max_attempts": 8, "mode": "adaptive"},
            read_timeout=120,
        ),
    )
    system_prompt = PROMPT.read_text(encoding="utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.quarantine.parent.mkdir(parents=True, exist_ok=True)
    jobs = []
    with args.input.open(encoding="utf-8") as source:
        for record_index, line in enumerate(source):
            if args.max_records > 0 and record_index >= args.max_records:
                break
            jobs.append(json.loads(line))

    def process(job: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        source_fields = {
            field: str(job.get(field, ""))
            for field in [
                "職務名稱",
                "職務內容",
                "電腦技能資料",
                "工作技能",
                "專業證照",
            ]
        }
        try:
            proposal = extract_with_bedrock(
                client,
                args.model_id,
                system_prompt,
                job,
                max_tokens=args.max_tokens,
            )
            usage = proposal.pop("__bedrock_usage", {})
            metrics = proposal.pop("__bedrock_metrics", {})
            validation = validate_extraction(
                source_fields,
                job["職缺最後修改時間"],
                args.graph_cutoff,
                proposal,
            )
            envelope = {
                "job_id": job["job_id"],
                "source_modified_at": job["職缺最後修改時間"],
                "model_id": args.model_id,
                "prompt_version": "jd-skill-v3",
                "usage": usage,
                "metrics": metrics,
                "mentions": validation.accepted_mentions,
                "relations": validation.accepted_relations,
                "rejections": validation.rejected,
            }
            return validation.valid, envelope
        except Exception as exc:
            return False, {
                "job_id": job.get("job_id"),
                "fatal": type(exc).__name__,
                "message": str(exc),
            }

    with (
        args.output.open("w", encoding="utf-8") as accepted,
        args.quarantine.open("w", encoding="utf-8") as rejected,
        concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, args.max_workers)
        ) as executor,
    ):
        for valid, envelope in executor.map(process, jobs):
            stream = accepted if valid else rejected
            stream.write(json.dumps(envelope, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
