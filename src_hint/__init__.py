"""Suggested-ontology baseline package (for ket_qua_benchmark_kg.hint.txt).

Only exists to produce the "before" numbers of the HINT ontology (DEFINES / Clause.penalty string /
Case keyed by name / INVOLVES) so the bonus section can compare before-vs-after. It reuses the
base RAG package and the DrugKG-2 extraction helpers, then maps them onto the suggested ontology.
"""

from src import (  # noqa: F401
    Document,
    EmbeddingStore,
    KnowledgeBaseAgent,
    RecursiveChunker,
    _mock_embed,
)

__all__ = ["Document", "EmbeddingStore", "KnowledgeBaseAgent", "RecursiveChunker", "_mock_embed"]
