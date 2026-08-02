#!/usr/bin/env python3
"""Verify that one public deployment serves this checkout's web and API.

Lambda code updates can briefly leave old execution environments draining. A
single successful GET is therefore not enough: require several consecutive
samples where every public web asset exactly matches the local deployment
inputs, then exercise the backend through the same API Gateway URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WEB_ASSETS = ("index.html", "app.js", "styles.css")
DEFAULT_QUERY = "AWS Docker Kubernetes"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expected_assets(root: Path = ROOT) -> dict[str, bytes]:
    return {name: (root / "web" / name).read_bytes() for name in WEB_ASSETS}


def deployment_url(base_url: str, path: str, nonce: str | None = None) -> str:
    base = base_url.rstrip("/") + "/"
    relative = "" if path == "index.html" else path
    url = urllib.parse.urljoin(base, relative)
    if nonce is not None:
        url += "?deployment_check=" + urllib.parse.quote(nonce)
    return url


def request_bytes(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> bytes:
    data = None
    headers = {"User-Agent": "skillweave-deployment-verifier/1"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read()


def request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    decoded = json.loads(request_bytes(url, payload=payload, timeout=timeout))
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{url} did not return a JSON object")
    return decoded


def asset_mismatches(
    expected: dict[str, bytes], observed: dict[str, bytes]
) -> dict[str, dict[str, str]]:
    return {
        name: {
            "expected": sha256(content),
            "observed": sha256(observed.get(name, b"")),
        }
        for name, content in expected.items()
        if observed.get(name) != content
    }


def validate_backend(
    health: dict[str, Any],
    search: dict[str, Any],
    *,
    require_full_corpus: bool,
    require_neptune: bool,
    expected_graph_version: str | None,
) -> dict[str, Any]:
    errors: list[str] = []
    if health.get("status") != "ok":
        errors.append("health.status is not ok")
    if require_full_corpus and health.get("full_corpus_retrieval") is not True:
        errors.append("health.full_corpus_retrieval is not true")
    if require_neptune and health.get("graph_backend") != "neptune_analytics":
        errors.append("health.graph_backend is not neptune_analytics")

    rows = search.get("result")
    meta = search.get("meta")
    if not isinstance(rows, list) or not rows:
        errors.append("search returned no result rows")
    if not isinstance(meta, dict):
        errors.append("search.meta is missing")
        meta = {}
    normalization = meta.get("query_normalization")
    if not isinstance(normalization, dict) or not normalization.get("normalized_query"):
        errors.append("search.meta.query_normalization is missing")
        normalization = {}
    if require_full_corpus and meta.get("candidate_source") != "opensearch_full_corpus":
        errors.append("search did not use opensearch_full_corpus")
    if require_neptune and meta.get("graph_backend") != "neptune_analytics":
        errors.append("search did not use neptune_analytics")
    if expected_graph_version and meta.get("graph_version") != expected_graph_version:
        errors.append(
            f"graph_version is {meta.get('graph_version')!r}, expected {expected_graph_version!r}"
        )
    degraded = meta.get("degraded_components")
    if degraded:
        errors.append(f"search degraded_components is not empty: {degraded!r}")
    if errors:
        raise RuntimeError("; ".join(errors))

    return {
        "health": health.get("status"),
        "full_corpus_retrieval": health.get("full_corpus_retrieval"),
        "candidate_source": meta.get("candidate_source"),
        "graph_backend": meta.get("graph_backend"),
        "graph_version": meta.get("graph_version"),
        "normalization_source": normalization.get("source"),
        "result_count": len(rows),
    }


def verify_assets(
    base_url: str,
    expected: dict[str, bytes],
    *,
    stable_samples: int,
    max_attempts: int,
    retry_seconds: float,
    timeout: float,
) -> dict[str, str]:
    consecutive = 0
    last_mismatches: dict[str, dict[str, str]] = {}
    for attempt in range(1, max_attempts + 1):
        observed: dict[str, bytes] = {}
        try:
            for name in WEB_ASSETS:
                observed[name] = request_bytes(
                    deployment_url(
                        base_url, name, f"{time.time_ns()}-{attempt}-{name}"
                    ),
                    timeout=timeout,
                )
            last_mismatches = asset_mismatches(expected, observed)
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            consecutive = 0
            print(
                f"Asset sample {attempt}/{max_attempts}: request failed: {exc}",
                flush=True,
            )
        else:
            if last_mismatches:
                consecutive = 0
                names = ", ".join(sorted(last_mismatches))
                print(
                    f"Asset sample {attempt}/{max_attempts}: waiting for {names} to converge",
                    flush=True,
                )
            else:
                consecutive += 1
                print(
                    f"Asset sample {attempt}/{max_attempts}: exact match "
                    f"({consecutive}/{stable_samples} stable)",
                    flush=True,
                )
                if consecutive >= stable_samples:
                    return {
                        name: sha256(content) for name, content in expected.items()
                    }
        if attempt < max_attempts:
            time.sleep(retry_seconds)
    raise RuntimeError(
        f"web assets did not converge after {max_attempts} samples: {last_mismatches}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify exact frontend assets and backend health/search on AWS"
    )
    parser.add_argument("--url", required=True, help="Public API Gateway stage URL")
    parser.add_argument("--stable-samples", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=12)
    parser.add_argument("--retry-seconds", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--require-full-corpus", action="store_true")
    parser.add_argument("--require-neptune", action="store_true")
    parser.add_argument("--expected-graph-version")
    args = parser.parse_args()
    if not args.url.startswith("https://"):
        parser.error("--url must use public HTTPS")
    if args.stable_samples < 1 or args.max_attempts < args.stable_samples:
        parser.error("samples must be positive and max-attempts >= stable-samples")

    hashes = verify_assets(
        args.url,
        expected_assets(),
        stable_samples=args.stable_samples,
        max_attempts=args.max_attempts,
        retry_seconds=args.retry_seconds,
        timeout=args.timeout,
    )
    health = request_json(deployment_url(args.url, "health"), timeout=args.timeout)
    search = request_json(
        deployment_url(args.url, "api/v1/jobs/search"),
        payload={"query": DEFAULT_QUERY, "top_k": 3, "use_graph": True},
        timeout=args.timeout,
    )
    backend = validate_backend(
        health,
        search,
        require_full_corpus=args.require_full_corpus,
        require_neptune=args.require_neptune,
        expected_graph_version=args.expected_graph_version,
    )
    print(
        json.dumps(
            {
                "passed": True,
                "url": args.url.rstrip("/") + "/",
                "assets": hashes,
                **backend,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
