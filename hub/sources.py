import json
from pathlib import PurePath
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import UploadFile

from hub.auth import admin_user, current_user
from hub.config import MAX_FILE
from hub.fetch import InvalidSource, public_url

router = APIRouter(prefix="/api")
UPLOAD_TYPES = {".pdf": "pdf", ".md": "markdown", ".markdown": "markdown", ".txt": "text"}


class GroupInput(BaseModel):
    group_ids: list[UUID] = Field(max_length=100)


class SourceInput(GroupInput):
    title: str = Field(min_length=1, max_length=160)


class URLInput(SourceInput):
    url: str = Field(min_length=1, max_length=2048)


class NewGroup(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MemberInput(GroupInput):
    enabled: bool


def clean_label(value: str) -> str:
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        raise HTTPException(422, "Enter a name without control characters.")
    return value


def audit(db, user, action, target):
    db.execute(
        "INSERT INTO audit_events (actor, action, target) VALUES (%s, %s, %s)", (user["id"], action, target)
    )


def assert_groups(db, groups, *, required=False):
    ids = list(dict.fromkeys(groups))
    if required and not ids:
        raise HTTPException(422, "Select at least one knowledge group.")
    matches = db.execute("SELECT id FROM groups WHERE id = ANY(%s::uuid[])", (ids,)).fetchall()
    if len(matches) != len(ids):
        raise HTTPException(422, "A selected knowledge group no longer exists.")
    return ids


def assign_groups(db, source_id, groups):
    ids = assert_groups(db, groups, required=True)
    db.execute("DELETE FROM source_groups WHERE source_id = %s", (source_id,))
    db.execute("INSERT INTO source_groups SELECT %s, unnest(%s::uuid[])", (source_id, ids))


def lock_source(db, source_id):
    source = db.execute("SELECT id, kind FROM sources WHERE id = %s FOR UPDATE", (source_id,)).fetchone()
    if not source:
        raise HTTPException(404, "Source not found.")
    return source


def enqueue(db, source_id, data=None, file_name=None):
    job = db.execute(
        """
        INSERT INTO jobs (source_id, upload, file_name) VALUES (%s, %s, %s)
        ON CONFLICT (source_id) WHERE status IN ('queued', 'running') DO NOTHING RETURNING id
    """,
        (source_id, data, file_name),
    ).fetchone()
    if not job:
        raise HTTPException(409, "This source already has an ingestion job in progress.")
    return {"source_id": source_id, "job_id": job["id"]}


async def read_upload(request: Request, new: bool):
    if request.headers.get("content-type", "").partition(";")[0].lower() != "multipart/form-data":
        raise HTTPException(415, "Upload a multipart form.")
    async with request.form(max_files=1, max_fields=2, max_part_size=8192) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile) or not file.filename:
            raise HTTPException(422, "Select a file.")
        filename = clean_label(file.filename.replace("\\", "/").split("/")[-1])[:220]
        suffix = PurePath(filename).suffix.lower()
        if suffix not in UPLOAD_TYPES:
            raise HTTPException(415, "Choose a PDF, Markdown, or UTF-8 text file.")
        content = await file.read(MAX_FILE + 1)
        if not content:
            raise HTTPException(422, "The selected file is empty.")
        if len(content) > MAX_FILE:
            raise HTTPException(413, "The file exceeds 25 MB.")
        kind = UPLOAD_TYPES[suffix]
        if kind == "pdf" and not content.startswith(b"%PDF-"):
            raise HTTPException(415, "The file does not have a PDF header.")
        data = None
        if new:
            try:
                data = SourceInput(title=form.get("title"), group_ids=json.loads(form.get("group_ids", "[]")))
            except (ValidationError, TypeError, ValueError):
                raise HTTPException(422, "Enter a title and choose the source groups.") from None
        return kind, filename, content, data


def insert_source(db, data, kind, url=None):
    row = db.execute(
        """
        INSERT INTO sources (title, kind, input_url) VALUES (%s, %s, %s) RETURNING id
    """,
        (clean_label(data.title), kind, url),
    ).fetchone()
    assign_groups(db, row["id"], data.group_ids)
    return row["id"]


@router.get("/sources")
def sources(request: Request, user=Depends(current_user)):
    with request.app.state.pool.connection() as db:
        return db.execute(
            """
            SELECT s.id, v.title, s.kind, v.created_at, p.id AS citation_id
            FROM accessible_sources(%s) s JOIN versions v ON v.source_id = s.id AND v.retired_at IS NULL
            JOIN LATERAL (SELECT id FROM chunks WHERE version_id = v.id ORDER BY ordinal LIMIT 1) p ON true
            ORDER BY lower(v.title), s.id
        """,
            (user["id"],),
        ).fetchall()


