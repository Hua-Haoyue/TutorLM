"""The pluggable ``ModelProvider`` standard interface.

Any LLM backend (OpenAI, a local model, or the built-in fake) implements this
protocol. The application layer calls it without knowing which one is wired in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from backend.domain.models import Node


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider can do, used for health reporting and routing."""

    name: str
    model: str
    supports_explanation: bool = True
    supports_narration: bool = True
    supports_retelling: bool = False
    is_remote: bool = False
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ExplanationRequest:
    """A question whose answer should be grounded in a single node."""

    node: Node
    question: str
    language: str = "zh"
    max_words: int = 220
    learning_context: dict | None = None


@dataclass(frozen=True, slots=True)
class NarrationStyle:
    """Tuning knobs for a generated walkthrough."""

    language: str = "zh"
    tone: str = "teacher"
    words_per_segment: int = 90
    audience: str = "beginner"


@dataclass(frozen=True, slots=True)
class NarrationRequest:
    """A request to narrate an ordered sequence of nodes."""

    project_title: str
    nodes: Sequence[Node]
    style: NarrationStyle = field(default_factory=NarrationStyle)


@dataclass(frozen=True, slots=True)
class RetellRequest:
    """One user-triggered return from resolved children to their parent."""

    parent_title: str
    parent_question: str
    parent_answer: str
    children: tuple[dict, ...]
    profile: dict
    partial: bool = False
    explanation_depth: int = 0
    rewrite_policy: dict = field(default_factory=dict)
    source_context: dict = field(default_factory=dict)
    required_terms: tuple[str, ...] = ()
    retry_feedback: str = ''
    previous_response: str = ''
    parent_original_text: str = ''


@dataclass(frozen=True, slots=True)
class CausalExtractionRequest:
    items: tuple[dict, ...]
    retry_feedback: str = ''


@runtime_checkable
class ModelProvider(Protocol):
    """Standard provider interface.

    Implementations must be cheap to construct and safe to reuse. They should
    raise :class:`backend.domain.errors.ProviderError` on unrecoverable
    failures.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Describe the provider (name, model, features)."""

    def explain(self, request: ExplanationRequest) -> str:
        """Return a textual explanation answering ``request.question``."""

    def narrate(self, request: NarrationRequest) -> list[str]:
        """Return one narration paragraph per requested node, in order."""

    def retell(self, request: RetellRequest) -> str:
        """Return structured retelling JSON; the workflow validates its shape."""

    def organize_relations(self, payload: dict) -> str:
        """Propose relationship changes without modifying the graph."""

    def extract_causal(self, request: CausalExtractionRequest) -> str:
        """Compress explanations into causal payloads for upward rewriting."""

