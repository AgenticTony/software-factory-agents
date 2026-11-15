"""F-83 test support: per-seat telemetry knobs and the per-task status arm.

The F-38/F-72/F-75 split holds: git, the clone manager, the queue, the
L0/L1 sandboxes and the push to the local bare remote are real; only the
model seats, the CI seams and the gh read/write seams are scripted. What
this harness adds over ``_support.f75``:

* :class:`TelemetrySeats` — the f72 :class:`~_support.f72.Seats` runner
  with per-seat TELEMETRY knobs: scripted ``num_turns``, ``usage``,
  ``total_cost_usd``, ``provider`` block and a scripted FAILED
  implementer record (status + reason) for the quota/abort arms. Every
  knob defaults to Seats' own canned values, so an unconfigured
  TelemetrySeats behaves exactly like Seats (the shared routing is
  inherited, never forked — F-76's rule).

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

from pathlib import Path

from _support.f72 import Seats
from factory.session import SessionRecord

__all__ = ["TelemetrySeats", "GREEN_CHECK", "FAILED_RUN", "LOG_TAIL"]


GREEN_CHECK = "Backend (3.11)"
FAILED_RUN = {"id": 101, "name": GREEN_CHECK, "conclusion": "failure"}
LOG_TAIL = (
    f"{GREEN_CHECK}\tRun tests\t2026-11-14T09:00:00.000Z\t"
    "FAILED tests/test_doc.py::test_ac_2 - AssertionError: extracted "
    "text differs on the CI runner\n"
)


class TelemetrySeats(Seats):
    """Seats with per-seat telemetry overrides (F-83 AC-1/AC-2).

    ``seat_turns``: ``{seat: num_turns}`` — a seat not named keeps
    Seats' default (implementer 3, review-family 1, arbiter 2).

    ``seat_usage``: ``{seat: usage-dict-or-None}`` — the record's
    ``usage`` block; None (or a seat not named) keeps Seats' canned
    block. ``seat_costs``: ``{seat: float-or-None}`` — the record's
    ``total_cost_usd``; a seat not named keeps None (the pre-F-83
    script shape, so the unknown-poisons arms have their poison).

    ``seat_providers``: ``{seat: provider-dict-or-None}`` — the
    PARENT-STAMPED provider block a real record carries (F-59); not
    named keeps None (attribution to the attempt's primary).

    ``impl_failed``: ``(status, reason)`` — the implementer's record is
    built FAILED with that reason (the quota/abort arms), keeping every
    telemetry field the knobs name.
    """

    def __init__(self, seat_turns: dict | None = None,
                 seat_usage: dict | None = None,
                 seat_costs: dict | None = None,
                 seat_providers: dict | None = None,
                 impl_failed: "tuple[str, str] | None" = None,
                 **kwargs):
        super().__init__(**kwargs)
        self.seat_turns = dict(seat_turns or {})
        self.seat_usage = dict(seat_usage or {})
        self.seat_costs = dict(seat_costs or {})
        self.seat_providers = dict(seat_providers or {})
        self.impl_failed = impl_failed

    def _telemetry(self, record: SessionRecord, seat: str) -> SessionRecord:
        """Apply the per-seat telemetry knobs to a built record."""
        if seat in self.seat_turns:
            record.num_turns = self.seat_turns[seat]
        if seat in self.seat_usage:
            record.usage = self.seat_usage[seat]
        if seat in self.seat_costs:
            record.total_cost_usd = self.seat_costs[seat]
        if seat in self.seat_providers:
            record.provider = self.seat_providers[seat]
        return record

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        record = super().__call__(seat, prompt, Path(run_dir), profile,
                                  **kwargs)
        record = self._telemetry(record, seat)
        if seat == "implementer" and self.impl_failed is not None:
            status, reason = self.impl_failed
            record.status = status
            record.reason = reason
        return record
