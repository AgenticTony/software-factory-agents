"""Factory CLI — the control-plane entry point.

``approve`` (F-04), ``run`` and ``loop`` (F-10), ``tick`` (F-29),
``trace`` (F-30) and ``sentinels replay`` (F-33) are real. Every other
subcommand is a stub naming the work order that implements it
(BUILD-ORDER.md).
"""

import argparse
import sys
from pathlib import Path

from factory.approve import (
    ApprovalError,
    approve_task,
    default_approver,
    reconcile_bootstrap,
    recover_task,
)
from factory.config import ConfigError, load_config
from factory.contract import ContractError, load_profile
from factory.gates import GateError
from factory.orchestrator import run_loop, run_once
from factory.prbody import PRBodyError
from factory.progress import ProgressLogError
from factory.publish import PublishError, PushRefused
from factory.replay import ReplayError, replay_lines, resolve_replay_files
from factory.runs import FACTORY_ROOT_DEFAULT, RUN_ID_RE, RunError
from factory.sandbox import SandboxError
from factory.session import SessionConfigError
from factory.state import TaskQueue, run_lease_held
from factory.tick import TickError, tick_steps
from factory.trace import read_trace, trace_path

# run/loop exit codes: 0 = a PR opened, nothing to do, the peak window, or
# the F-26 open-PR limit reached (a ready task waits); 1 = an escalation
# needs you; 3 = the quota stop; 2 = the orchestrator itself failed (the
# task was re-queued, every run dir removed).
_EXIT = {"pr_opened": 0, "idle": 0, "peak_window": 0, "queue_empty": 0,
         "concurrency_wait": 0,
         "needs_human": 1, "escalation": 1, "quota_exhausted": 3, "error": 2}
_RUN_ERRORS = (ConfigError, ContractError, GateError, PRBodyError, ProgressLogError,
               PublishError, PushRefused, RunError, SandboxError, SessionConfigError)

