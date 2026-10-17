"""Feynman checks: one private-answer quiz per optimized root explanation."""


def _root_with_optimized_explanation(client, headers, project_id, document, wait_for_run):
    node = client.post(f"/api/projects/{project_id}/nodes", headers=headers, json={
        "document_id": document["id"], "title": "root", "content": "root",
        "metadata": {"source": {"quote": document_quote(client, headers, document)}},
    })
    assert node.status_code == 201, node.text
    node = node.json()
    run = client.post("/api/runs", headers=headers, json={
        "kind": "explanation", "input": {"node_id": node["id"], "purpose": "optimize"}})
    assert run.status_code == 202, run.text
    result = wait_for_run(run.json()["id"])
    assert result["state"] == "completed", result
    explanation = result["result"]["explanation"]
    assert explanation["learning"]["feynman_eligible"] is True
    return node, explanation


def document_quote(client, headers, document):
    text = client.get(f"/api/documents/{document['id']}/content", headers=headers).json()["text"]
    return text.split()[0]


def test_quiz_answer_and_card_undo_keep_the_check(client, session_headers, project_id, text_document, wait_for_run):
    node, explanation = _root_with_optimized_explanation(client, session_headers, project_id, text_document, wait_for_run)
    run = client.post("/api/runs", headers=session_headers, json={
        "kind": "feynman", "input": {"node_id": node["id"], "explanation_id": explanation["id"]}})
    assert run.status_code == 202, run.text
    result = wait_for_run(run.json()["id"])
    assert result["state"] == "completed", result
    quiz = result["result"]["quiz"]
    assert quiz["state"] == "pending" and len(quiz["choices"]) == 2
    assert "correct_id" not in quiz and "correct_index" not in quiz, "the answer key stays private"

    # The fake provider marks the first generated choice correct; choices are shuffled.
    correct = next(c for c in quiz["choices"] if c["label"].startswith("前提与结论有方向"))
    answer = client.post(f"/api/nodes/{node['id']}/feynman/{quiz['id']}/answer", headers=session_headers,
                         json={"option_id": correct["id"]})
    assert answer.status_code == 200, answer.text
    assert answer.json()["quiz"]["state"] == "correct"

    # Deleting the card removes its checks; undo brings them back with it.
    deletion = client.post(f"/api/plugins/card-clear/documents/{text_document['id']}/delete-selection",
                           headers=session_headers, json={"cardIds": [node["id"]]})
    assert deletion.status_code == 200, deletion.text
    undo = client.post(f"/api/plugins/card-clear/clear/{deletion.json()['undoToken']}/undo",
                       headers=session_headers, json={})
    assert undo.status_code == 200, undo.text
    current = client.get(f"/api/nodes/{node['id']}/feynman", headers=session_headers,
                         params={"explanation_id": explanation["id"]})
    assert current.status_code == 200, current.text
    assert current.json()["quiz"]["id"] == quiz["id"] and current.json()["quiz"]["state"] == "correct"


def test_non_root_and_unoptimized_explanations_are_refused(client, session_headers, project_id, text_document, wait_for_run):
    node = client.post(f"/api/projects/{project_id}/nodes", headers=session_headers, json={
        "document_id": text_document["id"], "title": "plain", "content": "plain",
        "metadata": {"source": {"quote": document_quote(client, session_headers, text_document)}}}).json()
    run = client.post("/api/runs", headers=session_headers, json={"kind": "explanation", "input": {"node_id": node["id"]}})
    explanation = wait_for_run(run.json()["id"])["result"]["explanation"]
    refused = client.post("/api/runs", headers=session_headers, json={
        "kind": "feynman", "input": {"node_id": node["id"], "explanation_id": explanation["id"]}})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "feynman_not_eligible"
