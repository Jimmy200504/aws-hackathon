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

# Salary-type keyword -> canonical type, used both for job posting text and
# for parsing the user's query (e.g. "時薪200" -> hourly, target=200).
_QUERY_SALARY_TYPE_KEYWORDS = (
    ("時薪", "hourly"),
    ("日薪", "daily"),
    ("月薪", "monthly"),
    ("年薪", "yearly"),
)

# userSearchLog shows queries like 月薪4萬以上, 時薪200以上, 月薪5萬, 時薪300,
# 日薪2000, 年薪百萬. Chinese numerals combine a leading digit with 萬 (x10,000)
# or 千 (x1,000); "百萬" alone means 1,000,000. Comparison direction is almost
# always implicit "at least" (a bare number or explicit 以上/起); explicit
# "剛好/恰好" (exactly) is rare in the data but supported for completeness.
_SALARY_QUERY_PATTERN = re.compile(
    r"(?P<type>時薪|日薪|月薪|年薪)"
    r"\s*"
    r"(?P<exact>剛好|恰好)?"
    r"\s*"
    r"(?P<number>[0-9]+(?:\.[0-9]+)?)"
    r"(?P<wan>萬)?"
    r"(?P<qian>千)?"
    r"(?P<extra>[0-9]+)?"
    r"\s*(?P<unit>元)?"
    r"\s*(?P<at_least>以上|起)?"
)
# 百萬 without a leading digit (年薪百萬) means exactly 1,000,000.
_BAI_WAN_PATTERN = re.compile(r"(?P<type>時薪|日薪|月薪|年薪)\s*(?P<at_least>以上)?\s*百萬")


def _norm(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "")


def parse_salary_intent(query: str) -> dict[str, Any] | None:
    """Parse an explicit salary condition out of a search query.

    Mirrors real userSearchLog phrasing: 月薪4萬以上, 時薪200以上, 月薪5萬,
    時薪300, 日薪2000, 年薪百萬. Returns None when the query does not state a
    salary type + number (e.g. a bare "4萬以上" without 月薪/時薪/... is not
    parsed, since the salary *type* to compare against is ambiguous).

    Returns a dict with:
    - salary_type: "monthly" | "hourly" | "daily" | "yearly"
    - target: the parsed number (already scaled for 萬/千/百萬)
    - comparator: "at_least" (剛好/恰好 explicit) or "at_least" (bare number
      or 以上/起 -- job seekers overwhelmingly mean "at least" when typing a
      bare salary number, per the observed query distribution) or "exact"
    """
    text = _norm(query)

    bai_wan_match = _BAI_WAN_PATTERN.search(text)
    if bai_wan_match:
        salary_type = _SALARY_TYPE_MAP.get(bai_wan_match.group("type"))
        if salary_type in ("monthly", "hourly", "daily", "yearly"):
            return {
                "salary_type": salary_type,
                "target": 1_000_000.0,
                "comparator": "at_least",
            }

    match = _SALARY_QUERY_PATTERN.search(text)
    if not match:
        return None
    salary_type = _SALARY_TYPE_MAP.get(match.group("type"))
    if salary_type not in ("monthly", "hourly", "daily", "yearly"):
        return None

    # "年薪14個月" means "14 months' pay per year" (a bonus-month count), not
    # an absolute salary figure -- do not parse it as target=14.
    tail = text[match.end():match.end() + 3]
    if "個月" in tail:
        return None

    number = float(match.group("number"))
    if match.group("wan"):
        number *= 10_000
        # 4萬5 (以上) means 45,000 -- the trailing digit is in units of 千.
        extra = match.group("extra")
        if extra:
            number += float(extra) * 1_000
    elif match.group("qian"):
        number *= 1_000

    comparator = "exact" if match.group("exact") else "at_least"
    return {
        "salary_type": salary_type,
        "target": number,
        "comparator": comparator,
    }


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
