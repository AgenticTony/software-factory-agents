"""F-100 acceptance tests — one model registry, per-project stacks.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f100.py::test_ac_N``
means a test function with exactly that name.

The goal-level core (BUILD-ORDER §F-100): every model choice is made in
one owner-maintained registry; each seat asks it by role; each project
names a stack; every run records what actually ran. The costlier failure
is a client project silently running a model or provider it did not
approve because a shared stack was edited — so the project's approved
``providers_allowed`` and its recorded restriction answer stay the
authority, and a stack edit can never move a project outside them.

Scripted seats, fixture profiles and configs in tmp paths. No network,
never a paid call, never a real keychain item (the keychain seam is
patched per test).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from _support.f12 import _git_repo
from _support.f100 import (
    GLM_ALIAS,
    GLM_ID,
    OPUS,
    SONNET,
    STACKS_YAML,
    EntryStub,
    ScriptedSeat,
    client_profile,
    config_path,
    self_profile,
    write_profile,
)

from factory.config import ConfigError, FactoryConfig, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent

# A self project that may run every kind the standard stack names.
SELF_ALLOWED = {"providers_allowed": ["anthropic"], "model_stack": "standard"}


def _models():
    from factory.models import (
        DEFAULT_REGISTRY,
        MODEL_ROLES,
        REQUIRED_STACK_ROLES,
    )
    return DEFAULT_REGISTRY, MODEL_ROLES, REQUIRED_STACK_ROLES


def _resolve(profile, config, role):
    from factory.models import resolve_role
    return resolve_role(profile, config, role)


# --- AC-1: one registry ------------------------------------------------------


def test_ac_1(tmp_path: Path):
    """The ``models`` block: aliases, stacks (contained marked), provider
    origins and prices; a malformed registry refuses with the reason
    named; the top-level ``prices`` block keeps loading for one release;
    aliases expand fully."""
    from factory.models import RegistryError, expand_alias, model_family

    # (a) The documented default registry ships with the factory config:
    # three stacks, glm-latest, the provider origins.
    registry, roles, required = _models()
    assert "standard" in registry.stacks
    assert "no-china" in registry.stacks
    assert "contained" in registry.stacks
    assert registry.stacks["contained"].contained is True
    assert registry.stacks["standard"].contained is False
    assert registry.providers["zai"] == "cn"
    assert set(required) <= set(roles)
    assert "reviewer.hunt" in roles and "final_review.strong" in roles
    # The workhorse alias resolves to the newest GLM.
    assert expand_alias(registry, GLM_ALIAS) == GLM_ID
    assert model_family(GLM_ID) == "zai"
    assert model_family(SONNET) == "anthropic"
    assert model_family("mistral-large") is None

    # (b) A well-formed file loads into FactoryConfig.models and its
    # prices land where cost reads them.
    cfg = load_config(config_path(tmp_path))
    assert isinstance(cfg, FactoryConfig)
    assert cfg.models.stacks["standard"].roles["implementer"].kind == "zai"
    assert cfg.prices["anthropic"][SONNET]["input"] == 3.0

    # (c) Each malformed shape refuses with the reason named.
    def refuses(models_yaml: str, reason: str) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(models_yaml)
        with pytest.raises(ConfigError, match=reason):
            load_config(path)

    base = json.dumps(yaml.safe_load(STACKS_YAML)["models"])

    def variant(mutate) -> str:
        doc = json.loads(base)
        mutate(doc)
        return yaml.safe_dump({"models": doc})

    # unknown role (the misspelt lens)
    refuses(variant(lambda d: d["stacks"]["standard"]["roles"].update(
        {"reviewer.hunter": {"kind": "zai", "model": GLM_ALIAS}})),
        "reviewer.hunter")
    # alias cycle
    refuses(variant(lambda d: d["aliases"].update(
        {GLM_ALIAS: "glm-tip", "glm-tip": GLM_ALIAS})), "cycle")
    # dangling alias: a stack names an alias nobody declares
    refuses(variant(lambda d: d["stacks"]["standard"]["roles"].update(
        {"implementer": {"kind": "zai", "model": "glm-newest"}})),
        "glm-newest")
    # an alias chain that never ends at a model id — the refusal names
    # the dangling END of the chain
    refuses(variant(lambda d: d["aliases"].update(
        {GLM_ALIAS: "glm-tip", "glm-tip": "glm-tail"})), "glm-tail")
    # unknown provider kind in a stack
    refuses(variant(lambda d: d["stacks"]["standard"]["roles"].update(
        {"verifier": {"kind": "grok", "model": "grok-4"}})), "grok")
    # unknown provider kind in the providers block
    refuses(variant(lambda d: d["providers"].update(
        {"grok": {"origin": "us"}})), "grok")
    # a stack missing a required role
    refuses(variant(lambda d: d["stacks"]["standard"]["roles"].pop(
        "verifier")), "verifier")
    # an alias pointing a zai role at another provider's model family
    refuses(variant(lambda d: d["aliases"].update(
        {GLM_ALIAS: SONNET})), "another provider")

    # (d) The top-level prices block keeps loading for one release; both
    # blocks at once refuse rather than pick silently.
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(yaml.safe_dump({
        "prices": {"bedrock": {"sonnet": {
            "input": 3.0, "cache_read": 0.3, "cache_write": 3.75,
            "output": 15.0}}}}))
    assert load_config(legacy).prices["bedrock"]["sonnet"]["input"] == 3.0
    both = tmp_path / "both.yaml"
    doc = yaml.safe_load(STACKS_YAML)
    doc["prices"] = {"anthropic": {"other": {
        "input": 1.0, "cache_read": 0.1, "cache_write": 1.0, "output": 5.0}}}
    both.write_text(yaml.safe_dump(doc))
    with pytest.raises(ConfigError, match="prices"):
        load_config(both)

    # (e) Aliases expand through a chain to the final id.
    chained = tmp_path / "chain.yaml"
    doc = yaml.safe_load(STACKS_YAML)
    doc["models"]["aliases"] = {GLM_ALIAS: "glm-tip",
                                "glm-tip": GLM_ID}
    chained.write_text(yaml.safe_dump(doc))
    registry = load_config(chained).models
    assert expand_alias(registry, GLM_ALIAS) == GLM_ID
    with pytest.raises(RegistryError):
        expand_alias(registry, "glm-future")  # family-shaped, undeclared


# --- AC-2: roles fine enough to swap any area on its own --------------------


def test_ac_2(tmp_path: Path, monkeypatch):
    """Every seat resolves through ONE resolver by role; reviewer seats
    map by lens; arbiter and contract reviewer default to conformance;
    no model id hides in factory code outside the registry defaults."""
    from factory.models import (
        model_ids_outside_registry,
        reviewer_seat_role,
        seat_role,
    )
    from factory.session import run_sessions_parallel

    cfg = load_config(config_path(tmp_path))
    profile = self_profile(tmp_path, **SELF_ALLOWED)

    # (a) A two-seat review resolves seat 1 and seat 2 to different
    # models when the stack says so — through standard-plus (owner
    # decision 2026-11-19: standard itself is zai-only; the cross-family
    # hunt is the stack a project OPTS INTO).
    assert reviewer_seat_role(1, 2) == "reviewer.conformance"
    assert reviewer_seat_role(2, 2) == "reviewer.hunt"
    plus = self_profile(tmp_path, model_stack="standard-plus",
                        providers_allowed=["anthropic"])
    conformance = _resolve(plus, cfg, "reviewer.conformance")
    hunt = _resolve(plus, cfg, "reviewer.hunt")
    assert conformance.kind == "zai" and conformance.model == GLM_ID
    assert hunt.kind == "anthropic" and hunt.model == SONNET
    # A one-seat fan-out is the both-lens seat: the conformance role.
    assert reviewer_seat_role(1, 1) == "reviewer.conformance"
    # Seat names map to their default roles.
    assert seat_role("reviewer") == "reviewer.conformance"
    assert seat_role("implementer") == "implementer"

    # (b) The verifier, arbiter, contract reviewer and importer each
    # resolve from the stack; arbiter and contract reviewer default to
    # reviewer.conformance when the stack leaves them out.
    assert "arbiter" not in cfg.models.stacks["standard"].roles
    verifier = _resolve(profile, cfg, "verifier")
    arbiter = _resolve(profile, cfg, "arbiter")
    contract = _resolve(profile, cfg, "contract_reviewer")
    importer = _resolve(profile, cfg, "importer")
    assert (verifier.kind, verifier.model) == ("zai", GLM_ID)
    for seat in (arbiter, contract):
        assert (seat.kind, seat.model) == (conformance.kind, conformance.model)
    assert (importer.kind, importer.model) == ("zai", GLM_ID)
    # Resolved specs name the role and the stack they came from (AC-4's
    # facts ride the same resolution).
    std_conformance = _resolve(profile, cfg, "reviewer.conformance")
    assert std_conformance.role == "reviewer.conformance"
    assert std_conformance.stack == "standard"
    assert conformance.stack == "standard-plus"

    # (c) ONE resolver: the fan-out threads each seat's role and the
    # config to the session seam — captured in the scripted seats' kwargs.
    repo = _git_repo(tmp_path / "fanout")
    seat = ScriptedSeat()
    records = run_sessions_parallel(
        [("reviewer", "p1"), ("reviewer", "p2")],
        repo, profile, runner=seat, config=cfg,
        seat_roles=["reviewer.conformance", "reviewer.hunt"],
        max_turns=1, timeout_min=1,
    )
    assert all(r.status == "completed" for r in records)
    assert seat.kwargs_at(0)["role"] == "reviewer.conformance"
    assert seat.kwargs_at(1)["role"] == "reviewer.hunt"
    assert seat.kwargs_at(0)["config"] is cfg

    # (d) The grep: no model id anywhere under factory/ outside the
    # registry defaults — and the checker itself flags a planted id.
    assert model_ids_outside_registry(REPO_ROOT / "factory") == []
    planted = tmp_path / "factory"
    planted.mkdir()
    (planted / "helper.py").write_text(
        'MODEL = "glm-4-0"  # the silent hard-code\n')
    assert model_ids_outside_registry(planted) == ["glm-4-0"]


# --- AC-3: a project names its stack, inside what it approved ---------------


def test_ac_3(tmp_path: Path):
    """Selection and precedence, the providers_allowed guard, the
    restriction answers, and the F-59 client/zai refusal they replace."""
    from factory.approvals import ApprovalLedger, changed_groups
    from factory.contract import ContractError, load_profile
    from factory.models import ProviderError as ModelsProviderError
    from factory.models import check_profile_stack
    from factory.provider import ProviderError

    cfg = load_config(config_path(tmp_path))

    # (a) An alias bump moves an approved standard project's implementer
    # to the new id with NO re-approval: the profile file and its
    # approval hashes do not change, only the registry does.
    profile = self_profile(tmp_path, **SELF_ALLOWED)
    path = write_profile(tmp_path, profile)
    before = _resolve(load_profile(path), cfg, "implementer")
    assert before.model == GLM_ID
    ledger = ApprovalLedger.for_project(tmp_path / "ledger", "selfproj")
    ledger.record_profile_for(load_profile(path))
    bumped = tmp_path / "bumped.yaml"
    doc = yaml.safe_load(STACKS_YAML)
    doc["models"]["aliases"] = {GLM_ALIAS: "glm-5.4"}
    bumped.write_text(yaml.safe_dump(doc))
    cfg_bumped = load_config(bumped)
    after = _resolve(load_profile(path), cfg_bumped, "implementer")
    assert after.model == "glm-5.4"
    assert changed_groups(load_profile(path),
                          ledger.latest_profile_approval()) == []

    # (b) The guard: a role whose kind is neither the project's own nor
    # in providers_allowed refuses at load, naming project, role, kind —
    # never falling back. Round 2 (owner decision 2026-11-19): standard
    # is zai-only, so the refusing shape is standard-PLUS on a project
    # that allows nothing beyond its own zai.
    narrow = self_profile(tmp_path / "narrow", model_stack="standard-plus")
    narrow_path = write_profile(tmp_path / "narrow", narrow)
    with pytest.raises(ContractError, match="selfproj.*reviewer.hunt.*anthropic"):
        load_profile(narrow_path, config=cfg)
    with pytest.raises((ProviderError, ModelsProviderError),
                       match="reviewer.hunt"):
        _resolve(narrow, cfg, "reviewer.hunt")
    with pytest.raises((ContractError, ProviderError)):
        check_profile_stack(narrow, cfg)  # the sweep refuses the same way

    # (b2) Round 2's other side: a profile shaped like the real ones
    # (own kind zai, providers_allowed [], no model_stack) loads and
    # runs exactly as before under the DEFAULT registry — every role
    # zai, no new credential — because standard puts EVERY role on
    # zai/glm-latest. standard-plus is the opt-in a project reaches
    # only through model_stack (re-approval) plus an allowed anthropic.
    plain = self_profile(tmp_path / "plain")
    check_profile_stack(plain, FactoryConfig())
    plain_loaded = load_profile(write_profile(tmp_path / "plain", plain),
                                config=FactoryConfig())
    assert plain_loaded["name"] == "selfproj"
    for role in ("implementer", "reviewer.conformance", "reviewer.hunt",
                 "verifier", "importer", "final_review.fix",
                 "final_review.strong"):
        assert _resolve(plain, FactoryConfig(), role).kind == "zai", role

    # (c) Precedence: a project-level seat_providers entry wins for its
    # role; an F-59 provider.models block still works for one release.
    pinned = self_profile(
        tmp_path / "pinned",
        **SELF_ALLOWED,
        seat_providers={"reviewer": {"kind": "anthropic",
                                     "model": "m-r"}})
    both_seats = _resolve(pinned, cfg, "reviewer.conformance")
    assert (both_seats.kind, both_seats.model) == ("anthropic", "m-r")
    both_seats_hunt = _resolve(pinned, cfg, "reviewer.hunt")
    assert both_seats_hunt.model == "m-r"  # the seat pin beats the stack
    legacy = self_profile(
        tmp_path / "legacy",
        providers_allowed=["anthropic"],
        provider={"kind": "zai", "models": {
            "implementer": "model-impl", "reviewer": "model-rev",
            "verifier": "model-ver", "importer": "model-imp"}})
    legacy_impl = _resolve(legacy, cfg, "implementer")
    assert (legacy_impl.kind, legacy_impl.model) == ("zai", "model-impl")
    legacy_rev = _resolve(legacy, cfg, "reviewer.conformance")
    assert (legacy_rev.kind, legacy_rev.model) == ("zai", "model-rev")

    # (d) Clients are open by default: a recorded ``none`` loads on
    # standard (GLM included) — F-59's blanket client/zai refusal is
    # replaced by the recorded answer.
    open_client = client_profile(tmp_path / "open",
                                 providers_allowed=["anthropic"])
    open_path = write_profile(tmp_path / "open", open_client)
    loaded = load_profile(open_path, config=cfg)
    assert loaded["model_restrictions"]["value"] == "none"
    impl = _resolve(open_client, cfg, "implementer")
    assert impl.kind == "zai"  # the replaced refusal: client + zai loads
    zai_allowed = client_profile(
        tmp_path / "zaiallowed", providers_allowed=["anthropic", "zai"])
    zai_path = write_profile(tmp_path / "zaiallowed", zai_allowed)
    assert load_profile(zai_path, config=cfg)["name"] == "clientproj"

    # (e) no_china refuses any role whose provider the registry marks
    # origin cn — a no_china client pinned to standard refuses, naming
    # the restriction. (With no model_stack the stack DERIVES from the
    # answer — no-china — by the spec's "standard unless restricted";
    # the refusal arm is the explicit conflict.)
    no_china = client_profile(
        tmp_path / "nochina", model_stack="standard",
        providers_allowed=["anthropic", "zai"],
        restrictions={"value": "no_china", "answered_by": "owner",
                      "answered_at": "2026-11-19T08:00:00Z"})
    no_china_path = write_profile(tmp_path / "nochina", no_china)
    with pytest.raises(ContractError, match="no_china"):
        load_profile(no_china_path, config=cfg)
    with pytest.raises((ProviderError, ModelsProviderError), match="no_china"):
        _resolve(no_china, cfg, "implementer")
    # On the no-china stack the same project loads and resolves.
    no_china["model_stack"] = "no-china"
    no_china_path = write_profile(tmp_path / "nochina", no_china)
    assert load_profile(no_china_path, config=cfg)["name"] == "clientproj"
    assert _resolve(no_china, cfg, "implementer").kind == "anthropic"
    # A non-zai provider the registry marks origin cn refuses the same
    # way (the rule reads the registry, not the kind name).
    hostile = tmp_path / "hostile.yaml"
    doc = yaml.safe_load(STACKS_YAML)
    doc["models"]["providers"]["anthropic"] = {"origin": "cn"}
    hostile.write_text(yaml.safe_dump(doc))
    with pytest.raises((ProviderError, ModelsProviderError), match="no_china"):
        _resolve(no_china, load_config(hostile), "implementer")

    # (e2) R1, fail closed: no_china refuses a kind whose origin is not
    # explicitly non-cn — "unspecified" or a missing registry entry —
    # and a base_url on any zai-family host, whatever the kind. The
    # reviewer's probe: an anthropic_compatible pin on the Z.ai endpoint
    # under a no_china answer (the load sweep must refuse it too, before
    # any session).
    zai_pin = client_profile(
        tmp_path / "zaipin", model_stack="no-china",
        providers_allowed=["anthropic", "anthropic_compatible"],
        restrictions={"value": "no_china", "answered_by": "owner",
                      "answered_at": "2026-11-19T08:00:00Z"},
        seat_providers={"implementer": {
            "kind": "anthropic_compatible", "model": GLM_ID,
            "base_url": "https://api.z.ai/api/anthropic"}})
    with pytest.raises((ProviderError, ModelsProviderError),
                       match="no_china"):
        _resolve(zai_pin, cfg, "implementer")
    zai_pin_path = write_profile(tmp_path / "zaipin", zai_pin)
    # At load the profile refuses BEFORE the sweep even reaches the
    # restriction: F-59's own host rule names the zai-owned base_url.
    # Either named refusal is the fail-closed answer.
    with pytest.raises(ContractError, match="no_china|zai-owned"):
        load_profile(zai_pin_path, config=cfg)
    # The origin rule itself, without the Z.ai host: an
    # anthropic_compatible pin on a neutral endpoint still refuses —
    # its origin is "unspecified", not explicitly non-cn.
    neutral_pin = client_profile(
        tmp_path / "neutralpin", model_stack="no-china",
        providers_allowed=["anthropic", "anthropic_compatible"],
        restrictions={"value": "no_china", "answered_by": "owner",
                      "answered_at": "2026-11-19T08:00:00Z"},
        seat_providers={"implementer": {
            "kind": "anthropic_compatible", "model": "self-hosted-1",
            "base_url": "https://eu-model-host.example/v1"}})
    with pytest.raises((ProviderError, ModelsProviderError),
                       match="no_china|origin"):
        _resolve(neutral_pin, cfg, "implementer")
    # A registry with NO entry for the kind refuses the same way
    # (missing origin is not proof of non-cn).
    stripped = tmp_path / "stripped.yaml"
    doc = yaml.safe_load(STACKS_YAML)
    doc["models"]["providers"].pop("anthropic_compatible")
    stripped.write_text(yaml.safe_dump(doc))
    with pytest.raises((ProviderError, ModelsProviderError),
                       match="no_china|origin"):
        _resolve(neutral_pin, load_config(stripped), "implementer")

    # (f) contained refuses any stack not marked contained (the explicit
    # conflict; absent model_stack derives the contained stack).
    contained = client_profile(
        tmp_path / "cont", model_stack="standard",
        providers_allowed=["anthropic"],
        restrictions={"value": "contained", "answered_by": "owner",
                      "answered_at": "2026-11-19T08:00:00Z"})
    with pytest.raises((ProviderError, ModelsProviderError), match="contained"):
        check_profile_stack(contained, cfg)
    contained["model_stack"] = "contained"
    contained["provider"] = {"kind": "bedrock", "region": "eu-north-1"}
    check_profile_stack(contained, cfg)
    bedrock_impl = _resolve(contained, cfg, "implementer")
    assert (bedrock_impl.kind, bedrock_impl.model) == ("bedrock", OPUS)
    assert bedrock_impl.region == "eu-north-1"

    # (f2) R2, fail closed: under contained, every role's RESOLVED kind
    # must be one the registry treats as contained — a seat pin on a
    # non-contained kind (anthropic, whatever its origin) refuses,
    # naming the role and the kind, at the seam AND at the load sweep.
    escaped = client_profile(
        tmp_path / "escaped", model_stack="contained",
        provider={"kind": "bedrock", "region": "eu-north-1"},
        providers_allowed=["anthropic", "bedrock"],
        restrictions={"value": "contained", "answered_by": "owner",
                      "answered_at": "2026-11-19T08:00:00Z"},
        seat_providers={"verifier": {"kind": "anthropic",
                                     "model": SONNET}})
    with pytest.raises((ProviderError, ModelsProviderError),
                       match="contained.*verifier|verifier.*contained"):
        _resolve(escaped, cfg, "verifier")
    escaped_path = write_profile(tmp_path / "escaped", escaped)
    with pytest.raises(ContractError, match="verifier"):
        load_profile(escaped_path, config=cfg)
    # The legitimate shapes still load: no_china on the no-china stack
    # (above) and contained on the contained stack with no pin (above).

    # (g) A client with no recorded answer refuses to load, naming the
    # missing answer — the question is always asked, never assumed.
    silent = self_profile(tmp_path / "silent", data_owner="client")
    silent_path = write_profile(tmp_path / "silent", silent)
    with pytest.raises(ContractError, match="model_restrictions"):
        load_profile(silent_path)
    with pytest.raises((ProviderError, ModelsProviderError),
                       match="model_restrictions"):
        _resolve(silent, cfg, "implementer")


# --- AC-4: what ran is on the record -----------------------------------------


def test_ac_4(tmp_path: Path, monkeypatch):
    """Each session's record and trace carry role, stack, requested model
    (alias expanded) and the API-reported model; a mismatch is recorded,
    not refused; status shows the stack and models per role; the PR body
    names the implementer's model."""
    from factory.runs import create_run
    from factory.session import run_session_sandboxed
    from factory.status import render_status

    cfg = load_config(config_path(tmp_path))
    profile = self_profile(
        tmp_path, providers_allowed=["anthropic"],
        model_stack="standard-plus")
    repo = _git_repo(tmp_path / "rec")
    run = create_run("fixture", repo, factory_root=tmp_path)

    # (a) The real sandboxed path: the staged spec and the returned
    # record both carry role, stack and the requested (alias-expanded)
    # model; the record also carries the model the API reported.
    stub = EntryStub(monkeypatch)
    record = run_session_sandboxed(
        "reviewer", "find defects", run.root, profile,
        config=cfg, role="reviewer.hunt",
        max_turns=1, timeout_min=1, factory_root=tmp_path,
    )
    assert record.status == "completed"
    assert stub.spec["role"] == "reviewer.hunt"
    assert stub.spec["stack"] == "standard-plus"
    assert stub.spec["model"] == SONNET
    assert stub.spec["provider"]["kind"] == "anthropic"
    assert record.provider["role"] == "reviewer.hunt"
    assert record.provider["stack"] == "standard-plus"
    assert record.provider["model"] == SONNET  # requested, alias expanded
    assert record.model == "reported-by-api"  # the API's answer
    # The mismatch is a fact on the record, never a refusal.
    assert record.provider["model"] != record.model

    # (b) factory status: the project's stack and the resolved model per
    # role, plus the recorded requested-vs-reported mismatch.
    legacy_attempt = {
        "run_id": "r", "slice_id": "T-1",
        "started": "2026-11-19T08:00:00Z", "outcome": "pass",
        "prompt_versions": {}, "skills": [],
        "provider": {"kind": "zai", "model": GLM_ID,
                     "role": "implementer", "stack": "standard"},
        "model": "implementer=glm-5.3-renamed",
    }
    queue = {"version": 1, "project": "selfproj", "tasks": [{
        "id": "T-1", "title": "t", "source": "docs/t.md", "kind": "build",
        "priority": "P1", "status": "done", "depends_on": [], "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [], "links": {},
        "budget": {"size": "M", "max_attempts": 3, "max_turns": 30,
                   "wall_clock_min": 60},
        "attempts": [legacy_attempt],
    }]}
    write_profile(tmp_path, profile)  # next to the queue, as status reads it
    page = render_status(queue, project="selfproj", factory_root=tmp_path,
                         repo=tmp_path, config=cfg, profile=profile)
    assert "standard" in page
    assert "reviewer.hunt" in page and SONNET in page
    assert GLM_ID in page
    assert "glm-5.3-renamed" in page  # the reported model is shown
    assert "mismatch" in page.lower()

    # A matching pair renders no mismatch line.
    legacy_attempt["model"] = f"implementer={GLM_ID}"
    page = render_status(queue, project="selfproj", factory_root=tmp_path,
                         repo=tmp_path, config=cfg, profile=profile)
    assert "mismatch" not in page.lower()

    # (c) The PR body names the implementer's model.
    from factory.prbody import build_pr_body, check_pr_body
    body = build_pr_body(
        {"id": "T-1", "title": "t", "source": "docs/t.md", "acceptance": [],
         "out_of_scope": [], "budget": {"max_attempts": 3, "max_turns": 30},
         "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
         "risk": []},
        profile,
        branch="b", base_sha="0" * 40, head_sha="1" * 40, changed_paths=[],
        reports={"L0": {}, "L1": {}, "L2": {}}, l1_report=None,
        attempt=1, implementer_model=GLM_ID,
    )
    check_pr_body(body)
    assert f"Implementer model: `{GLM_ID}`" in body


