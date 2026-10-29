#!/usr/bin/env python3
"""Resolve the configured Claude path; validate gateway credentials without inference."""
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import sys
import urllib.request
from dotenv import dotenv_values

WRAPPER_SHA = 'e0e75a1ff083632b55ffbe632fe547b102eaad6e27a335e7560964f83824efd1'
WRAPPER = '/root/.local/bin/claude-or'
ENV_FILE = '/root/.config/claude-openrouter/openrouter.env'

LOCAL_WRAPPER = '/root/.local/bin/claude-gpt'
LOCAL_ENV = '/root/.claude-gpt-bridge/bridge.env'
LOCAL_SHA = '76f97e5291528818d1ae22d7d1e2d7ab91f777ee29e154c0b4cff12da41b6310'
LOCAL_MODELS = {'gpt-6.1-terra', 'gpt-6.1-sol', 'gpt-6-luna'}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('redirect_refused')

def verify_key(token):
    request = urllib.request.Request('https://openrouter.ai/api/v1/key',
        headers={'Authorization': 'Bearer ' + token})
    # Never follow a redirect carrying credentials; never issue an inference request.
    with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError('gateway_auth_failed')
        data = json.loads(response.read(262145))
    if not isinstance(data.get('data'), dict):
        raise ValueError('gateway_auth_payload')
    info = data['data']
    if info.get('limit_remaining') is not None and info['limit_remaining'] <= 0:
        raise ValueError('gateway_budget_exhausted')
    for budget in info.get('budgets', []):
        if budget['remaining_usd'] <= 0:
            raise ValueError('gateway_budget_exhausted')

def trusted_read(path, private=False):
    import importlib.util
    spec = importlib.util.spec_from_file_location('publisher_deploy', Path(__file__).with_name('publisher-deploy.py'))
    trust = importlib.util.module_from_spec(spec); spec.loader.exec_module(trust)
    return trust.trusted_read(path, private=private)

def resolve_literal_values(values):
    resolved = {}
    for key, value in values.items():
        if value is not None and '$' in value:
            match = re.fullmatch(r'\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}', value)
            if not match or (match[1] or match[2]) not in resolved:
                raise ValueError('unsupported_gateway_reference')
            value = resolved[match[1] or match[2]]
        resolved[key] = value
    return resolved


def verify_local_catalog(token, models):
    """Local transport/catalog check; not proof of upstream entitlement/inference."""
    request = urllib.request.Request('http://127.0.0.1:8317/v1/models',
        headers={'Authorization': 'Bearer ' + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    with opener.open(request, timeout=10) as response:
        if response.status != 200:
            raise ValueError('local_catalog_status')
        raw = response.read(1048577)
    if len(raw) > 1048576:
        raise ValueError('local_catalog_too_large')
    data = json.loads(raw)
    rows = data.get('data') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] for row in rows):
        raise ValueError('local_catalog_schema')
    if not set(models) <= {row['id'] for row in rows}:
        raise ValueError('local_model_unavailable')


def resolve_local(config, d):
    if d.get('executable') != LOCAL_WRAPPER or d.get('env_file') != LOCAL_ENV:
        raise ValueError('unreviewed_local_gateway')
    config_bytes, _ = trusted_read(config)
    if json.loads(config_bytes) != d:
        raise ValueError('local_config_changed')
    wrapper, _ = trusted_read(LOCAL_WRAPPER)
    if hashlib.sha256(wrapper).hexdigest() != LOCAL_SHA:
        raise ValueError('local_wrapper_changed')
    env_bytes, _ = trusted_read(LOCAL_ENV, private=True)
    values = dotenv_values(stream=io.StringIO(env_bytes.decode()), interpolate=False)
    expected = {'BRIDGE_PORT', 'BRIDGE_API_KEY', 'BRIDGE_MODEL', 'BRIDGE_OPUS_MODEL', 'BRIDGE_FAST_MODEL'}
    if set(values) != expected or values['BRIDGE_PORT'] != '8317':
        raise ValueError('local_environment_contract')
    if any(not v or '$' in v or '`' in v or any(c.isspace() for c in v) for v in values.values()):
        raise ValueError('local_environment_literal_required')
    models = [values[k] for k in ['BRIDGE_MODEL', 'BRIDGE_OPUS_MODEL', 'BRIDGE_FAST_MODEL']]
    if not set(models) <= LOCAL_MODELS:
        raise ValueError('unreviewed_local_model')
    verify_local_catalog(values['BRIDGE_API_KEY'], models)
    return LOCAL_WRAPPER


def resolve(configs, service_path, verify=verify_key):
    config = Path(configs) / 'claude-code.json'
    if not config.exists():
        executable = shutil.which('claude', path=service_path)
        if not executable:
            raise ValueError('subscription_executable_missing')
        return executable
    d = json.loads(config.read_text())
    if d.get('provider') == 'chatgpt-local':
        return resolve_local(config, d)
    if d.get('provider') != 'openrouter' or d.get('executable') != WRAPPER or d.get('env_file') != ENV_FILE:
        raise ValueError('unreviewed_gateway')
    wrapper = Path(WRAPPER)
    if hashlib.sha256(wrapper.read_bytes()).hexdigest() != WRAPPER_SHA:
        raise ValueError('gateway_wrapper_changed')
    trusted_read(wrapper)
    trusted_read(config)
    trusted_read(ENV_FILE, private=True)
    values = resolve_literal_values(dotenv_values(ENV_FILE, interpolate=False))
    token = values.get('ANTHROPIC_AUTH_TOKEN', '')
    if (values.get('ANTHROPIC_BASE_URL') != 'https://openrouter.ai/api'
            or not token or any(c.isspace() for c in token) or '$' in token
            or values.get('ANTHROPIC_API_KEY')):
        raise ValueError('gateway_credentials_contract')
    if not values.get('ANTHROPIC_MODEL'):
        raise ValueError('gateway_model_missing')
    verify(token)
    return WRAPPER

if __name__ == '__main__':
    try:
        print(resolve(sys.argv[1], sys.argv[2]))
    except Exception as error:
        # Exception messages and response bodies may contain secrets.
        print('claude-readiness-failed:' + type(error).__name__, file=sys.stderr)
        sys.exit(3)
