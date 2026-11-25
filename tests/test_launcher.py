import json
import pytest
from excudo.launcher import load_settings


def test_existing_launcher_settings_only_set_excudo_variables(tmp_path,monkeypatch):
    folder=tmp_path/'.excudo';folder.mkdir()
    (folder/'launcher-env.json').write_text(json.dumps({'EXCUDO_BASE_URL':'http://localhost:9876/v1','PATH':'untrusted','EXCUDO_AUTO_APPROVE':0}),encoding='utf-8')
    monkeypatch.delenv('EXCUDO_BASE_URL',raising=False)
    monkeypatch.setenv('PATH','original')
    monkeypatch.delenv('EXCUDO_AUTO_APPROVE',raising=False)
    import os
    load_settings(tmp_path)
    assert os.environ['EXCUDO_BASE_URL']=='http://localhost:9876/v1'
    assert os.environ['PATH']=='original'
    assert os.environ['EXCUDO_AUTO_APPROVE']=='0'


def test_invalid_settings_do_not_silently_start_with_another_endpoint(tmp_path):
    folder=tmp_path/'.excudo';folder.mkdir()
    (folder/'launcher-env.json').write_text('[]',encoding='utf-8')
    with pytest.raises(ValueError): load_settings(tmp_path)
