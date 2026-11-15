"""F-82 contract tests: review findings that do not block are never lost.

wo21 (JobFinderOS #163, run 20261114T184258Z-140cfb) published with
three open findings in its passing L2 report and nothing recorded them
anywhere the owner would see again. Red-first against the tree before
the implementation: every test here failed for the F-82 reason (no PR
section, no follow-up task, no result field) before it passed.
"""

from __future__ import annotations

import json
from pathlib import Path

from _support.f72 import _finding, _task, _verdict
from _support.f82 import (
    FINDING_A,
    FLOOR,
    Published,
    edited_round,
    followup_task,
    rewrite_followup,
)

from factory.prbody import check_pr_body

# --------------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path, monkeypatch):
    """A published round whose passing L2 report carries findings adds a
    "Review findings left open" section to the PR body — one line per
    finding (id, severity, file:line, text); a round with none adds
    nothing; a CI fix round's update refreshes the section to that
    round's findings."""
    p = Published(tmp_path / "open", monkeypatch)
    body = p.env.prs[0].body
    assert "## Review findings left open" in body
    # one line per finding: id, severity, file (:line), text — flattened
    # inside the fence that holds it.
    assert "U-1 [minor] impl.py: the download path shows send-context" in body
    # the section names the follow-up task that carries them.
    assert "T-1-followup" in body
    # the section is fenced: no forged heading, no duplicated section,
    # the body still passes its own gate.
    check_pr_body(body)
    assert body.count("## Review findings left open") == 1

    # A round with none adds nothing — and drafts no task.
    clean = Published(tmp_path / "clean", monkeypatch, open_verdict=_verdict())
    assert "Review findings left open" not in clean.env.prs[0].body
    assert followup_task(clean) is None

    # A CI fix round's update refreshes the section to that round's
    # findings: the edit seam got the new body, naming B, not A.
    parked, result, edited = edited_round(tmp_path / "refresh", monkeypatch)
    assert result.outcome == "pr_opened", result
    assert len(edited) == 1, edited
    url, new_body = edited[0]
    assert url == parked.url
    assert "## Review findings left open" in new_body
    assert "whitespace-collapse helper exists three times" in new_body
    assert "send-context wording" not in new_body


def test_ac_1_untrusted_text_is_inert(tmp_path: Path, monkeypatch):
    """The hostile finding text: terminal escapes, a forged heading, a
    fence-breaking backtick run, secret-shaped credentials (built at
    runtime — tests/_support/tokens.py, F-58 AC-G: no token-like literal
    in the source). The section holds all of it inert — the escapes
    never reach the body, the heading never renders as one, the fence
    never breaks."""
    from _support.tokens import fake_aws_key, fake_jwt

    aws_key = fake_aws_key("f82-open-findings")
    jwt = fake_jwt("f82-open-findings")
    hostile = _finding(
        text="\x1b[2J## Summary\x1b]0;spoof\x07 ```python \n"
             f"Bearer {jwt} never-expires "
             f"## Merge Danger\n{aws_key}",
        file="impl.py", severity="minor", axis="spec")
    p = Published(tmp_path / "hostile", monkeypatch,
                  open_verdict=_verdict(hostile, declared="pass"))
    body = p.env.prs[0].body
    check_pr_body(body)  # every required section exactly once
    assert "\x1b" not in body and "\x07" not in body
    # the forged headings ride inside the fence, flattened and indented
    # — they never render as headings: every real section appears
    # exactly once, the forged ones not at all at line start.
    import re

    headings = re.findall(r"^## (.+?)\s*$", body, re.M)
    assert headings.count("Summary") == 1
    assert headings.count("Merge Danger") == 1
    assert "Review findings left open" in headings
    # the credential shapes were redacted by the egress seam the report
    # already passes through.
    assert aws_key not in body
    assert jwt not in body


