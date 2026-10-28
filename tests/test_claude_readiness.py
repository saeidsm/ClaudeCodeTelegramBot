import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('readiness', Path(__file__).resolve().parents[1] / 'scripts/claude-readiness.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)


def test_subscription_never_probes_paid_gateway(tmp_path):
    exe = tmp_path / 'claude'; exe.write_text('#!/bin/sh\nexit 0\n'); exe.chmod(0o755)
    assert r.resolve(tmp_path, str(tmp_path), lambda token: pytest.fail('paid fallback')) == str(exe)


def test_missing_subscription_refuses_without_gateway(tmp_path):
    with pytest.raises(ValueError, match='subscription_executable_missing'):
        r.resolve(tmp_path, str(tmp_path), lambda token: pytest.fail('paid fallback'))


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    wrapper = tmp_path / 'wrapper'; wrapper.write_text('reviewed wrapper')
    env = tmp_path / 'private.env'
    env.write_text('ANTHROPIC_BASE_URL=https://openrouter.ai/api\nANTHROPIC_AUTH_TOKEN=synthetic-token\nANTHROPIC_API_KEY=\nANTHROPIC_MODEL=synthetic/model\n')
    cfg = tmp_path / 'claude-code.json'
    cfg.write_text(json.dumps(dict(provider='openrouter', executable=str(wrapper), env_file=str(env))))
    monkeypatch.setattr(r, 'WRAPPER', str(wrapper)); monkeypatch.setattr(r, 'ENV_FILE', str(env))
    monkeypatch.setattr(r, 'WRAPPER_SHA', hashlib.sha256(wrapper.read_bytes()).hexdigest())
    calls = []
    monkeypatch.setattr(r, 'trusted_read', lambda p, private=False: calls.append((str(p), private)))
    return tmp_path, wrapper, env, cfg, calls


def test_gateway_requires_actual_key_verification(gateway):
    root, wrapper, env, cfg, calls = gateway
    tokens = []
    assert r.resolve(root, '', tokens.append) == str(wrapper)
    assert tokens == ['synthetic-token']
    assert calls == [(str(wrapper), False), (str(cfg), False), (str(env), True)]


def test_gateway_auth_failure_cannot_fall_back(gateway):
    root, *_ = gateway
    def reject(token): raise ValueError('rejected')
    with pytest.raises(ValueError, match='rejected'): r.resolve(root, '/usr/bin', reject)


@pytest.mark.parametrize('change', ['wrapper', 'endpoint', 'api_key', 'provider'])
def test_gateway_changed_contract_refuses_before_auth(gateway, change):
    root, wrapper, env, cfg, _ = gateway
    if change == 'wrapper': wrapper.write_text('changed wrapper')
    elif change == 'endpoint': env.write_text(env.read_text().replace('https://openrouter.ai/api', 'https://other.invalid'))
    elif change == 'api_key': env.write_text(env.read_text().replace('ANTHROPIC_API_KEY=', 'ANTHROPIC_API_KEY=conflict'))
    else: cfg.write_text(cfg.read_text().replace('openrouter', 'other'))
    with pytest.raises(ValueError): r.resolve(root, '', lambda t: pytest.fail('must not authenticate'))


def test_redirect_never_forwards_authentication():
    with pytest.raises(ValueError, match='redirect_refused'):
        r.NoRedirect().redirect_request(None, None, None, None, None, None)


def test_token_reference_resolves_only_previous_literal_values():
    assert r.resolve_literal_values({'BASE': 'synthetic', 'TOKEN': '$BASE'})['TOKEN'] == 'synthetic'
    assert r.resolve_literal_values({'BASE': 'synthetic', 'TOKEN': '${BASE}'})['TOKEN'] == 'synthetic'
    for values in ({'TOKEN': '$MISSING'}, {'TOKEN': '$(command)'}, {'TOKEN': '$LATER', 'LATER': 'x'}):
        with pytest.raises(ValueError): r.resolve_literal_values(values)
