"""Shared derived-feature extraction for raw 職缺.csv rows.

userSearchLog shows job seekers searching heavily for remote/work-from-home
arrangements (e.g. 遠端, 在家工作, WFH, 居家辦公) and pay cadence (現領, 日領,
月薪4萬以上), but the job posting fields historically kept `薪資` as an
opaque display string and had no structured work-arrangement signal at all.
This module derives:

- `salary_min` / `salary_max` / `salary_type`: parsed from the already
  structured `薪資下限` / `薪資上限` columns and the `薪資` type prefix.
- `is_remote`: a deterministic keyword match against title + description,
  mirroring the vocabulary job seekers actually type into search.

Keeping this logic in one module avoids drift between the three job-document
builders (demo index, full OpenSearch index, benchmark fixture).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# Terms observed at high frequency in userSearchLog_20260601_20260607.csv's
# `ks` (search keyword) column. Ordered roughly by search volume.
_REMOTE_PATTERN = re.compile(
    "|".join(
        re.escape(term)
        for term in [
            "遠端工作",
            "遠端上班",
            "遠端客服",
            "全遠端",
            "遠端",
            "在家工作",
            "在家上班",
            "在家兼職",
            "居家辦公",
            "居家工作",
            "WFH",
            "wfh",
            "work from home",
            "remote work",
            "remote job",
            "remote",
        ]
    ),
    re.IGNORECASE,
)

# 居家照顧/居家服務/居家護理 etc. are in-person home-visit care jobs, not
# remote work, even though they contain "居家". Exclude them explicitly so
# is_remote does not misfire on that large, unrelated category.
_HOME_CARE_EXCLUSION = re.compile(
    "|".join(
        re.escape(term)
        for term in [
            "居家照顧",
            "居家照服",
            "居家服務",
            "居家護理",
            "居家督導",
            "居家清潔",
            "居家托育",
        ]
    )
)

_SALARY_TYPE_MAP = {
    "月薪": "monthly",
    "日薪": "daily",
    "時薪": "hourly",
    "年薪": "yearly",
    "面議": "negotiable",
}


def _norm(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "")


def is_remote_job(title: str | None, description: str | None) -> bool:
    """Detect a remote/work-from-home job from free-text fields.

    Deterministic keyword match; no ML dependency required so it stays
    usable in the dependency-free demo pipeline. False negatives (a remote
    job that never says so) are expected; the goal is to stop losing the
    common, explicit case job seekers already search for.
    """
    text = _norm(f"{title or ''} {description or ''}")
    if not text.strip():
        return False
    # Strip home-care phrases before matching so "居家" inside them cannot
    # combine with a stray "在家"/"遠端" elsewhere in the text.
    scrubbed = _HOME_CARE_EXCLUSION.sub(" ", text)
    return bool(_REMOTE_PATTERN.search(scrubbed))


def parse_salary(raw_salary: str | None, salary_min: str | None, salary_max: str | None) -> dict[str, Any]:
    """Parse 薪資/薪資下限/薪資上限 into structured min/max/type.

    `薪資下限`/`薪資上限` are already numeric strings in the source CSV; this
    just normalizes them to floats and derives a coarse `salary_type` from
    the `薪資` text prefix (月薪/日薪/時薪/年薪/面議) so the ranker and any
    downstream filter can reason about pay without re-parsing free text.
    """
    def to_float(value: str | None) -> float:
        try:
            return float(value) if value not in (None, "", "NULL") else 0.0
        except ValueError:
            return 0.0

    lower = to_float(salary_min)
    upper = to_float(salary_max)
    if upper and lower and upper < lower:
        lower, upper = upper, lower

    text = _norm(raw_salary or "")
    salary_type = "unknown"
    for prefix, type_name in _SALARY_TYPE_MAP.items():
        if text.startswith(prefix):
            salary_type = type_name
            break

    return {
        "salary_min": lower,
        "salary_max": upper,
        "salary_type": salary_type,
    }


def derive_job_fields(row: dict[str, str]) -> dict[str, Any]:
    """Compute the derived fields for one raw 職缺.csv row."""
    title = row.get("職務名稱", "")
    description = row.get("職務內容", "")
    fields = parse_salary(
        row.get("薪資"), row.get("薪資下限"), row.get("薪資上限")
    )
    fields["is_remote"] = is_remote_job(title, description)
    return fields
