import io
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from hub.ai import Draft, ProviderUnavailable
from hub.api import create_app
from hub.auth import SESSION, token_hash
from hub.config import Settings
from hub.db import migrate

ORIGIN = "https://hub.test"
ISSUER = "https://identity.test"
ADMIN_TOKEN, ALPHA_TOKEN, BETA_TOKEN = "a" * 43, "b" * 43, "c" * 43

FACTS = {
    "orchard": "The orchard access window is 09:30 on Tuesdays.",
    "lantern": "The lantern inventory threshold is 42 units.",
    "quartz": "The quartz escalation contact is the duty engineer.",
    "meadow": "The meadow training cycle repeats every six weeks.",
}


def make_pdf(text=None):
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    if text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 24 256 Td ({text}) Tj ET".encode())
        page.replace_contents(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeProvider:
    def __init__(self):
        self.contexts = []
        self.before_answer = None
        self.fabricate = False
        self.unavailable = False

    def embed(self, texts):
        if self.unavailable:
            raise ProviderUnavailable
        return [[1.0] + [0.0] * 1535 for _ in texts], len(texts)

    def answer(self, question, passages):
        self.contexts.append(passages)
        if self.before_answer:
            self.before_answer()
        if self.fabricate:
            return Draft(passages=[999]), 1
        for key, fact in FACTS.items():
            if key in question.lower():
                for passage in passages:
                    if fact in passage["text"]:
                        return Draft(passages=[passage["passage"]]), 1
        return Draft(passages=[]), 1


@pytest.fixture
def db_url():
    url = os.environ.get("HUB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set HUB_TEST_DATABASE_URL to run PostgreSQL + pgvector integration tests")
    schema = "test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        isolated = make_conninfo(url, options=f"-csearch_path={schema},public")
        migrate(isolated)
        migrate(isolated)
        yield isolated
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def hub(db_url):
    settings = Settings(
        database_url=db_url,
        public_origin=ORIGIN,
        web_hosts="docs.test",
        oidc_issuer=ISSUER,
        oidc_client_id="hub-test",
        oidc_client_secret="synthetic-client-secret",
    )
    provider = FakeProvider()
    app = create_app(settings, provider)
    with TestClient(app, base_url=ORIGIN) as client:
        pool = app.state.pool
        with pool.connection() as db:
            groups = [
                db.execute("INSERT INTO groups (name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
                for name in ("Alpha", "Beta")
            ]
            users = []
            for subject, token, group in (
                ("administrator", ADMIN_TOKEN, None),
                ("employee-a", ALPHA_TOKEN, groups[0]),
                ("employee-b", BETA_TOKEN, groups[1]),
            ):
                user = db.execute(
                    """
                    INSERT INTO users (issuer, subject, display_name, is_admin)
                    VALUES (%s, %s, %s, %s) RETURNING id
                """,
                    (ISSUER, subject, subject, group is None),
                ).fetchone()["id"]
                users.append(user)
                db.execute(
                    "INSERT INTO sessions (token_hash, user_id) VALUES (%s, %s)", (token_hash(token), user)
                )
                if group:
                    db.execute("INSERT INTO memberships VALUES (%s, %s)", (user, group))
        yield client, pool, settings, provider, groups, users


def headers(token, *, origin=ORIGIN):
    return {"Cookie": f"{SESSION}={token}", "Origin": origin}
