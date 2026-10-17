"""Durable user identities and project volumes, independent of compute leases."""
from __future__ import annotations

import uuid

from control_plane.config import ControlPlaneConfig
from control_plane.errors import InvalidToken, RuntimeUnavailable, WorkspaceExpired
from control_plane.ports import RuntimeAdapter
from control_plane.security import generate_token, hash_token, verify_token
from control_plane.store import ControlPlaneStore
from control_plane.domain.models import canonical_data_directory


class WorkspaceService:
    def __init__(self, config: ControlPlaneConfig, store: ControlPlaneStore, adapter: RuntimeAdapter):
        self.config, self.store, self.adapter = config, store, adapter

    def _pepper(self) -> str:
        return self.config.token_pepper.strip() or self.store.get_or_create_meta('token_pepper', generate_token(24))

    def register(self) -> dict:
        user_id, token = str(uuid.uuid4()), generate_token()
        token_hash = hash_token(f'user:{user_id}:{token}', self._pepper())
        return {'user': self.store.create_user(user_id, token_hash), 'recovery_token': token}

    def authenticate(self, user_id: str, token: str) -> dict:
        try:
            user_id = str(uuid.UUID(user_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise InvalidToken('Invalid user credentials') from exc
        if not token or len(token) > 256:
            raise InvalidToken('Invalid user credentials')
        user = self.store.get_user(user_id)
        digest = user['token_hash'] if user else '0' * 64
        if not verify_token(f'user:{user_id}:{token}', digest, self._pepper()) or user is None:
            raise InvalidToken('Invalid user credentials')
        return {'user_id': user_id, 'created_at': user['created_at']}

    def create(self, user_id: str, title: str):
        workspace_id = str(uuid.uuid4())
        return self.store.create_workspace(
            workspace_id, user_id, title, self.config.workspace_volume_name(workspace_id),
            data_directory=canonical_data_directory(user_id, workspace_id),
        )

    def get(self, workspace_id: str, user_id: str, *, touch: bool = False):
        return self.store.owned_workspace(
            workspace_id, user_id, self.config.workspace_idle_ttl_seconds, touch=touch,
        )

    def list(self, user_id: str) -> list:
        available = []
        for workspace in self.store.list_workspaces(user_id):
            try:
                available.append(self.get(workspace.workspace_id, user_id))
            except WorkspaceExpired:
                continue
        return available

    def gc_once(self) -> dict:
        report = {'deleted': [], 'failed': [], 'failed_copies_claimed': []}
        # Failed share copies leave a hidden, never-active destination. If the
        # rollback could not delete its volume, it is claimed here and handed to
        # the normal storage GC below (retry), never reopened or published.
        report['failed_copies_claimed'] = self.store.claim_failed_copy_destinations()
        if self.config.workspace_permanent or self.config.workspace_idle_ttl_seconds is None:
            return report
        for workspace in self.store.claim_expired_workspaces(self.config.workspace_idle_ttl_seconds):
            try:
                expected = self.config.workspace_volume_name(workspace.workspace_id)
                if workspace.volume_name != expected:
                    raise RuntimeUnavailable('Workspace volume does not match its UUID')
                # No mounted or quarantined workspace can be claimed by the store.
                self.adapter.remove_volume(expected)
                self.store.finish_workspace_deletion(workspace.workspace_id)
                report['deleted'].append(workspace.workspace_id)
            except Exception as exc:
                self.store.finish_workspace_deletion(workspace.workspace_id, error=str(exc)[:500])
                report['failed'].append(workspace.workspace_id)
        return report
