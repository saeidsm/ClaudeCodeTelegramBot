"""Claude Code transport configuration; never configures Codex or Chat."""
import json
import os
from pathlib import Path

from dotenv import dotenv_values


def settings() -> dict:
    root = os.environ.get('BOT_CONFIGS_DIR') or str(Path(os.environ.get('BOT_DATA_ROOT', '/opt/shahrzad-devops')) / 'configs')
    path = Path(root) / 'claude-code.json'
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text())
        if config.get('provider') != 'openrouter':
            raise ValueError
        if not all(Path(config[k]).is_absolute() for k in ('executable', 'env_file')):
            raise ValueError
        return config
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('Invalid Claude gateway configuration') from None


def configured_models() -> dict[str, str]:
    config = settings()
    if not config:
        return {}
    # Parse, never source/execute a shell file in the bot. Only model IDs leave
    # this function; the wrapper alone loads credentials in the Claude child.
    try:
        with open(config['env_file']) as stream:
            values = dotenv_values(stream=stream, interpolate=False)
        keys = {'': 'ANTHROPIC_MODEL', **{a: f'ANTHROPIC_DEFAULT_{a.upper()}_MODEL' for a in ('opus', 'sonnet', 'haiku')}}
        models = {a: values.get(k) for a, k in keys.items() if values.get(k)}
        if not models.get('') or any('$' in v or any(c.isspace() for c in v) for v in models.values()):
            raise ValueError
        return models
    except (OSError, ValueError, TypeError):
        raise ValueError('Claude gateway model configuration is missing or invalid') from None


def launch(selected: str) -> tuple[str, str]:
    config = settings()
    if not config:
        return 'claude', selected or os.environ.get('BOT_DEFAULT_CLAUDE_MODEL', '')
    models = configured_models()
    if selected in models:
        model = models[selected]
    elif selected in models.values():
        model = selected
    else:
        raise ValueError('Selected Claude model is not configured for OpenRouter; choose /model again')
    return config['executable'], model


def failure_message(output: str) -> str:
    """Provider errors can contain child-only secrets; never echo their body."""
    low = output.lower()
    if any(k in low for k in ('401', 'unauthorized', 'authentication', '403')):
        reason = 'authentication/access rejected; check the gateway configuration'
    elif any(k in low for k in ('429', 'rate limit', 'quota')):
        reason = 'rate limit or quota reached; retry manually later'
    elif any(k in low for k in ('402', 'credit', 'payment')):
        reason = 'credits or payment required'
    else:
        reason = 'request failed; inspect the provider dashboard without sharing credentials'
    return f'❌ Claude Code → OpenRouter: {reason}.'
