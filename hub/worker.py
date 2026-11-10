import logging
import time
from pathlib import Path

from hub.ai import OpenAIProvider, ProviderUnavailable
from hub.config import Settings
from hub.db import pool_for
from hub.extract import isolated_extract
from hub.fetch import InvalidSource, SourceUnavailable
from hub.search import vector_parameter

HEARTBEAT = Path("/run/hub-worker/health")
LEASE_SECONDS = 120
LOGGER = logging.getLogger("hub.worker")


def claim(db):
    db.execute("""
        UPDATE jobs SET status = 'failed', error_code = 'attempts_exhausted', upload = NULL,
            lease_until = NULL, lease_token = NULL
        WHERE status = 'running' AND lease_until <= now() AND attempts >= 3
    """)
    return db.execute(
        """
        WITH candidate AS (
            SELECT id FROM jobs WHERE attempts < 3 AND (
                (status = 'queued' AND available_at <= now()) OR (status = 'running' AND lease_until <= now())
            )
            ORDER BY available_at, created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
        )
        UPDATE jobs j SET status = 'running', attempts = attempts + 1, lease_until = now() + make_interval(secs => %s),
            lease_token = gen_random_uuid()
        FROM candidate c, sources s
        WHERE j.id = c.id AND s.id = j.source_id
        RETURNING j.id, j.source_id, j.upload, j.file_name, j.lease_token, j.attempts, s.kind, s.input_url
    """,
        (LEASE_SECONDS,),
    ).fetchone()


def renew(db, job) -> bool:
    return (
        db.execute(
            """
        UPDATE jobs SET lease_until = now() + make_interval(secs => %s)
        WHERE id = %s AND lease_token = %s AND status = 'running' AND lease_until > now()
    """,
            (LEASE_SECONDS, job["id"], job["lease_token"]),
        ).rowcount
        == 1
    )


def publish(db, job, final_url, passages, vectors):
    source = db.execute("SELECT title FROM sources WHERE id = %s FOR UPDATE", (job["source_id"],)).fetchone()
    if source is None or not renew(db, job):
        return
    db.execute(
        "UPDATE versions SET retired_at = now() WHERE source_id = %s AND retired_at IS NULL",
        (job["source_id"],),
    )
    version = db.execute(
        """
        INSERT INTO versions (source_id, title, file_name, original, original_url)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
    """,
        (job["source_id"], source["title"], job["file_name"], job["upload"], final_url),
    ).fetchone()
    with db.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO chunks (version_id, ordinal, location, body, embedding) VALUES (%s, %s, %s, %s, %s::vector)
        """,
            [
                (version["id"], i, location, body, vector_parameter(vector))
                for i, ((location, body), vector) in enumerate(zip(passages, vectors, strict=True))
            ],
        )
    db.execute(
        """
        UPDATE jobs SET status = 'ready', upload = NULL, lease_until = NULL, lease_token = NULL, error_code = NULL
        WHERE id = %s
    """,
        (job["id"],),
    )


def fail(db, job, code: str, transient: bool):
    retry = transient and job["attempts"] < 3
    db.execute(
        """
        UPDATE jobs SET status = %s, error_code = %s, available_at = now() + %s * interval '1 second',
            upload = CASE WHEN %s THEN upload ELSE NULL END, lease_until = NULL, lease_token = NULL
        WHERE id = %s AND lease_token = %s AND lease_until > now()
    """,
        (
            "queued" if retry else "failed",
            code,
            10 * (2 ** job["attempts"]),
            retry,
            job["id"],
            job["lease_token"],
        ),
    )


def process(pool, settings, provider, job, heartbeat=lambda: None):
    try:
        final_url, passages = isolated_extract(
            job["kind"],
            job["upload"] or b"",
            job["input_url"],
            settings.allowed_hosts,
        )
        vectors = []
        for offset in range(0, len(passages), 48):
            with pool.connection() as db:
                if not renew(db, job):
                    return
            heartbeat()
            batch, usage = provider.embed([body for _, body in passages[offset : offset + 48]])
            vectors.extend(batch)
            with pool.connection() as db:
                db.execute("UPDATE worker_stats SET embedding_tokens = embedding_tokens + %s", (usage,))
        with pool.connection() as db:
            publish(db, job, final_url, passages, vectors)
    except (InvalidSource, SourceUnavailable, ProviderUnavailable) as exc:
        with pool.connection() as db:
            if isinstance(exc, ProviderUnavailable):
                code = "provider_unavailable" if exc.transient else "provider_rejected"
                fail(db, job, code, exc.transient)
            else:
                fail(db, job, str(exc), isinstance(exc, SourceUnavailable))


def cleanup(db):
    db.execute("DELETE FROM versions WHERE retired_at <= now() - interval '30 days'")
    db.execute("DELETE FROM sessions WHERE expires_at <= now()")
    db.execute("DELETE FROM login_flows WHERE expires_at <= now()")
    db.execute("DELETE FROM login_limits WHERE minute <= now() - interval '2 hours'")
    db.execute("DELETE FROM audit_events WHERE created_at <= now() - interval '90 days'")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings.load()
    key = settings.openai_api_key.get_secret_value()
    if not key:
        raise SystemExit("Mount the approved OpenAI API key before starting the worker.")
    provider = OpenAIProvider(key, settings.openai_answer_model)
    with pool_for(settings.database_url.get_secret_value()) as pool:
        next_cleanup = 0.0
        while True:
            try:
                with pool.connection() as db:
                    if time.monotonic() >= next_cleanup:
                        cleanup(db)
                        next_cleanup = time.monotonic() + 3600
                    job = claim(db)
                HEARTBEAT.touch()
                if job:
                    process(pool, settings, provider, job, HEARTBEAT.touch)
                else:
                    time.sleep(1)
            except Exception:
                LOGGER.error("Worker cycle failed")
                time.sleep(5)


if __name__ == "__main__":
    main()
