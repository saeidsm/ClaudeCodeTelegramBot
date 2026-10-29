"""Synthetic provider and local readiness tests; no credentials or inference."""
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest
from engines import claude_gateway as g

spec = importlib.util.spec_from_file_location('local_readiness', Path(__file__).resolve().parents[1] / 'scripts/claude-readiness.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)

@pytest.fixture
def local(tmp_path, monkeypatch):
    wrapper = tmp_path / 'wrapper'; wrapper.write_text('synthetic reviewed wrapper')
    env = tmp_path / 'bridge.env'
    env.write_text('BRIDGE_PORT=8317\nBRIDGE_API_KEY=synthetic-only\nBRIDGE_MODEL=gpt-6.1-terra\nBRIDGE_OPUS_MODEL=gpt-6.1-sol\nBRIDGE_FAST_MODEL=gpt-6-luna\n')
    cfg = tmp_path / 'claude-code.json'
    cfg.write_text(json.dumps({'provider':'chatgpt-local','executable':str(wrapper),'env_file':str(env)}))
    monkeypatch.setattr(r,'LOCAL_WRAPPER',str(wrapper)); monkeypatch.setattr(r,'LOCAL_ENV',str(env))
    monkeypatch.setattr(r,'LOCAL_SHA',hashlib.sha256(wrapper.read_bytes()).hexdigest())
    monkeypatch.setattr(r,'trusted_read',lambda p,private=False:(Path(p).read_bytes(),None))
    calls=[]
    monkeypatch.setattr(r,'verify_local_catalog',lambda token,models:calls.append((token,models)))
    return tmp_path,wrapper,env,cfg,calls

def test_readiness_local_only(local):
    root,wrapper,_,_,calls=local
    assert r.resolve(root,'',lambda t:pytest.fail('paid route called'))==str(wrapper)
    assert calls==[('synthetic-only',['gpt-6.1-terra','gpt-6.1-sol','gpt-6-luna'])]

@pytest.mark.parametrize('change',['wrapper','port','unknown_model','missing_model','shell','extra_setting','wrong_path'])
def test_invalid_local_contract_never_falls_back(local,change):
    root,wrapper,env,cfg,calls=local
    if change=='wrapper':wrapper.write_text('changed')
    elif change=='wrong_path':cfg.write_text(cfg.read_text().replace(str(wrapper),'/unreviewed'))
    else:
        s=env.read_text()
        if change=='port':s=s.replace('8317','9999')
        elif change=='unknown_model':s=s.replace('gpt-6.1-sol','paid/model')
        elif change=='missing_model':s=s.replace('BRIDGE_FAST_MODEL=gpt-6-luna\n','')
        elif change=='shell':s=s.replace('synthetic-only','$(id)')
        else:s+='ANTHROPIC_BASE_URL=https://other.invalid\n'
        env.write_text(s)
    with pytest.raises(ValueError):r.resolve(root,'',lambda t:pytest.fail('paid fallback'))
    assert calls==[]

def test_catalog_failure_never_falls_back(local,monkeypatch):
    def fail(*args):raise TimeoutError('synthetic')
    monkeypatch.setattr(r,'verify_local_catalog',fail)
    with pytest.raises(TimeoutError):r.resolve(local[0],'',lambda t:pytest.fail('paid fallback'))

def test_provider_aliases_and_reject_old_paid_model(local,monkeypatch):
    _,wrapper,env,_,_=local
    monkeypatch.setattr(g,'settings',lambda:{'provider':'chatgpt-local','executable':str(wrapper),'env_file':str(env)})
    for alias,model in [('', 'gpt-6.1-terra'),('sonnet','gpt-6.1-terra'),('opus','gpt-6.1-sol'),('haiku','gpt-6-luna')]:
        assert g.launch(alias)==(str(wrapper),model)
        assert g.launch(model)==(str(wrapper),model)
    with pytest.raises(ValueError):g.launch('z-ai/glm-5.3:floor')

def test_local_settings_refuse_arbitrary_executable(tmp_path,monkeypatch):
    monkeypatch.setenv('BOT_CONFIGS_DIR',str(tmp_path))
    (tmp_path/'claude-code.json').write_text(json.dumps({'provider':'chatgpt-local','executable':'/unreviewed','env_file':'/unreviewed'}))
    with pytest.raises(ValueError):g.settings()

@pytest.mark.parametrize('body',[b'{',b'{"data":[]}',b'{"data":[{}]}',b'{"data":[{"id":"other"}]}',b'x'*1048577])
def test_catalog_invalid_payload_refuses(monkeypatch,body):
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,n):assert n==1048577;return body
    class Opener:
        def open(self,req,timeout):
            assert req.full_url=='http://127.0.0.1:8317/v1/models'
            assert timeout==10
            return Response()
    def build(*handlers):
        assert handlers[0].proxies=={}
        assert handlers[1] is r.NoRedirect
        return Opener()
    monkeypatch.setattr(r.urllib.request,'build_opener',build)
    with pytest.raises(ValueError):r.verify_local_catalog('synthetic',['gpt-6.1-terra'])

@pytest.mark.parametrize('previous', ['', 'openrouter', 'anthropic'])
def test_legacy_or_foreign_resume_refuses_subscription(previous):
    from types import SimpleNamespace
    session=SimpleNamespace(claude_created=True,claude_provider=previous)
    with pytest.raises(ValueError):g.bind_provider(session,{'provider':'chatgpt-local'})
    assert session.claude_provider==previous

def test_new_session_binds_and_same_provider_resumes():
    from types import SimpleNamespace
    session=SimpleNamespace(claude_created=False,claude_provider='')
    assert g.bind_provider(session,{'provider':'chatgpt-local'})=='chatgpt-local'
    session.claude_created=True
    assert g.bind_provider(session,{'provider':'chatgpt-local'})=='chatgpt-local'
    with pytest.raises(ValueError):g.bind_provider(session,{'provider':'openrouter'})
