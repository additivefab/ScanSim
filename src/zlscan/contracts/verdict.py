"""Three-valued outcome of a rule check (docs/dev/07_evidence_rules.md, section 3).

``UNRESOLVED`` is not a weak pass: "no conflict found" does not mean "passed" while the data
needed to decide is missing.
"""

from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNRESOLVED = "UNRESOLVED"
