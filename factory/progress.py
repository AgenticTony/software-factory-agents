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


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


class ProgressLog:
    """The append-only, hash-chained progress log of one task."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @classmethod
    def for_task(cls, factory_root: Path | str, project: str, task_id: str) -> "ProgressLog":
        """The canonical log location for a task (outside every clone)."""
        return cls(Path(factory_root) / "progress" / _safe(project) / f"{_safe(task_id)}.jsonl")

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
        if {"n", "prev"} & set(entry):
            raise ProgressLogError("entries may not set 'n' or 'prev': the log numbers them")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        with lock.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                entries, prev = self._read()
                record = {"n": len(entries) + 1, "prev": prev, **entry}
                line = json.dumps(record, sort_keys=True, ensure_ascii=False)
                with self.path.open("a") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                return record
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def render(self) -> str:
        """The entries as prompt text for a fresh session ('' when empty)."""
        entries = self.entries()
        if not entries:
            return ""
        blocks = []
        for record in entries:
            body = {k: v for k, v in record.items() if k not in ("n", "prev")}
            blocks.append(
                f"### entry {record['n']}\n\n```json\n"
                + json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n```"
            )
        return "\n\n".join(blocks)
