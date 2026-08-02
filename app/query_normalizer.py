from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VOCAB_PATH = ROOT / "config" / "query-intent-vocab.json"
DEFAULT_PROMPT_PATH = ROOT / "config" / "query-intent-prompt.txt"
DEFAULT_TOP_QUERIES_PATH = ROOT / "config" / "top-queries.json"
DEFAULT_INTENTS_PATH = ROOT / "config" / "query-intents.json"
DEFAULT_RELEASE_INTENTS_PATH = ROOT / "config" / "query-intents-release.json"
MAX_QUERY_CHARS = 500
MAX_EXPANSION_CHARS = 480

# One structured intent per input query. `additionalProperties: false` plus the
# closed enums keep the model inside a vocabulary the retriever can act on; the
# Python validator below still re-checks every value because a schema cannot
# express "must be one of these 690 duty labels" cheaply.
QUERY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "intent_type": {
                        "type": "string",
                        "enum": [
                            "occupation",
                            "attribute",
                            "company",
                            "location",
                            "industry",
                            "skill",
                            "noise",
                            "mixed",
                        ],
                    },
                    "duty_categories": {"type": "array", "items": {"type": "string"}},
                    "locations": {"type": "array", "items": {"type": "string"}},
                    "employment_types": {"type": "array", "items": {"type": "string"}},
                    "shifts": {"type": "array", "items": {"type": "string"}},
                    "salary_type": {"type": ["string", "null"]},
                    "company": {"type": ["string", "null"]},
                    "keep_terms": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "id",
                    "intent_type",
                    "duty_categories",
                    "locations",
                    "employment_types",
                    "shifts",
                    "salary_type",
                    "company",
                    "keep_terms",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

# Backward-compatible contract for callers that construct the normalizer
# directly. Production uses the richer batch contract by explicitly loading a
# QueryIntentVocabulary in ``from_environment``.
LEGACY_QUERY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"normalized_query": {"type": "string"}},
    "required": ["normalized_query"],
    "additionalProperties": False,
}

_SPACE = re.compile(r"\s+")
_SEPARATORS = re.compile(
    r"[,，、/／|｜;；:：+＋()（）\[\]【】{}「」『』·・_~～\-]+"
)
# Colloquial pay-cadence wording that still counts as the user stating one.
SALARY_SURFACE_ALIASES: dict[str, tuple[str, ...]] = {
    "hourly": ("時薪", "鐘點"),
    "daily": ("日薪", "日領", "日結"),
    "monthly": ("月薪", "月領"),
    "yearly": ("年薪",),
    "negotiable": ("面議",),
}


def deterministic_fallback(query: str) -> str:
    """Minimal safety fallback; semantic normalization belongs to Bedrock."""
    value = unicodedata.normalize("NFKC", query).strip()
    return " ".join(value.split())


def _norm(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower().replace("臺", "台")
    return _SPACE.sub(" ", text).strip()


@dataclass(frozen=True)
class StructuredQueryIntent:
    """Validated, retriever-ready reading of one raw search query."""

    intent_type: str = "unknown"
    duty_categories: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    employment_types: tuple[str, ...] = ()
    shifts: tuple[str, ...] = ()
    salary_type: str | None = None
    company: str | None = None
    keep_terms: tuple[str, ...] = ()
    confidence: float = 0.0
    # Taxonomy expansion the ranker may use as a second-pass rescue. Carried on
    # the intent so callers wire one object through instead of two.
    recall_expansion: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StructuredQueryIntent:
        def strings(key: str) -> tuple[str, ...]:
            value = payload.get(key)
            return tuple(str(item) for item in value) if isinstance(value, list) else ()

        salary = payload.get("salary_type")
        company = payload.get("company")
        return cls(
            intent_type=str(payload.get("intent_type", "unknown")),
            duty_categories=strings("duty_categories"),
            locations=strings("locations"),
            employment_types=strings("employment_types"),
            shifts=strings("shifts"),
            salary_type=salary if isinstance(salary, str) else None,
            company=company if isinstance(company, str) else None,
            keep_terms=strings("keep_terms"),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            recall_expansion=str(payload.get("recall_expansion", "")),
        )

    def is_empty(self) -> bool:
        return not (
            self.duty_categories
            or self.locations
            or self.employment_types
            or self.shifts
            or self.company
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_type": self.intent_type,
            "duty_categories": list(self.duty_categories),
            "locations": list(self.locations),
            "employment_types": list(self.employment_types),
            "shifts": list(self.shifts),
            "salary_type": self.salary_type,
            "company": self.company,
            "keep_terms": list(self.keep_terms),
            "confidence": round(self.confidence, 3),
        }


@dataclass(frozen=True)
class QueryNormalization:
    DEGRADED_COMPONENT = "bedrock_query_normalizer"

    query: str
    source: str
    model_id: str | None
    degraded: bool = False
    intent: StructuredQueryIntent = field(default_factory=StructuredQueryIntent)
    # Taxonomy expansion offered to the ranker as a second-pass rescue only.
    expansion: str = ""

    def metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "model_id": self.model_id,
            "normalized_query": self.query,
        }
        if not self.intent.is_empty() or self.intent.intent_type != "unknown":
            payload["structured_intent"] = self.intent.as_dict()
        return payload

    def merge_degraded_components(self, components: list[str]) -> list[str]:
        merged = list(dict.fromkeys(components))
        if self.degraded and self.DEGRADED_COMPONENT not in merged:
            merged.append(self.DEGRADED_COMPONENT)
        return merged


