"""
Data infrastructure models for the auto_cut_bot pipeline.

Defines core dataclasses used across the data ingestion, validation,
and transformation layers of the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union


class FieldClassification(StrEnum):
    """Taxonomy for classifying a data field's semantic role.

    Used to drive downstream validation rules, indexing strategies,
    and transformation behaviour.
    """

    DIMENSION = "dimension"  # categorical / grouping column (e.g. country, product_id)
    METRIC = "metric"  # numeric measure (e.g. revenue, click_count)
    TIMESTAMP = "timestamp"  # time-axis column
    IDENTIFIER = "identifier"  # primary / foreign key
    ATTRIBUTE = "attribute"  # free-form descriptive text
    DERIVED = "derived"  # computed from other fields (e.g. ctr, roi)
    FLAG = "flag"  # boolean indicator
    UNKNOWN = "unknown"  # classification not yet determined


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """Immutable record of where a piece of data originated and how it was
    obtained.

    Frozen so that provenance cannot be tampered with after creation,
    guaranteeing auditability.
    """

    source_id: str
    """Globally unique identifier for the upstream source (e.g. UUID, URI)."""

    source_name: str
    """Human-readable name of the source system or dataset."""

    extraction_timestamp: datetime
    """UTC timestamp when the data was extracted from the source."""

    ingestion_timestamp: datetime = field(default_factory=datetime.utcnow)
    """UTC timestamp when the data entered the pipeline."""

    confidence: float = 1.0
    """Signal confidence in the source [0.0, 1.0].  Defaults to 1.0 for
    trusted sources; lower for heuristics or ML-extracted data."""

    tool_name: str = ""
    """Name of the extractor / connector that produced the data
    (e.g. 'mysql-cdc', 's3-parquet-reader')."""

    tool_version: str = ""
    """Version of the tool at extraction time."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Arbitrary key-value pairs for source-specific context
    (e.g. query hash, partition path, snapshot time)."""

    def __post_init__(self) -> None:
        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )


# ---------------------------------------------------------------------------
# SourceConflict
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceConflict:
    """Describes a disagreement between two or more sources for the same
    logical field.

    Ingested by the reconciliation layer and surfaced to operators or
    automated resolution rules.
    """

    field_name: str
    """The logical field name that is in conflict."""

    conflicting_sources: Tuple[Provenance, ...]
    """The provenance records for each conflicting source.  Order is
    preserved from the resolver's input."""

    conflict_type: str
    """Category of conflict.  Examples: 'type_mismatch', 'value_divergence',
    'schema_mismatch', 'nullability_mismatch'."""

    description: str = ""
    """Human-readable summary of the conflict (e.g. 'source A types field as
    INT, source B types it as STRING')."""

    resolution: Optional[str] = None
    """How the conflict was resolved, if resolved.  Examples: 'keep_max',
    'keep_source_A', 'operator_override', 'union'."""

    resolved_by: Optional[str] = None
    """Who or what resolved the conflict (user id, rule name, pipeline step)."""

    resolved_at: Optional[datetime] = None
    """UTC timestamp when the conflict was resolved."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Additional context (e.g. sample values from each source, diff output)."""


# ---------------------------------------------------------------------------
# FieldSchema
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FieldSchema:
    """Complete schema definition for a single field (column) within a
    dataset.

    Encompasses type information, semantic classification, constraints,
    provenance tracking, and any unresolved conflicts.
    """

    name: str
    """Field / column name.  Should be snake_case per project convention."""

    data_type: str
    """Logical data type.  Examples: 'int64', 'float64', 'string', 'bool',
    'timestamp', 'date', 'decimal(18,4)', 'array<string>', 'struct<...>'."""

    nullable: bool = True
    """Whether the field accepts null values."""

    classification: FieldClassification = FieldClassification.UNKNOWN
    """Semantic role of the field."""

    description: str = ""
    """Human-readable description (ideally populated from a data catalog)."""

    constraints: Dict[str, Any] = field(default_factory=dict)
    """Validation constraints.  Examples:
        {'min': 0, 'max': 100}
        {'regex': '^[A-Z]{2}$'}
        {'allowed_values': ['US', 'CN', 'JP']}
        {'json_schema': {...}}
    """

    default_value: Any = None
    """Default value for this field when absent from a record."""

    tags: List[str] = field(default_factory=list)
    """Free-form tags for discovery (e.g. ['pii', 'encrypted', 'deprecated'])."""

    provenance: Optional[Provenance] = None
    """Provenance of the field definition itself (as opposed to data values).
    Set when the schema was imported from an external catalog."""

    conflicts: List[SourceConflict] = field(default_factory=list)
    """Unresolved or resolved conflicts for this field discovered during
    multi-source reconciliation."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Arbitrary extension data (e.g. original column ordinal, physical
    storage type, partition key flag)."""

    def is_nullable(self) -> bool:
        """Convenience accessor for readability in validation code."""
        return self.nullable

    def has_conflicts(self) -> bool:
        """Return True if there are unresolved conflicts."""
        return any(c.resolution is None for c in self.conflicts)

    @property
    def resolved_conflicts(self) -> List[SourceConflict]:
        """Return only conflicts that have been resolved."""
        return [c for c in self.conflicts if c.resolution is not None]


# ---------------------------------------------------------------------------
# ConceptMapping
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConceptMapping:
    """Defines a relationship between a concept in a source domain and a
    concept in a target domain.

    Used during semantic integration to align heterogeneous schemas
    (e.g. 'user_id' in system A maps to 'member_id' in system B).
    """

    source_concept: str
    """Fully-qualified source concept name (e.g. 'sales_db.orders.customer_id')."""

    target_concept: str
    """Fully-qualified target concept name (e.g. 'crm.contacts.external_id')."""

    mapping_type: str
    """Nature of the mapping.  Examples:
        'direct'     — 1:1 equivalence
        'transform'  — requires a transformation function
        'aggregate'  — roll-up / summarisation
        'split'      — one source maps to multiple targets
        'merge'      — multiple sources combine into one target
    """

    confidence: float = 1.0
    """Confidence in the mapping [0.0, 1.0].  Defaults to 1.0 for
    hand-curated mappings; lower for ML-suggested mappings."""

    transform_rule: Optional[Union[str, Callable[[Any], Any]]] = None
    """Transformation to apply when mapping_type is 'transform' or 'aggregate'.
    Can be a SQL expression string, a Python callable, or a DSL snippet."""

    transform_rule_type: str = ""
    """Describes the form of transform_rule.  Examples:
        'sql', 'python_callable', 'jq', 'dbt_macro'."""

    cardinality: str = "1:1"
    """Cardinality of the mapping: '1:1', '1:N', 'N:1', 'N:M'."""

    reversible: bool = False
    """Whether the mapping can be inverted (applied in reverse)."""

    tags: List[str] = field(default_factory=list)
    """Tags for discoverability (e.g. ['pii', 'critical', 'experimental'])."""

    description: str = ""
    """Human-readable explanation of the mapping and its rationale."""

    provenance: Optional[Provenance] = None
    """How this mapping was created (manual curation, ML inference, etc.)."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Extension data (e.g. lineage graph node id, version, owner team)."""

    def __post_init__(self) -> None:
        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        valid_cardinalities = {"1:1", "1:N", "N:1", "N:M"}
        if self.cardinality not in valid_cardinalities:
            raise ValueError(
                f"cardinality must be one of {valid_cardinalities}, "
                f"got {self.cardinality!r}"
            )