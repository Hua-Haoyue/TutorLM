"""Bulk card deletion keeps graph and follow-up references consistent."""


def _node(client, headers, project_id, document_id, title, *, parent=None, source=None):
    response = client.post(f"/api/projects/{project_id}/nodes", headers=headers, json={
        "document_id": document_id, "title": title, "content": title,
        "parent_id": parent, "metadata": {
            "source": source or {"quote": title},
            "position": {"x": 12, "y": 24},
            "layout_default": {"x": 12, "y": 24, "scale": 1},
        },
    })
    assert response.status_code == 201, response.text
    return response.json()


def _graph(client, headers, project_id):
    response = client.get(f"/api/projects/{project_id}/graph", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_selected_delete_reparents_followup_and_undo_restores_exact_graph(
    client, session_headers, project_id, text_document, wait_for_run,
):
    document_id = text_document["id"]
    root = _node(client, session_headers, project_id, document_id, "root")
    parent = _node(client, session_headers, project_id, document_id, "parent",
                   parent=root["id"], source={"quote": "parent", "fromNode": root["id"]})
    run = client.post("/api/runs", headers=session_headers, json={
        "kind": "explanation", "input": {"node_id": parent["id"]},
    })
    assert run.status_code == 202, run.text
    result = wait_for_run(run.json()["id"])
    assert result["state"] == "completed", result
    explanation_id = result["result"]["explanation"]["id"]
    child = _node(client, session_headers, project_id, document_id, "child",
                  parent=parent["id"], source={"quote": "child", "fromNode": parent["id"],
                                                 "fromExp": explanation_id})
    edge = client.post(f"/api/projects/{project_id}/graph/edges", headers=session_headers,
                       json={"source_node_id": parent["id"], "target_node_id": child["id"],
                             "relation": "explains"})
    assert edge.status_code == 201, edge.text

    deletion = client.post(f"/api/plugins/card-clear/documents/{document_id}/delete-selection",
                           headers=session_headers, json={"cardIds": [parent["id"]]})
    assert deletion.status_code == 200, deletion.text
    assert deletion.json()["cleared"] == 1
    graph = _graph(client, session_headers, project_id)
    assert parent["id"] not in {node["id"] for node in graph["nodes"]}
    survivor = next(node for node in graph["nodes"] if node["id"] == child["id"])
    assert survivor["parent_id"] == root["id"]
    assert survivor["metadata"]["source"]["fromNode"] == root["id"]
    assert "fromExp" not in survivor["metadata"]["source"]
    assert not graph["edges"]

    undo = client.post(f"/api/plugins/card-clear/clear/{deletion.json()['undoToken']}/undo",
                       headers=session_headers, json={})
    assert undo.status_code == 200, undo.text
    graph = _graph(client, session_headers, project_id)
    restored = next(node for node in graph["nodes"] if node["id"] == child["id"])
    assert restored["parent_id"] == parent["id"]
    assert restored["metadata"]["source"]["fromNode"] == parent["id"]
    assert restored["metadata"]["source"]["fromExp"] == explanation_id
    assert restored["metadata"]["layout_default"] == child["metadata"]["layout_default"]
    assert edge.json()["id"] in {item["id"] for item in graph["edges"]}
    history = client.get(f"/api/projects/{project_id}/explanations", headers=session_headers).json()["items"]
    assert explanation_id in {item["id"] for item in history}


def test_clear_is_document_scoped_and_undo_restores(client, session_headers, project_id, text_document):
    other = client.post(f"/api/projects/{project_id}/documents", headers=session_headers,
                        json={"title": "another", "text": "second document"})
    assert other.status_code == 201, other.text
    another_id = other.json()["document"]["id"]
    first = _node(client, session_headers, project_id, text_document["id"], "first")
    second = _node(client, session_headers, project_id, another_id, "second")
    clear = client.post(f"/api/plugins/card-clear/documents/{text_document['id']}/clear",
                        headers=session_headers, json={})
    assert clear.status_code == 200, clear.text
    assert {n["id"] for n in _graph(client, session_headers, project_id)["nodes"]} == {second["id"]}
    undo = client.post(f"/api/plugins/card-clear/clear/{clear.json()['undoToken']}/undo",
                       headers=session_headers, json={})
    assert undo.status_code == 200, undo.text
    assert {n["id"] for n in _graph(client, session_headers, project_id)["nodes"]} == {
        first["id"], second["id"],
    }


def test_selection_rejects_invalid_id_and_undo_rejects_modified_child(
    client, session_headers, project_id, text_document,
):
    document_id = text_document["id"]
    parent = _node(client, session_headers, project_id, document_id, "parent")
    child = _node(client, session_headers, project_id, document_id, "child",
                  parent=parent["id"], source={"quote": "child", "fromNode": parent["id"]})
    invalid = client.post(f"/api/plugins/card-clear/documents/{document_id}/delete-selection",
                          headers=session_headers, json={"cardIds": [parent["id"], "missing"]})
    assert invalid.status_code == 409
    deletion = client.post(f"/api/plugins/card-clear/documents/{document_id}/delete-selection",
                           headers=session_headers, json={"cardIds": [parent["id"]]})
    assert deletion.status_code == 200, deletion.text
    survivor = next(n for n in _graph(client, session_headers, project_id)["nodes"] if n["id"] == child["id"])
    changed = client.patch(f"/api/nodes/{child['id']}", headers=session_headers,
                           json={"revision": survivor["revision"], "metadata": {
                               **survivor["metadata"], "position": {"x": 90, "y": 60},
                           }})
    assert changed.status_code == 200, changed.text
    undo = client.post(f"/api/plugins/card-clear/clear/{deletion.json()['undoToken']}/undo",
                       headers=session_headers, json={})
    assert undo.status_code == 409
    assert parent["id"] not in {n["id"] for n in _graph(client, session_headers, project_id)["nodes"]}


def test_followup_still_explains_after_reparent_and_undo_detects_new_answer(
    client, session_headers, project_id, text_document, wait_for_run,
):
    document_id = text_document["id"]
    root = _node(client, session_headers, project_id, document_id, "root")
    parent = _node(client, session_headers, project_id, document_id, "parent",
                   parent=root["id"], source={"quote": "parent", "fromNode": root["id"]})
    child = _node(client, session_headers, project_id, document_id, "child",
                  parent=parent["id"], source={"quote": "child", "fromNode": parent["id"]})
    deletion = client.post(f"/api/plugins/card-clear/documents/{document_id}/delete-selection",
                           headers=session_headers, json={"cardIds": [parent["id"]]})
    assert deletion.status_code == 200, deletion.text
    run = client.post("/api/runs", headers=session_headers, json={
        "kind": "explanation", "input": {"node_id": child["id"]},
    })
    assert run.status_code == 202, run.text
    result = wait_for_run(run.json()["id"])
    assert result["state"] == "completed", result
    undo = client.post(f"/api/plugins/card-clear/clear/{deletion.json()['undoToken']}/undo",
                       headers=session_headers, json={})
    assert undo.status_code == 409