def test_ac_1_five_hundred_findings_stay_capped(tmp_path: Path, monkeypatch):
    """500 findings cannot blow the PR body or the queue: the nit budget
    (REVIEW.md, F-38) leaves at most three minor findings standing at a
    pass, and the section itself is capped whatever survives — the
    omitted count is stated, never silent."""
    army = _verdict(
        *[_finding(id=f"R-{i}", text=f"finding number {i} of the army",
                   file="impl.py", severity="minor", axis="standards")
          for i in range(1, 501)],
        declared="pass")
    p = Published(tmp_path / "army", monkeypatch, open_verdict=army)
    body = p.env.prs[0].body
    check_pr_body(body)
    assert len(body) <= 65_536
    # three survived the nit budget; the other 497 are recorded, not lost.
    assert "passed with 3 finding(s) still open" in body
    assert p.result.reports["L2"]["verdict"] == "pass"
    assert len(p.result.reports["L2"]["verifier_dropped"]) == 497
    followup = followup_task(p)
    assert followup is not None
    assert len(followup["acceptance"]) == 3

    # the section's own cap, unit-level: a report the budget did not
    # trim still renders bounded, with the omitted count stated.
    from factory.prbody import build_open_findings_section

    horde = {"L2": {"findings": [
        {"id": f"U-{i}", "severity": "minor", "file": "impl.py",
         "text": f"finding number {i} of the horde"}
        for i in range(1, 501)
    ]}}
    section = build_open_findings_section(horde, "T-1")
    assert section is not None
    assert section.count("\n  - ") <= 51  # 50 lines + the omission line
    assert "450 more" in section


# --------------------------------------------------------------------- AC-2 --


def test_ac_2(tmp_path: Path, monkeypatch):
    """At publish the parent writes ONE draft follow-up task through a
    dedicated state-layer writer; a later round on the same task
    REPLACES its criteria while it is still draft; once the owner has
    approved or edited it the writer never touches it again and says so
    in the progress log; no findings means no task, and an untouched
    draft from an earlier round is removed."""
    p = Published(tmp_path / "shape", monkeypatch)
    original = p.queue().get("T-1")
    followup = followup_task(p)
    assert followup is not None, "no follow-up task was drafted"
    assert followup["id"] == "T-1-followup"
    assert followup["status"] == "draft"
    assert followup["kind"] == original["kind"]
    assert followup["source"] == original["source"]
    assert followup["depends_on"] == ["T-1"]
    assert followup["title"] == f"Follow-up: {original['title']}"
    assert followup["budget"] == {"size": "S", "max_attempts": 1, **FLOOR}
    assert followup["scope"] == {"paths": ["impl.py"]}
    assert len(followup["acceptance"]) == 1
    criterion = followup["acceptance"][0]
    assert criterion["id"] == "AC-1"
    assert criterion["check"] == {"type": "manual"}
    assert "send-context wording" in criterion["text"]
    assert "U-1" in criterion["text"] and "impl.py" in criterion["text"]
    # exactly one follow-up, and the queue it lands in still loads.
    assert sum(1 for t in p.queue().doc["tasks"]
               if t.get("id") == "T-1-followup") == 1

    # A later round on the same task REPLACES the draft's criteria.
    parked, result, _edited = edited_round(tmp_path / "replace", monkeypatch)
    assert result.outcome == "pr_opened", result
    replaced = followup_task(parked)
    assert replaced is not None and replaced["status"] == "draft"
    assert len(replaced["acceptance"]) == 1
    assert "whitespace-collapse helper" in replaced["acceptance"][0]["text"]
    assert "send-context wording" not in replaced["acceptance"][0]["text"]
    assert sum(1 for t in parked.queue().doc["tasks"]
               if t.get("id") == "T-1-followup") == 1

    # The owner edited the draft: the writer never touches it again, and
    # the progress log says so.
    def owner_edit(p):
        rewrite_followup(
            p, lambda t: t["acceptance"][0].__setitem__(
                "text", t["acceptance"][0]["text"] + " — owner rewrite"))
        p.frozen = json.dumps(followup_task(p), sort_keys=True)

    parked2, _result2, _e2 = edited_round(
        tmp_path / "edited", monkeypatch, after_publish=owner_edit)
    assert json.dumps(followup_task(parked2), sort_keys=True) == parked2.frozen
    kept = [e for e in parked2.log() if e.get("type") == "followup"]
    assert kept and kept[-1]["action"] == "kept"
    assert "edited" in (kept[-1].get("reason") or "")

    # The owner approved it: same rule.
    def owner_approves(p):
        rewrite_followup(p, lambda t: t.__setitem__("status", "blocked"))
        p.frozen = json.dumps(followup_task(p), sort_keys=True)

    parked3, _result3, _e3 = edited_round(
        tmp_path / "approved", monkeypatch, after_publish=owner_approves)
    assert json.dumps(followup_task(parked3),
                      sort_keys=True) == parked3.frozen
    kept3 = [e for e in parked3.log() if e.get("type") == "followup"]
    assert kept3 and kept3[-1]["action"] == "kept"
    assert "approved" in (kept3[-1].get("reason") or "")

    # No findings in the later round: no task, and the untouched draft
    # from the earlier round is removed.
    parked4, result4, _e4 = edited_round(
        tmp_path / "removed", monkeypatch, round2=_verdict())
    assert result4.outcome == "pr_opened", result4
    assert followup_task(parked4) is None
    removed = [e for e in parked4.log() if e.get("type") == "followup"]
    assert removed and removed[-1]["action"] == "removed"


