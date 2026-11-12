# Transport compatibility baseline

The neighboring JSON contains 20 result hashes generated before changing
`transport/queues.py`, from commit `7d0265e1b30718e701adc3091b1ce8c3426c7252`.
The exact stimulus is `scenario()` in `test_transport_checkpoint.py`: four queue policies,
five horizons, tied arrivals, stalls, backpressure/drop and a disk failure interval.

The digest serializes the complete dataclass result, including delivery/loss order and
occupancy history. Only the in-flight ledger is sorted by item ID because it is an unordered
conservation set, not a delivery sequence. Do not regenerate these hashes from the new engine.
Changes require explaining the behavioral difference and keeping known-answer assertions.
