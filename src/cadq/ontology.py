"""Layer-name + hatch-pattern ontology mapper.

Loads the YAML rule pack shipped in ``rules/layers.default.yaml`` (or a user
override) and exposes a small classifier API used by the ingest pipeline.

The classifier is deliberately deterministic: it returns the first matching
rule plus a confidence score, and writes every attempted match into the
``ontology_log`` table so ``cadq explain`` can tell the user *why*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml


DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent.parent / "rules" / "layers.default.yaml"


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def _tokenize(name: str) -> str:
    """Insert ``_`` at case / letter-digit transitions.

    Many real-world surveys use PascalCase layer names like ``TreeSpread``,
    ``LevelsSpot`` or ``AssumedBoundary`` rather than the dash-separated
    convention assumed by the default rule pack.  Inserting ``_`` at the
    transitions lets the same rules match both styles without doubling the
    rule count.

    Examples:
        TreeSpread       -> Tree_Spread
        LevelsSpot       -> Levels_Spot
        AssumedBoundary  -> Assumed_Boundary
        Building3        -> Building_3
        L-LAWN-01        -> L-LAWN-01    (no transitions; unchanged)
    """
    if not name:
        return ""
    return _CAMEL_BOUNDARY.sub("_", name)


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    raw_pattern: str
    ontology: str
    confidence: float


@dataclass(frozen=True)
class Match:
    ontology: str
    confidence: float
    rule: str  # raw regex string for traceability


class Ontology:
    def __init__(
        self,
        rules: list[Rule],
        hatch_hints: dict[str, str],
        block_rules: list[Rule] | None = None,
    ) -> None:
        self.rules = rules
        self.hatch_hints = {k.upper(): v for k, v in hatch_hints.items()}
        self.block_rules = block_rules or []

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Ontology":
        p = Path(path) if path else DEFAULT_RULES_PATH
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        rules: list[Rule] = []
        for r in data.get("rules", []):
            rules.append(
                Rule(
                    pattern=re.compile(r["pattern"], re.IGNORECASE),
                    raw_pattern=r["pattern"],
                    ontology=r["ontology"],
                    confidence=float(r.get("confidence", 0.5)),
                )
            )
        block_rules: list[Rule] = []
        for r in data.get("block_rules", []) or []:
            block_rules.append(
                Rule(
                    pattern=re.compile(r["pattern"], re.IGNORECASE),
                    raw_pattern=r["pattern"],
                    ontology=r["ontology"],
                    confidence=float(r.get("confidence", 0.5)),
                )
            )
        hints = data.get("hatch_hints", {}) or {}
        return cls(rules=rules, hatch_hints=hints, block_rules=block_rules)

    def classify_layer(self, layer_name: str) -> Match | None:
        """Return the best-matching ontology entry for a layer name."""
        target = _tokenize(layer_name)
        for rule in self.rules:
            if rule.pattern.search(target):
                return Match(rule.ontology, rule.confidence, rule.raw_pattern)
        return None

    def all_layer_matches(self, layer_name: str) -> Iterable[Match]:
        """All matching rules — used by `cadq explain`."""
        target = _tokenize(layer_name)
        for rule in self.rules:
            if rule.pattern.search(target):
                yield Match(rule.ontology, rule.confidence, rule.raw_pattern)

    def hatch_hint(self, pattern_name: str | None) -> str | None:
        if not pattern_name:
            return None
        return self.hatch_hints.get(pattern_name.upper())

    def classify_block(self, block_name: str) -> Match | None:
        """Return the best-matching ontology entry for a block name."""
        target = _tokenize(block_name)
        for rule in self.block_rules:
            if rule.pattern.search(target):
                return Match(rule.ontology, rule.confidence, rule.raw_pattern)
        return None
