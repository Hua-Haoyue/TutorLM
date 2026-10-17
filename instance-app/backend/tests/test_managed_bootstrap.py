import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.adapters.http.app import create_app
from backend.host.config import HostConfig


@pytest.mark.parametrize('supplied', ['', 'wrong'])
def test_bootstrap_denies_invalid_credentials(env, supplied):
    config = HostConfig.from_env({**env, 'TUTORLM_APP_BOOTSTRAP_TOKEN': 'server-only-secret'})
    assert 'server-only-secret' not in repr(config)
    with TestClient(create_app(config)) as client:
        assert client.post('/internal/app-session', headers={'X-App-Bootstrap-Token': supplied}).status_code == 403
        assert client.post('/api/sessions', json={'invite_code': 'TLM-DEMO-TEST'}).status_code == 401


def test_bootstrap_disabled_by_default(env):
    with TestClient(create_app(HostConfig.from_env(env))) as client:
        assert client.post('/internal/app-session', headers={'X-App-Bootstrap-Token': 'anything'}).status_code == 403


def test_valid_bootstrap_issues_workspace_bound_bearer(persistent_env):
    env = {**persistent_env, 'TUTORLM_APP_BOOTSTRAP_TOKEN': 'server-only-secret'}
    with TestClient(create_app(HostConfig.from_env(env))) as client:
        issued = client.post('/internal/app-session', headers={'X-App-Bootstrap-Token': 'server-only-secret'},
                             json={'workspace_id': 'foreign'})
        assert issued.status_code == 201
        assert issued.json()['session']['workspace_id'] == persistent_env['TUTORLM_WORKSPACE_ID']
        assert client.get('/api/projects').status_code == 401
        assert client.get('/api/projects', headers={'Authorization': 'Bearer ' + issued.json()['access_token']}).status_code == 200


def test_copy_rebind_keeps_project_content_and_supports_second_generation(persistent_env, tmp_path):
    original = dict(persistent_env)
    with TestClient(create_app(HostConfig.from_env(original))) as client:
        token = client.post('/api/sessions', json={'invite_code': 'TLM-DEMO-TEST'}).json()['access_token']
        headers = {'Authorization': 'Bearer ' + token}
        project = client.get('/api/projects', headers=headers).json()['items'][0]
        document = client.post('/api/projects/' + project['id'] + '/documents', headers=headers,
                               json={'title': 'Copied document', 'text': '# Original content'}).json()['document']
    previous = original
    for index in range(2):
        owner, workspace, copy_id = str(uuid4()), str(uuid4()), str(uuid4())
        relative = f'users/{owner}/workspaces/{workspace}'
        mount = tmp_path / f'copy-{index}'
        destination = mount / relative
        shutil.copytree(Path(previous['TUTORLM_DATA_DIR']), destination)
        env = {**original, 'TUTORLM_OWNER_ID': owner, 'TUTORLM_WORKSPACE_ID': workspace,
               'TUTORLM_WORKSPACE_SOURCE_ID': previous['TUTORLM_WORKSPACE_ID'],
               'TUTORLM_WORKSPACE_COPY_ID': copy_id, 'TUTORLM_MOUNT_PATH': str(mount),
               'TUTORLM_WORKSPACE_SUBDIRECTORY': relative, 'TUTORLM_DATA_DIR': str(destination)}
        with TestClient(create_app(HostConfig.from_env(env))) as client:
            token = client.post('/api/sessions', json={'invite_code': 'TLM-DEMO-TEST'}).json()['access_token']
            headers = {'Authorization': 'Bearer ' + token}
            ready = client.get('/health/ready').json()
            assert ready['status'] == 'ok' and ready['workspace_id'] == workspace
            current = client.get('/api/projects', headers=headers).json()['items'][0]
            assert current['id'] == project['id'] and current['workspace_id'] == workspace
            assert client.get('/api/documents/' + document['id'] + '/content', headers=headers).json()['text'] == '# Original content'
        previous = env