# --- AC-5: try a model before switching ---------------------------------------


def test_ac_5(tmp_path: Path, monkeypatch):
    """``factory eval --role <role>=<model-or-alias>``: that eval only,
    never written anywhere, named in the report, still under the AC-3
    guard."""
    import dataclasses

    from factory.eval import run_on_demand_eval, run_seed, seed_by_id
    from factory.models import MODEL_ROLES, resolve_role

    cfg = load_config(config_path(tmp_path))

    # (a) The override reaches only that role: hunt moves, conformance
    # and the implementer do not.
    profile = self_profile(tmp_path, **SELF_ALLOWED)
    overridden = dataclasses.replace(
        cfg, role_overrides={"reviewer.hunt": SONNET})
    hunt = _resolve(profile, overridden, "reviewer.hunt")
    assert hunt.model == SONNET and hunt.role == "reviewer.hunt"
    assert _resolve(profile, overridden, "reviewer.conformance").model == GLM_ID
    assert _resolve(profile, overridden, "implementer").model == GLM_ID
    # The override expands aliases and resolves through the stack's kind.
    glm_hunt = dataclasses.replace(
        cfg, role_overrides={"reviewer.hunt": GLM_ALIAS})
    assert _resolve(profile, glm_hunt, "reviewer.hunt").model == GLM_ID
    assert _resolve(profile, glm_hunt, "reviewer.hunt").kind == "zai"

    # (b) The guard still holds: an override whose kind the fixture does
    # not allow refuses, named.
    from factory.provider import ProviderError
    narrow = self_profile(tmp_path / "narrow")  # allows nothing beyond zai
    with pytest.raises(ProviderError, match="reviewer.hunt.*anthropic"):
        resolve_role(narrow, overridden, "reviewer.hunt")

    # (c) A scripted eval run: the seats see the override config and
    # their roles; the report names every override; nothing outside the
    # report is written.
    from factory.eval import build_soloadventurer_fixture
    fixture = build_soloadventurer_fixture(tmp_path / "seed-fixture")
    profile_digest = (fixture / ".factory" / "project.yaml").read_bytes()
    cfg_file = config_path(tmp_path)
    cfg_digest = cfg_file.read_bytes()
    seats = ScriptedSeat()
    record = run_seed(fixture, seed_by_id("off-by-one"),
                      session_runner=seats, reviewers=2,
                      factory_root=tmp_path, config=overridden)
    assert record  # the seeded run completed and was judged
    roles_seen = [c[3].get("role") for c in seats.calls]
    assert "reviewer.hunt" in roles_seen and "reviewer.conformance" in roles_seen
    for seat, _prompt, _run_dir, kwargs in seats.calls:
        assert kwargs.get("config") is overridden

    from factory.gates import run_command
    result = run_on_demand_eval(
        tmp_path, tmp_path / "report.json", session_runner=ScriptedSeat(),
        command_runner=run_command,
        reviewers=2, config=overridden)
    assert result["report"]["role_overrides"] == {"reviewer.hunt": SONNET}

    # Nothing written outside the report: the fixture's profile and the
    # registry file are byte-identical, and the in-memory config the
    # eval started from still carries no overrides.
    assert (fixture / ".factory" / "project.yaml").read_bytes() == profile_digest
    assert cfg_file.read_bytes() == cfg_digest
    assert cfg.role_overrides == {}

    # (d) The CLI parses --role and refuses a bad one with the role named.
    from factory.cli import parse_role_overrides
    parsed = parse_role_overrides(["reviewer.hunt=" + SONNET], cfg)
    assert parsed == {"reviewer.hunt": SONNET}
    with pytest.raises(SystemExit):
        parse_role_overrides(["reviewer.hunter=" + SONNET], cfg)
    with pytest.raises(SystemExit):
        parse_role_overrides(["nonsense"], cfg)
    with pytest.raises(SystemExit):
        parse_role_overrides([f"implementer={GLM_ALIAS}-typo"], cfg)
    assert "reviewer.hunt" in MODEL_ROLES


