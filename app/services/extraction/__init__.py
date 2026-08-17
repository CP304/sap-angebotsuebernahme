"""Regelbasierte Angebotserkennung (Kopfdaten, Tabellen, Freitext, Lernen)."""

from __future__ import annotations

from .freetext_extractor import FreetextExtractor, FreetextResult
from .header_rules import (
    HEADER_RULES,
    HeaderMatch,
    HeaderRule,
    apply_header_matches,
    extract_header_fields,
    find_incoterm,
    label_value_pairs,
    label_value_text,
)
from .material_roles import (
    CUSTOMER_MARKERS,
    SUPPLIER_MARKERS,
    compile_own_pattern,
    find_labelled_material_matches,
    find_labelled_material_numbers,
    header_role,
    matches_own_pattern,
    own_ratio,
    resolve_position_roles,
)
from .learning import LearningConfig, LearningReport, describe_learning, forget_rule, \
    learn_from_corrections
from .profiles import (
    InMemoryProfileStore,
    JsonProfileStore,
    VendorProfile,
    VendorProfileStore,
    apply_profile,
    fingerprint,
    match_profile,
    new_profile,
)
from .table_extractor import (
    ColumnAssignment,
    ExtractionResult,
    TableAnalysis,
    TableExtractor,
    find_price_tiers,
    is_summary_row,
    parse_number,
)

__all__ = [
    "CUSTOMER_MARKERS",
    "SUPPLIER_MARKERS",
    "compile_own_pattern",
    "find_labelled_material_matches",
    "find_labelled_material_numbers",
    "header_role",
    "label_value_pairs",
    "label_value_text",
    "matches_own_pattern",
    "own_ratio",
    "resolve_position_roles",
    "ColumnAssignment",
    "ExtractionResult",
    "FreetextExtractor",
    "FreetextResult",
    "HEADER_RULES",
    "HeaderMatch",
    "HeaderRule",
    "InMemoryProfileStore",
    "JsonProfileStore",
    "LearningConfig",
    "LearningReport",
    "TableAnalysis",
    "TableExtractor",
    "VendorProfile",
    "VendorProfileStore",
    "apply_header_matches",
    "apply_profile",
    "describe_learning",
    "extract_header_fields",
    "find_incoterm",
    "find_price_tiers",
    "fingerprint",
    "forget_rule",
    "is_summary_row",
    "learn_from_corrections",
    "match_profile",
    "new_profile",
    "parse_number",
]
