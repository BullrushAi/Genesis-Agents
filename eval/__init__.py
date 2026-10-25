"""Local evaluation harness for the Genesis agent gateway.

Additive package. Nothing here is imported by the Genesis service itself.
"""

from .genesis_client import (
    AgentRunResult,
    GenesisClient,
    LIVE_SLUG_COUNT,
    LIVE_SLUGS,
    MoneyDomainBlocked,
    Outcome,
    UnknownSlug,
    resolve_live_slug,
)
from .redaction import redact
from .target import (
    agenesis_target,
    arun_example,
    genesis_target,
    make_async_target,
    make_target,
    parse_example,
)


__all__ = [
    "AgentRunResult",
    "GenesisClient",
    "LIVE_SLUGS",
    "LIVE_SLUG_COUNT",
    "MoneyDomainBlocked",
    "Outcome",
    "UnknownSlug",
    "agenesis_target",
    "arun_example",
    "genesis_target",
    "make_async_target",
    "make_target",
    "parse_example",
    "redact",
    "resolve_live_slug",
]