# --- AC-6: onboarding picks the stack -----------------------------------------


def test_ac_6(tmp_path: Path):
    """``stack_for(answers)`` maps the one onboarding restriction answer
    to model_restrictions and a stack, with the reason; contained wins
    when both restrictions are given; a skipped question is never
    mapped."""
    from factory.models import stack_for

    # Each answer maps to its restriction and stack.
    none = stack_for({"none": True})
    assert (none.restriction, none.stack) == ("none", "standard")
    assert none.reason
    no_china = stack_for({"no_china": True})
    assert (no_china.restriction, no_china.stack) == ("no_china", "no-china")
    contained = stack_for({"contained": True})
    assert (contained.restriction, contained.stack) == ("contained",
                                                        "contained")
    # contained wins when both are given, and the reason says so.
    both = stack_for({"no_china": True, "contained": True})
    assert (both.restriction, both.stack) == ("contained", "contained")
    assert "contained" in both.reason
    # A skipped question is never mapped: the interview cannot finish
    # without it.
    skipped = stack_for({})
    assert skipped.restriction is None and skipped.stack is None
    assert "not answered" in skipped.reason
    unset = stack_for({"no_china": False, "contained": False})
    assert unset.restriction is None and unset.stack is None
    # An unknown key refuses rather than guessing.
    with pytest.raises(ValueError):
        stack_for({"eu_only": True})


