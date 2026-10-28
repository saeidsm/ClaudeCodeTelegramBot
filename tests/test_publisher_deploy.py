"""Exercise the installed publisher loader unchanged inside private namespaces.

Only synthetic /opt and publisher configuration are mounted in the child.
No service, credential, socket, polling loop or model is started.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = Path('/opt/shahrzad-devops/tools/nikbot-mcp/releases/4232a44d521c03ff752ff8e8f868eb8310d11d06')
BOOTSTRAP = Path('/var/lib/nikbot-mcp/activation-347e785-9ceexh17/publisher-bootstrap.py')

CHILD = r'''
import hashlib, importlib.util, json, os, shutil, stat, subprocess, sys
from pathlib import Path
scenario, helper, candidate = sys.argv[1:]
spec=importlib.util.spec_from_file_location('publisher_deploy', helper)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
app=m.APPLICATION; config=m.CONFIG
old=app.read_bytes(); raw=config.read_bytes(); meta=config.stat()
planfile=Path('/opt/plan.json')
command=str(Path('/var/lib/nikbot-mcp/activation-347e785-9ceexh17/publisher-bootstrap.py'))
def refused(fn):
 try: fn()
 except (ValueError, OSError): return
 raise AssertionError('operation was not refused')
def smoke(mode, good=True):
 r=subprocess.run([sys.executable,helper,mode,str(planfile)],capture_output=True,text=True)
 assert (r.returncode == 0) == good, (r.returncode,r.stdout,r.stderr)
if scenario == 'old_pin_mismatch':
 d=json.loads(raw);d['application_sha256']='0'*64;config.write_text(json.dumps(d))
 refused(lambda:m.prepare(command,candidate,planfile));assert app.read_bytes()==old
elif scenario == 'duplicate_key':
 config.write_text(raw.decode().replace('{','{"application_sha256":"'+'0'*64+'",',1))
 refused(lambda:m.prepare(command,candidate,planfile));assert app.read_bytes()==old
else:
 m.prepare(command,candidate,planfile)
 plan=json.loads(planfile.read_text())
 app.write_bytes(Path(candidate).read_bytes())
 if scenario == 'config_race':
  d=json.loads(raw);d['private_root']='/opt/operator-change';config.write_text(json.dumps(d));changed=config.read_bytes()
  refused(lambda:m.update(plan));assert config.read_bytes()==changed
 elif scenario == 'application_changed':
  app.write_bytes(b'# unexpected application\n');refused(lambda:m.update(plan));assert config.read_bytes()==raw
 elif scenario == 'release_changed':
  p=m.RELEASE/'remote_gateway/publisher_entrypoint.py';p.write_bytes(p.read_bytes()+b'\n# modified\n')
  refused(lambda:m.update(plan));assert config.read_bytes()==raw
 else:
  m.update(plan)
  after=json.loads(config.read_bytes());before=json.loads(raw)
  assert after == {**before,'application_sha256':m.digest(app.read_bytes())}
  assert (config.stat().st_uid,config.stat().st_gid,stat.S_IMODE(config.stat().st_mode)) == (meta.st_uid,meta.st_gid,stat.S_IMODE(meta.st_mode))
  assert config.read_bytes()==raw.replace(plan['old_sha'].encode(),plan['new_sha'].encode())
  if scenario == 'loader_hash_mismatch':
   app.write_bytes(app.read_bytes()+b'\n# unexpected after pin update\n');smoke('smoke',good=False)
  elif scenario == 'shadow_import':
   (m.RELEASE/'mcp_bridge.py').write_text('MCP_DEFAULT_PROJECTS="Rag4Engineers"\n')
   smoke('smoke',good=False)
  else:
   smoke('smoke')
   # Simulate the installer's manifest restore, then use the SAME real loader.
   app.write_bytes(old);config.write_bytes(raw)
   smoke('rollback-smoke')
   assert config.read_bytes()==raw and app.read_bytes()==old
print('scenario-ok',scenario)
'''


@pytest.mark.parametrize('scenario', [
    'success_and_rollback', 'old_pin_mismatch', 'duplicate_key', 'config_race',
    'application_changed', 'release_changed', 'shadow_import', 'loader_hash_mismatch',
])
def test_real_publisher_loader_transaction(tmp_path, scenario):
    if os.geteuid() != 0 or not RELEASE.is_dir() or not BOOTSTRAP.is_file():
        pytest.skip('requires the reviewed DevOps publisher release and root namespace support')
    opt = tmp_path / 'opt'
    scripts = opt / 'shahrzad-devops/scripts'
    scripts.mkdir(parents=True)
    for name in ['bot.py', 'mcp_bridge.py', 'coordination.py', 'coord_publish.py',
                 'resource_footer.py', 'log_filters.py']:
        shutil.copyfile(ROOT/name, scripts/('claude-telegram-bot.py' if name=='bot.py' else name))
    for name in ['engines','video_module']:
        shutil.copytree(ROOT/name,scripts/name,ignore=shutil.ignore_patterns('__pycache__'))
    old=subprocess.run(['git','-C',str(ROOT),'show','17ca5c5:bot.py'],capture_output=True,check=True).stdout
    (scripts/'claude-telegram-bot.py').write_bytes(old)
    shutil.copytree(RELEASE,opt/RELEASE.relative_to('/opt'),ignore=shutil.ignore_patterns('__pycache__'))
    private = tmp_path/'publisher-config';private.mkdir(mode=0o700)
    import hashlib,json
    config=private/'publisher.json'
    config.write_text(json.dumps(dict(application_path='/opt/shahrzad-devops/scripts/claude-telegram-bot.py',
        application_sha256=hashlib.sha256(old).hexdigest(),private_root='/opt/synthetic-private',
        socket_path='/opt/synthetic-private/socket',runner_uid=12345,codex_quiesced=True)))
    config.chmod(0o600)
    data=opt/'synthetic-data';(data/'configs').mkdir(parents=True)
    (data/'configs/reports-token.env').write_text('REPORTS_PATH_TOKEN=offline-test\n')
    child=tmp_path/'child.py';child.write_text(CHILD)
    env=dict(os.environ,BOT_DATA_ROOT='/opt/synthetic-data',BOT_CONFIGS_DIR='/opt/synthetic-data/configs',
             PYTHONDONTWRITEBYTECODE='1')
    cmd=['unshare','--mount','--net','bash','-c',
         'set -eu; mount --make-rprivate /; mount --bind "$1" /opt; '
         'mount --bind "$2" /etc/nikbot-mcp; exec "$3" "$4" "$5" "$6" "$7"',
         'publisher-offline',str(opt),str(private),sys.executable,str(child),scenario,
         str(ROOT/'scripts/publisher-deploy.py'),str(ROOT/'bot.py')]
    result=subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=90)
    assert result.returncode==0, result.stdout+result.stderr
    assert 'scenario-ok' in result.stdout
