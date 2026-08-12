"""Schema-agnostic evaluation tooling for RVBBIT Business Topology.

The package deliberately lives outside the PostgreSQL extension runtime.  It
can inspect privacy-safe packets, build review corpora, and evaluate candidate
models without teaching the database or a model about any one customer.
"""

from .contracts import (
    CORPUS_SCHEMA_VERSION,
    CORRESPONDENCE_VERDICTS,
    POPULATION_ROLES,
    ContractError,
    validate_corpus,
    validate_outbound_packet,
)

__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "CORRESPONDENCE_VERDICTS",
    "POPULATION_ROLES",
    "ContractError",
    "validate_corpus",
    "validate_outbound_packet",
]