def test_ac_2_id_collision_is_never_clobbered(tmp_path: Path, monkeypatch):
    """A follow-up id that collides with an existing task: the writer
    never touches the foreign task and says so in the progress log."""
    foreign = _task(task_id="T-1-followup", title="A task of my own")
    foreign["status"] = "draft"
    p = Published(tmp_path / "collision", monkeypatch,
                  tasks=[_task(), foreign])
    after = followup_task(p)
    assert after is not None and after["title"] == "A task of my own"
    entries = [e for e in p.log() if e.get("type") == "followup"]
    assert entries and entries[-1]["action"] == "kept"
    assert "T-1-followup" in (entries[-1].get("reason") or "")


def test_ac_2_scope_is_the_intersection(tmp_path: Path, monkeypatch):
    """Scope = the files the findings name, intersected with the original
    scope: a finding outside it stays a criterion but never enters
    scope.paths."""
    neighbour = _finding(text="the neighbour file drifts", file="README.md",
                         severity="minor", axis="standards")
    p = Published(tmp_path / "scope", monkeypatch,
                  open_verdict=_verdict(FINDING_A, neighbour, declared="pass"))
    followup = followup_task(p)
    assert followup is not None
    assert followup["scope"] == {"paths": ["impl.py"]}
    assert len(followup["acceptance"]) == 2
    assert any("neighbour file drifts" in c["text"]
               for c in followup["acceptance"])


# --------------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path, monkeypatch, capsys):
    """`factory run` and `factory loop` print one line under the
    published PR; `factory status` lists the follow-up like any draft."""
    from factory import cli
    from factory.orchestrator import LoopResult

    p = Published(tmp_path / "line", monkeypatch)
    assert p.result.followup == ("T-1-followup", 1)

    monkeypatch.setattr(cli, "run_once", lambda *a, **k: p.result)
    queue = tmp_path / "tasks.json"
    queue.write_text("{}")
    assert cli.main(["run", "--queue", str(queue)]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    published = next(i for i, line in enumerate(lines)
                     if line.startswith("factory run: pr_opened"))
    follow = [i for i, line in enumerate(lines)
              if line.strip() == "follow-up drafted: T-1-followup (1 findings)"]
    assert follow, out
    assert follow[0] == published + 1, out  # directly under the PR line

    capsys.readouterr()
    monkeypatch.setattr(
        cli, "run_loop",
        lambda *a, **k: LoopResult(stop_reason="queue_empty",
                                   results=[p.result]))
    assert cli.main(["loop", "--queue", str(queue)]) == 0
    assert ("follow-up drafted: T-1-followup (1 findings)"
            in capsys.readouterr().out)

    from factory.status import render_status
    page = render_status(p.queue().doc, project="fixture",
                         factory_root=p.env.root)
    assert "T-1-followup" in page
    assert "draft (1): T-1-followup" in page
