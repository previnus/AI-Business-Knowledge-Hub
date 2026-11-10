# AI Business Knowledge Hub

Knowledge Hub answers employee questions with links to the passages it used. Central admins ingest company PDFs, Markdown and UTF-8 text files, and individual public HTTPS pages. Employees sign in through company SSO and can access sources shared with their knowledge groups.

## Start with synthetic data

Use Docker Engine with Compose v2, one OpenID Connect provider, and a company-approved OpenAI project. The examples below use a POSIX shell from the repository root.

1. Copy `.env.example` to `.env`. Set the exact HTTPS `PUBLIC_ORIGIN`, its hostname in `HUB_HOST`, the OIDC issuer and client ID, and a comma-separated `WEB_HOSTS` allowlist. Use exact public hostnames. The default loopback binding is for local testing. For a company deployment, use the appropriate bind address and firewall rules for your network.
2. Register a confidential web client with the identity provider. Allow authorization code flow with PKCE S256 and the exact callback `https://YOUR_HOST/api/auth/callback`. Assign the application to company employees through the provider. `OIDC_TOKEN_AUTH` defaults to `client_secret_basic` and supports `client_secret_post` if the provider requires it.
3. Create the ignored secret directory and generate independent database passwords.

   ```sh
   umask 077
   mkdir -p secrets
   openssl rand -hex 32 > secrets/postgres_password
   printf 'host=db dbname=knowledgehub user=hub_runtime password=%s\n' \
     "$(openssl rand -hex 32)" > secrets/database_url
   ```

   Mount the provider credentials through `secrets/oidc_client_secret` and `secrets/openai_api_key`. Use your secret manager to populate the files. On Linux, ensure container UID/GID `10001:10001` can read the mounted files, for example with group ownership and mode `0640`. Keep the secret directory restricted on the host. The API and worker use a non-owner database login. The database, migration, and backup containers receive the database owner credential.
4. Build, migrate, and start the app. Bootstrap the first administrator with the immutable OIDC `sub` value for the configured issuer.

   ```sh
   docker compose up -d --build
   docker compose ps
   docker compose run --rm migrate python -m hub.manage grant-admin 'EXACT_OIDC_SUBJECT'
   ```

   Open the configured origin. Caddy uses a local CA for `localhost`, so trust the development CA on the test machine or use a development hostname with a trusted TLS certificate. The session cookie requires HTTPS.
   When reusing Caddy volumes from an earlier root-run deployment, grant container UID/GID `10001:10001` access to the existing certificate and configuration files before restarting.
5. In **Administration → Manage access**, create a knowledge group. Have an employee sign in once, then assign the group to that account. In **Add source**, upload a synthetic document and select the group. After the source becomes **Active**, ask a question and open the citation. Use a second employee in a different group to verify isolation.

For credential-free verification, use the integration tests below. They use a mock identity provider and mock AI responses. Keep company material out of the pilot until its data classes and the project's provider settings are approved. OpenAI receives chunk text for embeddings and the question and selected excerpts for answer generation. `store=false` disables normal Responses API storage, and the project's abuse-monitoring retention may still apply. [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)

## Operations

- **Sources.** Use **Refresh** for a webpage or **Replace file** for an upload. A failed ingestion leaves the previous indexed version active. Citations to replaced versions remain available for 30 days under the source's current permissions. Deleting a source removes its versions and pending work from the live database. Earlier encrypted backups retain their contents until they expire.
- **Access.** Group membership changes take effect on subsequent API requests. Disabling an employee also revokes their app sessions. Sessions expire after eight hours. Caddy supplies the client address for sign-in throttling and overwrites incoming copies of that header. Keep the API behind the proxy. To revoke an administrator, use `docker compose run --rm migrate python -m hub.manage disable-admin 'EXACT_OIDC_SUBJECT'`.
- **Health and metrics.** Use `/api/health/live` and `/api/health/ready` for API checks. Scrape `api:8000/metrics` from a trusted internal network to monitor job failures, answer latency, abstentions, and provider token usage. Keep that endpoint off the public proxy. Use `docker compose ps` to check worker health. Configure host or volume encryption for PostgreSQL and Caddy data.
- **Backups.** Set `AGE_RECIPIENT` to the public recipient. Create `backups/` with write access for container UID/GID `10001:10001`, then run `docker compose run --rm backup`. The job streams a custom-format PostgreSQL dump through age and writes only encrypted output under `backups/`. Schedule it through your deployment scheduler, store the decryption key separately, and apply your retention policy. For a restore drill, provision a fresh database with pgvector and the runtime role, then stream `age --decrypt -i <identity-file> <backup-file>` into `pg_restore --exit-on-error --no-owner --no-privileges -d <isolated-db>`. Re-run the migration service to restore runtime grants before pointing the app at the recovered database.
- **Release review.** Run the Rafter code review and `rafter run` against the target branch before deploying company data. Keep company source documents and all secrets outside the public repository.

## Tests

Run the Python tests against a disposable PostgreSQL 16 database with pgvector available. Tests create and drop their own schemas. They cover source formats and exact citations, group isolation and revocation, worker recovery, SSRF, parser failures, and the OIDC PKCE flow. They use synthetic data and make no live OpenAI calls.

```sh
uv sync --locked
HUB_TEST_DATABASE_URL=postgresql://USER@127.0.0.1:5432/TEST_DB uv run --locked pytest
uv run --locked ruff check hub tests
npm --prefix web ci --ignore-scripts
npm --prefix web run build
```

The app is single-turn and uses English keyword indexing. It accepts text PDFs up to 500 pages, file uploads up to 25 MiB, and static HTML pages up to 5 MiB. Scanned documents need a text layer before upload. A citation verifies where a quote came from. Check the source before relying on a generated claim for a sensitive decision, and evaluate answer support with a small, approved pilot corpus before expanding access.
