#!/usr/bin/env python3
"""Resolve the configured Claude path; validate gateway credentials without inference."""
import hashlib
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


def resolve(configs, service_path, verify=verify_key):
    config = Path(configs) / 'claude-code.json'
    if not config.exists():
        executable = shutil.which('claude', path=service_path)
        if not executable:
            raise ValueError('subscription_executable_missing')
        return executable
    d = json.loads(config.read_text())
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
