"""What a query result is (docs/dev/03_contracts.md, section 10).

"Exactly restored discrete model" and "physically exact" are different claims; the status only
ever claims the first.
"""

from __future__ import annotations

from enum import StrEnum


class QueryStatus(StrEnum):
    EXACT_RECORD = "exact_record"
    REPLAYED = "replayed"
    INTERPOLATED = "interpolated"
    SUMMARY_ONLY = "summary_only"
    NOT_RECORDED = "not_recorded"
    NOT_COMPUTED = "not_computed"
