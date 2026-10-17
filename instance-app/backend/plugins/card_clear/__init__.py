"""Delete all or selected document cards, with one durable undo snapshot.

This plugin uses the Host's existing node deletion method inside one SQLite
transaction. The snapshot keeps the exact row IDs and metadata so that an undo
restores explanations, connectors, layout and learning state together.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, Field

from backend.host.errors import Conflict, Gone, NotFound
from backend.host.local.store import utcnow
from backend.host.state import AppState
from backend.plugin_contracts import PluginManifest
from backend.adapters.http.dependencies import get_state


MANIFEST = PluginManifest.build(
    "card-clear", "2.0.0", title="文档卡片清除、框选删除与撤销",
    description="按文档原子删除全部或选中的知识卡片，保留可撤销的完整快照。",
    depends_on=("documents", "explanations"),
    capabilities=("documents.read", "knowledge.read", "knowledge.nodes.write",
                  "knowledge.edges.write", "knowledge.write"),
)

UNDO_SECONDS = 24 * 60 * 60


def _ensure_table(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS card_clear_undo (
        token TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        restored_at REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_clear_undo_scope "
                 "ON card_clear_undo(project_id, document_id, expires_at)")


def _rows(conn: Any, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _learning_revision(conn: Any, node_id: str) -> int | None:
    row = conn.execute("SELECT revision FROM node_learning WHERE node_id=?", (node_id,)).fetchone()
    return row['revision'] if row else None


class Selection(BaseModel):
    cardIds: list[str] = Field(min_length=1, max_length=500)


def _delete_cards(state: AppState, document_id: str, card_ids: list[str] | None) -> dict[str, Any]:
    document = state.services.require_document(document_id)
    project_id = document["project_id"]
    with state.store.transaction() as conn:
        _ensure_table(conn)
        all_nodes = _rows(conn, "SELECT * FROM nodes WHERE project_id=? AND document_id=? "
                      "ORDER BY order_index, created_at", (project_id, document_id))
        requested = set(card_ids) if card_ids is not None else None
        if requested is not None and (len(requested) != len(card_ids) or
                                      requested - {row['id'] for row in all_nodes}):
            raise Conflict("选区包含无效或重复的卡片", code="card_selection_invalid")
        nodes = [row for row in all_nodes if requested is None or row['id'] in requested]
        if not nodes:
            return {"cleared": 0, "undoToken": None, "expiresAt": None}
        ids = [row["id"] for row in nodes]
        requested_ids = set(ids)
        # A document may contain more nodes than SQLite's bound-parameter limit.
        conn.execute("CREATE TEMP TABLE card_clear_selected_ids (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO card_clear_selected_ids (id) VALUES (?)", ((node_id,) for node_id in ids))
        # Refuse to erase a node while a run may still be writing its answer.
        active = conn.execute(
            "SELECT 1 FROM runs WHERE project_id=? AND state IN ('queued','running') LIMIT 1",
            (project_id,),
        ).fetchone()
        if active:
            raise Conflict("有卡片仍在生成解释，请等待任务完成后再清除", code="card_run_active")
        # Keep every relation touching a deleted card, including cross-document edges.
        # The canvas uses parent_id, while explanation context follows
        # metadata.source.fromNode. Reparent both references together.
        project_nodes = _rows(conn, "SELECT id, parent_id, metadata_json, revision FROM nodes "
                              "WHERE project_id=?", (project_id,))
        parents = {row['id']: row['parent_id'] for row in project_nodes}
        children = []
        for child in project_nodes:
            if child['id'] in requested_ids:
                continue
            metadata = json.loads(child['metadata_json'])
            source = metadata.get('source') or {}
            source_parent = source.get('fromNode') if isinstance(source, dict) else None
            if child['parent_id'] not in requested_ids and source_parent not in requested_ids:
                continue
            ancestor = child['parent_id'] or source_parent
            seen = set()
            while ancestor in requested_ids and ancestor not in seen:
                seen.add(ancestor)
                ancestor = parents.get(ancestor)
            ancestor = ancestor if ancestor in parents and ancestor not in requested_ids else None
            new_metadata = dict(metadata)
            if isinstance(source, dict) and ('fromNode' in source or 'fromExp' in source):
                new_source = dict(source)
                if ancestor:
                    new_source['fromNode'] = ancestor
                else:
                    new_source.pop('fromNode', None)
                # The old explanation belongs to the deleted parent.
                if source_parent != ancestor:
                    new_source.pop('fromExp', None)
                new_metadata['source'] = new_source
            children.append({**child, 'new_parent_id': ancestor,
                             'new_metadata_json': json.dumps(new_metadata, ensure_ascii=False),
                             'after_revision': child['revision'] + 1,
                             'explanation_count': conn.execute(
                                 "SELECT COUNT(*) FROM explanations WHERE node_id=?", (child['id'],)
                             ).fetchone()[0],
                             'learning_revision': _learning_revision(conn, child['id'])})
        snapshot = {
            "nodes": nodes,
            "edges": _rows(conn, "SELECT * FROM edges WHERE source_node_id IN "
                           "(SELECT id FROM card_clear_selected_ids) OR target_node_id IN "
                           "(SELECT id FROM card_clear_selected_ids)", ()),
            "explanations": _rows(conn, "SELECT * FROM explanations WHERE node_id IN "
                                  "(SELECT id FROM card_clear_selected_ids)", ()),
            "learning": _rows(conn, "SELECT * FROM node_learning WHERE node_id IN "
                              "(SELECT id FROM card_clear_selected_ids)", ()),
            "reparented_children": children,
        }
        token = str(uuid.uuid4())
        now = time.time()
        conn.execute("DELETE FROM card_clear_undo WHERE expires_at < ? OR restored_at IS NOT NULL", (now,))
        conn.execute("INSERT INTO card_clear_undo "
                     "(token, project_id, document_id, snapshot_json, created_at, expires_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (token, project_id, document_id,
                      json.dumps(snapshot, ensure_ascii=False, allow_nan=False), now, now + UNDO_SECONDS))
        # Reuse the current deletion behavior; the enclosing transaction makes
        # all card deletions plus snapshot creation one atomic operation.
        for node_id in ids:
            state.store.delete_node(node_id)
        for child in children:
            conn.execute("UPDATE nodes SET parent_id=?, metadata_json=?, revision=?, updated_at=? WHERE id=?",
                         (child['new_parent_id'], child['new_metadata_json'], child['after_revision'],
                          utcnow(), child['id']))
        return {"cleared": len(ids), "undoToken": token, "expiresAt": now + UNDO_SECONDS}


def clear_document(state: AppState, document_id: str) -> dict[str, Any]:
    return _delete_cards(state, document_id, None)


def delete_selected(state: AppState, document_id: str, card_ids: list[str]) -> dict[str, Any]:
    return _delete_cards(state, document_id, card_ids)


def _insert_rows(conn: Any, table: str, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    columns = tuple(records[0])
    marks = ",".join("?" for _ in columns)
    statement = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})"
    conn.executemany(statement, [tuple(row[column] for column in columns) for row in records])


def undo_clear(state: AppState, token: str) -> dict[str, Any]:
    project_id = state.services.current_project()["id"]
    with state.store.transaction() as conn:
        _ensure_table(conn)
        active = conn.execute(
            "SELECT 1 FROM runs WHERE project_id=? AND state IN ('queued','running') LIMIT 1",
            (project_id,),
        ).fetchone()
        if active:
            raise Conflict("有卡片仍在生成解释，请等待任务完成后再撤销", code="card_run_active")
        record = conn.execute("SELECT * FROM card_clear_undo WHERE token=? AND project_id=?",
                              (token, project_id)).fetchone()
        if not record:
            raise NotFound("找不到这次清除记录", code="clear_undo_not_found")
        if record["restored_at"] is not None or record["expires_at"] <= time.time():
            raise Gone("清除记录已撤销或过期", code="clear_undo_expired")
        state.services.require_document(record["document_id"])
        snapshot = json.loads(record["snapshot_json"])
        ids = [row["id"] for row in snapshot["nodes"]]
        for node_id in ids:
            if conn.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone():
                raise Conflict("部分卡片已经存在，无法重复恢复", code="clear_undo_conflict")
        restored_ids = set(ids)
        for edge in snapshot["edges"]:
            for endpoint in (edge["source_node_id"], edge["target_node_id"]):
                if endpoint not in restored_ids and not conn.execute(
                    "SELECT 1 FROM nodes WHERE id=?", (endpoint,)
                ).fetchone():
                    raise Conflict("相连卡片已被删除，无法完整恢复连线", code="clear_undo_conflict")
        children = snapshot.get("reparented_children", snapshot.get("external_children", []))
        for child in children:
            live = conn.execute("SELECT parent_id, metadata_json, revision FROM nodes WHERE id=?", (child["id"],)).fetchone()
            expected_parent = child.get("new_parent_id")
            if (not live or live["parent_id"] != expected_parent or
                live["revision"] != child.get("after_revision", child["revision"]) or
                ("new_metadata_json" in child and live["metadata_json"] != child["new_metadata_json"])):
                raise Conflict("相关卡片已发生修改，请刷新后重试", code="clear_undo_conflict")
            if "explanation_count" in child and conn.execute(
                "SELECT COUNT(*) FROM explanations WHERE node_id=?", (child["id"],)
            ).fetchone()[0] != child["explanation_count"]:
                raise Conflict("相关卡片已新增解释，无法恢复旧追问路径", code="clear_undo_conflict")
            if ("learning_revision" in child and
                _learning_revision(conn, child["id"]) != child["learning_revision"]):
                raise Conflict("相关卡片的学习状态已修改", code="clear_undo_conflict")
        _insert_rows(conn, "nodes", snapshot["nodes"])
        _insert_rows(conn, "edges", snapshot["edges"])
        _insert_rows(conn, "explanations", snapshot["explanations"])
        _insert_rows(conn, "node_learning", snapshot["learning"])
        for child in children:
            if "metadata_json" in child:
                conn.execute("UPDATE nodes SET parent_id=?, metadata_json=?, revision=revision+1, "
                             "updated_at=? WHERE id=?",
                             (child["parent_id"], child["metadata_json"], utcnow(), child["id"]))
            else:
                conn.execute("UPDATE nodes SET parent_id=? WHERE id=?",
                             (child["parent_id"], child["id"]))
        conn.execute("UPDATE card_clear_undo SET restored_at=? WHERE token=?", (time.time(), token))
        return {"restored": len(ids), "documentId": record["document_id"]}


class CardClearPlugin:
    manifest = MANIFEST
    workflows: dict[str, Any] = {}

    def register_services(self, host: Any) -> None:
        pass

    def register_routes(self, router: Any, host: Any) -> None:
        @router.post("/documents/{document_id}/clear")
        def clear(document_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
            return clear_document(state, document_id)

        @router.post("/documents/{document_id}/delete-selection")
        def delete_selection(document_id: str, body: Selection,
                             state: AppState = Depends(get_state)) -> dict[str, Any]:
            return delete_selected(state, document_id, body.cardIds)

        @router.post("/clear/{token}/undo")
        def undo(token: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
            return undo_clear(state, token)

    async def startup(self, host: Any) -> None:
        pass

    async def shutdown(self, host: Any) -> None:
        pass


def build() -> CardClearPlugin:
    return CardClearPlugin()
