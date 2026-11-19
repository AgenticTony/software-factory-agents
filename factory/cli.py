"""Factory CLI — the control-plane entry point.

``approve`` (F-04), ``run`` and ``loop`` (F-10), ``status`` (F-11),
``tick`` (F-29), ``trace`` (F-30), ``sentinels replay`` (F-33),
``import`` (F-12), ``toolkit vet`` (F-22) and ``doctor`` (F-66) are
real. Every other subcommand is a stub naming the work order that
implements it (BUILD-ORDER.md).
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from factory.approve import (
    ApprovalError,
    adopt_queue,
    approve_profile,
    approve_task,
    default_approver,
    reconcile_bootstrap,
    recover_task,
)
from factory.config import CONFIG_FILE, ConfigError, load_config
from factory.contract import ContractError, load_profile, load_tasks, read_file
from factory.contractreview import finding_lines, review_contract
from factory.dashboard import DEFAULT_PORT, make_server
from factory.eval import DEFAULT_REPEATS, EvalError, run_on_demand_eval
from factory.gates import GateError
from factory.importer import (
    TIMEOUT_MIN_CEILING,
    TURN_CEILING,
    ImportRefused,
    apply_import,
    keep_refused_import_output,
    run_import,
    validate_budget,
)
from factory.orchestrator import run_loop, run_once
from factory.prbody import PRBodyError
from factory.progress import ProgressLogError
from factory.provider import run_secrets
from factory.publish import PublishError, PushRefused
from factory.replay import ReplayError, replay_lines, resolve_replay_files
from factory.runs import (
    FACTORY_ROOT_DEFAULT,
    RUN_ID_RE,
    RunError,
    project_component,
)
from factory.sandbox import SandboxError
from factory.session import SessionConfigError
from factory.state import TaskQueue, run_lease_held
from factory.status import StatusError, render_status, render_task_status
from factory.tick import TickError, tick_steps
from factory.toolkit import ToolkitError
from factory.trace import TraceError, read_trace, trace_path

# run/loop exit codes: 0 = a PR opened, nothing to do, the peak window, or
# the F-26 open-PR limit reached (a ready task waits); 1 = an escalation
# needs you; 3 = the quota stop; 2 = the orchestrator itself failed (the
# task was re-queued, every run dir removed).
_EXIT = {"pr_opened": 0, "idle": 0, "peak_window": 0, "queue_empty": 0,
         "concurrency_wait": 0,
         "needs_human": 1, "escalation": 1, "quota_exhausted": 3, "error": 2}
_RUN_ERRORS = (ConfigError, ContractError, GateError, PRBodyError, ProgressLogError,
               PublishError, PushRefused, RunError, SandboxError, SessionConfigError,
               ToolkitError)

# subcommand -> the work order that implements it
STUBS: dict[str, str] = {
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
        help="non-interactive confirmation (refused for risk-flagged "
             "tasks, contracts carrying a protected-ref warning or "
             "declared dependencies (F-71), and never accepted for "
             "--profile or --adopt)",
    )
    approve.add_argument(
        "--reconcile-bootstrap",
        action="store_true",
        help="approve and advance every entry in the bootstrap ledger",
    )
    approve.add_argument(
        "--profile",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="approve the profile's security-relevant content (F-63): "
             "commands, sentinels, network/docs allowlists, sandbox, repo, "
             "paths, live_checks, toolkit. Interactive only. Default path: "
             "next to --queue",
    )
    approve.add_argument(
        "--adopt",
        action="store_true",
        help="adopt an existing queue into the owner approval ledger "
             "(F-63): show each unledgered task in an approvable-or-later "
             "status and record it on confirm. Interactive only",
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
        "--factory-root",
        default=None,
        help="factory root for the approval ledger (default: ~/factory)",
    )
    approve.add_argument(
        "--approved-by", default=None, help="approver name (default: git user.name)"
    )

    review = sub.add_parser(
        "review-contract",
        help="one fresh-context, read-only session reviews a task's "
             "contract and source document before you approve it (F-80): "
             "findings by lens (purpose, testability, scope, ambiguity, "
             "budget, defect_class — F-94) are stored under the factory root, keyed by the "
             "contract hash, and shown by `factory approve`. Advice only "
             "— it never blocks, edits or approves; drives one model "
             "session, never inside the peak window")
    review.add_argument("task_id", help="task id whose contract to review")
    review.add_argument("--queue", default=".factory/tasks.json",
                        help="queue file (default: %(default)s)")
    review.add_argument("--profile", default=None,
                        help="project.yaml path (default: next to the queue)")
    review.add_argument("--factory-root", default=None,
                        help="factory root (default: ~/factory)")
    review.add_argument("--config", default=None,
                        help="factory config (default: "
                             "<factory-root>/config.yaml)")

    recover = sub.add_parser(
        "recover", help="move a parked (needs_human) task: every park "
                        "has one exit (F-92)")
    recover.add_argument(
        "task_id",
        help="task id to recover (stale_base/pr_closed, "
             "credential_added with --evidence, attempts_exhausted or "
             "ci_failed — or, F-79, ANY park holding a normal open PR — "
             "with --evidence and --reset-attempts, a "
             "scope_missing/"
             "scope_change_needed escalation with --evidence after the "
             "widened contract is re-approved, an escalation draft PR "
             "answered with --accept (new_dependency/risk_path_touched) "
             "or --reject (any class), or — F-92 — ANY park holding no "
             "PR link with --redraft --evidence)")
    recover.add_argument(
        "--yes", action="store_true",
        help="non-interactive confirmation (never allowed for "
             "credential_added, attempts_exhausted, ci_failed, a scope "
             "escalation, an escalation-PR accept/reject "
             "or a --redraft)",
    )
    recover.add_argument(
        "--evidence", default=None,
        help="required for credential_added/attempts_exhausted/scope "
             "escalations and for --accept and --redraft: what you checked (e.g. 'test "
             "fixture, not a real credential', 'all attempts died to the "
             "sandbox bind bug, fixed in PR #NN', or 'read the diff; "
             "requests is the documented client')",
    )
    recover.add_argument(
        "--reset-attempts", action="store_true",
        help="grant a fresh attempt budget (history is kept; the "
             "budget counts only attempts after the reset marker). "
             "Required for attempts_exhausted, ci_failed and "
             "scope_change_needed, and — F-79 — the one resume command "
             "for ANY park that holds a normal PR link: GitHub reports "
             "the PR open — the reset keeps it, the task returns to "
             "in_review and the next loop runs the fix round on that "
             "same PR, starting from the last fix round's kept work "
             "when there is some (a scope park only after the widened "
             "contract is re-approved via `factory approve --adopt`); "
             "closed — a fresh run re-implements from the default "
             "head; merged or unreadable — refused. A park holding no "
             "normal PR link re-queues a fresh run. Never for "
             "stale_base (use --revalidate), pr_closed or "
             "credential_added (its --evidence exit only)",
    )
    recover.add_argument(
        "--accept", action="store_true",
        help="F-72: accept the escalation draft PR's work — records the "
             "waiver (task, escalation, PR head SHA, evidence, who, "
             "when), re-runs L0 on that exact head with only that class "
             "waived, continues L1 and review, and on pass marks the PR "
             "ready and moves the task to in_review. The branch is "
             "fetched from the remote (a remote-only branch works, "
             "F-101) and the head must still be reachable on it — a "
             "force-push past the reviewed SHA refuses before any gate "
             "runs. new_dependency/risk_path_touched only; interactive, "
             "no --yes",
    )
    recover.add_argument(
        "--reject", action="store_true",
        help="F-72: reject the escalation draft PR — closes it and "
             "returns the task to draft with its approval markers "
             "cleared (the contract can change; re-approval required). "
             "Any class holding an escalation draft PR (F-92); "
             "interactive, no --yes",
    )
    recover.add_argument(
        "--revalidate", action="store_true",
        help="F-76, two shapes. stale_base park (needs_human): "
             "re-validates the parked task's PR (GitHub must report it "
             "open) by merging its CURRENT remote head onto the current "
             "remote default head and re-running L0+L1 (one re-run on a "
             "fresh clone if the first pass fails a command); a pass "
             "moves the task back to in_review with the pair stamped — "
             "no prompt, the passing re-validation is the evidence; "
             "combines with no other flag. in_review head vouch (a PR "
             "the sweep reported moved/unrecorded): requires --head to "
             "name the head you judged; no gates run — your word on "
             "that head is the vouch",
    )
    recover.add_argument(
        "--head", default=None, metavar="SHA",
        help="F-76: with --revalidate on an in_review PR (the head-vouch "
             "shape): the head you judged and are vouching for — the "
             "full SHA or an unambiguous >=12-char prefix. The remote "
             "must hold exactly it when the command runs. Without "
             "--head the in_review shape refuses and prints the current "
             "head plus the command to run",
    )
    recover.add_argument(
        "--redraft", action="store_true",
        help="F-92: return ANY needs_human park that holds no PR link "
             "to draft — approval markers cleared, every attempt kept, "
             "a progress-log entry written. The task runs again only "
             "after `factory approve` (contract hash and ledger). "
             "Requires --evidence; interactive, no --yes. A park holding "
             "a PR keeps its own exit (--reset-attempts, --reject, or "
             "the sweep's merged recovery)",
    )
    recover.add_argument(
        "--done-by", default=None,
        help="your name for the recovery log entry "
             "(default: git user.name)",
    )
    recover.add_argument(
        "--factory-root", default=None,
        help="factory root for the progress log (default: ~/factory)",
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

    dashboard = sub.add_parser(
        "dashboard", help="read-only local dashboard: queue, metrics, runs (F-31)")
    dashboard.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help="loopback port to serve on (default: %(default)s)")
    dashboard.add_argument(
        "--queue", default=".factory/tasks.json",
        help="queue path; the project is read from its profile "
             "(default: %(default)s)")
    dashboard.add_argument(
        "--factory-root", default=None,
        help="factory root holding traces and runs (default: ~/factory)")

    eval_cmd = sub.add_parser(
        "eval",
        help="seeded-bug validator eval, on demand (F-16) — drives real "
             "model sessions; never part of run, loop or CI",
    )
    eval_cmd.add_argument(
        "--out", default=None,
        help="report JSON path (default: <factory-root>/eval/"
             "seeded-eval-<stamp>.json, rendered markdown beside it)")
    eval_cmd.add_argument("--factory-root", default=None,
                          help="factory root (default: ~/factory)")
    eval_cmd.add_argument(
        "--reviewers", type=int, default=None,
        help="reviewer sessions per diff (default: the factory config)")
    eval_cmd.add_argument(
        "--repeats", type=int, default=None,
        help="run each seed N times; the report carries mean and spread "
             "per metric, and per seed caught k/N and the pass^k line "
             "(default: 3; F-48/F-95 — cost scales with N)")
    eval_cmd.add_argument(
        "--keep-fixture", action="store_true",
        help="keep the fixture repo after the run for inspection")
    eval_cmd.add_argument(
        "--importer", action="store_true",
        help="run ONE real import against the prose fixture (F-57) and "
             "report schema validity, task count and whether the "
             "oversized work order was split; needs the explicit "
             "FACTORY_LIVE_TESTS=1 opt-in (a credential alone never "
             "turns it on)")

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

    toolkit_cmd = sub.add_parser(
        "toolkit", help="vetted agent toolkit utilities (F-22)")
    toolkit_sub = toolkit_cmd.add_subparsers(dest="toolkit_command", required=True)
    vet = toolkit_sub.add_parser(
        "vet",
        help="scan a candidate item and report — admission evidence only; "
             "a clean scan never admits (an item enters toolkit/index.yaml "
             "only through an owner-approved PR)")
    vet.add_argument("path", help="candidate item directory")
    vet.add_argument(
        "--profile", default=".factory/project.yaml",
        help="project.yaml whose allowlists the vet judges against "
             "(default: %(default)s)")
    vet.add_argument(
        "--report", default=None,
        help="write the vetting report (markdown) to this path — the "
             "artifact an admission PR carries")

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

    status = sub.add_parser(
        "status",
        help="daily summary and metrics (F-11); with a task id, that "
             "task's attempts and spend (F-83)")
    status.add_argument(
        "task_id", nargs="?", default=None,
        help="one task's page: per-attempt turns/usage/cost and the "
             "task total (F-83)",
    )
    status.add_argument(
        "--queue", default=".factory/tasks.json",
        help="queue file (default: %(default)s)",
    )
    status.add_argument(
        "--factory-root", default=None,
        help="factory root (default: ~/factory)",
    )
    status.add_argument(
        "--repo", default=".",
        help="project repo, for merge times and revert detection "
             "(default: the current directory)",
    )

    import_cmd = sub.add_parser(
        "import", help="propose tasks.json from the planning docs (F-12)"
    )
    import_cmd.add_argument(
        "--repo", default=".",
        help="project repo root (default: the current directory)",
    )
    import_cmd.add_argument(
        "--queue", default=None,
        help="queue file (default: <repo>/.factory/tasks.json)",
    )
    import_cmd.add_argument(
        "--docs", default="docs",
        help="planning-docs directory, repo-relative (default: %(default)s)",
    )
    import_cmd.add_argument(
        "--proposal", default=None,
        help="review a prepared proposal JSON instead of running the "
             "importer session",
    )
    import_cmd.add_argument(
        "--factory-root", default=None,
        help="factory root for the sandboxed importer session's disposable "
             "clone (default: ~/factory)",
    )
    import_cmd.add_argument(
        "--max-turns", type=int, default=None, metavar="N",
        help=f"importer session turn budget, 1-{TURN_CEILING} (default: "
             "scaled with the planning docs, from 30)",
    )
    import_cmd.add_argument(
        "--timeout-min", type=float, default=None, metavar="M",
        help=f"importer session wall-clock budget in minutes, above 0 and "
             f"at most {TIMEOUT_MIN_CEILING:g} (default: scaled with the "
             "planning docs, from 20)",
    )

    doctor_cmd = sub.add_parser(
        "doctor",
        help="read-only health check of every onboarded project (F-66): "
             "the approval ledger's repos plus --repo, every violation "
             "through all-errors loaders, each with its contract epoch, "
             "the work order that introduced it and the exact fix",
    )
    doctor_cmd.add_argument(
        "--repo", action="append", default=None, metavar="PATH",
        help="a repo to check in addition to every repo the approval "
             "ledger records (repeatable)",
    )
    doctor_cmd.add_argument(
        "--factory-root", default=None,
        help="factory root holding the approval ledger (default: ~/factory)",
    )

    for name, wo in STUBS.items():
        sub.add_parser(name, help=f"stub — implemented in {wo}")
    return parser


def _run_approve(args: argparse.Namespace) -> int:
    """Dispatch the approve subcommand; refusals exit 1, never a traceback."""
    try:
        approver = args.approved_by or default_approver()
        root = Path(args.factory_root) if args.factory_root else None
        if args.profile is not None:
            # F-63 AC-2: interactive only — no --yes exists for this mode.
            path = args.profile or str(
                Path(args.queue).parent / "project.yaml"
            )
            approve_profile(
                load_profile(path), root, approved_by=approver
            )
            print(f"profile approved: {path}")
            return 0
        queue = TaskQueue(args.queue)
        if args.adopt:
            # F-63 AC-1: interactive only — no --yes exists for this mode.
            results = adopt_queue(queue, root, approved_by=approver)
            for result in results:
                print(f"{result.task_id}: {result.action} — {result.detail}")
            return 1 if any(r.action == "failed" for r in results) else 0
        if args.reconcile_bootstrap:
            # No --yes here by design (see main): every entry is confirmed
            # interactively.
            results = reconcile_bootstrap(
                queue, args.ledger, approved_by=approver, factory_root=root
            )
            for result in results:
                print(f"{result.task_id}: {result.action} — {result.detail}")
            return 1 if any(r.action == "failed" for r in results) else 0
        status = approve_task(
            queue, args.task_id, approved_by=approver, assume_yes=args.yes,
            factory_root=root,
        )
        print(f"{args.task_id}: approved -> {status}")
        return 0
    except (ApprovalError, ContractError) as exc:
        print(f"factory approve: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory approve: interrupted", file=sys.stderr)
        return 130


def _run_review_contract(args: argparse.Namespace) -> int:
    """Dispatch ``review-contract`` (F-80 AC-1). 0 with the stored
    review's path and findings; 1 on any refusal or an invalid review
    (nothing stored), never a traceback."""
    try:
        root = (Path(args.factory_root) if args.factory_root
                else FACTORY_ROOT_DEFAULT)
        stored = review_contract(
            Path(args.queue), args.task_id, factory_root=root,
            profile_path=args.profile,
            config=load_config(args.config) if args.config else None,
        )
    except _RUN_ERRORS as exc:
        print(f"factory review-contract: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory review-contract: interrupted", file=sys.stderr)
        return 130
    lines = finding_lines(stored.review["findings"], indent="  ")
    print(f"{args.task_id}: contract review stored — "
          f"{len(stored.review['findings'])} finding(s), "
          f"contract hash {stored.review['contract_hash'][:12]}")
    for line in lines:
        print(line)
    print(f"stored: {stored.path}")
    print(f"`factory approve {args.task_id}` shows it while the contract "
          "is unchanged.")
    return 0


def _run_recover(args: argparse.Namespace) -> int:
    """Dispatch the recover subcommand; refusals exit 1, never a traceback."""
    try:
        status = recover_task(
            TaskQueue(args.queue), args.task_id, assume_yes=args.yes,
            evidence=args.evidence, reset_attempts=args.reset_attempts,
            accept=args.accept, reject=args.reject,
            revalidate=args.revalidate, head=args.head,
            redraft=args.redraft, done_by=args.done_by,
            factory_root=(Path(args.factory_root) if args.factory_root
                          else None),
        )
        if status == "in_review" and args.reset_attempts:
            print(f"{args.task_id}: needs_human -> in_review "
                  "(fresh attempt budget; PR kept — the next `factory "
                  "loop` runs the CI fix round on it)")
        elif status == "in_review" and args.revalidate:
            print(f"{args.task_id}: needs_human -> in_review "
                  "(stale_base re-validated on the PR's current head; "
                  "the sweep reports it ready or not from here)")
        elif status == "in_review" and args.accept:
            print(f"{args.task_id}: needs_human -> in_review "
                  "(escalation accepted; PR marked ready)")
        elif status == "in_review":
            # F-79: the credential evidence exit on a task whose PR is
            # still open — the PR is kept, no budget granted.
            print(f"{args.task_id}: needs_human -> in_review "
                  "(PR kept — the sweep owns it again; attempts not "
                  "reset)")
        elif status == "draft" and args.redraft:
            print(f"{args.task_id}: needs_human -> draft "
                  "(redrafted; edit the contract if needed, then "
                  "`factory approve` it)")
        elif status == "draft":
            print(f"{args.task_id}: needs_human -> draft "
                  "(escalation rejected; contract re-approval required)")
        else:
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
        run_dir = (
            root / "runs" / project_component(profile["name"]) / args.run_id
        )
        queue_path = Path(args.queue)

        def live() -> bool:
            return run_dir.exists() and run_lease_held(queue_path)

        read_trace(path, follow=args.follow, live=live if args.follow else None)
        return 0
    except (ContractError, TraceError, OSError) as exc:
        print(f"factory trace: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory trace: interrupted", file=sys.stderr)
        return 130


def _run_dashboard(args: argparse.Namespace) -> int:
    """Dispatch the dashboard subcommand (F-31). It serves read-only on
    127.0.0.1; a port it cannot bind exits 1 with a message, never a
    traceback."""
    root = Path(args.factory_root) if args.factory_root else FACTORY_ROOT_DEFAULT
    try:
        server = make_server(factory_root=root, queue=args.queue,
                             port=args.port)
    except OSError as exc:
        print(f"factory dashboard: cannot bind 127.0.0.1:{args.port}: {exc}",
              file=sys.stderr)
        return 1
    port = server.server_address[1]
    print(f"factory dashboard: http://127.0.0.1:{port} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("factory dashboard: interrupted", file=sys.stderr)
        return 130
    finally:
        server.server_close()
    return 0


def _run_status(args: argparse.Namespace) -> int:
    """Dispatch the status subcommand (F-11). It reads only; an unreadable
    queue exits 1 with a message, never a traceback. A task id (F-83)
    renders that task's page instead of the project's."""
    try:
        queue = load_tasks(args.queue)
        root = Path(args.factory_root) if args.factory_root else FACTORY_ROOT_DEFAULT
        if args.task_id:
            # F-83 AC-2: the per-task page — attempts, turns against the
            # cap, usage and cost, per attempt and in total.
            print(render_task_status(queue, args.task_id,
                                     project=queue["project"]))
            return 0
        print(render_status(queue, project=queue["project"], factory_root=root,
                            repo=Path(args.repo)))
        return 0
    except (ContractError, StatusError, KeyError, OSError) as exc:
        print(f"factory status: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory status: interrupted", file=sys.stderr)
        return 130


def _keep_path_profile(args: argparse.Namespace) -> dict:
    """The import repo's profile for the keep path's secrets (F-59).

    Best effort BY DESIGN: this runs inside an except branch — the
    import already refused, and its refusal may be exactly why the
    profile will not load. An unloadable profile returns {} (a self
    project shape: no provider block, the zai default), never turns a
    keep into a crash; the egress seam's pattern redaction still runs
    over whatever is saved."""
    try:
        return load_profile(Path(args.repo) / ".factory" / "project.yaml")
    except (ContractError, OSError):
        return {}


def _run_import(args: argparse.Namespace) -> int:
    """Dispatch the import subcommand (F-12). Proposes ``tasks.json``
    from the project's planning docs, shows the full diff and writes
    nothing until you confirm; a refusal exits 1, never a traceback."""
    try:
        # Every read under this try refuses as ContractError at its own
        # reader (contract.read_file, the importer's register read), so
        # the tuple below names no decode error; OSError is for the
        # write path (TaskQueue lock, mkdir, atomic rename).
        validate_budget(args.max_turns, args.timeout_min)
        # Optional callable (F-51/A-7: the explicit annotation keeps the
        # None sentinel and the later def assignable to one name).
        session: Callable[[str], str] | None = None
        # `is not None`, not truthiness: --proposal "" (an unset shell
        # variable) is a missing file to refuse, not a request for the
        # model session.
        if args.proposal is not None:
            if not args.proposal:  # Path("") is ".", a confusing refusal
                raise ContractError("--proposal is empty: name a proposal file")
            proposal_text = read_file(Path(args.proposal), args.proposal)

            def _proposal_session(prompt: str) -> str:
                return proposal_text

            session = _proposal_session

        # session=None: the importer session runs sandboxed (srt, the
        # read-only reviewer guard) in a disposable clone, never
        # in-process on the live checkout (review #61 finding 1).
        result = run_import(
            Path(args.repo), args.queue, docs_dir=args.docs, session=session,
            factory_root=Path(args.factory_root) if args.factory_root else None,
            max_turns=args.max_turns, timeout_min=args.timeout_min,
        )
        print(result.summary)
        if not result.plan.diff:
            print("factory import: nothing to change.")
            return 0
        try:
            answer = input(f"Apply this import to {result.queue_path}? [y/N] ")
        except EOFError:
            # Closed stdin cannot answer the prompt — the same "user
            # could not answer" class as ^C (review R-3). A refusal,
            # exit 1: a piped caller needs the non-success signal (an
            # explicit answer is the only decline that exits 0). Scoped
            # to input(): an EOFError from anywhere else is not stdin.
            print("factory import: no answer on stdin — nothing written.",
                  file=sys.stderr)
            return 1
        if answer.strip().lower() != "y":
            print("factory import: nothing written.")
            return 0
        # F-67 AC-4 + R2-1: EVERY drop gets its own named prompt —
        # never one blanket yes, and never a bypass.
        accepted_drops: set[str] = set()
        for drop in result.plan.dropped:
            try:
                drop_answer = input(
                    f"Accept dropping {drop.label} (cited {drop.doc}:"
                    f"{drop.line})? [y/N] "
                )
            except EOFError:
                print(
                    "factory import: no answer on stdin — nothing written.",
                    file=sys.stderr,
                )
                return 1
            if drop_answer.strip().lower() == "y":
                accepted_drops.add(drop.label)
            else:
                print(
                    "factory import: drop declined — nothing written "
                    "(re-propose the criterion or edit the proposal)."
                )
                return 0
        try:
            apply_import(
                result.plan, result.queue_path, confirm=lambda: True,
                confirm_drop=lambda drop: drop.label in accepted_drops,
            )
        except (ImportRefused, OSError) as exc:
            # The real session's output is keepable; a --proposal review
            # already holds its file and keeps nothing (round 2: keying
            # this on --factory-root re-saved the owner's own file).
            if session is not None:
                raise
            kept = keep_refused_import_output(
                exc, text=result.raw_output, project=result.plan.project,
                repo=Path(args.repo), queue_path=result.queue_path,
                docs_dir=args.docs,
                factory_root=Path(args.factory_root)
                if args.factory_root else None,
                # F-43 review round 1: the raw output is the session's;
                # the saved copy passes the egress seam with the token.
                # Round 2: a TUPLE — this was a parenthesised string,
                # iterated character by character, scrubbing nothing.
                # F-59: every provider token the project's profile
                # resolves to (best effort here — the import already
                # refused; a profile that cannot load contributes no
                # secrets, and the seam still pattern-redacts).
                secrets=run_secrets(_keep_path_profile(args)),
            )
            if kept is not exc:
                raise kept from exc
            raise
        print(f"factory import: wrote {result.queue_path}")
        return 0
    except (ContractError, OSError) as exc:
        print(f"factory import: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("factory import: interrupted", file=sys.stderr)
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


def _run_eval(args: argparse.Namespace) -> int:
    """Dispatch the seeded-bug validator eval (F-16 AC-3's owner step),
    or — with ``--importer`` (F-57 AC-4) — the one real import against
    the prose fixture.

    Both drive real model sessions, so they only ever run explicitly, by
    this command — never from ``run``/``loop`` or CI. The importer eval
    additionally requires the explicit ``FACTORY_LIVE_TESTS=1`` opt-in
    (refused here otherwise, exit 2, zero spend — a credential alone
    never turns it on). The reviewer fan-out width is the factory
    config's (the same ``load_config(factory_root / CONFIG_FILE)`` read
    the orchestrator does, so the eval certifies the width the queue
    actually runs); ``--reviewers`` overrides it. ``--repeats`` (F-48)
    runs each seed N times — default 3 — so the report carries each
    metric's mean and spread; a repeats below 1 is refused here, exit 2.
    Known failures exit 2 with a message, not a traceback."""
    if args.importer:
        return _run_importer_eval(args)
    factory_root = (Path(args.factory_root) if args.factory_root
                    else FACTORY_ROOT_DEFAULT)
    repeats = args.repeats if args.repeats is not None else DEFAULT_REPEATS
    if repeats < 1:
        print(f"factory eval: --repeats must be an integer >= 1, "
              f"got {repeats}", file=sys.stderr)
        return 2
    try:
        reviewers = args.reviewers or load_config(
            factory_root / CONFIG_FILE
        ).reviewers
        result = run_on_demand_eval(
            factory_root if args.factory_root else None,
            Path(args.out) if args.out else None,
            reviewers=reviewers,
            repeats=repeats,
            keep_fixture=args.keep_fixture,
        )
    except (ConfigError, EvalError, GateError, RunError, SandboxError,
            SessionConfigError) as exc:
        print(f"factory eval: {exc}", file=sys.stderr)
        return 2
    print(result["rendered_path"].read_text(), end="")
    print(f"factory eval: report written to {result['report_path']}")
    return 0


def _run_importer_eval(args: argparse.Namespace) -> int:
    """Dispatch ``factory eval --importer`` (F-57 AC-4): ONE real import
    against the prose fixture, on demand. The live gate (exactly
    ``FACTORY_LIVE_TESTS=1``) is enforced inside the eval itself; a
    refusal prints the variable's name and exits 2 — zero spend, never a
    traceback. Success prints the rendered report."""
    from factory.eval import run_importer_eval

    try:
        result = run_importer_eval(
            Path(args.factory_root) if args.factory_root else None
        )
    except EvalError as exc:
        print(f"factory eval: {exc}", file=sys.stderr)
        return 2
    print(result["rendered_path"].read_text(), end="")
    print(f"factory eval: report written to {result['report_path']}")
    return 0


def _run_toolkit_vet(args: argparse.Namespace) -> int:
    """Dispatch ``toolkit vet`` (F-22 AC-3/AC-4).

    Reads the candidate and prints the verdict; ``--report`` writes the
    markdown an admission PR carries. It never writes the index — a
    clean scan is evidence, not admission: an item enters
    ``toolkit/index.yaml`` only through an owner-approved PR. Refusals
    exit 1, never a traceback.
    """
    from factory.toolkit import ToolkitError, render_report, vet_candidate

    try:
        profile = load_profile(args.profile)
    except ContractError as exc:
        print(f"factory toolkit vet: {exc}", file=sys.stderr)
        return 1
    allowlists = list(profile.get("docs_allowlist") or []) + list(
        profile.get("network_allowlist") or []
    )
    try:
        result = vet_candidate(args.path, allowlists=allowlists)
    except ToolkitError as exc:
        print(f"factory toolkit vet: {exc}", file=sys.stderr)
        return 1
    if result.licence:
        print(f"licence: {result.licence}")
    else:
        print(f"licence: REFUSED — {result.licence_reason}")
    if not result.findings:
        print("findings: none (clean scan — admission stays a human PR)")
    for finding in result.findings:
        where = f"{finding.file}:{finding.line}" if finding.line else finding.file
        print(f"{finding.cls} {where} {finding.detail}")
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            render_report(result, args.path, allowlists), encoding="utf-8"
        )
        print(f"report: {args.report}")
    return 0 if result.clean else 1


def _run_doctor(args: argparse.Namespace) -> int:
    """Dispatch the doctor subcommand (F-66 AC-2). Read-only: reports
    every finding for every repo the approval ledger records plus any
    ``--repo``, exits 1 on any finding, 0 with a clean summary. Known
    failures exit 2 with a message, never a traceback."""
    from factory import doctor
    from factory.epochs import EpochError

    root = Path(args.factory_root) if args.factory_root else None
    try:
        reports = doctor.factory_report(root, extra_repos=[
            Path(repo) for repo in (args.repo or [])
        ])
        print(doctor.render(reports), end="")
        clean = [report for report in reports if not report.findings]
        if reports and len(clean) == len(reports):
            print("factory doctor: all checked repos clean")
        return 0 if len(clean) == len(reports) else 1
    except (ContractError, EpochError, OSError) as exc:
        print(f"factory doctor: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("factory doctor: interrupted", file=sys.stderr)
        return 130


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
    # F-75: a pr_opened whose CI window ended unsettled carries its
    # note ("CI pending" / "no CI reported").
    detail = result.pr or result.escalation or ""
    if result.outcome == "needs_human" and result.pr and result.escalation:
        # F-77 AC-3: a park that holds a PR prints the reason AND the
        # PR, as the loop's parked line does — never one hiding the other.
        detail = f"{result.escalation} — {result.pr}"
    if getattr(result, "note", None):
        detail = f"{detail} — {result.note}".strip(" —")
    return detail


def _run_orchestrator(args: argparse.Namespace) -> int:
    """Dispatch run/loop; known failures exit 2 with a message, not a traceback."""
    try:
        # Any-typed bag (F-51/A-7): run_once/loop take different
        # keyword subsets; the calls below are type-checked at their own
        # call sites.
        kwargs: dict[str, Any] = {
            "factory_root": Path(args.factory_root) if args.factory_root else None
        }
        if args.config:
            kwargs["config"] = load_config(args.config)
        if args.command == "run":
            result = run_once(Path(args.queue), profile_path=args.profile, **kwargs)
            detail = f" {result.task_id}" if result.task_id else ""
            print(f"factory run: {result.outcome}{detail} {_run_extra(result)}".rstrip())
            # F-82 AC-3: one line under the published PR — the follow-up
            # draft this run's publish wrote (or replaced). The PR line
            # above names the round; this names what it left open.
            if result.followup:
                print(f"  follow-up drafted: {result.followup[0]} "
                      f"({result.followup[1]} findings)")
            # F-26 AC-3 (round-7 finding 2): each open PR proven current
            # against the remote default head, so the owner merges only
            # what this run reported ready.
            for line in result.ready_prs:
                print(f"  ready: {line}")
            # F-58 AC-5: the counterpart — PRs the sweep refused to call
            # ready (CI red/pending/unknown), visible, never silent.
            for line in result.not_ready_prs:
                print(f"  not ready: {line}")
            # F-63 AC-1: tasks the owner ledger does not cover — ready
            # ones were demoted, the rest were not acted on. Visible,
            # never silent.
            for task_id in result.unledgered:
                print(f"  unledgered (not acted on): {task_id}")
            return _EXIT[result.outcome]
        loop = run_loop(Path(args.queue), profile_path=args.profile, **kwargs)
        for result in loop.results:
            print(f"  {result.task_id or '-'}: {result.outcome} {_run_extra(result)}".rstrip())
            # F-82 AC-3: the same line under each published PR.
            if result.followup:
                print(f"    follow-up drafted: {result.followup[0]} "
                      f"({result.followup[1]} findings)")
            for line in result.ready_prs:
                print(f"    ready: {line}")
            for line in result.not_ready_prs:
                print(f"    not ready: {line}")
            for task_id in result.unledgered:
                print(f"    unledgered (not acted on): {task_id}")
        # F-72 AC-5: one line per task parked during the loop — id,
        # reason, and the task's PR: the escalation draft PR, or (F-77)
        # the normal PR a sweep park is about; "no PR" only when the
        # task holds no PR link. A parked batch is NOT a clean finish:
        # the exit code below says so.
        for result in loop.parked:
            where = result.pr or "no PR"
            # F-92 AC-2: both classes when keeping the work replaced the
            # one the run escalated on.
            shown = getattr(result, "park_label", None) or result.escalation
            print(f"  parked: {result.task_id} "
                  f"({shown}) — {where}")
        print(f"factory loop: stopped ({loop.stop_reason}) after {len(loop.results)} run(s)")
        code = _EXIT[loop.stop_reason]
        if loop.parked and code == 0:
            # Parked tasks at the end: non-zero, so a script never reads
            # "parked" as "all done" (F-72 AC-5). A quota/error stop
            # already exits non-zero and keeps its own meaning.
            code = 1
        return code
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
        Process exit code. Stubs return 2 (not implemented); approve,
        tick and import return 0 on success and 1 on any refusal —
        import's only exit-0 decline is an explicit answer at its
        confirm prompt (no answer on stdin is a refusal, not a
        decline); run/loop per ``_EXIT`` (2 when the orchestrator
        itself failed).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "approve":
        modes = [bool(args.task_id), args.reconcile_bootstrap,
                 args.profile is not None, args.adopt]
        if sum(modes) != 1:
            parser.error(
                "approve takes exactly one of: a task id, --reconcile-bootstrap, "
                "--profile [PATH], or --adopt"
            )
        if (args.reconcile_bootstrap or args.profile is not None
                or args.adopt) and args.yes:
            parser.error(
                "--yes is not accepted with --reconcile-bootstrap, --profile "
                "or --adopt: these modes confirm every entry interactively"
            )
        return _run_approve(args)
    if args.command == "recover":
        return _run_recover(args)
    if args.command == "review-contract":
        return _run_review_contract(args)
    if args.command in ("run", "loop"):
        return _run_orchestrator(args)
    if args.command == "tick":
        return _run_tick(args)
    if args.command == "trace":
        return _run_trace(args)
    if args.command == "status":
        return _run_status(args)
    if args.command == "dashboard":
        return _run_dashboard(args)
    if args.command == "sentinels":
        return _run_sentinels_replay(args)
    if args.command == "toolkit":
        return _run_toolkit_vet(args)
    if args.command == "eval":
        return _run_eval(args)
    if args.command == "import":
        return _run_import(args)
    if args.command == "doctor":
        return _run_doctor(args)
    print(
        f"factory {args.command}: not implemented yet (scheduled in {STUBS[args.command]})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
