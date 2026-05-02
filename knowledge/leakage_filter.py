"""
Minimal leakage filter for on-demand evidence search.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


YIELD_PATTERN = re.compile(
    r"""
    (?:
        (?:yield|conversion|selectivity|ee|er|dr)
        \s*[:=]?\s*
        [\d]+(?:\.[\d]+)?\s*%
    )
    |
    (?:
        [\d]+(?:\.[\d]+)?\s*%
        \s*(?:yield|conversion|selectivity|ee|er|dr)
    )
    |
    (?:
        (?:yield|conversion|selectivity)\s+
        (?:of\s+|was\s+|=\s*)?
        [\d]+(?:\.[\d]+)?\s*%
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
LOOSE_YIELD_PATTERN = re.compile(
    r"(?:gave|obtained|achieved|produced|resulted\s+in|afforded)\s+[\d]+(?:\.[\d]+)?\s*%",
    re.IGNORECASE,
)
SMILES_PATTERN = re.compile(r"(?<![A-Za-z])([A-Z][A-Za-z0-9@\+\-\[\]\(\)=#/\\\.]{4,})(?![A-Za-z])")
AROMATIC_SMILES_PATTERN = re.compile(r"(?<![A-Za-z])([cnops]\d[A-Za-z0-9@\+\-\[\]\(\)=#/\\\.]{3,})(?![A-Za-z])")
_RDKIT_WARNING_EMITTED = False


@dataclass(frozen=True)
class SanitizeResult:
    text: str
    status: str
    reasons: list[str]


class LeakageFilter:
    """Block target-identity leakage and redact explicit outcome numbers."""

    def __init__(self, problem_spec: dict[str, Any]) -> None:
        self.problem_spec = dict(problem_spec or {})
        self.reaction_type = self._extract_reaction_type(self.problem_spec)
        self.substrate_names = self._extract_substrate_names(self.problem_spec)
        self.target_smiles = set(
            self._canonicalize_smiles_list(
                self._extract_substrate_smiles(self.problem_spec) + self._extract_product_smiles(self.problem_spec)
            )
        )

    def sanitize(self, text: str) -> SanitizeResult:
        content = str(text or "").strip()
        if not content:
            return SanitizeResult(text="", status="blocked", reasons=["empty text"])

        reasons: list[str] = []
        if self.target_smiles:
            smiles_in_text = set(self._canonicalize_smiles_list(_extract_smiles_from_text(content)))
            overlap = self.target_smiles & smiles_in_text
            if overlap:
                return SanitizeResult(text="", status="blocked", reasons=[f"target SMILES overlap: {sorted(overlap)}"])

        lowered = content.lower()
        matched_names = [name for name in self.substrate_names if len(name) >= 4 and name in lowered]
        if matched_names and self._mentions_reaction_type(content):
            return SanitizeResult(text="", status="blocked", reasons=[f"substrate name with reaction family: {matched_names}"])

        sanitized = _redact_outcomes(content)
        if sanitized != content:
            meaningful = re.sub(r"\s+", " ", sanitized).strip()
            if len(meaningful) < 40:
                return SanitizeResult(text="", status="blocked", reasons=["too little text after outcome redaction"])
            reasons.append("outcome numbers redacted")
            return SanitizeResult(text=meaningful, status="redacted", reasons=reasons)

        return SanitizeResult(text=content, status="pass", reasons=[])

    def _extract_reaction_type(self, problem_spec: dict[str, Any]) -> str:
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        return str(reaction.get("family") or problem_spec.get("reaction_type") or "").strip().upper()

    def _extract_substrate_names(self, problem_spec: dict[str, Any]) -> list[str]:
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        names: list[str] = []
        for item in reaction.get("substrates", []) if isinstance(reaction.get("substrates"), list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip().lower()
            if name and name not in names:
                names.append(name)
        return names

    def _extract_substrate_smiles(self, problem_spec: dict[str, Any]) -> list[str]:
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        smiles: list[str] = []
        for item in reaction.get("substrates", []) if isinstance(reaction.get("substrates"), list) else []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("smiles", "")).strip()
            if value:
                smiles.append(value)
        reactants, _, _ = _split_reaction_smiles(str(reaction.get("reaction_smiles", "")).strip())
        smiles.extend(reactants)
        return _dedupe_preserve(smiles)

    def _extract_product_smiles(self, problem_spec: dict[str, Any]) -> list[str]:
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        smiles: list[str] = []
        product_smiles = reaction.get("product_smiles", "")
        if isinstance(product_smiles, list):
            smiles.extend(str(item).strip() for item in product_smiles if str(item).strip())
        elif str(product_smiles or "").strip():
            smiles.append(str(product_smiles).strip())
        _, _, products = _split_reaction_smiles(str(reaction.get("reaction_smiles", "")).strip())
        smiles.extend(products)
        return _dedupe_preserve(smiles)

    def _canonicalize_smiles_list(self, smiles_list: list[str]) -> list[str]:
        global _RDKIT_WARNING_EMITTED
        canonical: list[str] = []
        try:
            from rdkit import Chem

            for smiles in smiles_list:
                value = str(smiles or "").strip()
                if not value:
                    continue
                molecule = Chem.MolFromSmiles(value)
                if molecule is not None:
                    canonical.append(Chem.MolToSmiles(molecule))
        except ImportError:
            if not _RDKIT_WARNING_EMITTED:
                logger.warning("RDKit unavailable; leakage filter is using exact string SMILES matching.")
                _RDKIT_WARNING_EMITTED = True
            canonical = [str(smiles).strip() for smiles in smiles_list if str(smiles).strip()]
        return _dedupe_preserve(canonical)

    def _mentions_reaction_type(self, text: str) -> bool:
        lowered = text.lower()
        keyword_map = {
            "DAR": ["direct arylation", "c-h arylation", "c-h activation"],
            "BH": ["buchwald-hartwig", "buchwald hartwig", "amination", "c-n coupling"],
            "SUZUKI": ["suzuki", "suzuki-miyaura", "boronic acid coupling"],
            "OCM": ["oxidative coupling of methane", "ocm"],
            "SCR": ["selective catalytic reduction", "scr", "nh3-scr", "ammonia scr"],
        }
        keywords = keyword_map.get(self.reaction_type, [self.reaction_type.lower()] if self.reaction_type else [])
        return any(keyword and keyword in lowered for keyword in keywords)


def _redact_outcomes(text: str) -> str:
    redacted = YIELD_PATTERN.sub("[outcome redacted]", text)
    redacted = LOOSE_YIELD_PATTERN.sub("[outcome redacted]", redacted)
    return redacted


def _extract_smiles_from_text(text: str) -> list[str]:
    matches = SMILES_PATTERN.findall(str(text or ""))
    matches.extend(AROMATIC_SMILES_PATTERN.findall(str(text or "")))
    reactants, agents, products = _split_reaction_smiles(str(text or ""))
    matches.extend(reactants)
    matches.extend(agents)
    matches.extend(products)
    return _dedupe_preserve(matches)


def _split_reaction_smiles(reaction_smiles: str) -> tuple[list[str], list[str], list[str]]:
    if ">>" not in reaction_smiles:
        return [], [], []
    left, right = reaction_smiles.split(">>", 1)
    if ">" in left:
        reactants_text, agents_text = left.split(">", 1)
    else:
        reactants_text, agents_text = left, ""
    return _split_smiles_side(reactants_text), _split_smiles_side(agents_text), _split_smiles_side(right)


def _split_smiles_side(text: str) -> list[str]:
    return [item.strip() for item in str(text or "").split(".") if item.strip()]


def _dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


__all__ = ["LeakageFilter", "LOOSE_YIELD_PATTERN", "SanitizeResult", "YIELD_PATTERN"]
