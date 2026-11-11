"""F-59 acceptance tests — per-project model provider and provider record.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f59.py::test_ac_N``
means a test function with exactly that name.

The goal-level core: the OWNER approves the provider choice. ``data_owner``,
``provider``, ``providers_allowed`` and ``seat_providers`` are F-63 profile
security groups, so a client repo cannot flip itself to ``self``, change its
provider or widen its allowed list without re-approval; and no path —
default, fallback, quota, missing credential, import or eval — reaches Z.ai
from a ``client`` project.

Env names and hosts in these tests were verified against the pinned CLI
binary (claude-agent-sdk 2.1.301, ``_bundled/claude`` — see the F-59
BOOTSTRAP entry for the exact fragments). Nothing here makes a paid call:
AC-7 is structural only.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from factory.config import load_config
from factory.contract import ContractError, load_profile
from factory.sandbox import build_settings, sandbox_env

REPO_ROOT = Path(__file__).resolve().parent.parent


def _support_verdict() -> str:
    """A reviewer verdict that passes clean (the lightest full run)."""
    from _support.f38 import _verdict
    return _verdict()

# --- fixtures ---------------------------------------------------------------

ZAI_HOST = "api.z.ai"
BEDROCK_EU = "eu-north-1"
BEDROCK_EU_HOST = f"bedrock-runtime.{BEDROCK_EU}.amazonaws.com"
ANTHROPIC_HOST = "api.anthropic.com"
COMPAT_HOST = "eu-model-host.example"


def _profile_dict(tmp_path: Path, name: str = "clientproj", **extra) -> dict:
    """A minimal loadable profile dict (the F-38 fixture shape)."""
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


def _write_profile(tmp_path: Path, profile: dict) -> Path:
    path = tmp_path / ".factory" / "project.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(profile))
    return path


def _provider_block(kind: str, **overrides) -> dict:
    block = {
        "kind": kind,
        "models": {
            "implementer": "model-impl",
            "reviewer": "model-rev",
            "verifier": "model-ver",
            "importer": "model-imp",
        },
    }
    block.update(overrides)
    return block


def _load(tmp_path: Path, profile: dict) -> dict:
    return load_profile(_write_profile(tmp_path, profile))


def _client_bedrock(tmp_path: Path, **extra) -> dict:
    return _profile_dict(
        tmp_path,
        data_owner="client",
        provider=_provider_block("bedrock", region=BEDROCK_EU),
        **extra,
    )


# --- AC-1: project.yaml gains data_owner and provider -----------------------


def test_ac_1(tmp_path: Path, monkeypatch):
    from factory.approvals import (
        ApprovalLedger,
        changed_groups,
        require_profile_approval,
    )
    from factory.provider import resolve_provider

    # (a) Every kind loads with its required fields (zai is a SELF
    # project's kind — a client project naming it refuses, case (e)).
    cases = {
        "zai": (None, _provider_block("zai")),
        "anthropic": ("client", _provider_block("anthropic")),
        "bedrock": ("client", _provider_block("bedrock", region=BEDROCK_EU)),
        "vertex": ("client", _provider_block("vertex", region="europe-west1")),
        "anthropic_compatible": (
            "client",
            _provider_block("anthropic_compatible",
                            base_url=f"https://{COMPAT_HOST}/api"),
        ),
    }
    for kind, (owner, block) in cases.items():
        extra = {"provider": block}
        if owner is not None:
            extra["data_owner"] = owner
        profile = _load(tmp_path / kind, _profile_dict(tmp_path, **extra))
        assert profile["provider"]["kind"] == kind, kind

    # (b) Each missing-field case refuses, the field named.
    with pytest.raises(ContractError, match="region"):
        _load(tmp_path / "no-region", _profile_dict(
            tmp_path, data_owner="client",
            provider=_provider_block("bedrock")))
    with pytest.raises(ContractError, match="base_url"):
        _load(tmp_path / "no-base-url", _profile_dict(
            tmp_path, data_owner="client",
            provider=_provider_block("anthropic_compatible")))
    with pytest.raises(ContractError, match="models"):
        _load(tmp_path / "no-models", _profile_dict(
            tmp_path, data_owner="client",
            provider={"kind": "anthropic"}))
    with pytest.raises(ContractError, match="model"):
        _load(tmp_path / "missing-seat-model", _profile_dict(
            tmp_path, data_owner="client",
            provider={"kind": "anthropic",
                      "models": {"implementer": "m", "reviewer": "m",
                                 "verifier": "m"}}))

    # (c) Each forbidden-field case refuses, the field named.
    with pytest.raises(ContractError, match="region"):
        _load(tmp_path / "region-on-anthropic", _profile_dict(
            tmp_path, data_owner="client",
            provider=_provider_block("anthropic", region="us-east-1")))
    with pytest.raises(ContractError, match="base_url"):
        _load(tmp_path / "base-url-on-bedrock", _profile_dict(
            tmp_path, data_owner="client",
            provider=_provider_block(
                "bedrock", region=BEDROCK_EU,
                base_url=f"https://{COMPAT_HOST}/api")))
    for bad in ("http://eu-model-host.example/api",       # not https
                "https://user:pw@eu-model-host.example/api",  # userinfo
                "https://93.184.216.34/api",               # IP literal
                "https://localhost/api",                   # localhost
                "https://eu-model-host.example./api",      # trailing dot
                "https://eu-model-host.example/api?q=1"):  # query
        with pytest.raises(ContractError, match="base_url"):
            _load(tmp_path / f"bad-{hash(bad) & 0xffff}", _profile_dict(
                tmp_path, data_owner="client",
                provider=_provider_block(
                    "anthropic_compatible", base_url=bad)))

    # (c2) R1-1: an anthropic_compatible base_url whose HOST is a known
    # model provider's refuses at load — for every data_owner, on the
    # project block AND a seat override. The kind label is not the
    # control; the host is: this is the "client source reaches Z.ai
    # through anthropic_compatible" path.
    for alien_url in ("https://api.z.ai/api/anthropic",
                      "https://open.bigmodel.cn/api/anthropic",
                      "https://api.anthropic.com",
                      "https://bedrock-runtime.us-east-1.amazonaws.com"):
        for owner in ("client", "self"):
            with pytest.raises(ContractError, match="anthropic_compatible"):
                _load(tmp_path / f"compat-alien-{hash(alien_url) & 0xffff}",
                      _profile_dict(
                          tmp_path, data_owner=owner,
                          provider=_provider_block(
                              "anthropic_compatible", base_url=alien_url)))
    with pytest.raises(ContractError, match="anthropic_compatible"):
        _load(tmp_path / "compat-alien-seat", _profile_dict(
            tmp_path, data_owner="client",
            provider=_provider_block("bedrock", region=BEDROCK_EU),
            providers_allowed=["anthropic_compatible"],
            seat_providers={"reviewer": {
                "kind": "anthropic_compatible",
                "model": "m",
                "base_url": "https://api.z.ai/api/anthropic"}}))

    # (d) A free-text region never reaches a host: pattern-checked.
    with pytest.raises(ContractError, match="region"):
        _load(tmp_path / "evil-region", _profile_dict(
            tmp_path, data_owner="client",
            provider=_provider_block("bedrock", region="eu-north-1.evil.com")))

    # (e) Both client refusals.
    with pytest.raises(ContractError, match="client"):
        _load(tmp_path / "client-no-provider",
              _profile_dict(tmp_path, data_owner="client"))
    with pytest.raises(ContractError, match="zai"):
        _load(tmp_path / "client-zai", _profile_dict(
            tmp_path, data_owner="client", provider=_provider_block("zai")))

    # (f) A self project with no block keeps Z.ai (the missing data_owner
    # is self by default — the schema description states it).
    self_profile = _load(tmp_path / "self", _profile_dict(tmp_path))
    spec = resolve_provider(self_profile)
    assert spec.kind == "zai"
    assert spec.endpoint_host == ZAI_HOST

    # (g) Owner approval: data_owner / provider / providers_allowed are
    # security groups — a flip, a change or a widen refuses until
    # re-approved, through require_profile_approval (not a new mechanism).
    monkeypatch.chdir(tmp_path)
    ledger = ApprovalLedger.for_project(tmp_path / "ledger", "clientproj")
    approved = _client_bedrock(tmp_path)
    ledger.record_profile_for(approved)
    require_profile_approval(approved, tmp_path / "ledger")  # covered

    flipped = dict(approved, data_owner="self")
    assert "data_owner" in changed_groups(flipped, ledger.latest_profile_approval())
    with pytest.raises(Exception, match="data_owner"):
        require_profile_approval(flipped, tmp_path / "ledger")

    changed_kind = json.loads(json.dumps(approved))
    changed_kind["provider"]["kind"] = "anthropic"
    del changed_kind["provider"]["region"]
    assert "provider" in changed_groups(
        changed_kind, ledger.latest_profile_approval())
    with pytest.raises(Exception, match="provider"):
        require_profile_approval(changed_kind, tmp_path / "ledger")

    # Z4 (round-1 addendum): re-pointing a seat after approval is the
    # same flip — the seat_providers group names it.
    repointed = json.loads(json.dumps(approved))
    repointed["providers_allowed"] = ["anthropic"]
    repointed["seat_providers"] = {"reviewer": {"kind": "anthropic",
                                                "model": "m"}}
    assert "seat_providers" in changed_groups(
        repointed, ledger.latest_profile_approval())
    with pytest.raises(Exception, match="seat_providers"):
        require_profile_approval(repointed, tmp_path / "ledger")

    widened = json.loads(json.dumps(approved))
    widened["providers_allowed"] = ["anthropic"]
    assert "providers_allowed" in changed_groups(
        widened, ledger.latest_profile_approval())
    with pytest.raises(Exception, match="providers_allowed"):
        require_profile_approval(widened, tmp_path / "ledger")

    # A missing key hashes as null: REMOVING a group is a change too.
    stripped = json.loads(json.dumps(approved))
    del stripped["provider"]
    assert "provider" in changed_groups(
        stripped, ledger.latest_profile_approval())
    # And the anchor/merge-key sneaking class: whatever spelling reaches
    # the loaded dict is what is hashed — an anchored provider block
    # changes the hash exactly like a literal one.
    anchored = yaml.safe_load(yaml.safe_dump(approved))
    anchored["provider"]["models"]["implementer"] = "other-model"
    assert "provider" in changed_groups(
        anchored, ledger.latest_profile_approval())
    # Re-approval returns the run: the widened profile, once approved,
    # passes.
    ledger.record_profile_for(widened)
    require_profile_approval(widened, tmp_path / "ledger")

    # (h) The factory's own profile and the project drafts still load.
    assert load_profile(REPO_ROOT / ".factory" / "project.yaml")["name"]
    for draft in sorted((REPO_ROOT / "projects").glob("*.project.yaml")):
        assert load_profile(draft)["name"], draft


# --- AC-2: credentials are per provider -------------------------------------


def test_ac_2(tmp_path: Path, monkeypatch):
    from factory import provider
    from factory.provider import (
        ProviderError,
        anthropic_family_env,
        bedrock_env,
        keychain_service,
        provider_token,
        resolve_provider,
        run_secrets,
    )
    from factory.session import seat_token, session_env

    # (a) The exact env key set per kind (set equality — no extra key,
    # no inherited one).
    zai = resolve_provider(_load(tmp_path / "zai", _profile_dict(
        tmp_path, data_owner="self", provider=_provider_block("zai"))))
    assert set(zai.env_keys) == {
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "API_TIMEOUT_MS"}
    anthropic = resolve_provider(_load(tmp_path / "anth", _profile_dict(
        tmp_path, data_owner="client",
        provider=_provider_block("anthropic"))))
    assert set(anthropic.env_keys) == {
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "API_TIMEOUT_MS"}
    compat = resolve_provider(_load(tmp_path / "compat", _profile_dict(
        tmp_path, data_owner="client",
        provider=_provider_block(
            "anthropic_compatible", base_url=f"https://{COMPAT_HOST}/api"))))
    assert set(compat.env_keys) == {
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "API_TIMEOUT_MS"}
    bedrock = resolve_provider(_client_bedrock(tmp_path))
    assert set(bedrock.env_keys) == {
        "CLAUDE_CODE_USE_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_REGION", "API_TIMEOUT_MS"}

    # The literal builders carry exactly those names and values.
    assert session_env("https://api.example", "tok", 1000) == (
        anthropic_family_env("https://api.example", "tok", 1000))
    env = bedrock_env("btok", BEDROCK_EU, 1000)
    assert env == {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_BEARER_TOKEN_BEDROCK": "btok",
        "AWS_REGION": BEDROCK_EU,
        "API_TIMEOUT_MS": "1000",
    }

    # (b) One keychain item per kind; factory-zai is unchanged.
    assert keychain_service("zai") == "factory-zai"
    assert keychain_service("bedrock") == "factory-bedrock"
    assert keychain_service("anthropic") == "factory-anthropic"

    # (c) Token isolation — the cross-provider refusal, generalised from
    # the Z.ai ambient-URL rule. A token exported for one provider is
    # refused when the project names another: never sent, never None-ish.
    fake_keychain = {"factory-zai": "zai-secret", "factory-anthropic":
                     "anth-secret", "factory-bedrock": "bed-secret"}
    monkeypatch.setattr(provider, "_keychain_token",
                        lambda service: fake_keychain.get(service))
    # zai token with the ambient URL naming zai: fine (today's rule).
    monkeypatch.setenv("ANTHROPIC_BASE_URL",
                       "https://api.z.ai/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-zai")
    assert provider_token("zai") == "zai-secret"  # keychain wins
    # The SAME ambient token for a project naming anthropic: refused —
    # the ambient URL names Z.ai, another provider's endpoint.
    assert provider_token("anthropic") == "anth-secret"
    del fake_keychain["factory-anthropic"]
    assert provider_token("anthropic") is None
    # And the symmetric direction: an anthropic-URL'd ambient token never
    # reaches the zai kind (today's rule, unchanged).
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-anthropic")
    del fake_keychain["factory-zai"]
    got_none = provider_token("zai") is None
    assert got_none, "foreign-URL token reached the zai kind"
    got_none = seat_token() is None
    assert got_none, "foreign-URL token reached the zai delegate"
    # ...but the anthropic kind accepts it (no keychain item present):
    assert provider_token("anthropic") == "ambient-anthropic"
    # Bedrock's own name is unambiguous: accepted (keychain first, then
    # the ambient name), and an ANTHROPIC_* ambient value is never read
    # for it.
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "ambient-bedrock")
    assert provider_token("bedrock") == "bed-secret"  # keychain wins
    del fake_keychain["factory-bedrock"]
    assert provider_token("bedrock") == "ambient-bedrock"

    # (d) No ambient credentials: a planted ANTHROPIC_API_KEY,
    # AWS_ACCESS_KEY_ID, AWS_PROFILE, GOOGLE_APPLICATION_CREDENTIALS and
    # CLAUDE_CODE_USE_BEDROCK reach no session env — for a zai project
    # AND for a bedrock project (the resolver's keys only).
    for name, value in (
        ("ANTHROPIC_API_KEY", "sk-ambient"),
        ("AWS_ACCESS_KEY_ID", "AKIA-ambient"),
        ("AWS_SECRET_ACCESS_KEY", "ambient-secret"),
        ("AWS_PROFILE", "ambient-profile"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/ambient/adc.json"),
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("CLAUDE_CODE_USE_VERTEX", "1"),
    ):
        monkeypatch.setenv(name, value)
    for spec, credential in ((zai, "zai-tok"), (bedrock, "bed-tok")):
        seat_env = spec.env(credential, 600_000)
        child = sandbox_env(tmp_path, extra=seat_env)
        for planted in ("ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID",
                        "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
                        "GOOGLE_APPLICATION_CREDENTIALS",
                        "CLAUDE_CODE_USE_VERTEX"):
            assert planted not in child, (spec.kind, planted)
        # The resolver's own keys survive exactly: a zai session is not
        # flipped to bedrock by the ambient CLAUDE_CODE_USE_BEDROCK.
        if spec.kind == "zai":
            assert "CLAUDE_CODE_USE_BEDROCK" not in child
            assert child["ANTHROPIC_AUTH_TOKEN"] == "zai-tok"
        else:
            assert child["CLAUDE_CODE_USE_BEDROCK"] == "1"
            assert child["AWS_BEARER_TOKEN_BEDROCK"] == "bed-tok"
            assert child["AWS_REGION"] == BEDROCK_EU

    # (c2) Z11 (round-1 addendum): with no keychain item and ONLY
    # ANTHROPIC_* exported, the bedrock lookup returns None — a
    # cross-kind credential is never sent to AWS — and the session
    # refuses naming factory-bedrock.
    monkeypatch.setattr(provider, "_keychain_token", lambda service: None)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "an-anthropic-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    got_none = provider_token("bedrock") is None
    assert got_none, "an ANTHROPIC_* token reached the bedrock lookup"
    from factory.session import SessionConfigError as _SCE
    from factory.session import run_session_sandboxed as _rss
    with pytest.raises(_SCE, match="factory-bedrock"):
        _rss("implementer", "p", tmp_path, _client_bedrock(tmp_path),
             factory_root=tmp_path, settings_path=tmp_path / "s.json",
             max_turns=1, timeout_min=1, srt_bin_path=None)

    # (d2) The provider-prefix strip is the guard against a future
    # widening of AMBIENT_ENV_ALLOWLIST: even with an ambient name
    # admitted (simulated by widening the allowlist the way a future
    # change might), the provider namespaces still never ride the
    # AMBIENT half — only the resolver's literal extras carry them.
    import factory.sandbox as sandbox_mod
    widened = frozenset({"PATH", "LANG", "TZ", "AWS_PROFILE",
                         "CLAUDE_CODE_USE_BEDROCK"})
    monkeypatch.setattr(sandbox_mod, "AMBIENT_ENV_ALLOWLIST", widened)
    child = sandbox_env(tmp_path, extra={})
    assert "AWS_PROFILE" not in child
    assert "CLAUDE_CODE_USE_BEDROCK" not in child
    monkeypatch.undo()

    # (e) Egress secrets: every token the run holds — a Bedrock token is
    # in the tuple, so the F-43 seam scrubs it exactly as the Z.ai token.
    monkeypatch.setattr(provider, "_keychain_token",
                        lambda service: fake_keychain.get(service))
    fake_keychain["factory-bedrock"] = "bed-secret"
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    profile = _load(tmp_path / "run", _client_bedrock(tmp_path))
    secrets = run_secrets(profile)
    assert "bed-secret" in secrets
    from factory.egress import scrub_session_text
    body = "review note: AWS_BEARER_TOKEN_BEDROCK=bed-secret tail"
    assert "bed-secret" not in scrub_session_text(body, secrets)

    # (e2) R1-3: run_secrets resolves the same specs the seats resolve
    # — a seat override of anthropic_compatible with ITS OWN base_url is
    # looked up with that URL (the ambient fallback accepts it there),
    # so the token the seat will actually send is in the scrub set.
    override_url = f"https://{COMPAT_HOST}/api"
    monkeypatch.setattr(provider, "_keychain_token", lambda service: None)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", override_url)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "compat-ambient-token")
    override_profile = _load(tmp_path / "ovr-compat", _profile_dict(
        tmp_path, data_owner="client",
        provider=_provider_block("bedrock", region=BEDROCK_EU),
        providers_allowed=["anthropic_compatible"],
        seat_providers={"reviewer": {"kind": "anthropic_compatible",
                                     "model": "m",
                                     "base_url": override_url}}))
    secrets = run_secrets(override_profile)
    assert "compat-ambient-token" in secrets, secrets
    # And it is exactly what the seat's own resolution would send:
    seat_spec = resolve_provider(override_profile, "reviewer")
    assert provider_token(seat_spec.kind,
                          base_url=seat_spec.base_url) == "compat-ambient-token"

    # (f) Vertex cannot run without a credentials file or ambient lookup:
    # it refuses at RESOLVE time, with the reason named — the rule is not
    # weakened for it.
    vertex_profile = _load(tmp_path / "vertex", _profile_dict(
        tmp_path, data_owner="client",
        provider=_provider_block("vertex", region="europe-west1")))
    with pytest.raises(ProviderError, match="vertex"):
        resolve_provider(vertex_profile)


# --- AC-3: session egress follows the provider -------------------------------


def test_ac_3(tmp_path: Path):
    from factory.provider import resolve_provider

    run_dir = tmp_path / "runs" / "clientproj" / "20260101T000000Z-test"
    run_dir.mkdir(parents=True)

    def settings_for(profile: dict, seat: str | None = None) -> dict:
        spec = resolve_provider(profile, seat)
        return build_settings(
            profile, run_dir, model_endpoint=spec.egress_hosts,
            factory_root=tmp_path,
        )

    # (a) The exact model host set per kind, read from the generated
    # srt settings.
    zai_profile = _load(tmp_path / "zai", _profile_dict(tmp_path))
    assert settings_for(zai_profile)["network"]["allowedDomains"][0] == ZAI_HOST

    bedrock_profile = _load(tmp_path / "bed", _client_bedrock(tmp_path))
    assert settings_for(bedrock_profile)["network"]["allowedDomains"] == [
        BEDROCK_EU_HOST]

    anthropic_profile = _load(tmp_path / "anth", _profile_dict(
        tmp_path, data_owner="client",
        provider=_provider_block("anthropic")))
    assert settings_for(anthropic_profile)["network"]["allowedDomains"] == [
        ANTHROPIC_HOST]

    compat_profile = _load(tmp_path / "compat", _profile_dict(
        tmp_path, data_owner="client",
        provider=_provider_block(
            "anthropic_compatible", base_url=f"https://{COMPAT_HOST}/api")))
    assert settings_for(compat_profile)["network"]["allowedDomains"] == [
        COMPAT_HOST]

    # (b) A client project's settings never contain the Z.ai host —
    # not for any kind, not on any seat.
    for kind, profile in (("bedrock", bedrock_profile),
                          ("anthropic", anthropic_profile),
                          ("compat", compat_profile)):
        for seat in (None, "implementer", "reviewer", "verifier",
                     "importer"):
            domains = settings_for(profile, seat)["network"]["allowedDomains"]
            assert ZAI_HOST not in domains, (kind, seat)

    # (c) "No other model endpoint": an allowlist naming a model host
    # that is not the project's own provider refuses at profile load.
    for alien in (ZAI_HOST, ANTHROPIC_HOST,
                  "bedrock-runtime.us-east-1.amazonaws.com",
                  "europe-west1-aiplatform.googleapis.com",
                  # R1-4: the real forms the family takes — FIPS,
                  # dual-stack, the agent-runtime sibling, the China
                  # partition, the Z.ai operator's other domain and the
                  # apex itself.
                  "bedrock-runtime-fips.us-east-1.amazonaws.com",
                  "bedrock-runtime.us-east-1.api.aws",
                  "bedrock-agent-runtime.us-east-1.amazonaws.com",
                  "bedrock-runtime.cn-north-1.amazonaws.com.cn",
                  "open.bigmodel.cn",
                  "z.ai",
                  # R2-4: the family is ANY bedrock* label under the
                  # partitions — PrivateLink endpoint names and
                  # subdomain-prefixed hosts included.
                  "vpce-0abc-1234.bedrock-runtime.eu-north-1.vpce.amazonaws.com",
                  "x.bedrock-runtime.us-east-1.amazonaws.com"):
        with pytest.raises(ContractError, match=alien.replace(".", "\\.")):
            _load(tmp_path / f"alien-{hash(alien) & 0xffff}",
                  _profile_dict(
                      tmp_path, data_owner="client",
                      provider=_provider_block("bedrock", region=BEDROCK_EU),
                      network_allowlist=[alien]))
    # The project's OWN provider host in its allowlist is not a refusal
    # (redundant, not hostile).
    own = _load(tmp_path / "own", _profile_dict(
        tmp_path, data_owner="client",
        provider=_provider_block("bedrock", region=BEDROCK_EU),
        network_allowlist=[BEDROCK_EU_HOST]))
    assert BEDROCK_EU_HOST in own["network_allowlist"]
    # And a non-model host is untouched by the rule.
    plain = _load(tmp_path / "plain", _profile_dict(
        tmp_path, data_owner="client",
        provider=_provider_block("bedrock", region=BEDROCK_EU),
        network_allowlist=["pypi.org"]))
    assert plain["network_allowlist"] == ["pypi.org"]


# --- AC-4: provider record and token totals ----------------------------------


def test_ac_4(tmp_path: Path, monkeypatch):
    from factory.config import FactoryConfig
    from factory.contract import load_tasks
    from factory.provider import resolve_provider
    from factory.session import SessionRecord, record_field_error
    from factory.status import render_status

    # (a) The session record carries provider + usage and NEVER a token.
    record = SessionRecord(
        seat="implementer", status="completed", num_turns=2,
        model="model-impl",
        tokens={"input_tokens": 100, "output_tokens": 20,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 10},
        provider={"kind": "bedrock", "model": "model-impl",
                  "region": BEDROCK_EU},
        usage={"input": 100, "cache_read": 900, "cache_write": 10,
               "output": 20},
    )
    assert record_field_error(record) is None
    dumped = json.dumps(record.to_dict())
    assert "token" not in dumped.lower() or "tokens" in dumped
    assert "AWS_BEARER_TOKEN_BEDROCK" not in dumped
    assert record.provider["kind"] == "bedrock"
    assert record.usage == {"input": 100, "cache_read": 900,
                            "cache_write": 10, "output": 20}

    # The extractor nulls a forged count BEFORE it can be counted:
    # -1 and non-int shapes are unknown at the source, so no sum, cost
    # figure or validator pass ever sees them.
    from factory.session import _usage_counts
    ok = {"input_tokens": 7, "output_tokens": 3,
          "cache_read_input_tokens": 0,
          "cache_creation_input_tokens": 0}
    assert _usage_counts({**ok, "input_tokens": -1})["input"] is None
    assert _usage_counts({**ok, "input_tokens": "9"})["input"] is None
    assert _usage_counts({**ok, "input_tokens": True})["input"] is None
    assert _usage_counts(ok) == {"input": 7, "cache_read": 0,
                                 "cache_write": 0, "output": 3}
    assert _usage_counts(None) is None
    assert _usage_counts({"input_tokens": -1}) is None  # all unknown

    # The provider fields come from the PARENT's resolver, not the
    # session: a scripted record claiming another provider changes
    # nothing the parent writes.
    forged = json.loads(json.dumps(record.to_dict()))
    forged["provider"] = {"kind": "zai", "model": "x"}
    parent_spec = resolve_provider(_client_bedrock(tmp_path), "implementer")
    parent_record = parent_spec.provider_record()
    assert parent_record["kind"] == "bedrock"
    assert parent_record != forged["provider"]
    assert "token" not in json.dumps(parent_record)

    # (b) The attempt schema gains the fields; existing attempts without
    # them still load.
    legacy = {
        "run_id": "r", "slice_id": "T-1", "started": "2026-11-10T00:00:00Z",
        "outcome": "pass", "prompt_versions": {}, "skills": [],
    }
    with_provider = dict(
        legacy,
        provider={"kind": "bedrock", "model": "model-impl",
                  "region": BEDROCK_EU},
        token_usage={"input": 1, "cache_read": 2, "cache_write": 3,
                     "output": 4},
    )
    for attempt in (legacy, with_provider):
        doc = {"version": 1, "project": "clientproj", "tasks": [{
            "id": "T-1", "title": "t", "source": "docs/t.md",
            "kind": "build", "priority": "P1", "status": "draft",
            "depends_on": [], "risk": [],
            "gates": {"spec_approval": True, "merge": "human",
                      "deploy": "human"},
            "acceptance": [], "attempts": [attempt], "links": {},
            "budget": {"size": "M", "max_attempts": 3, "max_turns": 30,
                       "wall_clock_min": 60},
        }]}
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps(doc))
        assert load_tasks(path)["tasks"][0]["attempts"], attempt

    # (c) `factory status` shows per-provider token totals and an
    # estimated cost from the owner-maintained price table; a missing
    # price is n/a, never 0; an invalid usage count is unknown, not 0.
    queue = {"version": 1, "project": "clientproj", "tasks": [{
        "id": "T-1", "title": "t", "source": "docs/t.md", "kind": "build",
        "priority": "P1", "status": "done", "depends_on": [], "risk": [],
        "gates": {"spec_approval": True, "merge": "human",
                  "deploy": "human"},
        "acceptance": [], "links": {},
        "budget": {"size": "M", "max_attempts": 3, "max_turns": 30,
                   "wall_clock_min": 60},
        "attempts": [
            dict(legacy, outcome="pass",
                 provider={"kind": "bedrock", "model": "sonnet",
                           "region": BEDROCK_EU},
                 token_usage={"input": 1_000_000, "cache_read": 2_000_000,
                              "cache_write": 0, "output": 100_000}),
            dict(legacy, outcome="pass",
                 provider={"kind": "anthropic", "model": "other",
                           "endpoint_host": ANTHROPIC_HOST},
                 token_usage={"input": 500_000, "cache_read": None,
                              "cache_write": None, "output": 50_000}),
            # The forged usage on its OWN model row: -1 is not a
            # count — unknown, never 0, and never a cost figure.
            dict(legacy, outcome="pass",
                 provider={"kind": "bedrock", "model": "forged-model",
                           "region": BEDROCK_EU},
                 token_usage={"input": -5, "cache_read": 0,
                              "cache_write": 0, "output": 0}),
        ],
    }]}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "prices": {"bedrock": {"sonnet": {
            "input": 3.0, "cache_read": 0.3, "cache_write": 3.75,
            "output": 15.0}}},
    }))
    cfg = load_config(config)
    assert isinstance(cfg, FactoryConfig)
    assert cfg.prices["bedrock"]["sonnet"]["input"] == 3.0

    (tmp_path / "progress").mkdir(exist_ok=True)
    page = render_status(queue, project="clientproj",
                         factory_root=tmp_path, repo=tmp_path,
                         config=cfg)
    # Per-provider totals are shown, grouped by kind+model.
    assert "bedrock" in page and "anthropic" in page
    # Cost: 1M input ($3.00) + 2M cache-read ($0.60) + 0.1M output
    # ($1.50) = $5.10 — computed, not invented.
    assert "5.10" in page
    # The priced-out anthropic row shows n/a (no price), never a $0.
    assert "n/a" in page
    assert "$0.00" not in page
    # The forged -1 usage renders unknown, never 0 — on its own row,
    # with no cost figure invented for it.
    assert "unknown" in page
    assert "forged-model" in page

    # R1-6: an unknown count in a column makes that column's TOTAL
    # unknown, wherever it sits in the order — a partial sum must never
    # render as the total.
    for order in ((500, None, 700), (None, 500, 700), (500, 700, None)):
        rows = []
        for i, value in enumerate(order):
            rows.append(dict(
                legacy, outcome="pass",
                provider={"kind": "bedrock", "model": "order-model",
                          "region": BEDROCK_EU},
                token_usage={"input": value, "cache_read": 0,
                             "cache_write": 0, "output": 0}))
        partial_queue = {"version": 1, "project": "clientproj", "tasks": [{
            "id": "T-3", "title": "t", "source": "docs/t.md",
            "kind": "build", "priority": "P1", "status": "done",
            "depends_on": [], "risk": [],
            "gates": {"spec_approval": True, "merge": "human",
                      "deploy": "human"},
            "acceptance": [], "links": {},
            "budget": {"size": "M", "max_attempts": 9, "max_turns": 30,
                       "wall_clock_min": 60},
            "attempts": rows,
        }]}
        page_p = render_status(partial_queue, project="clientproj",
                               factory_root=tmp_path, repo=tmp_path,
                               config=cfg)
        row_line = next(line for line in page_p.splitlines()
                        if "order-model" in line)
        in_column = row_line.split("|")[3].strip()
        assert in_column == "unknown", (order, row_line)

    # (c2) R1-5: usage aggregates per (kind, model) from each record's
    # own parent-stamped provider — a reviewer on another provider (or
    # model) is never priced under the implementer's. The attempt keeps
    # the implementer's `provider` (the brief's shape) and adds the
    # breakdown; status sums the breakdown.
    from _support.f38 import Env as _Env
    from _support.f38 import Fanout as _Fanout

    Env, Fanout = _Env, _Fanout
    import factory.provider as provider_mod
    monkeypatch.setattr(
        provider_mod, "_keychain_token",
        lambda service: {"factory-bedrock": "bed-rock-fake-token",
                         "factory-anthropic": "anth-fake-token"}.get(service))
    env2 = Env(tmp_path / "split-run")
    split = dict(env2.profile)
    split["data_owner"] = "client"
    split["provider"] = {
        "kind": "bedrock", "region": BEDROCK_EU,
        "models": {"implementer": "impl-model", "reviewer": "rev-model",
                   "verifier": "ver-model", "importer": "imp-model"}}
    split["providers_allowed"] = ["anthropic"]
    split["seat_providers"] = {"reviewer": {"kind": "anthropic",
                                            "model": "anth-rev-model"}}
    (env2.repo / ".factory" / "project.yaml").write_text(
        yaml.safe_dump(split))
    subprocess.run(["git", "-C", str(env2.repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(env2.repo), "commit", "-qm", "split providers"],
        check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@e.com",
             "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@e.com",
             "PATH": "/usr/bin:/bin"})
    base_seats2 = Fanout(reviews=_support_verdict())

    def per_provider_records(seat, prompt, run_dir, profile, **kwargs):
        record = base_seats2(seat, prompt, run_dir, profile, **kwargs)
        if seat == "implementer":
            record.provider = {"kind": "bedrock", "model": "impl-model",
                               "region": BEDROCK_EU}
            record.usage = {"input": 1_000, "cache_read": 0,
                            "cache_write": 0, "output": 100}
        elif seat == "reviewer":
            record.provider = {"kind": "anthropic",
                               "model": "anth-rev-model",
                               "endpoint_host": ANTHROPIC_HOST}
            record.usage = {"input": 2_000, "cache_read": 0,
                            "cache_write": 0, "output": 200}
            # Z12 (round-1 addendum): the OVERRIDE seat echoes its own
            # credential — BOTH kinds' tokens must be in the run's
            # scrub set (trace, progress log, PR body).
            record.result = (record.result or "") + (
                " reviewer env: ANTHROPIC_AUTH_TOKEN=anth-fake-token")
        return record

    env2.run(per_provider_records)
    attempt2 = env2.task()["attempts"][-1]
    assert attempt2["provider"]["kind"] == "bedrock"  # the implementer's
    breakdown = attempt2["provider_usage"]
    by_key = {(row["kind"], row["model"]): row["usage"] for row in breakdown}
    assert by_key[("bedrock", "impl-model")]["input"] == 1_000
    # TWO reviewer seats run (the config's reviewers=2 fan-out), each
    # attributed to its own provider — never folded into the
    # implementer's row.
    assert by_key[("anthropic", "anth-rev-model")]["input"] == 4_000
    assert ("bedrock", "impl-model") in by_key
    page2 = render_status(
        {"version": 1, "project": "fixture", "tasks": [env2.task()]},
        project="fixture", factory_root=env2.root, repo=env2.repo,
        config=load_config(config))
    assert "anth-rev-model" in page2 and "impl-model" in page2
    # R2-6: an unknown count driven through the ORCHESTRATOR's
    # breakdown (not hand-built attempts): one round whose three
    # records share a (kind, model) with usage input 500 / unknown /
    # 700 — the attempt's provider_usage input column is null and the
    # status table renders unknown for it.
    env3 = Env(tmp_path / "unknown-run")
    same = dict(env3.profile)
    same["data_owner"] = "client"
    same["provider"] = {
        "kind": "bedrock", "region": BEDROCK_EU,
        "models": {r: "same-model" for r in
                   ("implementer", "reviewer", "verifier", "importer")}}
    (env3.repo / ".factory" / "project.yaml").write_text(
        yaml.safe_dump(same))
    subprocess.run(["git", "-C", str(env3.repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(env3.repo), "commit", "-qm", "same model"],
        check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@e.com",
             "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@e.com",
             "PATH": "/usr/bin:/bin"})
    base3 = Fanout(reviews=_support_verdict())
    usage_by_seat = {
        "implementer": {"input": 500, "cache_read": 0,
                        "cache_write": 0, "output": 0},
        "reviewer": None,  # reported nothing: unknown
    }
    seen_reviewers = {"n": 0}

    def mixed_usage(seat, prompt, run_dir, profile, **kwargs):
        record = base3(seat, prompt, run_dir, profile, **kwargs)
        record.provider = {"kind": "bedrock", "model": "same-model",
                           "region": BEDROCK_EU}
        if seat in usage_by_seat:
            record.usage = usage_by_seat[seat]
        elif seat == "reviewer":
            seen_reviewers["n"] += 1
            record.usage = ({"input": 700, "cache_read": 0,
                             "cache_write": 0, "output": 0}
                            if seen_reviewers["n"] == 2 else None)
        return record

    monkeypatch.setattr(
        provider_mod, "_keychain_token",
        lambda service: "bed-tok" if service == "factory-bedrock" else None)
    env3.run(mixed_usage)
    attempt3 = env3.task()["attempts"][-1]
    rows3 = attempt3["provider_usage"]
    assert len(rows3) == 1 and rows3[0]["kind"] == "bedrock", rows3
    assert rows3[0]["usage"]["input"] is None, rows3[0]["usage"]
    page3 = render_status(
        {"version": 1, "project": "fixture", "tasks": [env3.task()]},
        project="fixture", factory_root=env3.root, repo=env3.repo,
        config=cfg)
    row_line = next(line for line in page3.splitlines()
                    if "same-model" in line)
    assert row_line.split("|")[3].strip() == "unknown", row_line

    # Z12: both kinds' tokens are in the run's scrub set — the override
    # seat's own token (echoed in its result) never reaches the
    # progress log, the trace or the PR body.
    from factory.progress import ProgressLog as _PL
    for entry in _PL.for_task(env2.root, "fixture", "T-1").entries():
        assert "anth-fake-token" not in json.dumps(entry)
        assert "bed-rock-fake-token" not in json.dumps(entry)
    for trace_file in sorted((env2.root / "trace" / "fixture").glob(
            "*.jsonl")):
        blob = trace_file.read_text()
        assert "anth-fake-token" not in blob, trace_file
        assert "bed-rock-fake-token" not in blob, trace_file
    for pr in env2.prs:
        body = pr.body or ""
        assert "anth-fake-token" not in body, body[:200]
        assert "bed-rock-fake-token" not in body, body[:200]

    # (d) The no-provider legacy queue still renders.
    legacy_queue = {"version": 1, "project": "clientproj", "tasks": [{
        "id": "T-2", "title": "t", "source": "docs/t.md", "kind": "build",
        "priority": "P1", "status": "done", "depends_on": [], "risk": [],
        "gates": {"spec_approval": True, "merge": "human",
                  "deploy": "human"},
        "acceptance": [], "links": {},
        "budget": {"size": "M", "max_attempts": 3, "max_turns": 30,
                   "wall_clock_min": 60},
        "attempts": [dict(legacy, outcome="pass", input_tokens=10,
                          output_tokens=5)],
    }]}
    render_status(legacy_queue, project="clientproj",
                  factory_root=tmp_path, repo=tmp_path, config=cfg)

    # (e) A real run over a CLIENT bedrock profile whose scripted session
    # record CLAIMS provider.kind zai: the attempt's provider is the
    # parent resolver's (bedrock), and no token value lands anywhere in
    # the attempt record.
    import yaml as _yaml
    from _support.f38 import Env, Fanout


    env = Env(tmp_path / "client-run")
    client = dict(env.profile)
    client["data_owner"] = "client"
    client["provider"] = _provider_block("bedrock", region=BEDROCK_EU)
    (env.repo / ".factory" / "project.yaml").write_text(
        _yaml.safe_dump(client))
    subprocess.run(
        ["git", "-C", str(env.repo), "add", "-A"], check=True,
        capture_output=True)
    subprocess.run(
        ["git", "-C", str(env.repo), "commit", "-qm", "client provider"],
        check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@e.com",
             "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@e.com",
             "PATH": "/usr/bin:/bin"})

    # Plant the run's bedrock credential, and a forged zai claim on the
    # implementer record the parent must ignore.
    import factory.provider as provider_mod
    real_keychain = provider_mod._keychain_token
    monkeypatch.setattr(
        provider_mod, "_keychain_token",
        lambda service: "bed-rock-fake-token"
        if service == "factory-bedrock" else None)
    base_seats = Fanout(reviews=_support_verdict())

    def claiming(seat, prompt, run_dir, profile, **kwargs):
        record = base_seats(seat, prompt, run_dir, profile, **kwargs)
        if seat == "implementer":
            record.provider = {"kind": "zai", "model": "glm-5.3",
                               "endpoint_host": ZAI_HOST}
            # The session ECHOES its own credential: the run's egress
            # secrets (the orchestrator's run_secrets of the profile)
            # must scrub it from everything the run writes — a
            # Bedrock token exactly like the Z.ai one before F-59.
            record.result = (record.result or "") + (
                " env holds bed-rock-fake-token and the runner echoes "
                "AWS_BEARER_TOKEN_BEDROCK=bed-rock-fake-token")
        return record

    result = env.run(claiming)
    assert result.outcome == "pr_opened", result
    attempt = env.task()["attempts"][-1]
    assert attempt["provider"]["kind"] == "bedrock", attempt["provider"]
    assert attempt["provider"]["region"] == BEDROCK_EU
    dumped_attempt = json.dumps(attempt)
    assert "bed-rock-fake-token" not in dumped_attempt
    assert ZAI_HOST not in dumped_attempt
    # ...and the token the session echoed is scrubbed from the run's
    # own writes: the progress log (the vents/notes channel) and the
    # trace, through the orchestrator's egress_secrets — the F-43 seam
    # with the NEW secret, driven the real way.
    from factory.progress import ProgressLog
    for entry in ProgressLog.for_task(
            env.root, "fixture", "T-1").entries():
        assert "bed-rock-fake-token" not in json.dumps(entry)
    for trace_file in sorted((env.root / "trace" / "fixture").glob(
            "*.jsonl")):
        assert "bed-rock-fake-token" not in trace_file.read_text()
    # ...and the PR body — the channel the implementer's echoed result
    # actually rides (its notes tail): the run's egress secrets scrub it
    # there too, exactly as the Z.ai token was before F-59.
    for pr in env.prs:
        assert "bed-rock-fake-token" not in (pr.body or ""), pr.body[:200]
    monkeypatch.setattr(provider_mod, "_keychain_token", real_keychain)


# --- AC-5: seat overrides and no fallback ------------------------------------


def test_ac_5(tmp_path: Path, monkeypatch):
    from factory import provider
    from factory.provider import (
        ProviderError,
        provider_token,
        resolve_provider,
    )
    from factory.session import SessionConfigError, run_session_sandboxed

    # (a) The allowed override: a reviewer seat on another provider from
    # the project's own providers_allowed list, with its own credential
    # and its own egress host — added only to that seat.
    profile = _load(tmp_path / "ovr", _profile_dict(
        tmp_path, data_owner="client",
        provider=_provider_block("bedrock", region=BEDROCK_EU),
        providers_allowed=["anthropic"],
        seat_providers={"reviewer": {"kind": "anthropic",
                                     "model": "rev-model"}}))
    impl = resolve_provider(profile, "implementer")
    rev = resolve_provider(profile, "reviewer")
    assert impl.kind == "bedrock" and rev.kind == "anthropic"
    assert impl.endpoint_host == BEDROCK_EU_HOST
    assert rev.endpoint_host == ANTHROPIC_HOST
    assert set(rev.env_keys) == {
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "API_TIMEOUT_MS"}
    assert rev.credential_key == "ANTHROPIC_AUTH_TOKEN"
    # The seat's settings carry its own host only.
    run_dir = tmp_path / "runs" / "p" / "20260101T000000Z-t"
    run_dir.mkdir(parents=True)
    for seat, expect in (("implementer", [BEDROCK_EU_HOST]),
                         ("reviewer", [ANTHROPIC_HOST])):
        spec = resolve_provider(profile, seat)
        settings = build_settings(
            profile, run_dir, model_endpoint=spec.egress_hosts,
            factory_root=tmp_path)
        assert settings["network"]["allowedDomains"] == expect, seat

    # (b) The refused override: a provider outside the list — refused
    # at LOAD (the front half), and the resolver's own backstop refuses
    # a hand-built dict that skipped validation the same way.
    with pytest.raises(ContractError, match="providers_allowed"):
        _load(tmp_path / "refused", _profile_dict(
            tmp_path, data_owner="client",
            provider=_provider_block("bedrock", region=BEDROCK_EU),
            providers_allowed=["anthropic"],
            seat_providers={"reviewer": {"kind": "vertex",
                                         "model": "m",
                                         "region": "europe-west1"}}))
    # No list at all: every override is outside it.
    with pytest.raises(ContractError, match="providers_allowed"):
        _load(tmp_path / "no-list", _profile_dict(
            tmp_path, data_owner="client",
            provider=_provider_block("bedrock", region=BEDROCK_EU),
            seat_providers={"reviewer": {"kind": "anthropic",
                                         "model": "m"}}))
    # The resolver backstop, driven directly on a hand-built dict.
    with pytest.raises(ProviderError, match="providers_allowed"):
        resolve_provider({
            "provider": _provider_block("bedrock", region=BEDROCK_EU),
            "seat_providers": {"reviewer": {"kind": "anthropic",
                                            "model": "m"}},
        }, "reviewer")
    # A client project may not allow or override to zai at all.
    with pytest.raises(ContractError, match="zai"):
        _load(tmp_path / "allow-zai", _profile_dict(
            tmp_path, data_owner="client",
            provider=_provider_block("bedrock", region=BEDROCK_EU),
            providers_allowed=["zai"]))

    # (c) No fallback — each trigger fails closed with the reason named,
    # and no Z.ai env or host exists anywhere on the retry.
    monkeypatch.setattr(provider, "_keychain_token", lambda service: None)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    client_profile = _client_bedrock(tmp_path)

    # missing credential: the session refuses, naming the provider and
    # its keychain item — it does NOT fall back to Z.ai's credential.
    with pytest.raises(SessionConfigError, match="factory-bedrock"):
        run_session_sandboxed(
            "implementer", "p", tmp_path, client_profile,
            factory_root=tmp_path, settings_path=tmp_path / "s.json",
            max_turns=2, timeout_min=1,
            srt_bin_path=None,
        )

    # quota: Bedrock's throttle shape still classifies as quota (so
    # F-64's requeue works on the SAME provider) — and the requeue
    # re-resolves the same provider, never Z.ai.
    from factory.orchestrator import _is_quota
    from factory.session import SessionRecord
    for reason in ("api_error_429: too many requests",
                   "error_throttlingexception: ThrottlingException",
                   "rate limit exceeded"):
        assert _is_quota(SessionRecord(
            seat="implementer", status="failed", reason=reason)), reason
    for spec_seat in (None, "implementer", "reviewer", "verifier",
                      "importer"):
        again = resolve_provider(client_profile, spec_seat)
        assert again.kind == "bedrock", spec_seat
        assert ZAI_HOST not in again.egress_hosts

    # error: an unknown-error record changes no resolution either.
    failing = SessionRecord(seat="implementer", status="failed",
                            reason="api_error_500")
    assert not _is_quota(failing)
    after_error = resolve_provider(client_profile, "implementer")
    assert after_error.kind == "bedrock"

    # And the env a retried session would build has no Z.ai trace.
    spec = resolve_provider(client_profile, "implementer")
    env = spec.env("a-bedrock-token", 600_000)
    assert "api.z.ai" not in json.dumps(env)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"

    # (d) A self project keeps today's behaviour exactly: the factory's
    # own queue/profile runs on zai (F-64's quota requeue unchanged).
    self_profile = _profile_dict(tmp_path)
    self_spec = resolve_provider(self_profile, "implementer")
    assert self_spec.kind == "zai"
    assert self_spec.endpoint_host == ZAI_HOST
    monkeypatch.setattr(provider, "_keychain_token",
                        lambda service: "zai-key" if service == "factory-zai"
                        else None)
    assert provider_token("zai") == "zai-key"


# --- AC-6: one resolution for run, import and eval ---------------------------


class _CapturedSandbox:
    """Monkeypatched factory.sandbox.run_sandboxed capturing the env and
    the settings each sandboxed session is launched with (the F-57 test
    pattern: the real construction functions run up to the exec)."""

    def __init__(self, monkeypatch, record_text):
        self.calls: list[dict] = []
        self.record_texts = (list(record_text) if isinstance(record_text, list)
                             else [record_text])
        self._record_iter = iter(self.record_texts)
        # session.py binds run_sandboxed at import (from factory.sandbox
        # import run_sandboxed), so the seam to patch is factory.session's
        # own reference - the tests/test_f57.py pattern.
        import factory.session as session_mod

        outer = self

        def fake_run_sandboxed(cmd, run_dir, settings_path, *, env=None,
                               timeout=None, srt_bin_path=None,
                               on_stdout_line=None, **kwargs):
            settings = json.loads(Path(settings_path).read_text())
            spec_file = [a for a in cmd if str(a).endswith("spec.json")]
            spec = {}
            if spec_file:
                spec = json.loads(Path(spec_file[0]).read_text())
            outer.calls.append({
                "argv": [str(a) for a in cmd],
                "env": dict(env or {}),
                "settings": settings,
                "spec": spec,
            })
            # The F-56 record transport: write the planted record where
            # the orchestrator will read it back — one scripted text per
            # call, in order.
            record_dir = Path(settings_path).with_name(
                Path(settings_path).name + "-record")
            record_dir.mkdir(parents=True, exist_ok=True)
            try:
                next_text = next(outer._record_iter)
            except StopIteration:
                next_text = outer.record_texts[-1]
            (record_dir / "record.json").write_text(next_text)
            return subprocess.CompletedProcess(
                cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(session_mod, "run_sandboxed",
                            fake_run_sandboxed)

    def last(self) -> dict:
        assert self.calls, "no sandboxed session ran"
        return self.calls[-1]


_PASSING_IMPLEMENTER = json.dumps({
    "seat": "implementer", "status": "completed", "num_turns": 2,
    "result": "done", "model": "model-impl",
    "tokens": {"input_tokens": 10, "output_tokens": 5,
               "cache_read_input_tokens": 0,
               "cache_creation_input_tokens": 0},
    "provider": {"kind": "bedrock", "model": "model-impl",
                 "region": BEDROCK_EU},
    "usage": {"input": 10, "cache_read": 0, "cache_write": 0,
              "output": 5},
})


def test_ac_6(tmp_path: Path, monkeypatch):
    from factory.importer import sdk_import_session
    from factory.provider import resolve_provider
    from factory.session import run_session_sandboxed

    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "bed-secret"
                        if service == "factory-bedrock" else None)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    profile = _client_bedrock(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                    "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name",
                    "Fixture"], check=True)
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text("# plan\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"],
                   check=True)

    # The importer's session construction for the client project.
    captured = _CapturedSandbox(monkeypatch, _PASSING_IMPLEMENTER.replace(
        '"implementer"', '"reviewer"').replace("model-impl", "model-imp"))
    try:
        sdk_import_session(
            "review the docs", repo, profile=profile,
            queue_path=None, factory_root=tmp_path,
            max_turns=2, timeout_min=1,
        )
    except Exception as exc:  # the refusal-after-session paths are fine
        assert "could not run" not in str(exc), exc
    import_call = captured.last()
    assert import_call["spec"]["env_keys"], "the spec carries env_keys"

    # What `factory run` builds for the same profile (the implementer
    # seat through the real run_session_sandboxed).
    run_dir = tmp_path / "runs" / "clientproj" / "20260101T000000Z-ac6"
    run_dir.mkdir(parents=True)
    run_session_sandboxed(
        "implementer", "p", run_dir, profile,
        factory_root=tmp_path, max_turns=2, timeout_min=1,
        srt_bin_path=None,
    )
    run_call = captured.last()

    # The resolved provider, the env key set and the egress hosts are
    # the SAME, driven through the real construction functions.
    for call in (import_call, run_call):
        assert set(call["env"]) == {
            "CLAUDE_CODE_USE_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_REGION", "API_TIMEOUT_MS"}
        assert call["env"]["AWS_BEARER_TOKEN_BEDROCK"] == "bed-secret"
        assert call["env"]["AWS_REGION"] == BEDROCK_EU
        assert call["settings"]["network"]["allowedDomains"] == [
            BEDROCK_EU_HOST]
        assert ZAI_HOST not in json.dumps(call["settings"])
    assert import_call["spec"]["env_keys"] == run_call["spec"]["env_keys"]

    # R1-2: the REPAIR is an import session too — it resolves the
    # importer role's provider and sends THAT provider's own credential
    # to THAT provider's host. Driven through the real run_import: the
    # main session's scripted answer is a repairable-but-broken
    # proposal, the repair's is valid; both sessions run through the
    # REAL run_session_sandboxed with only run_sandboxed captured, and
    # the (provider, host, credential) triple of every call must be
    # consistent. The reviewer seat OVERRIDES to bedrock here — the
    # exact shape that used to send the anthropic key to the AWS host.
    from _support.f57 import _broken_wo, _completed, _proposal, _prose_repo, _wo

    from factory.importer import run_import

    monkeypatch.setattr("factory.provider._keychain_token", lambda service: {
        "factory-anthropic": "ANTH-KEY-0123456789",
        "factory-bedrock": "BED-KEY-0123456789",
    }.get(service))
    repo = _prose_repo(tmp_path)
    # Z16 (round-1 addendum): the importer ROLE gets its own model
    # (and would get its own override) — distinct from the reviewer's,
    # so a dropped role= surfaces as the wrong model/env/egress.
    repair_profile = _load(tmp_path / "repair", _profile_dict(
        tmp_path, name="prose-demo", data_owner="client",
        provider={
            "kind": "anthropic",
            "models": {"implementer": "impl-model",
                       "reviewer": "rev-model",
                       "verifier": "ver-model",
                       "importer": "IMPORTER-MODEL"}},
        providers_allowed=["anthropic", "bedrock"],
        seat_providers={"reviewer": {"kind": "bedrock",
                                     "model": "rev-model",
                                     "region": BEDROCK_EU}}))
    (repo / ".factory").mkdir(exist_ok=True)
    (repo / ".factory" / "project.yaml").write_text(
        yaml.safe_dump(repair_profile))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "client provider"],
        check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@e.com",
             "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@e.com",
             "PATH": "/usr/bin:/bin"})

    # The F-63 ledger: approve the client profile under the import's
    # factory root (the owner step every real run_import needs).
    from factory.approvals import ApprovalLedger
    ApprovalLedger.for_project(tmp_path / "froot2", "prose-demo") \
        .record_profile_for(repair_profile, approved_by="owner")

    main_record = json.dumps(_completed(
        _proposal(_broken_wo())).to_dict())
    repair_record = json.dumps(_completed(
        _proposal(_wo())).to_dict())
    captured2 = _CapturedSandbox(monkeypatch,
                                 [main_record, repair_record])
    try:
        run_import(repo, factory_root=tmp_path / "froot2",
                   confirm=lambda s: True)
    except Exception as exc:
        assert "could not run" not in str(exc), exc
    assert len(captured2.calls) == 2, [
        c["env"].get("ANTHROPIC_BASE_URL") for c in captured2.calls]
    for call in captured2.calls:
        env, domains = call["env"], call["settings"]["network"][
            "allowedDomains"]
        # Z16: the importer ROLE's model on both sessions — the
        # reviewer's override/model never leaks into an import seat.
        assert call["spec"]["model"] == "IMPORTER-MODEL", call["spec"]
        # The triple is consistent: the anthropic credential, the
        # anthropic env shape, the anthropic host — on BOTH import
        # sessions. No AWS name carries the anthropic key; the bedrock
        # host never appears.
        assert env.get("ANTHROPIC_AUTH_TOKEN") == "ANTH-KEY-0123456789", env
        assert "AWS_BEARER_TOKEN_BEDROCK" not in env, env
        assert domains == [ANTHROPIC_HOST], domains
        assert BEDROCK_EU_HOST not in json.dumps(call["settings"])

    # R2-5: at the point of SENDING — for every seat kind the resolver
    # produces, with credentials present for EVERY kind (distinct
    # values) plus the ambient names, the env's credential key holds
    # exactly that kind's own token and no other kind's token appears
    # in the env, the spec or the settings. Driven through the real
    # run_session_sandboxed AND the real fan-out.
    ALL_CREDS = {
        "factory-zai": "ZAI-OWN-0123456789",
        "factory-anthropic": "ANTH-OWN-0123456789",
        "factory-bedrock": "BED-OWN-0123456789",
        "factory-anthropic_compatible": "COMP-OWN-0123456789",
    }
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: ALL_CREDS.get(service))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "AMBIENT-TOKEN-aaa")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "AMBIENT-BEDROCK-bbb")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://foreign.example")

    from factory.session import run_sessions_parallel

    compat_url = f"https://{COMPAT_HOST}/api"
    r25_cases = [
        ("zai", None, "ZAI-OWN-0123456789", "ANTHROPIC_AUTH_TOKEN",
         "https://api.z.ai/api/anthropic"),
        ("anthropic", "client", "ANTH-OWN-0123456789",
         "ANTHROPIC_AUTH_TOKEN", "https://api.anthropic.com"),
        ("bedrock", "client", "BED-OWN-0123456789",
         "AWS_BEARER_TOKEN_BEDROCK", None),
        ("anthropic_compatible", "client", "COMP-OWN-0123456789",
         "ANTHROPIC_AUTH_TOKEN", compat_url),
    ]
    for kind, owner, own_token, cred_key, url in r25_cases:
        extra = {"provider": _provider_block(
            kind, base_url=url) if kind == "anthropic_compatible"
            else _provider_block(
                kind, region=BEDROCK_EU if kind == "bedrock" else None)}
        if owner:
            extra["data_owner"] = owner
        profile_case = extra.pop("provider")
        if kind == "anthropic_compatible":
            profile_case = _provider_block("anthropic_compatible",
                                           base_url=compat_url)
        elif kind == "bedrock":
            profile_case = _provider_block("bedrock", region=BEDROCK_EU)
        r25_profile = _profile_dict(tmp_path, **{
            **({"data_owner": owner} if owner else {}),
            "provider": profile_case})
        r25_dir = tmp_path / "runs" / "clientproj" / f"20260101T000000Z-{kind}"
        r25_dir.mkdir(parents=True)

        captures_r25: list[dict] = []

        def capture_r25(cmd, run_dir, settings_path, *, env=None,
                        timeout=None, srt_bin_path=None,
                        on_stdout_line=None, **kwargs):
            settings = json.loads(Path(settings_path).read_text())
            spec = {}
            sf = [a for a in cmd if str(a).endswith("spec.json")]
            if sf:
                spec = json.loads(Path(sf[0]).read_text())
            captures_r25.append({"env": dict(env or {}),
                                 "settings": settings, "spec": spec})
            rp = Path(settings_path).with_name(
                Path(settings_path).name + "-record")
            rp.mkdir(parents=True, exist_ok=True)
            seat = spec.get("seat") or "implementer"
            (rp / "record.json").write_text(json.dumps({
                "seat": seat, "status": "completed", "result": "ok"}))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("factory.session.run_sandboxed", capture_r25)
        # Single session (implementer seat).
        run_session_sandboxed(
            "implementer", "p", r25_dir, r25_profile,
            factory_root=tmp_path, max_turns=1, timeout_min=1,
            srt_bin_path=None)
        # Real fan-out (reviewer seat, same profile/kind).
        subprocess.run(["git", "-C", str(r25_dir), "init", "-q", "-b", "main"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r25_dir), "config",
                        "user.email", "f@e.com"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(r25_dir), "config",
                        "user.name", "F"], check=True, capture_output=True)
        (r25_dir / "x").write_text("x\n")
        subprocess.run(["git", "-C", str(r25_dir), "add", "-A"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r25_dir), "commit", "-qm", "b"],
                       check=True, capture_output=True)
        run_sessions_parallel([("reviewer", "r")], r25_dir, r25_profile,
                              factory_root=tmp_path, max_turns=1,
                              timeout_min=1)
        assert len(captures_r25) == 2, (kind, len(captures_r25))
        others = [v for k, v in ALL_CREDS.items() if v != own_token]
        for call in captures_r25:
            env = call["env"]
            assert env.get(cred_key) == own_token, (kind, env)
            blob = json.dumps(call)
            for foreign in others + ["AMBIENT-TOKEN-aaa",
                                     "AMBIENT-BEDROCK-bbb"]:
                assert foreign not in blob, (kind, foreign)
        monkeypatch.undo()
        monkeypatch.setattr("factory.provider._keychain_token",
                            lambda service: ALL_CREDS.get(service))
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "AMBIENT-TOKEN-aaa")
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "AMBIENT-BEDROCK-bbb")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://foreign.example")

    # R2-1: a session-written record claiming provider.kind zai on a
    # client bedrock run: the PARENT re-stamps the record's provider
    # from ITS resolver's spec, so the returned record, the attempt's
    # breakdown and the status table all show bedrock — no zai row.
    z21_dir = tmp_path / "runs" / "clientproj" / "20260101T000000Z-r21"
    z21_dir.mkdir(parents=True)
    forged_record = {
        "seat": "implementer", "status": "completed",
        "result": "done", "model": "glm-forged",
        "provider": {"kind": "zai", "model": "glm-forged",
                     "endpoint_host": ZAI_HOST},
        "usage": {"input": 100, "cache_read": 0, "cache_write": 0,
                  "output": 10},
    }

    import pathlib

    def forge_provider(cmd, run_dir, settings_path, *, env=None,
                       timeout=None, srt_bin_path=None,
                       on_stdout_line=None, **kwargs):
        rp = pathlib.Path(settings_path).with_name(
            pathlib.Path(settings_path).name + "-record")
        rp.mkdir(parents=True, exist_ok=True)
        (rp / "record.json").write_text(json.dumps(forged_record))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("factory.session.run_sandboxed", forge_provider)
    returned = run_session_sandboxed(
        "implementer", "p", z21_dir, _client_bedrock(tmp_path),
        factory_root=tmp_path, max_turns=1, timeout_min=1,
        srt_bin_path=None)
    assert returned.provider == {"kind": "bedrock", "model": "model-impl",
                                 "region": BEDROCK_EU}, returned.provider
    # The TIMEOUT path re-stamps too: a runner that hung AFTER the entry
    # wrote its (forged) record returns the record with the PARENT's
    # provider.
    def hang_after_write(cmd, run_dir, settings_path, *, env=None,
                         timeout=None, srt_bin_path=None,
                         on_stdout_line=None, **kwargs):
        rp = pathlib.Path(settings_path).with_name(
            pathlib.Path(settings_path).name + "-record")
        rp.mkdir(parents=True, exist_ok=True)
        (rp / "record.json").write_text(json.dumps(forged_record))
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=90.0)

    monkeypatch.setattr("factory.session.run_sandboxed", hang_after_write)
    hung = run_session_sandboxed(
        "implementer", "p", z21_dir, _client_bedrock(tmp_path),
        factory_root=tmp_path, max_turns=1, timeout_min=1,
        srt_bin_path=None)
    assert hung.provider == {"kind": "bedrock", "model": "model-impl",
                             "region": BEDROCK_EU}, hung.provider
    monkeypatch.undo()

    # Z15 (round-1 addendum): the in-sandbox entry reads ONLY the
    # spec's env_keys from its environment — every ambient
    # ANTHROPIC_*/AWS_* name planted around it reaches nothing.
    from factory import session as session_mod
    from factory.session import _stage_runner

    _stage_runner(tmp_path / "z15-runner")
    z15_spec = tmp_path / "z15-spec.json"
    z15_spec.write_text(json.dumps({
        "seat": "implementer", "prompt": "p", "cwd": str(tmp_path),
        "max_turns": 1, "timeout_min": 0.5,
        "env_keys": ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                     "API_TIMEOUT_MS"],
        "credential_key": "ANTHROPIC_AUTH_TOKEN",
        "provider": {"kind": "zai", "endpoint_host": "api.z.ai"},
        "timeout_ms": 60000, "protected": [],
        "record_path": str(tmp_path / "z15-record" / "record.json"),
    }))
    (tmp_path / "z15-record").mkdir(exist_ok=True)
    seen_env: dict = {}

    async def capture_session(*args, **kwargs):
        seen_env.update(kwargs.get("env") or {})
        from factory.session import SessionRecord
        return SessionRecord(seat="implementer", status="completed")

    monkeypatch.setattr(session_mod, "run_session", capture_session)
    # The arm controls EVERY name it asserts on: the three env_keys come
    # from the environment the orchestrator's run_sandboxed would have
    # built (R3-1 — the host's ambient API_TIMEOUT_MS must be neither
    # required nor trusted), each with the value the spec's session
    # would carry; the foreign names carry planted JUNK values.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "zai-tok")
    monkeypatch.setenv("API_TIMEOUT_MS", "60000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-ambient")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "ambient-bed")
    rc = session_mod._main([str(z15_spec)])
    assert rc == 0
    # The env handed to the session is EXACTLY the spec's env_keys, each
    # with the environment's value (the orchestrator's env IS the source
    # _main reads) — nothing else crosses, whatever the ambient holds.
    assert set(seen_env) == {"ANTHROPIC_BASE_URL",
                             "ANTHROPIC_AUTH_TOKEN", "API_TIMEOUT_MS"}
    assert seen_env["ANTHROPIC_AUTH_TOKEN"] == "zai-tok"
    assert seen_env["API_TIMEOUT_MS"] == "60000"

    # And eval takes the same resolver: the eval profile (factory-owned,
    # self) resolves exactly like a run-built zai profile.
    from factory.eval import eval_profile
    from factory.provider import provider_token
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "zai-key"
                        if service == "factory-zai" else None)
    eprofile = eval_profile(tmp_path)
    espec = resolve_provider(eprofile, "reviewer")
    zspec = resolve_provider(_profile_dict(tmp_path), "reviewer")
    assert espec.kind == zspec.kind == "zai"
    assert espec.env_keys == zspec.env_keys
    assert espec.egress_hosts == zspec.egress_hosts == (ZAI_HOST,)
    zenv = zspec.env(provider_token("zai") or "t", 600_000)
    assert zenv["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"


# --- AC-7: the owner's paid run (structural only — never a paid call) --------


def test_ac_7(tmp_path: Path):
    from factory.provider import keychain_service, resolve_provider

    # The runbook's profile (rehearsal-fixture shape, data_owner client,
    # kind bedrock, an EU region, Sonnet 5.5) resolves: kind, region,
    # host and the keychain item to create.
    profile = _profile_dict(
        tmp_path, name="rehearsal",
        data_owner="client",
        provider={
            "kind": "bedrock",
            "region": "eu-north-1",
            "models": {
                "implementer": "anthropic.claude-sonnet-5-5",
                "reviewer": "anthropic.claude-sonnet-5-5",
                "verifier": "anthropic.claude-sonnet-5-5",
                "importer": "anthropic.claude-sonnet-5-5",
            },
        },
    )
    spec = resolve_provider(load_profile(_write_profile(tmp_path, profile)),
                            "implementer")
    assert spec.kind == "bedrock"
    assert spec.region == "eu-north-1"
    assert spec.endpoint_host == "bedrock-runtime.eu-north-1.amazonaws.com"
    assert keychain_service(spec.kind) == "factory-bedrock"
    assert spec.credential_key == "AWS_BEARER_TOKEN_BEDROCK"

    # The runbook itself is written into the F-59 BOOTSTRAP entry: the
    # command, the keychain item, the profile, the record expectations
    # and the egress-log check.
    bootstrap = (REPO_ROOT / ".factory" / "BOOTSTRAP.md").read_text()
    section = bootstrap.split("## F-59", 1)[-1]
    for needle in ("security add-generic-password -s factory-bedrock",
                   "data_owner", "bedrock", "eu-north-1",
                   "sonnet", "egress", "AC-7"):
        assert needle.lower() in section.lower(), needle
