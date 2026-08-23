"""Non-authoritative proposal-shape plus pinned F0/errata subset checks."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .verify_packet import verify_proposal_shape_subset

__all__ = ["verify_proposal_shape_subset"]


def __getattr__(name: str) -> Any:
    """Load the public checker lazily so its CLI module can execute cleanly."""

    if name == "verify_proposal_shape_subset":
        from .verify_packet import verify_proposal_shape_subset

        return verify_proposal_shape_subset
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