# subcommand -> the work order that implements it
STUBS: dict[str, str] = {
    "status": "F-11",
    "import": "F-12",
    "verify-live": "F-14",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory",
        description="Local agent software factory control plane.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    approve = sub.add_parser("approve", help="approve a task contract (F-04)")
    approve.add_argument("task_id", nargs="?", help="task id to approve, e.g. F-04")
    approve.add_argument(
        "--yes",
        action="store_true",
        help="non-interactive confirmation (refused for risk-flagged tasks)",
    )
    approve.add_argument(
        "--reconcile-bootstrap",
        action="store_true",
        help="approve and advance every entry in the bootstrap ledger",
    )
    approve.add_argument(
        "--queue",
        default=".factory/tasks.json",
        help="queue file (default: %(default)s)",
    )
    approve.add_argument(
        "--ledger",
        default=".factory/BOOTSTRAP.md",
        help="bootstrap ledger path for --reconcile-bootstrap (default: %(default)s)",
    )
    approve.add_argument(
        "--approved-by", default=None, help="approver name (default: git user.name)"
    )

    recover = sub.add_parser(
        "recover", help="queue a fix round for a parked F-26 escalation")
    recover.add_argument(
        "task_id", help="task id to recover (stale_base/pr_closed only)")
    recover.add_argument(
        "--yes", action="store_true",
        help="non-interactive confirmation",
    )
    recover.add_argument(
        "--queue", default=".factory/tasks.json",
        help="queue file (default: %(default)s)",
    )

    tick = sub.add_parser("tick", help="tick owner steps of a task (F-29)")
    tick.add_argument("task_id", help="task id, e.g. F-00")
    tick.add_argument("steps", nargs="+", type=int, help="1-based owner-step numbers")
    tick.add_argument("--evidence", required=True,
                      help="what you checked; recorded on every ticked step")
    tick.add_argument("--queue", default=".factory/tasks.json",
                      help="queue path (default: %(default)s)")
    tick.add_argument("--done-by", default=None, help="your name (default: git user.name)")

    trace = sub.add_parser("trace", help="print a run's trace, live or whole (F-30)")
    trace.add_argument("run_id", help="run id, e.g. 20261023T101010Z-1a2b3c")
    trace.add_argument("--follow", action="store_true",
                       help="keep printing new events until the attempt ends "
                            "(published, failed or aborted) or can no longer "
                            "write its end marker (killed run)")
    trace.add_argument("--queue", default=".factory/tasks.json",
                       help="queue path; the project is read from its profile "
                            "(default: %(default)s)")
    trace.add_argument("--factory-root", default=None,
                       help="factory root (default: ~/factory)")

    sentinels = sub.add_parser(
        "sentinels", help="built-in sentinel utilities")
    sent_sub = sentinels.add_subparsers(dest="sentinels_command", required=True)
    replay = sent_sub.add_parser(
        "replay", help="run the built-in sentinels over corpus diffs (F-33)")
    replay.add_argument(
        "--corpus", choices=["benign", "attack"],
        help="replay every patch under corpus/<name>/ of the profile's repo")
    replay.add_argument(
        "patches", nargs="*",
        help="explicit patch files (mutually exclusive with --corpus)")
    replay.add_argument("--queue", default=".factory/tasks.json",
                        help="queue path; the profile is read from next to "
                             "it (default: %(default)s)")

    for name, text in (
        ("run", "pick the next ready task and take it to a PR (F-10)"),
        ("loop", "run until the queue is empty, quota, peak window or escalation (F-10)"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--queue", default=".factory/tasks.json",
                         help="queue path (default: %(default)s)")
        cmd.add_argument("--profile", default=None,
                         help="project.yaml path (default: next to the queue)")
        cmd.add_argument("--factory-root", default=None,
                         help="factory root (default: ~/factory)")
        cmd.add_argument("--config", default=None,
                         help="factory config (default: <factory-root>/config.yaml)")

    for name, wo in STUBS.items():
        sub.add_parser(name, help=f"stub — implemented in {wo}")
    return parser


def _run_approve(args: argparse.Namespace) -> int:
    """Dispatch the approve subcommand; refusals exit 1, never a traceback."""
    try:
        approver = args.approved_by or default_approver()
        queue = TaskQueue(args.queue)
        if args.reconcile_bootstrap:
            # No --yes here by design (see main): every entry is confirmed
            # interactively.
            results = reconcile_bootstrap(queue, args.ledger, approved_by=approver)
            for result in results:
                print(f"{result.task_id}: {result.action} — {result.detail}")
            return 1 if any(r.action == "failed" for r in results) else 0
        status = approve_task(
            queue, args.task_id, approved_by=approver, assume_yes=args.yes
        )
        print(f"{args.task_id}: approved -> {status}")
        return 0
    except (ApprovalError, ContractError) as exc:
        print(f"factory approve: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory approve: interrupted", file=sys.stderr)
        return 130


def _run_recover(args: argparse.Namespace) -> int:
    """Dispatch the recover subcommand; refusals exit 1, never a traceback."""
    try:
        status = recover_task(
            TaskQueue(args.queue), args.task_id, assume_yes=args.yes
        )
        print(f"{args.task_id}: needs_human -> {status} (fix round queued)")
        return 0
    except (ApprovalError, ContractError) as exc:
        print(f"factory recover: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory recover: interrupted", file=sys.stderr)
        return 130


def _run_tick(args: argparse.Namespace) -> int:
    """Dispatch the tick subcommand; refusals exit 1, never a traceback."""
    try:
        status = tick_steps(
            TaskQueue(args.queue), args.task_id, args.steps,
            evidence=args.evidence, done_by=args.done_by or default_approver(),
        )
    except (TickError, ContractError) as exc:
        print(f"factory tick: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory tick: interrupted", file=sys.stderr)
        return 130
    print(f"{args.task_id}: ticked step(s) {', '.join(map(str, args.steps))} -> {status}")
    return 0


def _run_trace(args: argparse.Namespace) -> int:
    """Dispatch the trace subcommand (F-30 AC-6). It reads only; a run id
    that isn't one (the pattern check keeps trace paths inside
    ``<factory_root>/trace/<project>/``) or has no trace exits 1."""
    if not RUN_ID_RE.fullmatch(args.run_id):
        print(f"factory trace: {args.run_id!r} is not a run id", file=sys.stderr)
        return 1
    try:
        profile = load_profile(Path(args.queue).parent / "project.yaml")
        root = Path(args.factory_root) if args.factory_root else FACTORY_ROOT_DEFAULT
        path = trace_path(root, profile["name"], args.run_id)
        if not path.is_file():
            print(f"factory trace: no trace for run {args.run_id}", file=sys.stderr)
            return 1
        # --follow's stop condition besides run_end: a run that can no
        # longer write the marker. Live means its run dir still exists AND
        # the queue's run lease is held — the OS drops the lease the moment
        # the orchestrator dies, while a killed run's dir stays behind until
        # the next loop reclaims it. read_trace gives a dead run a grace
        # window, then stops. (The lease is per queue: a later run of the
        # same project holding it keeps this follow waiting until that run
        # ends — bounded, never forever.)
        run_dir = root / "runs" / profile["name"] / args.run_id
        queue_path = Path(args.queue)

        def live() -> bool:
            return run_dir.exists() and run_lease_held(queue_path)

        read_trace(path, follow=args.follow, live=live if args.follow else None)
        return 0
    except (ContractError, OSError) as exc:
        print(f"factory trace: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory trace: interrupted", file=sys.stderr)
        return 130


def _run_sentinels_replay(args: argparse.Namespace) -> int:
    """Dispatch the sentinels replay subcommand (F-33 AC-4). It reads
    only; misuse, a missing corpus and a missing patch exit 1 with a
    message, never a traceback."""
    if args.corpus and args.patches:
        print(
            "factory sentinels replay: give --corpus or patch files, not both",
            file=sys.stderr,
        )
        return 1
    try:
        profile = load_profile(Path(args.queue).parent / "project.yaml")
        entries = resolve_replay_files(profile, args.corpus, args.patches)
        for line in replay_lines(profile, entries):
            print(line)
        return 0
    except (ContractError, ReplayError, OSError) as exc:
        print(f"factory sentinels replay: {exc}", file=sys.stderr)
        return 1


def _run_extra(result) -> str:
    """The detail printed after a run's outcome.

    F-26 round-4 finding 3: a concurrency_wait names its cause — the PRs
    GitHub reports open, and the links whose state couldn't be resolved
    (which the limit presumes open) — so a scheduled stall never looks
    like a healthy tick.
    """
    if result.outcome == "concurrency_wait":
        named = [
            *(f"open: {url}" for url in result.open_prs),
            *(f"state unknown (counts as open): {url}"
              for url in result.unresolved_prs),
        ]
        if named:
            return "; ".join(named)
    return result.pr or result.escalation or ""


def _run_orchestrator(args: argparse.Namespace) -> int:
    """Dispatch run/loop; known failures exit 2 with a message, not a traceback."""
    try:
        kwargs = {"factory_root": Path(args.factory_root) if args.factory_root else None}
        if args.config:
            kwargs["config"] = load_config(args.config)
        if args.command == "run":
            result = run_once(Path(args.queue), profile_path=args.profile, **kwargs)
            detail = f" {result.task_id}" if result.task_id else ""
            print(f"factory run: {result.outcome}{detail} {_run_extra(result)}".rstrip())
            # F-26 AC-3 (round-7 finding 2): each open PR proven current
            # against the remote default head, so the owner merges only
            # what this run reported ready.
            for line in result.ready_prs:
                print(f"  ready: {line}")
            return _EXIT[result.outcome]
        loop = run_loop(Path(args.queue), profile_path=args.profile, **kwargs)
        for result in loop.results:
            print(f"  {result.task_id or '-'}: {result.outcome} {_run_extra(result)}".rstrip())
            for line in result.ready_prs:
                print(f"    ready: {line}")
        print(f"factory loop: stopped ({loop.stop_reason}) after {len(loop.results)} run(s)")
        return _EXIT[loop.stop_reason]
    except _RUN_ERRORS as exc:
        print(f"factory {args.command}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"factory {args.command}: interrupted", file=sys.stderr)
        return 130


def main(argv: list | None = None) -> int:
    """Parses arguments and dispatches a subcommand.

    Args:
        argv: Arguments without the program name; defaults to sys.argv[1:].

    Returns:
        Process exit code. Stubs return 2 (not implemented); approve
        and tick return 0 on success and 1 on any refusal or decline; run/loop
        per ``_EXIT`` (2 when the orchestrator itself failed).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "approve":
        if bool(args.task_id) == bool(args.reconcile_bootstrap):
            parser.error(
                "approve takes exactly one of: a task id, or --reconcile-bootstrap"
            )
        if args.reconcile_bootstrap and args.yes:
            parser.error(
                "--yes is not accepted with --reconcile-bootstrap: the ledger is "
                "plain markdown, so reconcile confirms every entry interactively"
            )
        return _run_approve(args)
    if args.command == "recover":
        return _run_recover(args)
    if args.command in ("run", "loop"):
        return _run_orchestrator(args)
    if args.command == "tick":
        return _run_tick(args)
    if args.command == "trace":
        return _run_trace(args)
    if args.command == "sentinels":
        return _run_sentinels_replay(args)
    print(
        f"factory {args.command}: not implemented yet (scheduled in {STUBS[args.command]})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
