import hashlib
import hmac
import re
import secrets
from ipaddress import ip_address
from urllib.parse import urlsplit

from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from joserfc import jwt
from joserfc.jwk import KeySet
from starlette.concurrency import run_in_threadpool

SESSION = "__Host-hub-session"
FLOW = "__Host-hub-flow"
SAFE_RETURN = re.compile(r"/citation/[0-9a-f-]{36}")
SIGNATURE_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512")


def token_hash(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def get_session(db, raw: str | None):
    if not raw or len(raw) != 43:
        return None
    return db.execute(
        """
        SELECT u.id, u.display_name, u.is_admin
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.expires_at > now() AND u.enabled
    """,
        (token_hash(raw),),
    ).fetchone()


def current_user(request: Request):
    if hasattr(request.state, "authenticated_user"):
        return request.state.authenticated_user
    with request.app.state.pool.connection() as db:
        user = get_session(db, request.cookies.get(SESSION))
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    return user


def admin_user(request: Request):
    user = current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Administrator access required.")
    return user


def reserve_login(request: Request):
    address = request.headers.get("x-hub-client-ip") or (request.client.host if request.client else "")
    try:
        address = str(ip_address(address))
    except ValueError:
        address = "unknown"
    with request.app.state.pool.connection() as db:
        for bucket, limit in ((b"\x00", 120), (token_hash(address), 12)):
            admitted = db.execute(
                """
                INSERT INTO login_limits VALUES (%s, date_trunc('minute', now()), 1)
                ON CONFLICT (bucket) DO UPDATE SET minute = GREATEST(login_limits.minute, EXCLUDED.minute),
                    count = CASE WHEN login_limits.minute < EXCLUDED.minute THEN 1 ELSE login_limits.count + 1 END
                WHERE login_limits.minute < EXCLUDED.minute OR login_limits.count < %s
                RETURNING count
                """,
                (bucket, limit),
            ).fetchone()
            if not admitted:
                raise HTTPException(
                    429, "Too many sign-in attempts. Try again shortly.", headers={"Retry-After": "60"}
                )


def set_cookie(response, name: str, value: str, max_age: int):
    response.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


async def metadata(client: AsyncOAuth2Client, issuer: str) -> dict:
    response = await client.request(
        "GET", issuer.rstrip("/") + "/.well-known/openid-configuration", auth=None
    )
    response.raise_for_status()
    data = response.json()
    if data["issuer"] != issuer:
        raise ValueError("issuer mismatch")
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        endpoint = urlsplit(data[field])
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.fragment:
            raise ValueError("invalid OIDC endpoint")
    return data


def oidc_client(settings):
    if not (
        settings.oidc_issuer and settings.oidc_client_id and settings.oidc_client_secret.get_secret_value()
    ):
        raise HTTPException(503, "Company sign-in is not configured.")
    return AsyncOAuth2Client(
        settings.oidc_client_id,
        settings.oidc_client_secret.get_secret_value(),
        scope="openid profile",
        redirect_uri=settings.public_origin + "/api/auth/callback",
        code_challenge_method="S256",
        token_endpoint_auth_method=settings.oidc_token_auth,
        timeout=8,
        follow_redirects=False,
        trust_env=False,
    )


async def login(request: Request, return_to: str = "/"):
    app = request.app.state
    await run_in_threadpool(reserve_login, request)
    raw, state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
    return_path = return_to if SAFE_RETURN.fullmatch(return_to) else "/"
    try:
        async with oidc_client(app.settings) as client:
            data = await metadata(client, app.settings.oidc_issuer)
            url, _ = client.create_authorization_url(
                data["authorization_endpoint"],
                state=state,
                nonce=nonce,
                code_verifier=verifier,
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Company sign-in is temporarily unavailable.") from None
    with app.pool.connection() as db:
        db.execute(
            """
            INSERT INTO login_flows (token_hash, state, nonce, verifier, return_path)
            VALUES (%s, %s, %s, %s, %s)
        """,
            (token_hash(raw), state, nonce, verifier, return_path),
        )
    response = RedirectResponse(url, status_code=303)
    set_cookie(response, FLOW, raw, 600)
    return response


async def callback(request: Request):
    app = request.app.state
    settings = app.settings
    raw = request.cookies.get(FLOW, "")
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if len(raw) != 43 or len(state) != 43 or not 0 < len(code) <= 4096:
        raise HTTPException(400, "Sign-in could not be verified. Start again.")
    if request.query_params.get("iss", settings.oidc_issuer) != settings.oidc_issuer:
        raise HTTPException(400, "Sign-in could not be verified. Start again.")
    with app.pool.connection() as db:
        flow = db.execute(
            """
            DELETE FROM login_flows WHERE token_hash = %s AND state = %s AND expires_at > now()
            RETURNING nonce, verifier, return_path
        """,
            (token_hash(raw), state),
        ).fetchone()
    if flow is None:
        raise HTTPException(400, "Sign-in expired or could not be verified. Start again.")
    try:
        async with oidc_client(settings) as client:
            data = await metadata(client, settings.oidc_issuer)
            response = await client.request("GET", data["jwks_uri"], auth=None)
            response.raise_for_status()
            token = await client.fetch_token(
                data["token_endpoint"],
                code=code,
                code_verifier=flow["verifier"],
            )
        allowed = [
            alg for alg in SIGNATURE_ALGORITHMS if alg in data["id_token_signing_alg_values_supported"]
        ]
        if not allowed:
            raise ValueError("unsupported signature")
        verified = jwt.decode(token["id_token"], KeySet.import_key_set(response.json()), algorithms=allowed)
        claims = CodeIDToken(
            verified.claims,
            verified.header,
            {"iss": {"essential": True, "value": settings.oidc_issuer}},
            {
                "nonce": flow["nonce"],
                "client_id": settings.oidc_client_id,
                "access_token": token.get("access_token"),
            },
        )
        claims.validate(leeway=30)
        audience = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if settings.oidc_client_id not in audience or (
            (len(audience) > 1 or "azp" in claims) and claims.get("azp") != settings.oidc_client_id
        ):
            raise ValueError("invalid audience")
        if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"], flow["nonce"]):
            raise ValueError("invalid nonce")
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject or len(subject) > 255 or "\x00" in subject:
            raise ValueError("invalid subject")
        name = next(
            (claims[field] for field in ("name", "preferred_username") if isinstance(claims.get(field), str)),
            "Employee",
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Company sign-in could not be verified.") from None
    session = secrets.token_urlsafe(32)
    with app.pool.connection() as db:
        user = db.execute(
            """
            INSERT INTO users (issuer, subject, display_name) VALUES (%s, %s, %s)
            ON CONFLICT (issuer, subject) DO UPDATE SET display_name = EXCLUDED.display_name
            RETURNING id, enabled
        """,
            (settings.oidc_issuer, subject, name.replace("\x00", "")[:160] or "Employee"),
        ).fetchone()
        if not user["enabled"]:
            raise HTTPException(403, "Your Knowledge Hub account is disabled.")
        previous = request.cookies.get(SESSION)
        if previous:
            db.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(previous),))
        db.execute(
            "INSERT INTO sessions (token_hash, user_id) VALUES (%s, %s)", (token_hash(session), user["id"])
        )
    response = RedirectResponse(flow["return_path"], status_code=303)
    response.delete_cookie(FLOW, secure=True, httponly=True, samesite="lax")
    set_cookie(response, SESSION, session, 28800)
    return response
