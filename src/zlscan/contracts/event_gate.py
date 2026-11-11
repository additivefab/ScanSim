"""Gate for events handed to the calibration and reconstruction (rule 6 of AGENTS.md).

Only events that are observable on a real instrument may reach the reconstruction, and nothing
else may ride along: an unknown top-level field is treated as a leak (for example a true facet
id). The three evidence fields are independent (docs/dev/03_contracts.md, section 4).

This is the rule, applied to a plain mapping. The full ``Event`` model arrives with S0-T08.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zlscan.contracts.verdict import Verdict

ALLOWED_FIELDS = frozenset(
    {
        "kind", "origin", "visibility", "derivation", "clock_domain", "count", "tick",
        "session_id", "epoch", "uncertainty_ticks", "capability_ref", "related_sample", "payload",
    }
)  # fmt: skip
ORIGINS = frozenset({"SIMULATED", "MEASURED"})
DERIVATIONS = frozenset({"SENSOR", "INFERRED", "COMMAND"})


def recon_event_gate(event: Mapping[str, Any]) -> Verdict:
    """FAIL for a leak or an oracle-only event, UNRESOLVED when evidence fields are missing."""
    if set(event) - ALLOWED_FIELDS:
        return Verdict.FAIL
    if event.get("visibility") != "OBSERVABLE":
        return Verdict.FAIL
    origin, derivation = event.get("origin"), event.get("derivation")
    if (origin is not None and origin not in ORIGINS) or (
        derivation is not None and derivation not in DERIVATIONS
    ):
        return Verdict.FAIL
    if origin is None or derivation is None:
        return Verdict.UNRESOLVED
    if derivation == "SENSOR" and not event.get("capability_ref"):
        return Verdict.UNRESOLVED
    return Verdict.PASS