def test_every_seat_call_passes_the_registry_config():
    """F-100 round 4 (R3): the AST guard — every seat-runner and secrets
    call in factory/ passes ``config`` (and the reviewer fan-outs their
    seat roles), so a missed call site is a loud test failure, not a
    silent DEFAULT_REGISTRY scrub or resolution."""
    import ast

    NAMES = {"run_session_sandboxed", "run_sessions_parallel",
             "run_verifier_fanout", "run_secrets", "_run_prepared"}
    ATTRS = {"session_runner"}  # ctx.session_runner — the orchestrator seam
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "factory").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else None)
            if name not in NAMES and name not in ATTRS:
                continue
            if name in ATTRS and not isinstance(func, ast.Attribute):
                continue  # a bare session_runner name, not ctx's
            # config= as a KEYWORD (round 4's mutation showed a
            # positional FactoryConfig() dodging the guard — the
            # registry must be the caller's own config, spelled out).
            has_config = any(kw.arg == "config" for kw in node.keywords)
            if not has_config:
                offenders.append(f"{path.name}:{node.lineno} {name}")
            # R5: an explicit config=None keyword is the silent-degrade
            # shape — refuse it with its own line.
            for kw in node.keywords:
                if kw.arg == "config" and isinstance(
                        kw.value, ast.Constant) and kw.value.value is None:
                    offenders.append(
                        f"{path.name}:{node.lineno} {name} config=None")
    assert not offenders, (
        "seat/secrets calls missing config= (a missed site resolves or "
        f"scrubs against the wrong registry): {offenders}")

    # The reviewer fan-outs thread each seat's lens role (AC-2): the
    # orchestrator's two review fan-outs carry seat_roles beside the
    # per-seat prompts.
    orch = (REPO_ROOT / "factory" / "orchestrator.py").read_text()
    fanouts = orch.count("seat_system_prompts=reviewer_systems")
    roles = (orch.count("seat_roles=reviewer_roles")
             + orch.count("seat_roles=[reviewer_seat_role("))
    # three fan-outs (round, final, accept), each with its seat roles
    assert fanouts == 3 and roles >= 3, (fanouts, roles)