@router.get("/admin/sources")
def admin_sources(request: Request, user=Depends(admin_user)):
    with request.app.state.pool.connection() as db:
        return db.execute(
            """
            SELECT s.id, s.title, s.kind, s.input_url, j.id AS job_id, j.status, j.error_code,
                v.id IS NOT NULL AS active, p.id AS citation_id,
                ARRAY(SELECT group_id FROM source_groups WHERE source_id = s.id ORDER BY group_id) AS group_ids
            FROM accessible_sources(%s) s
            LEFT JOIN versions v ON v.source_id = s.id AND v.retired_at IS NULL
            LEFT JOIN LATERAL (
                SELECT id, status, error_code FROM jobs WHERE source_id = s.id
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) j ON true
            LEFT JOIN LATERAL (SELECT id FROM chunks WHERE version_id = v.id ORDER BY ordinal LIMIT 1) p ON true
            ORDER BY lower(s.title), s.id
        """,
            (user["id"],),
        ).fetchall()


@router.post("/admin/sources/file", status_code=202)
async def upload_source(request: Request, user=Depends(admin_user)):
    kind, filename, content, data = await read_upload(request, new=True)
    with request.app.state.pool.connection() as db:
        source_id = insert_source(db, data, kind)
        result = enqueue(db, source_id, content, filename)
        audit(db, user, "source_create", source_id)
    return result


@router.post("/admin/sources/web", status_code=202)
def web_source(data: URLInput, request: Request, user=Depends(admin_user)):
    app = request.app.state
    try:
        url = public_url(data.url, app.settings.allowed_hosts)
    except InvalidSource:
        raise HTTPException(422, "Enter a public HTTPS URL from a configured hostname.") from None
    with app.pool.connection() as db:
        source_id = insert_source(db, data, "web", url)
        result = enqueue(db, source_id)
        audit(db, user, "source_create", source_id)
    return result


@router.put("/admin/sources/{source_id}/file", status_code=202)
async def replace_file(source_id: UUID, request: Request, user=Depends(admin_user)):
    kind, filename, content, _ = await read_upload(request, new=False)
    with request.app.state.pool.connection() as db:
        source = lock_source(db, source_id)
        if source["kind"] != kind:
            raise HTTPException(422, "The replacement must be the same file format.")
        result = enqueue(db, source_id, content, filename)
        audit(db, user, "source_replace", source_id)
    return result


@router.post("/admin/sources/{source_id}/refresh", status_code=202)
def refresh(source_id: UUID, request: Request, user=Depends(admin_user)):
    with request.app.state.pool.connection() as db:
        source = lock_source(db, source_id)
        if source["kind"] != "web":
            raise HTTPException(422, "Upload a replacement to refresh a file.")
        result = enqueue(db, source_id)
        audit(db, user, "source_refresh", source_id)
    return result


@router.put("/admin/sources/{source_id}/groups", status_code=204)
def share_source(source_id: UUID, data: GroupInput, request: Request, user=Depends(admin_user)):
    with request.app.state.pool.connection() as db:
        lock_source(db, source_id)
        assign_groups(db, source_id, data.group_ids)
        audit(db, user, "source_permissions", source_id)


@router.delete("/admin/sources/{source_id}", status_code=204)
def delete_source(source_id: UUID, request: Request, user=Depends(admin_user)):
    with request.app.state.pool.connection() as db:
        lock_source(db, source_id)
        db.execute("DELETE FROM sources WHERE id = %s", (source_id,))
        audit(db, user, "source_delete", source_id)


@router.get("/admin/access")
def access(request: Request, user=Depends(admin_user)):
    with request.app.state.pool.connection() as db:
        groups = db.execute("SELECT id, name FROM groups ORDER BY lower(name), id").fetchall()
        users = db.execute("""
            SELECT u.id, u.display_name, u.subject, u.enabled, u.is_admin,
                ARRAY(SELECT group_id FROM memberships WHERE user_id = u.id ORDER BY group_id) AS group_ids
            FROM users u ORDER BY lower(display_name), id
        """).fetchall()
    return {"groups": groups, "users": users}


@router.post("/admin/groups", status_code=201)
def create_group(data: NewGroup, request: Request, user=Depends(admin_user)):
    with request.app.state.pool.connection() as db:
        row = db.execute(
            """
            INSERT INTO groups (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id, name
        """,
            (clean_label(data.name),),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "A group with that name already exists.")
        audit(db, user, "group_create", row["id"])
    return row


@router.put("/admin/users/{user_id}", status_code=204)
def update_member(user_id: UUID, data: MemberInput, request: Request, user=Depends(admin_user)):
    with request.app.state.pool.connection() as db:
        member = db.execute("SELECT is_admin FROM users WHERE id = %s FOR UPDATE", (user_id,)).fetchone()
        if not member:
            raise HTTPException(404, "Account not found.")
        if member["is_admin"] and not data.enabled:
            raise HTTPException(422, "Administrators must be deprovisioned by the deployment operator.")
        ids = assert_groups(db, data.group_ids)
        db.execute("UPDATE users SET enabled = %s WHERE id = %s", (data.enabled, user_id))
        if not data.enabled:
            db.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        db.execute("DELETE FROM memberships WHERE user_id = %s", (user_id,))
        db.execute("INSERT INTO memberships SELECT %s, unnest(%s::uuid[])", (user_id, ids))
        audit(db, user, "user_permissions", user_id)
