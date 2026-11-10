# AI Business Knowledge Hub

Knowledge Hub answers employee questions with links to the passages it used. Central admins ingest company PDFs, Markdown and UTF-8 text files, and individual public HTTPS pages. Employees sign in through company SSO and can access sources shared with their knowledge groups.

This project is licensed under the GNU General Public License version 3 only (`GPL-3.0-only`). [License text](LICENSE)

## Preview locally

Clone the repository and use Docker Engine with Compose v2. These commands are for a fresh checkout in a Linux shell. The app can show its sign-in screen before you choose a host, an identity provider, or an OpenAI project.

```sh
cp .env.example .env
umask 077
mkdir -p secrets
openssl rand -hex 32 > secrets/postgres_password
printf 'host=db dbname=knowledgehub user=hub_runtime password=%s\n' \
  "$(openssl rand -hex 32)" > secrets/database_url
touch secrets/oidc_client_secret secrets/openai_api_key
sudo chgrp 10001 secrets/*
sudo chmod 0640 secrets/*
docker compose up -d --build --wait web
```

Open the preview at `https://localhost`. Trust the local Caddy CA in your test browser. You can export the CA certificate with `docker compose exec -T web cat /data/caddy/pki/authorities/local/root.crt`. Sign-in and ingestion become available after you configure the providers. Keep the generated database secrets with their database volume.

## Configure hosting and providers

1. Copy `.env.example` to `.env` on a fresh host. Set `HUB_HOST` to a bare hostname such as `knowledge.example.com` and `BIND_ADDRESS` to the server interface that should serve ports 80 and 443. Compose derives the browser origin and callback from `https://HUB_HOST`. If an existing `.env` sets `PUBLIC_ORIGIN` explicitly, remove it to use the derived value or update it to match the browser URL. For public TLS, point the hostname's DNS at the host and allow ports 80 and 443 so Caddy can obtain a certificate. [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https)
2. When your identity provider is ready, register a confidential web client with authorization code flow and PKCE S256. Register `https://YOUR_HOST/api/auth/callback` as the exact redirect URI and assign the application to your employees. Set `OIDC_ISSUER` and `OIDC_CLIENT_ID` in `.env`. Set `OIDC_TOKEN_AUTH=client_secret_post` if the provider requires it. For webpage ingestion, set `WEB_HOSTS` to a comma-separated list of exact public hostnames.
3. Create the database secrets as shown above for a fresh install. Populate `secrets/oidc_client_secret` and `secrets/openai_api_key` from your secret manager once you have provider credentials. Keep `.env` and `secrets/` local to each deployment. Ensure the files are readable by container UID/GID `10001:10001`. The API and worker use a separate database login from the database owner used for migration and backup.
4. Build and start the full stack. Use `--force-recreate` on this first full startup if you previously ran the preview or are replacing provider credentials.

   ```sh
   docker compose up -d --build --wait
   docker compose ps
   docker compose run --rm migrate python -m hub.manage grant-admin 'EXACT_OIDC_SUBJECT'
   ```

   Use the administrator's immutable OIDC `sub` value from the configured issuer. When reusing Caddy volumes from an earlier root-run deployment, grant container UID/GID `10001:10001` access to the existing certificate and configuration files before restarting.
5. Sign in at the configured HTTPS hostname. In **Administration → Manage access**, create a knowledge group. Have an employee sign in once, then assign the group to that account. In **Add source**, upload a synthetic document and select the group. After the source becomes **Active**, ask a question and open the citation. Use a second employee in a different group to verify isolation.

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

The app is single-turn and uses English keyword indexing. It accepts text PDFs up to 500 pages, file uploads up to 25 MiB, and static HTML pages up to 5 MiB. Scanned documents need a text layer before upload. Answers display complete indexed passages in the source's own wording. The model selects which passages to show, so check the original context for sensitive decisions and evaluate passage selection with an approved pilot corpus before expanding access.
