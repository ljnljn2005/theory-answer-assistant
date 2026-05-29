from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass


OPTION_SPLIT_RE = re.compile(r"[\s,，、/|]+")
ANSWER_LINE_RE = re.compile(
    r"(?:答案|选择|选项|正确答案|answer)\s*[:：]?\s*([A-Z](?:\s*[,，、/|]\s*[A-Z])*)",
    re.IGNORECASE,
)
OPTION_LINE_RE = re.compile(r"^\s*[A-H]\s*[\.\):：、]\s*")
PICK_LINE_RE = re.compile(
    r"(?:我选|选|应选|答案选|选的是|最终选|故选|因此选)\s*([A-H](?:\s*[,，、/|]\s*[A-H])*)",
    re.IGNORECASE,
)
PURE_OPTION_RE = re.compile(r"^\s*([A-H](?:\s*[,，、/|]\s*[A-H])*)\s*$", re.IGNORECASE)


@dataclass(slots=True)
class ProviderAnswer:
    provider_key: str
    provider_name: str
    raw_answer: str = ""
    parsed_options: tuple[str, ...] = ()
    error: str | None = None


@dataclass(slots=True)
class ConsensusResult:
    recommended_options: tuple[str, ...]
    exact_match_count: int
    parsed_provider_count: int
    option_support: dict[str, int]
    exact_support: dict[str, int]


def extract_options(text: str) -> tuple[str, ...]:
    if not text:
        return ()

    normalized = (
        text.upper()
        .replace("（", "(")
        .replace("）", ")")
        .replace("；", ";")
        .replace("：", ":")
    )

    matches = list(ANSWER_LINE_RE.finditer(normalized))
    if matches:
        tokens = OPTION_SPLIT_RE.split(matches[-1].group(1).strip())
        return _normalize_tokens(tokens)

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    for line in reversed(lines[-12:]):
        if OPTION_LINE_RE.match(line):
            continue

        match = PICK_LINE_RE.search(line)
        if match:
            tokens = OPTION_SPLIT_RE.split(match.group(1).strip())
            return _normalize_tokens(tokens)

        match = PURE_OPTION_RE.match(line)
        if match:
            tokens = OPTION_SPLIT_RE.split(match.group(1).strip())
            normalized_tokens = _normalize_tokens(tokens)
            if normalized_tokens:
                return normalized_tokens

    return ()


def compute_consensus(items: list[ProviderAnswer]) -> ConsensusResult:
    parsed = [item.parsed_options for item in items if item.parsed_options]
    if not parsed:
        return ConsensusResult(
            recommended_options=(),
            exact_match_count=0,
            parsed_provider_count=0,
            option_support={},
            exact_support={},
        )

    exact_counter = Counter(",".join(options) for options in parsed)
    option_counter: Counter[str] = Counter()
    for options in parsed:
        option_counter.update(options)

    best_options_text, best_count = max(
        exact_counter.items(),
        key=lambda entry: (entry[1], _option_overlap_score(entry[0], option_counter), -len(entry[0])),
    )

    return ConsensusResult(
        recommended_options=tuple(best_options_text.split(",")),
        exact_match_count=best_count,
        parsed_provider_count=len(parsed),
        option_support=dict(sorted(option_counter.items(), key=lambda entry: (-entry[1], entry[0]))),
        exact_support=dict(sorted(exact_counter.items(), key=lambda entry: (-entry[1], entry[0]))),
    )


def format_options(options: tuple[str, ...]) -> str:
    return ",".join(options) if options else "-"


def _normalize_tokens(tokens: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        value = token.strip().upper()
        if not value or not re.fullmatch(r"[A-H]", value):
            continue
        if value not in seen:
            seen.add(value)
            cleaned.append(value)
    return tuple(sorted(cleaned))


def _option_overlap_score(option_text: str, option_counter: Counter[str]) -> int:
    parts = [item for item in option_text.split(",") if item]
    return sum(option_counter.get(part, 0) for part in parts)
