CREATE TABLE login_limits (
    bucket bytea PRIMARY KEY,
    minute timestamptz NOT NULL,
    count integer NOT NULL
);
CREATE INDEX login_limits_expiry ON login_limits (minute);
