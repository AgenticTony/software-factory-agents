"""F-100 test support: registry configs, profiles and scripted seats.

Fixture profiles and configs live in tmp paths; seats are scripted (a
capturing stub or the real ``run_session_sandboxed`` with the in-sandbox
entry stubbed, the F-56 pattern). No network, no paid call, no keychain
item: the zai keychain seam is patched per test.

Shapes only — no project's real content.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from factory.session import SessionRecord

__all__ = [
    "CAPTURE_CLI", "EntryStub", "ScriptedSeat", "client_profile",
    "config_path", "self_profile", "write_profile",
]

# The model ids the default registry ships (the documented example). A
# test builds variants by editing these names, never by inventing new
# families outside glm-/claude-.
GLM_ALIAS = "glm-latest"
GLM_ID = "glm-5.3"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-2"

STACKS_YAML = f"""\
models:
  aliases:
    {GLM_ALIAS}: {GLM_ID}
  providers:
    zai: {{origin: cn}}
    anthropic: {{origin: us}}
    bedrock: {{origin: us, contained: true}}
    vertex: {{origin: us}}
    anthropic_compatible: {{origin: unspecified}}
  stacks:
    standard:
      contained: false
      roles:
        implementer: {{kind: zai, model: {GLM_ALIAS}}}
        reviewer.conformance: {{kind: zai, model: {GLM_ALIAS}}}
        reviewer.hunt: {{kind: zai, model: {GLM_ALIAS}}}
        verifier: {{kind: zai, model: {GLM_ALIAS}}}
        importer: {{kind: zai, model: {GLM_ALIAS}}}
        final_review.fix: {{kind: zai, model: {GLM_ALIAS}}}
        final_review.strong: {{kind: zai, model: {GLM_ALIAS}}}
    standard-plus:
      contained: false
      roles:
        implementer: {{kind: zai, model: {GLM_ALIAS}}}
        reviewer.conformance: {{kind: zai, model: {GLM_ALIAS}}}
        reviewer.hunt: {{kind: anthropic, model: {SONNET}}}
        verifier: {{kind: zai, model: {GLM_ALIAS}}}
        importer: {{kind: zai, model: {GLM_ALIAS}}}
        final_review.fix: {{kind: anthropic, model: {OPUS}}}
        final_review.strong: {{kind: anthropic, model: {FABLE}}}
    no-china:
      contained: false
      roles:
        implementer: {{kind: anthropic, model: {OPUS}}}
        reviewer.conformance: {{kind: anthropic, model: {SONNET}}}
        reviewer.hunt: {{kind: anthropic, model: {OPUS}}}
        verifier: {{kind: anthropic, model: {SONNET}}}
        importer: {{kind: anthropic, model: {OPUS}}}
        final_review.fix: {{kind: anthropic, model: {OPUS}}}
        final_review.strong: {{kind: anthropic, model: {FABLE}}}
    contained:
      contained: true
      roles:
        implementer: {{kind: bedrock, model: {OPUS}}}
        reviewer.conformance: {{kind: bedrock, model: {SONNET}}}
        reviewer.hunt: {{kind: bedrock, model: {OPUS}}}
        verifier: {{kind: bedrock, model: {SONNET}}}
        importer: {{kind: bedrock, model: {SONNET}}}
        final_review.fix: {{kind: bedrock, model: {OPUS}}}
        final_review.strong: {{kind: bedrock, model: {FABLE}}}
  prices:
    anthropic:
      {SONNET}: {{input: 3.0, cache_read: 0.3, cache_write: 3.75, output: 15.0}}
    bedrock:
      {SONNET}: {{input: 3.0, cache_read: 0.3, cache_write: 3.75, output: 15.0}}
