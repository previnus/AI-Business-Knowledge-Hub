CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    issuer text NOT NULL,
    subject text NOT NULL,
    display_name text NOT NULL DEFAULT '',
    is_admin boolean NOT NULL DEFAULT false,
    enabled boolean NOT NULL DEFAULT true,
    UNIQUE (issuer, subject)
);

CREATE TABLE sessions (
    token_hash bytea PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '8 hours'
);
CREATE INDEX sessions_user ON sessions (user_id);
CREATE INDEX sessions_expiry ON sessions (expires_at);

CREATE TABLE login_flows (
    token_hash bytea PRIMARY KEY,
    state text NOT NULL,
    nonce text NOT NULL,
    verifier text NOT NULL,
    return_path text NOT NULL,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '10 minutes'
);
CREATE INDEX login_flows_expiry ON login_flows (expires_at);

CREATE TABLE groups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE
);
CREATE TABLE memberships (
    user_id uuid REFERENCES users ON DELETE CASCADE,
    group_id uuid REFERENCES groups ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);
CREATE INDEX memberships_group ON memberships (group_id);

CREATE TABLE sources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('pdf', 'markdown', 'text', 'web')),
    input_url text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind = 'web') = (input_url IS NOT NULL))
);
CREATE TABLE source_groups (
    source_id uuid REFERENCES sources ON DELETE CASCADE,
    group_id uuid REFERENCES groups ON DELETE RESTRICT,
    PRIMARY KEY (source_id, group_id)
);
CREATE INDEX source_groups_group ON source_groups (group_id, source_id);

CREATE FUNCTION accessible_sources(principal uuid)
RETURNS SETOF sources LANGUAGE sql STABLE AS $$
    SELECT s.* FROM sources s
    WHERE EXISTS (
        SELECT FROM users u WHERE u.id = principal AND u.enabled AND (
            u.is_admin OR EXISTS (
                SELECT FROM memberships m JOIN source_groups sg USING (group_id)
                WHERE m.user_id = u.id AND sg.source_id = s.id
            )
        )
    )
$$;

CREATE TABLE versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid NOT NULL REFERENCES sources ON DELETE CASCADE,
    title text NOT NULL,
    file_name text,
    original bytea,
    original_url text,
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    CHECK ((original IS NOT NULL AND file_name IS NOT NULL AND original_url IS NULL)
        OR (original IS NULL AND file_name IS NULL AND original_url IS NOT NULL))
);
CREATE UNIQUE INDEX one_active_version ON versions (source_id) WHERE retired_at IS NULL;
CREATE INDEX versions_source ON versions (source_id);
CREATE INDEX versions_retired ON versions (retired_at) WHERE retired_at IS NOT NULL;

CREATE TABLE chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id uuid NOT NULL REFERENCES versions ON DELETE CASCADE,
    ordinal integer NOT NULL,
    location text NOT NULL,
    body text NOT NULL,
    embedding vector(1536) NOT NULL,
    search tsvector GENERATED ALWAYS AS (to_tsvector('english', location || ' ' || body)) STORED,
    UNIQUE (version_id, ordinal)
);
CREATE INDEX chunks_text_search ON chunks USING gin (search);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid NOT NULL REFERENCES sources ON DELETE CASCADE,
    upload bytea,
    file_name text,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','ready','failed')),
    attempts integer NOT NULL DEFAULT 0,
    lease_until timestamptz,
    lease_token uuid,
    error_code text,
    available_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_open_job ON jobs (source_id) WHERE status IN ('queued', 'running');
CREATE INDEX jobs_source ON jobs (source_id, created_at DESC, id DESC);
CREATE INDEX jobs_claim ON jobs (available_at, created_at) WHERE status IN ('queued', 'running');

CREATE TABLE question_limits (
    user_id uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
    minute timestamptz NOT NULL,
    count integer NOT NULL
);
CREATE TABLE audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor uuid NOT NULL,
    action text NOT NULL,
    target uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_expiry ON audit_events (created_at);

CREATE TABLE worker_stats (embedding_tokens bigint NOT NULL DEFAULT 0);
INSERT INTO worker_stats DEFAULT VALUES;
