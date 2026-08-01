#!/usr/bin/env python3
"""Build the closed vocabulary the online query normalizer is constrained to.

The organizer duty table is the only taxonomy that is simultaneously a job
field (`職務小類`), the meaning of the `d0` search filter, and small enough to
fit in a cached system prompt. Emitting it as a build artifact keeps the online
path free of CSV parsing and gives the validator an exact allow-list.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter, OrderedDict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "dataset"
OUTPUT = ROOT / "config" / "query-intent-vocab.json"

# `職缺屬性` and `工時` are single-select / comma-joined closed sets in the job
# CSV. They carry the attribute queries (現領/正職/兼職/晚班/暑期) that dominate
# the head of the search log but barely appear in job free text.
EMPLOYMENT_TYPES = ["全職", "兼職", "工讀", "中高階", "其他"]
SHIFTS = ["日班", "晚班", "中班", "假日班", "輪班"]
SALARY_TYPES = ["月薪", "時薪", "日薪", "年薪", "面議"]
# The model is prompted in Chinese, but `app/job_fields.derive_job_fields`
# indexes the canonical English type. Map here so a normalized intent can be
# used directly as an OpenSearch term without a second vocabulary.
SALARY_TYPE_MAP = {
    "月薪": "monthly",
    "日薪": "daily",
    "時薪": "hourly",
    "年薪": "yearly",
    "面議": "negotiable",
}


def norm(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower().replace("臺", "台")
    return re.sub(r"\s+", " ", text).strip()


def build_duties(data_dir: Path) -> tuple[OrderedDict[str, list[str]], dict[str, str]]:
    groups: OrderedDict[str, list[str]] = OrderedDict()
    alias_frequency: Counter[str] = Counter()
    pending: list[tuple[str, list[str]]] = []
    with (data_dir / "職務對照表.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            label = row["CodeNameA"].strip()
            major = row["CodeNameC"].strip()
            if not label or not major:
                continue
            bucket = groups.setdefault(major, [])
            if label not in bucket:
                bucket.append(label)
            aliases = [label, row["CodeNameEN"].strip()]
            aliases.extend(
                re.split(r"<br\s*/?>|[、;；\n]", row["CodeAlike"], flags=re.IGNORECASE)
            )
            cleaned: list[str] = []
            for alias in aliases:
                value = norm(re.sub(r"<[^>]+>", " ", alias)).strip(" /,，")
                if 2 <= len(value) <= 40 and value not in cleaned:
                    cleaned.append(value)
            for alias in cleaned:
                alias_frequency[alias] += 1
            pending.append((label, cleaned))
    # An alias shared by many duty codes is a generic word, not a resolution.
    alias_to_duty: dict[str, str] = {}
    for label, aliases in pending:
        for alias in aliases:
            if alias == norm(label) or alias_frequency[alias] <= 2:
                alias_to_duty.setdefault(alias, label)
    return groups, alias_to_duty


def build_locations(data_dir: Path) -> tuple[list[str], dict[str, str]]:
    """Map every district/city name to the city granularity jobs are stored at."""
    cities: list[str] = []
    district_cities: dict[str, set[str]] = {}
    with (data_dir / "城市對照表.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = row["CodeNameA"].strip()
            city = row["CodeNameB"].strip()
            if not name or not city:
                continue
            if row["CodeType"] == "2" and city not in cities:
                cities.append(city)
            district_cities.setdefault(norm(name), set()).add(city)
    resolved = {
        alias: next(iter(candidates))
        # An ambiguous district (中山區 exists in several cities) must not be
        # silently resolved to one city; it stays a free-text term instead.
        for alias, candidates in district_cities.items()
        if len(candidates) == 1
    }
    for city in cities:
        resolved[norm(city)] = city
    return cities, resolved


def prompt_block(groups: OrderedDict[str, list[str]]) -> str:
    return "\n".join(
        f"[{major}] " + "、".join(labels) for major, labels in groups.items()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    groups, alias_to_duty = build_duties(args.data_dir)
    cities, location_aliases = build_locations(args.data_dir)
    duty_categories = [label for labels in groups.values() for label in labels]
    vocab = {
        "schema": "skillweave-query-intent-vocab-v1",
        "source": "organizer 職務對照表.csv + 城市對照表.csv",
        "duty_groups": groups,
        "duty_categories": sorted(set(duty_categories)),
        "duty_aliases": alias_to_duty,
        "cities": cities,
        "location_aliases": location_aliases,
        "employment_types": EMPLOYMENT_TYPES,
        "shifts": SHIFTS,
        "salary_types": SALARY_TYPES,
        "salary_type_map": SALARY_TYPE_MAP,
        "prompt_block": prompt_block(groups),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(vocab, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "duty_major_groups": len(groups),
                "duty_categories": len(vocab["duty_categories"]),
                "duty_aliases": len(alias_to_duty),
                "cities": len(cities),
                "location_aliases": len(location_aliases),
                "prompt_block_chars": len(vocab["prompt_block"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