def test_r1_secrets_follow_the_owners_registry(tmp_path: Path, monkeypatch):
    """Round 4 (R1): the scrub list resolves through the registry the
    run carries. A custom stack with an anthropic role puts that token
    in run_secrets' tuple; the DEFAULT registry (which does not know
    the stack) must not be what any session-bearing site consults."""
    from factory import provider as provider_mod
    from factory.config import FactoryConfig
    from factory.models import registry_from_raw
    from factory.provider import run_secrets

    monkeypatch.setattr(
        provider_mod, "_keychain_token",
        lambda service: {"factory-zai": "zai-tok",
                         "factory-anthropic": "anth-tok"}.get(service))
    mine = FactoryConfig(models=registry_from_raw({
        "aliases": {"glm-latest": GLM_ID},
        "providers": {"zai": {"origin": "cn"},
                      "anthropic": {"origin": "us"}},
        "stacks": {"mine": {"contained": False, "roles": {
            "implementer": {"kind": "zai", "model": GLM_ALIAS},
            "reviewer.conformance": {"kind": "zai", "model": GLM_ALIAS},
            "reviewer.hunt": {"kind": "anthropic", "model": SONNET},
            "verifier": {"kind": "zai", "model": GLM_ALIAS},
            "importer": {"kind": "zai", "model": GLM_ALIAS},
            "final_review.fix": {"kind": "zai", "model": GLM_ALIAS},
            "final_review.strong": {"kind": "zai", "model": GLM_ALIAS},
        }}},
    }))
    profile = self_profile(tmp_path, model_stack="mine",
                           providers_allowed=["anthropic"])
    assert "anth-tok" in run_secrets(profile, mine)  # the owner's registry
    assert "anth-tok" not in run_secrets(
        profile, FactoryConfig())  # the defaults do not know "mine"
    # The reviewer's hunt seat resolves anthropic only under `mine`.
    from factory.models import resolve_role
    assert resolve_role(profile, mine, "reviewer.hunt").kind == "anthropic"
    # The contract-review and import sites pass their config through
    # (the AST guard pins the threading; the mutation row proves it).
    review_src = (REPO_ROOT / "factory" / "contractreview.py").read_text()
    import_src = (REPO_ROOT / "factory" / "importer.py").read_text()
    assert "run_secrets(profile, config=config)" in review_src
    assert "run_secrets(profile, config=config)" in import_src