class QueryIntentVocabulary:
    """Closed vocabulary + validator for structured query intents.

    Everything the model emits is re-checked here. An unrecognised duty label,
    an ambiguous district, or a hallucinated city is demoted to a free-text
    term rather than becoming a retrieval filter, because a wrong filter costs
    far more recall than a wrong keyword.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.duty_categories: set[str] = set(payload.get("duty_categories", []))
        self.duty_aliases: dict[str, str] = payload.get("duty_aliases", {})
        self.cities: list[str] = payload.get("cities", [])
        self.location_aliases: dict[str, str] = payload.get("location_aliases", {})
        self.employment_types: set[str] = set(payload.get("employment_types", []))
        self.shifts: set[str] = set(payload.get("shifts", []))
        self.salary_types: set[str] = set(payload.get("salary_types", []))
        # `app/job_fields.derive_job_fields` indexes the canonical English type,
        # so a Chinese reading has to be translated before it can be a filter.
        self.salary_type_map: dict[str, str] = payload.get("salary_type_map", {})
        self.prompt_block: str = payload.get("prompt_block", "")
        self._duty_by_norm = {_norm(label): label for label in self.duty_categories}
        # Longest-match sliding window beats a 5,700-branch regex alternation and
        # keeps deterministic parsing viable as the vocabulary grows.
        self._max_alias_chars = max(
            (len(alias) for alias in self.duty_aliases),
            default=0,
        )
        self._max_location_chars = max(
            (len(alias) for alias in self.location_aliases),
            default=0,
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> QueryIntentVocabulary | None:
        vocab_path = Path(path or os.getenv("QUERY_VOCAB_PATH", DEFAULT_VOCAB_PATH))
        if not vocab_path.is_file():
            LOGGER.warning(
                "Query intent vocabulary missing at %s; run scripts/build_query_vocab.py",
                vocab_path,
            )
            return None
        return cls(json.loads(vocab_path.read_text(encoding="utf-8")))

    def resolve_duty(self, value: str) -> str | None:
        candidate = str(value).strip()
        if candidate in self.duty_categories:
            return candidate
        normalized = _norm(candidate)
        return self._duty_by_norm.get(normalized) or self.duty_aliases.get(normalized)

    def resolve_location(self, value: str) -> tuple[str | None, str | None]:
        """Return (city, district_term) for a model-proposed location string."""
        normalized = _norm(value)
        if not normalized:
            return None, None
        city = None
        remainder = normalized
        for known in self.cities:
            known_norm = _norm(known)
            if known_norm and known_norm in normalized:
                city = known
                remainder = normalized.replace(known_norm, " ").strip()
                break
        if city is None:
            city = self.location_aliases.get(normalized)
            remainder = "" if city else normalized
        district = remainder if remainder and remainder != _norm(city or "") else None
        if city is None and district and district in self.location_aliases:
            city = self.location_aliases[district]
        return city, district

    def scan_duties(self, text: str) -> list[str]:
        return self._scan(text, self.duty_aliases, self._max_alias_chars)

    def scan_locations(self, text: str) -> list[str]:
        return self._scan(text, self.location_aliases, self._max_location_chars)

    @staticmethod
    def _scan(text: str, table: dict[str, str], max_len: int) -> list[str]:
        value = _norm(text)
        found: list[str] = []
        seen: set[str] = set()
        index = 0
        length = len(value)
        while index < length:
            window = min(max_len, length - index)
            while window >= 2:
                resolved = table.get(value[index : index + window])
                if resolved is not None:
                    if resolved not in seen:
                        seen.add(resolved)
                        found.append(resolved)
                    index += window - 1
                    break
                window -= 1
            index += 1
        return found

    def _grounded_salary_type(self, value: Any, raw_query: str) -> str | None:
        """Accept a pay cadence only when the query states one."""
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = value.strip()
        canonical = self.salary_type_map.get(candidate)
        if canonical is None and candidate in set(self.salary_type_map.values()):
            canonical = candidate
        if canonical is None:
            return None
        haystack = _norm(raw_query)
        surfaces = [
            surface
            for surface, mapped in self.salary_type_map.items()
            if mapped == canonical
        ]
        surfaces.extend(SALARY_SURFACE_ALIASES.get(canonical, ()))
        return canonical if any(_norm(s) in haystack for s in surfaces) else None

    def validate(self, payload: dict[str, Any], raw_query: str) -> StructuredQueryIntent:
        duties: list[str] = []
        rejected_terms: list[str] = []
        for value in _as_strings(payload.get("duty_categories"))[:8]:
            resolved = self.resolve_duty(value)
            if resolved and resolved not in duties:
                duties.append(resolved)
            elif value:
                rejected_terms.append(value)

        cities: list[str] = []
        district_terms: list[str] = []
        for value in _as_strings(payload.get("locations"))[:4]:
            city, district = self.resolve_location(value)
            # The district is the more specific claim and comes from the code
            # table, so it outranks a city the model stated alongside it.
            if district:
                corrected = self.location_aliases.get(_norm(district))
                if corrected:
                    city = corrected
                if district not in district_terms:
                    district_terms.append(district)
            if city and city not in cities:
                cities.append(city)
        # The model often puts the district in keep_terms and a wrong city in
        # locations (竹北市 is 新竹縣, not 新竹市). Cross-check and let the code
        # table win.
        for term in _as_strings(payload.get("keep_terms")):
            corrected = self.location_aliases.get(_norm(term))
            if corrected and corrected not in cities:
                cities.append(corrected)

        employment = [
            value
            for value in _as_strings(payload.get("employment_types"))
            if value in self.employment_types
        ]
        shifts = [
            value for value in _as_strings(payload.get("shifts")) if value in self.shifts
        ]
        # A pay cadence is only kept when the user actually wrote one. The model
        # otherwise guesses from the job type — 現領 came back as `daily`, while
        # the applies it generates are 73% monthly-salaried full-time roles.
        salary = self._grounded_salary_type(payload.get("salary_type"), raw_query)
        company = payload.get("company")
        company = company.strip()[:60] if isinstance(company, str) and company.strip() else None

        keep_terms: list[str] = []
        for value in [
            *_as_strings(payload.get("keep_terms")),
            *district_terms,
            *rejected_terms,
        ]:
            trimmed = value.strip()[:40]
            if trimmed and trimmed not in keep_terms:
                keep_terms.append(trimmed)

        intent_type = payload.get("intent_type")
        if not isinstance(intent_type, str):
            intent_type = "unknown"
        try:
            confidence = min(1.0, max(0.0, float(payload.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        # A "noise" verdict must not be able to erase the user's own words.
        if intent_type == "noise" and not duties:
            keep_terms = keep_terms or [deterministic_fallback(raw_query)[:40]]
        return StructuredQueryIntent(
            intent_type=intent_type,
            duty_categories=tuple(duties[:5]),
            locations=tuple(cities[:3]),
            employment_types=tuple(dict.fromkeys(employment)),
            shifts=tuple(dict.fromkeys(shifts)),
            salary_type=salary,
            company=company,
            keep_terms=tuple(keep_terms[:6]),
            confidence=confidence,
        )

    def parse_deterministically(self, query: str) -> StructuredQueryIntent:
        """Vocabulary-only reading used on cache miss, timeout, or local mode."""
        text = deterministic_fallback(query)
        duties = self.scan_duties(text)[:5]
        cities = list(dict.fromkeys(self.scan_locations(text)))[:3]
        employment = [value for value in self.employment_types if value in text]
        shifts = [value for value in self.shifts if value in text]
        keep_terms = [
            part
            for part in _SEPARATORS.split(text)
            if 1 < len(part) <= 40
        ][:6]
        return StructuredQueryIntent(
            intent_type="deterministic",
            duty_categories=tuple(duties),
            locations=tuple(cities),
            employment_types=tuple(employment),
            shifts=tuple(shifts),
            keep_terms=tuple(keep_terms),
            confidence=0.3 if duties else 0.0,
        )


def _as_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _join_terms(parts: Iterable[str], fallback: str) -> str:
    ordered: list[str] = []
    for value in parts:
        value = value.strip()
        if value and value not in ordered:
            ordered.append(value)
    # Truncate on a term boundary so a half-word never reaches the matcher.
    trimmed: list[str] = []
    used = 0
    for value in ordered:
        if used + len(value) + 1 > MAX_EXPANSION_CHARS:
            break
        trimmed.append(value)
        used += len(value) + 1
    return " ".join(trimmed) or fallback[:MAX_EXPANSION_CHARS]


def build_expansion(raw_query: str, intent: StructuredQueryIntent) -> str:
    """Build the normalized query the ranker scores against.

    Deliberately conservative: only the user's own words, the resolved company
    or brand, and preserved terms. Duty categories are *not* folded in here.
    `exact_title`, `title_phrase` and `query_unit_overlap` are all computed from
    this string, so injecting five taxonomy labels would dilute a query that
    already matches job titles — measured as a top-3 regression on 青埔/診所.
    """
    cleaned = deterministic_fallback(raw_query)
    haystack = _norm(cleaned)
    # Only the user's own words may change the lexical score. A derived term —
    # the district behind a landmark (青埔 → 中壢區), a duty label — belongs to
    # retrieval, not to scoring: adding 中壢區 here pushed literal 青埔 postings
    # out of the top 3. ASCII canonicalization (nodejs → Node.js) is exempt
    # because it rewrites the user's word rather than adding a new concept.
    own_words = [
        term
        for term in intent.keep_terms
        if _norm(term) in haystack or term.isascii()
    ]
    return _join_terms([cleaned, intent.company or "", *own_words], cleaned)


def build_recall_expansion(raw_query: str, intent: StructuredQueryIntent) -> str:
    """Build the taxonomy expansion used only to rescue a thin result set.

    Attribute and brand queries (現領, 萊爾富) name nothing that occurs in job
    text; their answer is a set of duty categories. Applying that expansion as
    a second pass keeps queries that already work untouched.
    """
    if not intent.duty_categories:
        return ""
    cleaned = deterministic_fallback(raw_query)
    return _join_terms([*intent.duty_categories, cleaned], cleaned)


class _Slot:
    __slots__ = ("done", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: StructuredQueryIntent | None = None


class BatchCoalescer:
    """Group concurrent queries into one Bedrock request.

    Bedrock enforces a per-minute request quota, so the cost of a normalization
    is one request regardless of how many queries it carries. A window closes as
    soon as `max_batch` queries are waiting, or after `max_wait` seconds,
    whichever comes first. Callers that hit their own deadline stop waiting and
    degrade; the batch still completes and populates the cache.
    """

    def __init__(
        self,
        worker: Callable[[Sequence[str]], dict[str, StructuredQueryIntent]],
        *,
        max_batch: int = 10,
        max_wait: float = 1.0,
        idle_shutdown: float = 30.0,
    ) -> None:
        self._worker = worker
        self.max_batch = max(1, int(max_batch))
        self.max_wait = max(0.0, float(max_wait))
        self._idle_shutdown = max(1.0, float(idle_shutdown))
        self._condition = threading.Condition()
        self._queue: list[str] = []
        self._pending: dict[str, _Slot] = {}
        self._thread: threading.Thread | None = None
        self.batches_dispatched = 0
        self.queries_dispatched = 0

    def submit(
        self, query: str, deadline_seconds: float
    ) -> tuple[StructuredQueryIntent | None, bool]:
        """Return (intent, completed). `completed` is False on caller timeout."""
        with self._condition:
            slot = self._pending.get(query)
            if slot is None:
                slot = _Slot()
                self._pending[query] = slot
                self._queue.append(query)
            self._ensure_worker_locked()
            self._condition.notify_all()
        if not slot.done.wait(max(0.0, deadline_seconds)):
            return None, False
        return slot.result, True

    def _ensure_worker_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="bedrock-query-batcher", daemon=True
        )
        self._thread.start()

    def _collect(self) -> tuple[list[str], list[_Slot]] | None:
        with self._condition:
            while not self._queue:
                if not self._condition.wait(timeout=self._idle_shutdown):
                    if not self._queue:
                        self._thread = None
                        return None
            deadline = time.monotonic() + self.max_wait
            while len(self._queue) < self.max_batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            batch = self._queue[: self.max_batch]
            del self._queue[: self.max_batch]
            return batch, [self._pending[query] for query in batch]

    def _run(self) -> None:
        while True:
            collected = self._collect()
            if collected is None:
                return
            batch, slots = collected
            try:
                results = self._worker(batch)
            except Exception as exc:
                LOGGER.warning(
                    "Batched query normalization failed for %d queries: %s",
                    len(batch),
                    type(exc).__name__,
                )
                results = {}
            with self._condition:
                self.batches_dispatched += 1
                self.queries_dispatched += len(batch)
                for query, slot in zip(batch, slots):
                    slot.result = results.get(query)
                    self._pending.pop(query, None)
                    slot.done.set()


class BedrockQueryNormalizer:
    """Normalize online queries with batched Bedrock structured outputs.

    The boto3 import and client construction are lazy so the dependency-free
    local demo continues to work when Bedrock is not configured.
    """

    def __init__(
        self,
        model_id: str | None,
        *,
        region: str | None = None,
        client: Any = None,
        max_tokens: int = 4000,
        vocabulary: QueryIntentVocabulary | None = None,
        prompt_path: str | Path | None = None,
        max_batch: int = 10,
        max_wait_seconds: float = 0.05,
        deadline_seconds: float = 6.0,
        cache_size: int = 4096,
    ) -> None:
        self.model_id = (model_id or "").strip() or None
        self.region = region or os.getenv("AWS_REGION", "us-east-1")
        self.max_tokens = max(256, min(int(max_tokens), 8192))
        self.deadline_seconds = max(0.1, float(deadline_seconds))
        self.vocabulary = vocabulary
        self._prompt_path = Path(prompt_path or DEFAULT_PROMPT_PATH)
        self._client = client
        self._client_lock = threading.Lock()
        self._system_prompt: str | None = None
        self._cache: OrderedDict[str, StructuredQueryIntent] = OrderedDict()
        self._preloaded: dict[str, StructuredQueryIntent] = {}
        self._cache_size = max(0, int(cache_size))
        self._cache_lock = threading.Lock()
        self.cache_hits = 0
        self.cache_misses = 0
        self.prewarmed = 0
        self.prewarm_requests = 0
        self._coalescer = BatchCoalescer(
            self._normalize_batch,
            max_batch=max_batch,
            max_wait=max_wait_seconds,
        )

    @classmethod
    def from_environment(cls) -> BedrockQueryNormalizer:
        normalizer = cls(
            os.getenv("BEDROCK_QUERY_MODEL_ID"),
            max_tokens=int(os.getenv("BEDROCK_QUERY_MAX_TOKENS", "4000")),
            max_batch=int(os.getenv("BEDROCK_QUERY_MAX_BATCH", "10")),
            max_wait_seconds=float(os.getenv("BEDROCK_QUERY_MAX_WAIT_SECONDS", "0.05")),
            deadline_seconds=float(os.getenv("BEDROCK_QUERY_DEADLINE_SECONDS", "6.0")),
            cache_size=int(os.getenv("BEDROCK_QUERY_CACHE_SIZE", "4096")),
            vocabulary=QueryIntentVocabulary.load(),
        )
        loaded = normalizer.load_intents()
        if loaded:
            LOGGER.info("Loaded %d precomputed query intents", loaded)
        return normalizer

    @property
    def enabled(self) -> bool:
        return self.model_id is not None and self.vocabulary is not None

    @property
    def batch_stats(self) -> dict[str, int]:
        return {
            "batches_dispatched": self._coalescer.batches_dispatched,
            "queries_dispatched": self._coalescer.queries_dispatched,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "prewarmed": self.prewarmed,
            "cached_queries": len(self._cache),
            "precomputed_intents": len(self._preloaded),
            "prewarm_requests": self.prewarm_requests,
        }

    def verify_connection(self) -> QueryNormalization:
        """Make one live inference to prove Bedrock normalization is usable.

        This deliberately bypasses shipped and in-memory intent caches. It is
        intended for strict local-demo startup checks, where merely having a
        model ID configured is not enough evidence that credentials, region,
        model access, and the structured-output contract all work together.
        """
        if not self.enabled:
            raise RuntimeError(
                "BEDROCK_QUERY_MODEL_ID and the query-intent vocabulary are required"
            )
        probe = "SkillWeave Bedrock query normalization connectivity check"
        intent = self._normalize_batch([probe]).get(probe)
        if intent is None:
            raise RuntimeError("Amazon Bedrock returned no result for the startup probe")
        return self._result(probe, intent, "amazon_bedrock")

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                import boto3
                from botocore.config import Config

                self._client = boto3.client(
                    "bedrock-runtime",
                    region_name=self.region,
                    # A batch carries up to `max_batch` queries and generates
                    # proportionally more output tokens, so the read timeout has
                    # to cover generation rather than a single short answer.
                    config=Config(
                        connect_timeout=2.0,
                        read_timeout=float(
                            os.getenv("BEDROCK_QUERY_READ_TIMEOUT_SECONDS", "60")
                        ),
                        retries={"max_attempts": 2, "mode": "adaptive"},
                    ),
                )
        return self._client

    def _get_system_prompt(self) -> str:
        if self._system_prompt is None:
            assert self.vocabulary is not None
            template = self._prompt_path.read_text(encoding="utf-8")
            self._system_prompt = (
                template.replace("{DUTY_BLOCK}", self.vocabulary.prompt_block)
                .replace("{EMPLOYMENT_TYPES}", " / ".join(self.vocabulary.employment_types))
                .replace("{SHIFTS}", " / ".join(self.vocabulary.shifts))
                .replace("{SALARY_TYPES}", " / ".join(self.vocabulary.salary_types))
            )
        return self._system_prompt

    def load_intents(self, path: str | Path | None = None) -> int:
        """Seed intents precomputed offline by scripts/build_query_intents.py.

        A Lambda invocation serves one request, so neither batching nor
        background pre-warming can help there: there is no sibling query to
        coalesce with and the container is frozen between calls. Shipping the
        head of the query distribution as a lookup is what makes structured
        normalization actually reachable on the serverless path.
        """
        paths = (
            [Path(path)]
            if path is not None
            else [
                Path(os.getenv("QUERY_INTENTS_PATH", DEFAULT_INTENTS_PATH)),
                Path(
                    os.getenv(
                        "QUERY_RELEASE_INTENTS_PATH",
                        DEFAULT_RELEASE_INTENTS_PATH,
                    )
                ),
            ]
        )
        loaded: dict[str, StructuredQueryIntent] = {}
        for intents_path in dict.fromkeys(paths):
            if not intents_path.is_file():
                continue
            try:
                payload = json.loads(intents_path.read_text(encoding="utf-8"))
                entries = payload["intents"]
                loaded.update(
                    {
                        deterministic_fallback(query): StructuredQueryIntent.from_dict(
                            value
                        )
                        for query, value in entries.items()
                        if isinstance(value, dict)
                    }
                )
            except (ValueError, KeyError, OSError) as exc:
                LOGGER.warning(
                    "Ignoring unreadable precomputed intents at %s: %s",
                    intents_path,
                    type(exc).__name__,
                )
        # Held outside the LRU: runtime misses must never evict the shipped head
        # of the distribution.
        self._preloaded = loaded
        return len(loaded)

    def _cache_get(self, query: str) -> StructuredQueryIntent | None:
        preloaded = self._preloaded.get(query)
        if preloaded is not None:
            return preloaded
        if self._cache_size <= 0:
            return None
        with self._cache_lock:
            intent = self._cache.get(query)
            if intent is not None:
                self._cache.move_to_end(query)
            return intent

    def _cache_put(self, query: str, intent: StructuredQueryIntent) -> None:
        if self._cache_size <= 0:
            return
        with self._cache_lock:
            self._cache[query] = intent
            self._cache.move_to_end(query)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def _normalize_batch(
        self, queries: Sequence[str]
    ) -> dict[str, StructuredQueryIntent]:
        assert self.vocabulary is not None
        payload = [{"id": index, "q": query} for index, query in enumerate(queries)]
        response = self._get_client().converse(
            modelId=self.model_id,
            system=[{"text": self._get_system_prompt()}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": json.dumps(payload, ensure_ascii=False)}],
                }
            ],
            outputConfig={
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            "schema": json.dumps(
                                QUERY_OUTPUT_SCHEMA,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            "name": "job_search_query_intents",
                            "description": "Structured job-search query intents",
                        }
                    },
                }
            },
            inferenceConfig={"temperature": 0, "maxTokens": self.max_tokens},
        )
        content = response["output"]["message"]["content"]
        text = next(block["text"] for block in content if "text" in block)
        decoded = json.loads(text)
        results = decoded.get("results")
        if not isinstance(results, list):
            raise ValueError("Bedrock returned no query-intent results")
        resolved: dict[str, StructuredQueryIntent] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("id", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(queries):
                continue
            query = queries[index]
            intent = self.vocabulary.validate(item, query)
            resolved[query] = intent
            self._cache_put(query, intent)
        return resolved

    def _normalize_legacy(self, query: str) -> QueryNormalization:
        """Run the original one-query string normalization contract.

        Keeping this path makes the class safe for existing direct callers;
        Lambda construction goes through ``from_environment`` and therefore
        always uses the validated structured-intent vocabulary.
        """
        try:
            response = self._get_client().converse(
                modelId=self.model_id,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": query}],
                    }
                ],
                outputConfig={
                    "textFormat": {
                        "type": "json_schema",
                        "structure": {
                            "jsonSchema": {
                                "schema": json.dumps(
                                    LEGACY_QUERY_OUTPUT_SCHEMA,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                "name": "normalized_job_search_query",
                                "description": "Normalized job-search query",
                            }
                        },
                    }
                },
                inferenceConfig={"temperature": 0, "maxTokens": self.max_tokens},
            )
            content = response["output"]["message"]["content"]
            text = next(block["text"] for block in content if "text" in block)
            normalized = deterministic_fallback(
                str(json.loads(text)["normalized_query"])
            )
            if not normalized:
                raise ValueError("Bedrock returned an empty normalized query")
            return QueryNormalization(normalized, "amazon_bedrock", self.model_id)
        except Exception as exc:
            # Never log the query itself: it may contain personal information.
            LOGGER.warning(
                "Query normalization failed; using deterministic fallback: %s",
                type(exc).__name__,
            )
            return QueryNormalization(
                deterministic_fallback(query),
                "deterministic_fallback",
                self.model_id,
                degraded=True,
            )

    def _result(
        self,
        raw_query: str,
        intent: StructuredQueryIntent,
        source: str,
        *,
        degraded: bool = False,
    ) -> QueryNormalization:
        expansion = build_recall_expansion(raw_query, intent)
        intent = replace(intent, recall_expansion=expansion)
        return QueryNormalization(
            build_expansion(raw_query, intent),
            source,
            self.model_id,
            degraded=degraded,
            intent=intent,
            expansion=expansion,
        )

    def normalize(self, query: str) -> QueryNormalization:
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query must be at most {MAX_QUERY_CHARS} characters")
        cleaned = deterministic_fallback(query)
        if self.vocabulary is None:
            if self.model_id is None:
                return QueryNormalization(cleaned, "deterministic", None)
            return self._normalize_legacy(cleaned)
        # Checked before the enabled/disabled split: a precomputed intent is
        # most valuable exactly when Bedrock is unreachable or unconfigured.
        cached = self._cache_get(cleaned)
        if cached is not None:
            self.cache_hits += 1
            return self._result(query, cached, "amazon_bedrock_cached")
        if not self.enabled:
            return self._result(
                query, self.vocabulary.parse_deterministically(query), "deterministic"
            )
        self.cache_misses += 1
        intent, completed = self._coalescer.submit(cleaned, self.deadline_seconds)
        if completed and intent is not None:
            return self._result(query, intent, "amazon_bedrock")
        if not completed:
            # Do not log query text: search terms may contain personal data.
            LOGGER.info(
                "Batched query normalization exceeded the %.2fs request deadline; "
                "serving the deterministic reading while the batch completes",
                self.deadline_seconds,
            )
        else:
            LOGGER.warning(
                "Batched query normalization returned no intent; using deterministic parse"
            )
        fallback_intent = self.vocabulary.parse_deterministically(query)
        return self._result(
            query, fallback_intent, "deterministic_fallback", degraded=True
        )

    def prewarm(
        self,
        queries: Iterable[str],
        *,
        requests_per_minute: float = 45.0,
        max_workers: int = 6,
        background: bool = True,
    ) -> threading.Thread | None:
        """Populate the intent cache for the head of the query distribution.

        The coalescer serialises batches, which is right online but far too slow
        to warm thousands of queries. This path fans batches out instead, capped
        below the Bedrock requests-per-minute quota (50 RPM for Claude Haiku 4.5)
        so warming never starves live traffic.
        """
        pending = [
            query
            for query in dict.fromkeys(
                deterministic_fallback(value) for value in queries
            )
            if query and len(query) <= MAX_QUERY_CHARS and self._cache_get(query) is None
        ]
        if not self.enabled or not pending:
            return None
        batches = [
            pending[index : index + self._coalescer.max_batch]
            for index in range(0, len(pending), self._coalescer.max_batch)
        ]

        def run() -> None:
            import concurrent.futures

            interval = 60.0 / max(1.0, float(requests_per_minute))
            gate = threading.Lock()
            next_start = [time.monotonic()]
            started = time.monotonic()

            def dispatch(batch: Sequence[str]) -> int:
                with gate:
                    delay = next_start[0] - time.monotonic()
                    next_start[0] = max(next_start[0], time.monotonic()) + interval
                if delay > 0:
                    time.sleep(delay)
                try:
                    resolved = len(self._normalize_batch(batch))
                    with gate:
                        self.prewarm_requests += 1
                    return resolved
                except Exception as exc:
                    LOGGER.warning(
                        "Prewarm batch of %d failed: %s", len(batch), type(exc).__name__
                    )
                    return 0

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, max_workers)
            ) as pool:
                warmed = sum(pool.map(dispatch, batches))
            self.prewarmed = warmed
            LOGGER.info(
                "Prewarmed %d/%d queries in %d requests over %.1fs",
                warmed,
                len(pending),
                len(batches),
                time.monotonic() - started,
            )

        if not background:
            run()
            return None
        thread = threading.Thread(target=run, name="query-intent-prewarm", daemon=True)
        thread.start()
        return thread

    def prewarm_from_config(self) -> threading.Thread | None:
        """Warm from config/top-queries.json when the deployment opts in."""
        limit = int(os.getenv("QUERY_PREWARM_LIMIT", "0"))
        if limit <= 0 or not self.enabled:
            return None
        path = Path(os.getenv("QUERY_PREWARM_PATH", DEFAULT_TOP_QUERIES_PATH))
        if not path.is_file():
            LOGGER.warning(
                "QUERY_PREWARM_LIMIT is set but %s is missing; "
                "run scripts/build_top_queries.py",
                path,
            )
            return None
        queries = json.loads(path.read_text(encoding="utf-8")).get("queries", [])
        return self.prewarm(
            queries[:limit],
            requests_per_minute=float(os.getenv("QUERY_PREWARM_RPM", "45")),
        )

    def normalize_many(self, queries: Iterable[str]) -> list[QueryNormalization]:
        """Normalize a known set of queries through the same batching path."""
        threads: list[threading.Thread] = []
        ordered = list(queries)
        results: list[QueryNormalization | None] = [None] * len(ordered)

        def run(position: int, value: str) -> None:
            results[position] = self.normalize(value)

        for position, value in enumerate(ordered):
            thread = threading.Thread(target=run, args=(position, value), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        return [
            result
            if result is not None
            else QueryNormalization(deterministic_fallback(value), "deterministic", None)
            for result, value in zip(results, ordered)
        ]
