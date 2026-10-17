"""Opt-in HTTP acceptance test for an isolated, loopback Docker stack with fake models."""
import argparse
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:18091')
    parser.add_argument('--admin-url', default='http://127.0.0.1:18090')
    parser.add_argument('--admin-token', required=True)
    parser.add_argument('--run', action='store_true', help='Explicit opt-in; creates test projects and preempts test compute')
    args = parser.parse_args()
    if not args.run:
        parser.error('Pass --run only for a disposable local stack')
    for origin in (args.url, args.admin_url):
        if urlsplit(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
            parser.error('Only loopback test stacks are supported')
    clients = []
    admin = httpx.Client(base_url=args.admin_url, trust_env=False, timeout=90,
                         headers={'X-Admin-Token': args.admin_token})

    def call(client, method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        if response.is_error:
            raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {response.text}')
        response.raise_for_status()
        return response.json() if response.content else None

    def user():
        client = httpx.Client(base_url=args.url, trust_env=False, timeout=90)
        clients.append(client)
        call(client, 'POST', '/control/users')
        return client

    def open_project(client, workspace, code=None):
        payload = {'workspace_id': workspace}
        if code:
            payload['invitation_code'] = code
        result = call(client, 'POST', '/control/leases', json=payload)
        issued = call(client, 'POST', '/control/app-session', json={})
        client.headers['Authorization'] = 'Bearer ' + issued['access_token']
        assert call(client, 'GET', '/api/runtime')['provider']['name'] == 'fake'
        return result['lease']

    try:
        policy = call(admin, 'GET', '/control/access-policy')
        system = call(admin, 'POST', '/control/admin/invitations', json={'label': 'local-smoke'})
        code = system['code']
        owner, receiver = user(), user()
        source = call(owner, 'POST', '/control/workspaces', json={'title': 'Share source'})['workspace']['workspace_id']
        lease = open_project(owner, source, code if policy['invitation_only'] else None)
        project = call(owner, 'GET', '/api/projects')['items'][0]['id']
        doc = call(owner, 'POST', f'/api/projects/{project}/documents', json={
            'title': 'Original', 'text': '# Isolated content',
        })['document']
        node = call(owner, 'POST', f'/api/projects/{project}/nodes', json={
            'document_id': doc['id'], 'title': 'Learning node', 'kind': 'concept',
        })
        share = call(owner, 'POST', f'/control/workspaces/{source}/invitations', json={})
        imported = call(receiver, 'POST', '/control/workspace-imports',
                        json={'invitation_code': share['code']}, headers={'Idempotency-Key': 'local-copy-1'})
        copy = imported['workspace']['workspace_id']
        assert copy != source
        assert call(receiver, 'POST', '/control/workspace-imports',
                    json={'invitation_code': share['code']}, headers={'Idempotency-Key': 'local-copy-1'})['workspace']['workspace_id'] == copy
        assert receiver.get(f'/control/workspaces/{source}').status_code == 404
        if policy['invitation_only']:
            denied = receiver.post('/control/leases', json={'workspace_id': copy})
            assert denied.status_code == 403 and denied.json()['error']['code'] == 'invitation_required'
        open_project(receiver, copy, code if policy['invitation_only'] else None)
        assert call(receiver, 'GET', '/api/projects')['items'][0]['id'] == project
        assert call(receiver, 'GET', f'/api/documents/{doc["id"]}/content')['text'] == '# Isolated content'
        assert any(item['id'] == node['id'] for item in call(receiver, 'GET', f'/api/projects/{project}/graph')['nodes'])
        call(receiver, 'PATCH', f'/api/documents/{doc["id"]}', json={'title': 'Receiver-only change'})
        open_project(owner, source, code if policy['invitation_only'] else None)
        assert call(owner, 'GET', f'/api/documents/{doc["id"]}')['document']['title'] == 'Original'
        print('PASS: real volume copy, stable document/node IDs, receiver edit isolation and idempotent import')
        call(owner, 'DELETE', f'/control/workspaces/{source}/invitations/{share["invitation"]["invitation_id"]}')
        third = user()
        assert third.post('/control/workspace-imports', json={'invitation_code': share['code']}).status_code == 403
        if not policy['invitation_only']:
            target = call(third, 'POST', '/control/workspaces', json={'title': 'Priority test'})['workspace']['workspace_id']
            assert third.post('/control/leases', json={'workspace_id': target}).status_code == 503
            high = open_project(third, target, code)
            assert high['priority'] == 100
            statuses = [client.get('/control/leases/current').status_code for client in (owner, receiver)]
            assert sum(status in (401, 410) for status in statuses) == 1
            print('PASS: full pool rejects ordinary allocation; system invitation preempts lower priority')
        else:
            print('PASS: imported copy requires an invitation in gated mode')
        print('PASS: share revocation denies further imports')
    finally:
        for client in clients:
            try:
                client.delete('/control/leases/current')
            finally:
                client.close()
        if 'system' in locals():
            admin.delete('/control/admin/invitations/' + system['invitation']['invitation_id'])
        admin.close()


if __name__ == '__main__':
    main()
