#!/usr/bin/env python3
"""Source-pinned publisher integration; never starts polling or private IPC."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

BOOTSTRAP_SHA = '47a7eee55d116dc2bae91630cd87c42b407087f52f200bf545e6a49bc2ceb939'
RELEASE = Path('/opt/shahrzad-devops/tools/nikbot-mcp/releases/4232a44d521c03ff752ff8e8f868eb8310d11d06')
CONFIG = Path('/etc/nikbot-mcp/publisher.json')
APPLICATION = Path('/opt/shahrzad-devops/scripts/claude-telegram-bot.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def trusted_read(path, private=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('canonical_path_required')
    for parent in path.parents:
        st = parent.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError('untrusted_parent')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1
                or st.st_mode & 0o022 or (private and stat.S_IMODE(st.st_mode) != 0o600)
                or not 0 < st.st_size <= 2_000_000):
            raise ValueError('untrusted_file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(2_000_001)
        if len(data) != st.st_size:
            raise ValueError('file_changed')
        return data, st
    finally:
        os.close(fd)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_setting')
        result[key] = value
    return result


def settings(data):
    value = json.loads(data, object_pairs_hook=unique_object)
    keys = {'application_path', 'application_sha256', 'private_root', 'socket_path', 'runner_uid'}
    if (not isinstance(value, dict) or not keys <= value.keys()
            or value.keys() - keys - {'codex_quiesced'}
            or type(value.get('codex_quiesced', False)) is not bool):
        raise ValueError('publisher_settings_contract')
    if value['application_path'] != str(APPLICATION):
        raise ValueError('application_path_changed')
    return value


def release_identity():
    files = sorted((RELEASE / 'remote_gateway').rglob('*.py'))
    if not files:
        raise ValueError('publisher_release_missing')
    return {str(p.relative_to(RELEASE)): digest(trusted_read(p)[0]) for p in files}


def prepare(command, candidate, output):
    matches = re.findall(r'(/[A-Za-z0-9_./-]+/publisher-bootstrap\.py)', command)
    if not matches:
        raise ValueError('publisher_bootstrap_not_identified')
    paths = set(matches)
    if len(paths) != 1:
        raise ValueError('ambiguous_publisher_bootstrap')
    bootstrap = Path(paths.pop())
    if digest(trusted_read(bootstrap)[0]) != BOOTSTRAP_SHA:
        raise ValueError('unreviewed_publisher_bootstrap')
    raw, meta = trusted_read(CONFIG, private=True)
    current = settings(raw)
    if current['application_sha256'] != digest(trusted_read(APPLICATION)[0]):
        raise ValueError('existing_application_pin_mismatch')
    plan = dict(bootstrap=str(bootstrap), bootstrap_sha=BOOTSTRAP_SHA,
                release=str(RELEASE), release_identity=release_identity(),
                config=str(CONFIG), config_sha=digest(raw),
                old_sha=current['application_sha256'], new_sha=digest(Path(candidate).read_bytes()),
                application=str(APPLICATION), uid=meta.st_uid, gid=meta.st_gid,
                mode=stat.S_IMODE(meta.st_mode))
    Path(output).write_text(json.dumps(plan))
    os.chmod(output, 0o600)
    print(CONFIG)  # path only; never private settings


def validate_identity(plan):
    if (plan['bootstrap_sha'] != BOOTSTRAP_SHA
            or digest(trusted_read(plan['bootstrap'])[0]) != BOOTSTRAP_SHA
            or plan['release'] != str(RELEASE)
            or plan['release_identity'] != release_identity()
            or plan['config'] != str(CONFIG) or plan['application'] != str(APPLICATION)):
        raise ValueError('publisher_identity_changed')


def update(plan):
    validate_identity(plan)
    raw, meta = trusted_read(CONFIG, private=True)
    current = settings(raw)
    if (digest(raw) != plan['config_sha'] or current['application_sha256'] != plan['old_sha']
            or (meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode)) !=
               (plan['uid'], plan['gid'], plan['mode'])
            or digest(trusted_read(APPLICATION)[0]) != plan['new_sha']):
        raise ValueError('publisher_update_precondition_changed')
    pattern = rb'("application_sha256"\s*:\s*")[0-9a-f]{64}(")'
    updated, count = re.subn(pattern, lambda m: m[1] + plan['new_sha'].encode() + m[2], raw)
    if count != 1 or settings(updated) != {**current, 'application_sha256': plan['new_sha']}:
        raise ValueError('publisher_pin_edit_invalid')
    fd, name = tempfile.mkstemp(prefix='.publisher-deploy-', dir=CONFIG.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(updated)
            stream.flush()
            os.fchown(stream.fileno(), meta.st_uid, meta.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(meta.st_mode))
            os.fsync(stream.fileno())
        # Do not overwrite an operator's intervening settings change.
        if digest(trusted_read(CONFIG, private=True)[0]) != plan['config_sha']:
            raise ValueError('publisher_config_raced')
        os.replace(name, CONFIG)
        dfd = os.open(CONFIG.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def smoke(plan, rollback=False):
    validate_identity(plan)
    raw, _ = trusted_read(CONFIG, private=True)
    current = settings(raw)
    expected = plan['old_sha'] if rollback else plan['new_sha']
    if current['application_sha256'] != expected:
        raise ValueError('publisher_smoke_pin_mismatch')
    sys.path[:0] = [str(RELEASE), str(APPLICATION.parent)]
    loader = importlib.import_module('remote_gateway.publisher_entrypoint')
    app = loader.load_application(str(APPLICATION), expected)
    if not rollback:
        bridge = importlib.import_module('mcp_bridge')
        if Path(bridge.__file__).resolve() != APPLICATION.parent / 'mcp_bridge.py':
            raise ValueError('publisher_mcp_import_shadowed')
    if current.get('codex_quiesced', False):
        from remote_gateway.codex_quiescence import quiesce_codex
        quiesce_codex(app, source_sha256=expected)
        if app._mcp_codex_quiesced is not True:
            raise ValueError('publisher_quiescence_missing')
    from telegram.ext import Application, ExtBot
    from telegram.request import HTTPXRequest
    bot = app.FooterBot('123456:AAHfake-offline-token_xxxxxxxxxxxxxxxxxx',
        request=HTTPXRequest(), get_updates_request=HTTPXRequest(connection_pool_size=1))
    built = Application.builder().bot(bot).build()
    if not isinstance(built.bot, ExtBot):
        raise ValueError('publisher_application_construction_failed')
    # Deliberately do not call run(), main(), attach(), post_init or networking.
    print('publisher-loader-smoke-ok')


if __name__ == '__main__':
    try:
        action = sys.argv[1]
        if action == 'prepare':
            prepare(*sys.argv[2:])
        else:
            plan = json.loads(Path(sys.argv[2]).read_text())
            if action == 'update': update(plan)
            elif action in ('smoke', 'rollback-smoke'): smoke(plan, action == 'rollback-smoke')
            else: raise ValueError('unknown_action')
    except Exception as error:
        # No configuration, application exception messages or credential values.
        print('publisher-deploy-failed:' + type(error).__name__, file=sys.stderr)
        raise SystemExit(3)
