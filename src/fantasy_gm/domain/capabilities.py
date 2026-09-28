"""What a fantasy platform integration can actually do."""

from __future__ import annotations

from enum import StrEnum


class ProviderCapability(StrEnum):
    """Capabilities a fantasy transaction provider declares.

    ``READ`` alone == a read-only provider. The spec's "READ_ONLY" is modelled as the absence of
    every ``*_WRITE`` capability rather than as a capability itself, so a provider can never
    declare the contradictory set {READ_ONLY, LINEUP_WRITE}.
    """

    READ = "read"
    LINEUP_WRITE = "lineup_write"
    ADD_DROP_WRITE = "add_drop_write"
    WAIVER_WRITE = "waiver_write"
    TRADE_PROPOSE_WRITE = "trade_propose_write"
    TRADE_RESPOND_WRITE = "trade_respond_write"
    DRAFT_WRITE = "draft_write"


WRITE_CAPABILITIES = frozenset(c for c in ProviderCapability if c is not ProviderCapability.READ)


def is_read_only(capabilities: frozenset[ProviderCapability]) -> bool:
    return not (capabilities & WRITE_CAPABILITIES)
