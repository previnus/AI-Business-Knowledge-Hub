import json

from conftest import ADMIN_TOKEN, ALPHA_TOKEN, BETA_TOKEN, FACTS, headers, make_pdf

from hub import extract, worker
from hub.fetch import SourceUnavailable


def upload(client, group, title, filename, content):
    return client.post(
        "/api/admin/sources/file",
        headers=headers(ADMIN_TOKEN),
        data={
            "title": title,
            "group_ids": json.dumps([str(group)]),
        },
        files={"file": (filename, content)},
    )


def run_next(pool, settings, provider):
    with pool.connection() as db:
        job = worker.claim(db)
    assert job is not None
    worker.process(pool, settings, provider, job)
    return job


def ask(client, token, question):
    return client.post("/api/ask", headers=headers(token), json={"question": question})


def test_citations_for_all_source_types_and_group_boundaries(hub, monkeypatch):
    client, pool, settings, provider, groups, _ = hub
    original_parser = worker.isolated_extract
    monkeypatch.setattr(
        extract,
        "fetch_html",
        lambda url, hosts: (
            "https://docs.test/training",
            ("<main><h1>Training</h1><p>" + FACTS["meadow"] + "</p></main>").encode(),
        ),
    )
    monkeypatch.setattr(
        worker,
        "isolated_extract",
        lambda kind, content, url, hosts: (
            extract.extract(kind, content, url, hosts)
            if kind == "web"
            else original_parser(kind, content, url, hosts)
        ),
    )
    fixtures = [
        ("Orchard Hours", "orchard.pdf", make_pdf(FACTS["orchard"]), "orchard", "Page 1"),
        (
            "Lantern Inventory",
            "lantern.md",
            ("# Inventory\n\n" + FACTS["lantern"]).encode(),
            "lantern",
            "Inventory",
        ),
        ("Quartz Support", "quartz.txt", FACTS["quartz"].encode(), "quartz", "Lines 1–1"),
    ]
    for title, name, content, _, _ in fixtures:
        response = upload(client, groups[0], title, name, content)
        assert response.status_code == 202, response.text
        run_next(pool, settings, provider)
    response = client.post(
        "/api/admin/sources/web",
        headers=headers(ADMIN_TOKEN),
        json={
            "title": "Meadow Training",
            "url": "https://docs.test/training",
            "group_ids": [str(groups[0])],
        },
    )
    assert response.status_code == 202, response.text
    run_next(pool, settings, provider)
    beta = upload(client, groups[1], "Restricted", "secret.txt", b"Restricted group marker: tigers.")
    assert beta.status_code == 202
    run_next(pool, settings, provider)

    for key, location in [(item[3], item[4]) for item in fixtures] + [("meadow", "Training")]:
        response = ask(client, ALPHA_TOKEN, f"What is the {key} policy?")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "answered", body
        item = body["segments"][0]["citations"][0]
        assert body["segments"][0]["text"] == item["quote"]
        assert FACTS[key] in item["quote"]
        assert item["location"] == location
        assert item["url"] == "/citation/" + item["id"]
        target = "/api/citations/" + item["id"]
        detail = client.get(target, headers=headers(ALPHA_TOKEN)).json()
        assert item["quote"] == detail["excerpt"]
        assert client.get(target, headers=headers(BETA_TOKEN)).status_code == 404
        if key == "meadow":
            assert detail["original_url"] == "https://docs.test/training"
        else:
            path = "/api/attachments/" + detail["attachment_id"]
            assert (
                "attachment" in client.get(path, headers=headers(ALPHA_TOKEN)).headers["content-disposition"]
            )
            assert client.get(path, headers=headers(BETA_TOKEN)).status_code == 404
    assert all(item["title"] != "Restricted" for items in provider.contexts for item in items)
    assert len(client.get("/api/sources", headers=headers(ADMIN_TOKEN)).json()) == 5
    assert len(client.get("/api/sources", headers=headers(BETA_TOKEN)).json()) == 1
    assert ask(client, BETA_TOKEN, "What is the orchard policy?").json()["status"] == "insufficient_evidence"
    assert client.get("/api/admin/access", headers=headers(ALPHA_TOKEN)).status_code == 403


def test_revocation_while_model_is_answering(hub):
    client, pool, settings, provider, groups, users = hub
    assert upload(client, groups[0], "Orchard", "orchard.txt", FACTS["orchard"].encode()).status_code == 202
    run_next(pool, settings, provider)

    def revoke():
        with pool.connection() as db:
            db.execute("DELETE FROM memberships WHERE user_id = %s", (users[1],))

    provider.before_answer = revoke
    assert ask(client, ALPHA_TOKEN, "orchard").json() == {"status": "insufficient_evidence", "segments": []}
    assert client.get("/api/sources", headers=headers(ALPHA_TOKEN)).json() == []


def test_delete_during_ingestion_cannot_publish(hub):
    client, pool, settings, provider, groups, _ = hub
    data = upload(client, groups[0], "Orchard", "orchard.txt", FACTS["orchard"].encode()).json()
    with pool.connection() as db:
        job = worker.claim(db)
    assert (
        client.delete("/api/admin/sources/" + data["source_id"], headers=headers(ADMIN_TOKEN)).status_code
        == 204
    )
    worker.process(pool, settings, provider, job)
    with pool.connection() as db:
        for table in ("sources", "jobs", "versions", "chunks"):
            assert db.execute(f"SELECT count(*) AS total FROM {table}").fetchone()["total"] == 0


