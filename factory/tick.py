"""``factory tick`` — the owner ticks owner steps (F-29).

Owner steps are things only the owner can do (TASK-CONTRACT §2). Ticking
one is a human statement that it was done, so like approval it is
deliberately human-paced:

- each named step is printed and confirmed on its own; there is no
  ``--yes``,
- every tick carries evidence (what the owner checked), who ticked it and
  when, so the queue says why a step is closed,
- all refusals happen before anything is written, and the ticks plus any
  resulting status change go out in one atomic save.

Ticking the last open step closes the task: ``awaiting_owner -> done``,
or ``draft -> not_buildable -> done`` for an ``owner_only`` task.
"""

from collections.abc import Callable

from factory.approve import input_confirm
from factory.state import StateError, TaskQueue, TransitionError, check_tick


class TickError(Exception):
    """A tick was refused or declined; nothing was written."""


def tick_steps(
    queue: TaskQueue,
    task_id: str,
    step_numbers: list,
    *,
    evidence: str,
    done_by: str,
    confirm: Callable[[str], bool] = input_confirm,
) -> str:
    """Confirm and tick owner steps of one task.

    Args:
        queue: The loaded queue.
        task_id: Task id.
        step_numbers: 1-based owner-step numbers to tick.
        evidence: What the owner checked; must not be blank.
        done_by: Who is ticking.
        confirm: Asks one yes/no question; the default reads the terminal,
            and EOF declines.

    Returns:
        The task's status after the save.

    Raises:
        TickError: On any refusal or a declined confirmation; the queue
            file is unchanged.
    """
    try:
        task = queue.get(task_id)
        check_tick(task, step_numbers, evidence=evidence, done_by=done_by)
    except StateError as exc:
        raise TickError(str(exc)) from exc
    steps = task["owner_steps"]
    for number in step_numbers:
        question = (
            f"{task_id} owner step {number}: {steps[number - 1].get('text')}\n"
            f"  evidence: {evidence.strip()}\n"
            "Tick this step as done?"
        )
        if not confirm(question):
            raise TickError(f"{task_id}: owner step {number} declined; nothing was ticked")
    try:
        return queue.tick_owner_steps(task_id, step_numbers, evidence=evidence, done_by=done_by)
    except (StateError, TransitionError) as exc:
        raise TickError(str(exc)) from exc