def test_r2_the_fanout_repair_carries_role_and_config(tmp_path, monkeypatch):
    """Round 4 (R2): a repair turn resumes under the role and registry
    it is repairing — the fan-out's verifier repair (the shared
    collect path) passes role="verifier" and the caller's config."""
    from factory.config import FactoryConfig
    from factory.orchestrator import accept_collect_vote
    from factory.session import SessionRecord

    cfg = FactoryConfig()
    captured: list[dict] = []

    def runner(seat, prompt, run_dir, profile, **kwargs):
        captured.append(dict(kwargs))
        return SessionRecord(seat=seat, status="completed",
                             result='{"id": "V-1", "drop": true}')

    repo = _git_repo(tmp_path / "r2")
    # A completed session whose vote fails to parse → ONE repair turn.
    broken = SessionRecord(seat="verifier", status="completed",
                           session_id="sess-1", result="not json")
    vote, repaired = accept_collect_vote(
        broken, [{"id": "V-1", "severity": "major",
                  "file": "a.py", "line": 1, "text": "t"}], 1,
        runner=runner, run_dir=repo,
        profile=self_profile(tmp_path), factory_root=tmp_path,
        config=cfg, base=tmp_path / "base",
        protected=[], system_prompt="s", srt_bin_path=None,
        run_id="r", task_id="t", toolkit=None,
        remaining_min=lambda: 30.0)
    assert captured, "the repair never ran"
    assert captured[0].get("role") == "verifier"
    assert captured[0].get("config") is cfg
    # The accept reviewer repair (the lens seat) pins its role at the
    # source — the guard's mutation row drops it and goes RED there.
    orch = (REPO_ROOT / "factory" / "orchestrator.py").read_text()
    assert "role=reviewer_seat_role(seat_no, config.reviewers)," in orch


