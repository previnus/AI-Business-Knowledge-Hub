import logging
from contextlib import asynccontextmanager
from urllib.parse import quote
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from hub import auth
from hub.ai import OpenAIProvider, ProviderUnavailable
from hub.config import MAX_REQUEST, Settings
from hub.db import pool_for
from hub.search import (
    ANSWER_TIME,
    ANSWERS,
    SEARCH,
    TOKENS,
    evidence_for_model,
    vector_parameter,
    verified_segments,
)
from hub.sources import router

LOGGER = logging.getLogger("hub.api")
INSUFFICIENT = {"status": "insufficient_evidence", "segments": []}


class RequestGuard:
    def __init__(self, app, origin):
        self.app = app
        self.origin = origin

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        limit = MAX_REQUEST if path.startswith("/api/admin/sources/") and path.endswith("/file") else 65536
        headers = dict(scope["headers"])
        writing = scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        if writing and headers.get(b"origin") != self.origin:
            await JSONResponse({"detail": "Invalid request origin."}, 403)(scope, receive, send)
            return
        length = headers.get(b"content-length")
        if length and (not length.isdigit() or len(length) > 10 or int(length) > limit):
            await JSONResponse({"detail": "Request is too large."}, 413)(scope, receive, send)
            return
        size = 0

        async def bounded_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > limit:
                raise StarletteHTTPException(413, "Request is too large.")
            return message

        started = False

        async def protected_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"].extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                    ]
                )
            await send(message)

        try:
            if writing and path.startswith("/api/"):
                try:
                    user = await run_in_threadpool(auth.current_user, Request(scope))
                except HTTPException as exc:
                    await JSONResponse({"detail": exc.detail}, exc.status_code, headers=exc.headers)(
                        scope,
                        receive,
                        protected_send,
                    )
                    return
                scope.setdefault("state", {})["authenticated_user"] = user
            await self.app(scope, bounded_receive, protected_send)
        except Exception:
            LOGGER.error("API request failed")
            if not started:
                await JSONResponse({"detail": "The request could not be completed."}, 500)(
                    scope,
                    receive,
                    protected_send,
                )


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def valid_text(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Enter a question.")
        return value


def create_app(settings=None, provider=None):
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        key = settings.openai_api_key.get_secret_value()
        live_provider = (
            OpenAIProvider(key, settings.openai_answer_model) if provider is None and key else None
        )
        try:
            with pool_for(settings.database_url.get_secret_value()) as pool:
                pool.wait()
                app.state.pool = pool
                app.state.settings = settings
                app.state.provider = provider or live_provider
                yield
        finally:
            if live_provider:
                live_provider.close()

    app = FastAPI(
        title="Company Knowledge Hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(RequestGuard, origin=settings.public_origin.encode("ascii"))
    app.include_router(router)
    app.get("/api/auth/login")(auth.login)
    app.get("/api/auth/callback")(auth.callback)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Check the request fields and try again."}, 422)

    @app.get("/api/me")
    def me(user=Depends(auth.current_user)):
        return user

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, user=Depends(auth.current_user)):
        with app.state.pool.connection() as db:
            db.execute(
                "DELETE FROM sessions WHERE token_hash = %s",
                (auth.token_hash(request.cookies[auth.SESSION]),),
            )
        response = Response(status_code=204)
        response.delete_cookie(auth.SESSION, secure=True, httponly=True, samesite="lax")
        return response

    @app.get("/api/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/api/health/ready")
    def ready():
        try:
            with app.state.pool.connection(timeout=3) as db:
                db.execute("SELECT 1 FROM sources LIMIT 1")
        except Exception:
            raise HTTPException(503, "Database is unavailable.") from None
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics():
        with app.state.pool.connection() as db:
            jobs = db.execute("SELECT status, count(*) AS total FROM jobs GROUP BY status").fetchall()
            worker = db.execute("SELECT embedding_tokens FROM worker_stats").fetchone()
        counters = [
            "# TYPE hub_ingestion_jobs gauge",
            *(f'hub_ingestion_jobs{{status="{row["status"]}"}} {row["total"]}' for row in jobs),
            "# TYPE hub_worker_embedding_tokens_total counter",
            f"hub_worker_embedding_tokens_total {worker['embedding_tokens']}",
        ]
        return Response(
            generate_latest() + ("\n".join(counters) + "\n").encode(), media_type=CONTENT_TYPE_LATEST
        )

    @app.get("/api/citations/{citation_id}")
    def citation(citation_id: UUID, user=Depends(auth.current_user)):
        with app.state.pool.connection() as db:
            row = db.execute(
                """
                SELECT c.id, v.title, c.location, c.body AS excerpt, s.kind, v.original_url, v.created_at,
                    CASE WHEN v.file_name IS NOT NULL THEN v.id END AS attachment_id
                FROM accessible_sources(%s) s JOIN versions v ON v.source_id = s.id
                JOIN chunks c ON c.version_id = v.id
                WHERE c.id = %s AND (v.retired_at IS NULL OR v.retired_at > now() - interval '30 days')
            """,
                (user["id"], citation_id),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Citation not found.")
        return row

    @app.get("/api/attachments/{version_id}")
    def attachment(version_id: UUID, user=Depends(auth.current_user)):
        with app.state.pool.connection() as db:
            row = db.execute(
                """
                SELECT v.original, v.file_name FROM accessible_sources(%s) s
                JOIN versions v ON v.source_id = s.id
                WHERE v.id = %s AND v.original IS NOT NULL
                    AND (v.retired_at IS NULL OR v.retired_at > now() - interval '30 days')
            """,
                (user["id"], version_id),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Attachment not found.")
        return Response(
            row["original"],
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["file_name"], safe=""),
            },
        )

    @app.post("/api/ask")
    @ANSWER_TIME.time()
    def ask(data: Question, request: Request, user=Depends(auth.current_user)):
        pool = app.state.pool
        provider = app.state.provider
        with pool.connection() as db:
            if not db.execute(
                """
                INSERT INTO question_limits VALUES (%s, date_trunc('minute', now()), 1)
                ON CONFLICT (user_id) DO UPDATE SET minute = EXCLUDED.minute,
                    count = CASE WHEN question_limits.minute = EXCLUDED.minute THEN question_limits.count + 1 ELSE 1 END
                WHERE question_limits.minute <> EXCLUDED.minute OR question_limits.count < %s
                RETURNING count
            """,
                (user["id"], settings.question_limit_per_minute),
            ).fetchone():
                raise HTTPException(
                    429, "Too many questions. Try again in a minute.", headers={"Retry-After": "60"}
                )
            exists = db.execute(
                """
                SELECT 1 FROM accessible_sources(%s) s
                JOIN versions v ON v.source_id = s.id AND v.retired_at IS NULL LIMIT 1
            """,
                (user["id"],),
            ).fetchone()
        if not exists:
            ANSWERS.labels("insufficient_evidence").inc()
            return INSUFFICIENT
        if provider is None:
            raise HTTPException(503, "The answer provider is not configured.")
        try:
            vectors, usage = provider.embed([data.question])
            TOKENS.labels("question_embedding").inc(usage)
            with pool.connection() as db:
                rows = db.execute(
                    SEARCH, (user["id"], vector_parameter(vectors[0]), data.question)
                ).fetchall()
            if not rows:
                ANSWERS.labels("insufficient_evidence").inc()
                return INSUFFICIENT
            draft, usage = provider.answer(data.question, evidence_for_model(rows))
            TOKENS.labels("generation").inc(usage)
        except ProviderUnavailable:
            ANSWERS.labels("provider_unavailable").inc()
            raise HTTPException(
                503,
                "The answer provider is temporarily unavailable. Try again.",
                headers={
                    "Retry-After": "10",
                },
            ) from None
        ids = [row["id"] for row in rows]
        with pool.connection() as db:
            still_allowed = db.execute(
                """
                SELECT count(*) AS total
                FROM sessions se CROSS JOIN accessible_sources(%s) s
                JOIN versions v ON v.source_id = s.id
                JOIN chunks c ON c.version_id = v.id
                WHERE se.token_hash = %s AND se.user_id = %s AND se.expires_at > now()
                    AND c.id = ANY(%s::uuid[])
                    AND (v.retired_at IS NULL OR v.retired_at > now() - interval '30 days')
            """,
                (user["id"], auth.token_hash(request.cookies[auth.SESSION]), user["id"], ids),
            ).fetchone()
        segments = verified_segments(draft, rows) if still_allowed["total"] == len(ids) else []
        if not segments:
            ANSWERS.labels("insufficient_evidence").inc()
            return INSUFFICIENT
        ANSWERS.labels("answered").inc()
        return {"status": "answered", "segments": segments}

    return app