"""

# A client's recorded restriction answer (F-63 security content shape).
def _restrictions(value: str, **extra) -> dict:
    block = {"value": value, "answered_by": "owner",
             "answered_at": "2026-11-19T08:00:00Z"}
    block.update(extra)
    return block


def self_profile(tmp_path: Path, name: str = "selfproj", **extra) -> dict:
    """A minimal loadable SELF profile (the F-38 fixture shape)."""
    profile = {
        "name": name,
        "repo": {
            "path": str(tmp_path / "repo"),
            "default_branch": "main",
            "remote": str(tmp_path / "remote.git"),
            "merge_method": "squash",
        },
        "commands": {"setup": "true", "test": "python -m pytest -q"},
        "paths": {"risk": {}, "protected": [".factory/**"]},
        "dependency_manifests": [],
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
    }
    profile.update(extra)
    return profile


def client_profile(tmp_path: Path, name: str = "clientproj",
                   restrictions: dict | None = None, **extra) -> dict:
    """A minimal loadable CLIENT profile: a recorded restriction answer
    by default (``none`` — clients are open by default, F-100 AC-3)."""
    profile = self_profile(tmp_path, name=name, data_owner="client")
    profile["model_restrictions"] = (
        restrictions if restrictions is not None
        else _restrictions("none"))
    profile.update(extra)
    return profile


def write_profile(tmp_path: Path, profile: dict) -> Path:
    path = tmp_path / ".factory" / "project.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(profile, sort_keys=False))
    return path


def config_path(tmp_path: Path, models_yaml: str = STACKS_YAML) -> Path:
    """Write a factory config carrying the models block; the returned
    path is what ``load_config`` reads."""
    path = tmp_path / "config.yaml"
    path.write_text(models_yaml)
    return path


class ScriptedSeat:
    """The capturing seat stub: records (seat, prompt, run_dir, kwargs)
    per call and returns a completed record. ``result``/``status``/
    ``reason`` shape the record; ``model`` is what the session REPORTED
    (the API's model, not the parent's resolution)."""

    def __init__(self, result: str = "ok", *, status: str = "completed",
                 reason: str | None = None, model: str | None = "stub-model"):
        self.result = result
        self.status = status
        self.reason = reason
        self.model = model
        self.calls: list[tuple[str, str, Path, dict]] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        self.calls.append((seat, prompt, Path(run_dir), dict(kwargs)))
        return SessionRecord(
            seat=seat, status=self.status, reason=self.reason,
            num_turns=2, model=self.model,
            tokens={"input_tokens": 10, "output_tokens": 2},
            usage={"input": 10, "cache_read": 0, "cache_write": 0,
                   "output": 2},
            result=self.result if self.status == "completed" else None,
        )

    def kwargs_at(self, index: int) -> dict:
        return self.calls[index][3]


class EntryStub:
    """The F-56 pattern, local: stub ``run_sandboxed`` as the in-sandbox
    entry so the REAL ``run_session_sandboxed`` path runs — the spec is
    staged, the record read back and re-stamped by the parent. The
    planted zai keychain item keeps the credential seam hermetic."""

    def __init__(self, monkeypatch):
        from factory import session as session_mod

        monkeypatch.setattr(
            "factory.provider._keychain_token",
            lambda service: "tok" if service.startswith("factory-")
            else None)
        self.spec: dict = {}
        self.record_path: Path | None = None
        self.reported_model: str | None = "reported-by-api"

        def fake_run_sandboxed(cmd, run_dir, settings_path, **kwargs):
            spec = json.loads(Path(cmd[-1]).read_text())
            self.spec = spec
            record_path = (Path(settings_path).parent
                           / (Path(settings_path).name + "-record")
                           / "record.json")
            self.record_path = record_path
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps(SessionRecord(
                seat=spec["seat"], status="completed",
                model=self.reported_model,
                result="entry half").to_dict()))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(session_mod, "run_sandboxed", fake_run_sandboxed)


# A fake "claude" CLI the sandboxed entry can point at (cli_path override
# shape); the EntryStub replaces the whole sandboxed leg, so this exists
# only for tests that assert on the staged spec instead.
CAPTURE_CLI = "#!/bin/sh\nexit 0\n"