def test_r5_run_secrets_refuses_none_config(tmp_path: Path, monkeypatch):
    """Round 5 (R5): an EXPLICIT config=None on run_secrets refuses up
    front — inside _own_specs' per-role try the same refusal would be
    swallowed and the stack-role tokens would silently vanish from the
    scrub list (the probe: standard-plus scrubs both tokens with the
    config, only zai with None)."""
    from factory import provider as provider_mod
    from factory.config import FactoryConfig
    from factory.provider import ProviderError, run_secrets

    monkeypatch.setattr(
        provider_mod, "_keychain_token",
        lambda service: {"factory-zai": "TOKEN-zai",
                         "factory-anthropic": "TOKEN-anthropic"}.get(service))
    profile = self_profile(tmp_path, model_stack="standard-plus",
                           providers_allowed=["anthropic"])
    assert run_secrets(profile, FactoryConfig()) == (
        "TOKEN-zai", "TOKEN-anthropic")
    with pytest.raises(ProviderError, match="missed call site"):
        run_secrets(profile, None)  # noqa: SE018 — the explicit miss

    # The revalidation path's sink receives the RUN's config: the
    # captured _sweep_trace_sink kwargs carry it (source-pinned; the
    # guard forbids config=None on these calls).
    orch = (REPO_ROOT / "factory" / "orchestrator.py").read_text()
    assert "profile=profile,\n                config=config)" in orch \
        or "config=config)" in orch
    # And no FactoryConfig() substitution remains on the ci-fix sweep
    # (the path that reaches run_secrets at ~4541).
    assert "config if config is not None else FactoryConfig()" not in orch


