"""Per-task progress log (BUILD-ORDER F-10 AC-10, VALIDATOR-SPEC "Rounds").

Not every round shares a session: an aborted run re-enters the queue, and a
rejected PR comes back as a fix round, maybe days later. A fresh session
carries no context, so the orchestrator keeps one append-only log per task
— each attempt's outcome and numbered findings — and hands every new
session its entries 1..N in the prompt (a read-only copy: the session never
sees the file).

The log is orchestrator-owned and lives outside every clone, under
``<factory_root>/progress/<project>/<task-id>.jsonl``. Append-only is
enforced, not trusted: every entry carries its number and the SHA-256 of
the previous entry's line (a hash chain), so rewriting, dropping or
reordering an earlier entry breaks the chain and the next read or append
raises :class:`ProgressLogError`. The module has no edit API at all.

Vent entries (F-27, ``kind: "vent"``) share the same chain: each
``vent(category, text)`` tool call a session made becomes exactly one
entry, flushed by the orchestrator when the session returns. They are
telemetry for the owner, `factory status` (F-11) and the F-19 review —
never rendered into fresh-session prompts (:meth:`ProgressLog.render`).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

GENESIS = "0" * 64


class ProgressLogError(Exception):
    """The progress log is corrupt or was rewritten."""


def _digest(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()


def safe_name(name: str) -> str:
    """Filesystem-safe form of a project/task id (public: F-27's vent
    reader walks the same ``progress/<project>/`` layout as
    :meth:`ProgressLog.for_task`)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


class ProgressLog:
    """The append-only, hash-chained progress log of one task."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @classmethod
    def for_task(cls, factory_root: Path | str, project: str, task_id: str) -> "ProgressLog":
        """The canonical log location for a task (outside every clone)."""
        return cls(
            Path(factory_root) / "progress" / safe_name(project)
            / f"{safe_name(task_id)}.jsonl"
        )

    def _read(self) -> tuple[list[dict], str]:
        """Parse and chain-verify every entry; return (entries, last digest)."""
        if not self.path.exists():
            return [], GENESIS
        entries: list[dict] = []
        prev = GENESIS
        for number, line in enumerate(self.path.read_text().splitlines(), start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProgressLogError(f"{self.path}: line {number} is not JSON: {exc}") from exc
            if not isinstance(record, dict) or record.get("n") != number:
                raise ProgressLogError(
                    f"{self.path}: line {number} is not entry {number} — an entry "
                    "was dropped, reordered or rewritten"
                )
            if record.get("prev") != prev:
                raise ProgressLogError(
                    f"{self.path}: entry {number} does not chain to entry {number - 1} — "
                    "an earlier entry was rewritten"
                )
            entries.append(record)
            prev = _digest(line)
        return entries, prev

    def entries(self) -> list[dict]:
        """All entries, oldest first, after verifying the chain.

        Raises:
            ProgressLogError: The file was rewritten, truncated or reordered.
        """
        return self._read()[0]

    def append(self, entry: dict) -> dict:
        """Append one entry (numbered and chained here).

        Args:
            entry: JSON-serialisable mapping; ``n`` and ``prev`` are set by
                the log and may not be supplied.

        Returns:
            The stored entry.

        Raises:
            ProgressLogError: The existing log fails verification, or the
                entry tries to set ``n``/``prev`` itself.
        """
        return self.append_many([entry])[0]

    def append_many(self, entries: "list[dict]") -> list[dict]:
        """Append several entries under one lock, one read-and-verify and
        one fsync — chained exactly as the same number of :meth:`append`
        calls would chain them (F-27: a session's vents flush as a batch).

        Args:
            entries: JSON-serialisable mappings; none may set ``n``/``prev``.

        Returns:
            The stored entries, in order ([] for an empty batch, which
            touches nothing).

        Raises:
            ProgressLogError: The existing log fails verification, or an
                entry tries to set ``n``/``prev`` itself (nothing is written).
        """
        for entry in entries:
            if {"n", "prev"} & set(entry):
                raise ProgressLogError("entries may not set 'n' or 'prev': the log numbers them")
        if not entries:
            return []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        with lock.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                existing, prev = self._read()
                stored, lines = [], []
                for offset, entry in enumerate(entries):
                    record = {"n": len(existing) + offset + 1, "prev": prev, **entry}
                    line = json.dumps(record, sort_keys=True, ensure_ascii=False)
                    prev = _digest(line)
                    stored.append(record)
                    lines.append(line + "\n")
                with self.path.open("a") as fh:
                    fh.write("".join(lines))
                    fh.flush()
                    os.fsync(fh.fileno())
                return stored
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def render(self, omit_last_findings: bool = False) -> str:
        """The entries as prompt text for a fresh session ('' when empty).

        ``finding_keys`` is machine data for the next run's convergence
        seeding, never prompt content — the implementer can do nothing
        with 64-char hashes. ``omit_last_findings`` renders the newest
        entry without its ``findings`` field: the orchestrator sets it on
        a fix round, where the prompt's "Findings to fix" block already
        carries that round's findings (with the sibling instruction), so
        the implementer reads them once, not twice. The entry itself —
        outcome, escalation, run id — still renders: F-10 AC-10, every
        round receives entries 1..N.

        Vent entries (``kind: "vent"``, F-27) do NOT render: they are
        owner telemetry for `factory status` (F-11) and the F-19 review,
        not fresh-session context — a vent says what friction the last
        sessions hit, which the round entries already summarise better.

        Args:
            omit_last_findings: Drop the newest entry's findings field.
        """
        entries = [
            record for record in self.entries()
            if record.get("kind") != "vent"
        ]
        if not entries:
            return ""
        blocks = []
        for index, record in enumerate(entries):
            exclude = ("n", "prev", "finding_keys")
            if omit_last_findings and index == len(entries) - 1:
                exclude = exclude + ("findings",)
            body = {k: v for k, v in record.items() if k not in exclude}
            blocks.append(
                f"### entry {record['n']}\n\n```json\n"
                + json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n```"
            )
        return "\n\n".join(blocks)
