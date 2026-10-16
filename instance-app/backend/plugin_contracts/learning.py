"""Shared learning data v1. All plugins use these contracts and scoped services."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

class VersionedWrite(Contract):
    expected_revision: int = Field(ge=0)

class DomainFamiliarity(Contract):
    domain: str = Field(min_length=1, max_length=120)
    level: Literal["unfamiliar", "basic", "familiar", "advanced"]

class LearnerProfile(Contract):
    education_stage: Literal["primary_secondary", "undergraduate", "graduate"] | None = None
    domains: list[DomainFamiliarity] = Field(default_factory=list, max_length=30)
    verification_enabled: bool = False

    @model_validator(mode="after")
    def distinct_domains(self):
        keys = [d.domain.strip().casefold() for d in self.domains]
        if any(not key for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("Domain names must be nonempty and unique")
        return self

class ProfileWrite(VersionedWrite):
    profile: LearnerProfile

class ConceptInput(Contract):
    label: str = Field(min_length=1, max_length=160)
    sense: str = Field(min_length=1, max_length=2000)
    domain: str = Field(default="", max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def meaningful_strings(self):
        if not self.label.strip() or not self.sense.strip():
            raise ValueError("Label and sense must be nonempty")
        if any(not s.strip() or len(s) > 160 for s in self.aliases):
            raise ValueError("Aliases must contain 1..160 characters")
        return self

class ConceptWrite(VersionedWrite):
    concept: ConceptInput

class SourceAnchor(Contract):
    quote: str = Field(min_length=1, max_length=20000)
    prefix: str = Field(default="", max_length=500)
    suffix: str = Field(default="", max_length=500)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    # An occurrence can refer to a passage inside an earlier explanation.
    explanation_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def paired_offsets(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Both offsets are required together")
        if self.start is not None and self.end <= self.start:
            raise ValueError("End must be after start")
        if not self.quote.strip():
            raise ValueError("Quote must not be blank")
        return self

class OccurrenceInput(Contract):
    concept_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    anchor: SourceAnchor
    context: str = Field(default="", max_length=8000)

class LearningStateWrite(VersionedWrite):
    understanding: Literal["unresolved", "resolved"]
    # Suspension is controlled by checkpoint operations, independently of understanding.
    note: str = Field(default="", max_length=2000)

class Term(Contract):
    text: str = Field(min_length=1, max_length=160)
    note: str = Field(default="", max_length=1000)
    concept_id: str | None = Field(default=None, min_length=1, max_length=128)

class Analogy(Contract):
    text: str = Field(min_length=1, max_length=4000)
    mapping: str = Field(min_length=1, max_length=4000)
    limits: str = Field(default="", max_length=2000)
    source_explanation_id: str | None = Field(default=None, min_length=1, max_length=128)

class ExplanationStructure(Contract):
    treatment: Literal["mechanism", "black_box"]
    prerequisites: list[Term] = Field(default_factory=list, max_length=2)
    glosses: list[Term] = Field(default_factory=list, max_length=30)
    analogy: Analogy | None = None
    profile_revision: int = Field(ge=0)
    strategy_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_terms(self):
        terms = [t.text.strip().casefold() for t in self.prerequisites + self.glosses]
        if any(not t for t in terms) or len(terms) != len(set(terms)):
            raise ValueError("Terms must be nonempty and unique across prerequisites and glosses")
        return self

class StructureWrite(VersionedWrite):
    structure: ExplanationStructure

class KnownConclusion(Contract):
    node_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)

class CheckpointInput(VersionedWrite):
    node_id: str = Field(min_length=1, max_length=128)
    temporary_conclusion: str = Field(min_length=1, max_length=4000)
    known_conclusions: list[KnownConclusion] = Field(default_factory=list, max_length=100)
    unresolved_node_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def distinct_nodes(self):
        known = [x.node_id for x in self.known_conclusions]
        if len(known) != len(set(known)) or len(self.unresolved_node_ids) != len(set(self.unresolved_node_ids)):
            raise ValueError("Node references must be unique")
        if set(known) & set(self.unresolved_node_ids):
            raise ValueError("A node cannot be both understood and unresolved")
        if not self.temporary_conclusion.strip():
            raise ValueError("Temporary conclusion must not be blank")
        return self

class ResumeInput(VersionedWrite):
    expected_state_revision: int = Field(ge=0)