def test_r5_sweep_sink_gets_the_runs_config(tmp_path: Path, monkeypatch):
    """Round 5 (R5): the revalidation sweep's trace sink resolves the
    run's config — a None config refuses on the spot, and the sink built
    with a real config scrubs the stack's anthropic token (the writer's
    extra secrets, resolved through cfg)."""
    from factory.config import FactoryConfig
    from factory.orchestrator import _sweep_trace_sink
    from factory.provider import ProviderError

    cfg = FactoryConfig()
    monkeypatch.setattr(
        "factory.provider._keychain_token",
        lambda service: {"factory-zai": "TOKEN-zai",
                         "factory-anthropic": "TOKEN-anth"}.get(service))
    sink = _sweep_trace_sink(
        tmp_path, "p", "r1", "T-1",
        profile={"name": "p", "providers_allowed": ["anthropic"],
                 "model_stack": "standard-plus"},
        config=cfg)
    sink({"type": "sandbox_escapee", "label": "x"})
    # The writer lands under <root>/trace/p/ — its extra_secrets carry
    # the anthropic token ONLY because the sink resolved under cfg.
    traces = list((tmp_path / "trace" / "p").rglob("*.jsonl"))
    assert traces, "the sink built a trace writer"
    # A None config on the sink refuses — never a silent defaults read.
    with pytest.raises(ProviderError, match="missed call site"):
        _sweep_trace_sink(tmp_path, "p", "r2", "T-1", profile=None,
                          config=None)