def test_refresh_keeps_old_citations_and_previous_version_on_failure(hub, monkeypatch):
    client, pool, settings, provider, groups, _ = hub
    page = {"body": FACTS["meadow"]}

    def parse(kind, content, url, hosts):
        if page["body"] is None:
            raise SourceUnavailable("website_unavailable")
        return url, [("Training", page["body"])]

    monkeypatch.setattr(worker, "isolated_extract", parse)
    response = client.post(
        "/api/admin/sources/web",
        headers=headers(ADMIN_TOKEN),
        json={
            "title": "Meadow",
            "url": "https://docs.test/training",
            "group_ids": [str(groups[0])],
        },
    )
    assert response.status_code == 202
    source = "/api/admin/sources/" + response.json()["source_id"]
    run_next(pool, settings, provider)
    old = ask(client, ALPHA_TOKEN, "meadow").json()["segments"][0]["citations"][0]
    page["body"] = "Updated training information."
    assert client.post(source + "/refresh", headers=headers(ADMIN_TOKEN)).status_code == 202
    run_next(pool, settings, provider)
    assert (
        client.get("/api/citations/" + old["id"], headers=headers(ALPHA_TOKEN)).json()["excerpt"]
        == FACTS["meadow"]
    )
    assert ask(client, ALPHA_TOKEN, "meadow").json()["status"] == "insufficient_evidence"
    page["body"] = None
    assert client.post(source + "/refresh", headers=headers(ADMIN_TOKEN)).status_code == 202
    for attempt in range(1, 4):
        job = run_next(pool, settings, provider)
        assert job["attempts"] == attempt
        with pool.connection() as db:
            db.execute("UPDATE jobs SET available_at = now() WHERE status = 'queued'")
    status = client.get("/api/admin/sources", headers=headers(ADMIN_TOKEN)).json()[0]
    assert status["status"] == "failed" and status["active"]
    assert (
        client.put(
            source + "/groups", headers=headers(ADMIN_TOKEN), json={"group_ids": [str(groups[1])]}
        ).status_code
        == 204
    )
    assert client.get("/api/citations/" + old["id"], headers=headers(ALPHA_TOKEN)).status_code == 404
    assert client.get("/api/citations/" + old["id"], headers=headers(BETA_TOKEN)).status_code == 200
    with pool.connection() as db:
        db.execute("UPDATE versions SET retired_at = now() - interval '31 days' WHERE retired_at IS NOT NULL")
        worker.cleanup(db)
    assert client.get("/api/citations/" + old["id"], headers=headers(BETA_TOKEN)).status_code == 404


def test_expired_lease_cannot_publish_and_is_reclaimed(hub):
    client, pool, _, provider, groups, _ = hub
    assert upload(client, groups[0], "Orchard", "orchard.txt", b"some text").status_code == 202
    with pool.connection() as db:
        first = worker.claim(db)
    with pool.connection() as db:
        assert worker.claim(db) is None
        db.execute("UPDATE jobs SET lease_until = now() - interval '1 second'")
    with pool.connection() as db:
        second = worker.claim(db)
    vectors, _ = provider.embed(["some text"])
    with pool.connection() as db:
        worker.publish(db, first, None, [("Lines 1–1", "old worker")], vectors)
        assert db.execute("SELECT count(*) AS total FROM versions").fetchone()["total"] == 0
        assert second["attempts"] == 2 and second["lease_token"] != first["lease_token"]
        worker.publish(db, second, None, [("Lines 1–1", "new worker")], vectors)
    assert client.get("/api/admin/sources", headers=headers(ADMIN_TOKEN)).json()[0]["status"] == "ready"


def test_upload_replacement_is_versioned_and_attachment_stays_exact(hub):
    client, pool, settings, provider, groups, _ = hub
    old_bytes = FACTS["quartz"].encode()
    added = upload(client, groups[0], "Quartz", "quartz.txt", old_bytes)
    assert added.status_code == 202
    run_next(pool, settings, provider)
    citation = ask(client, ALPHA_TOKEN, "quartz").json()["segments"][0]["citations"][0]["id"]
    original = client.get("/api/citations/" + citation, headers=headers(ALPHA_TOKEN)).json()
    path = "/api/admin/sources/" + added.json()["source_id"] + "/file"
    rejected = client.put(path, headers=headers(ADMIN_TOKEN), files={"file": ("quartz.md", b"# New")})
    assert rejected.status_code == 422
    replaced = client.put(path, headers=headers(ADMIN_TOKEN), files={"file": ("quartz.txt", b"New text.")})
    assert replaced.status_code == 202
    run_next(pool, settings, provider)
    assert ask(client, ALPHA_TOKEN, "quartz").json()["status"] == "insufficient_evidence"
    attachment = client.get("/api/attachments/" + original["attachment_id"], headers=headers(ALPHA_TOKEN))
    assert attachment.content == old_bytes


def test_fabricated_citations_provider_errors_and_request_limits(hub):
    client, pool, settings, provider, groups, _ = hub
    assert upload(client, groups[0], "Orchard", "orchard.txt", FACTS["orchard"].encode()).status_code == 202
    run_next(pool, settings, provider)
    provider.fabricate = True
    assert ask(client, ALPHA_TOKEN, "orchard").json()["status"] == "insufficient_evidence"
    provider.unavailable = True
    response = ask(client, ALPHA_TOKEN, "orchard")
    assert response.status_code == 503 and response.headers["retry-after"] == "10"
    assert (
        client.post(
            "/api/ask",
            headers=headers(ALPHA_TOKEN, origin="https://untrusted.test"),
            json={"question": "orchard"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/admin/sources/file",
            headers={
                **headers(ADMIN_TOKEN),
                "Content-Length": "30000000",
            },
        ).status_code
        == 413
    )
    response = client.post(
        "/api/ask",
        headers={**headers(ALPHA_TOKEN), "Content-Type": "application/json"},
        content=iter([b" " * 40000, b" " * 40000]),
    )
    assert response.status_code == 413
    assert client.get("/api/me").status_code == 401
