# main.py — Python / FastAPI micro-server
# Web portal that embeds the watsonx Orchestrate (wxO) chat and passes it the
# user's Entra ID identity for an OAuth2 On-Behalf-Of (OBO) flow.
#
# Flow overview:
#   1. /              -> the user signs in to Entra ID (Authorization Code, MSAL).
#   2. /auth/callback -> we get the sso_token (aud = api://CLIENT_ID), stored
#                        ONLY in the server-side session.
#   3. build_wxo_jwt  -> the sso_token is encrypted with the IBM public key (RSA-OAEP,
#                        split into blocks like Node-RSA) in the user_payload claim,
#                        then the JWT is signed (RS256) with our private key.
#   4. The wxO widget receives this JWT; wxO verifies the signature, decrypts
#      user_payload and uses the sso_token as the OBO assertion in the
#      "oauth_auth_on_behalf_of_flow" connection:
#        a. IdP config: sso_token      -> api://CLIENT_ID/access_as_user token
#        b. App config: token from (a) -> Microsoft Graph token
#   5. The wxO Outlook tool calls Microsoft Graph with the token from (b).
#   /debug replays steps 4a, 4b and 5 from this server to validate the config.

import os
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import msal
import jwt                         # PyJWT — signs the wxO JWT (RS256)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

# print() that does not crash on Windows consoles (cp1252) because of emojis.
def _safe_print(*args, **kwargs):
    """Print that never crashes on Windows cp1252 consoles."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        flat = " ".join(str(a) for a in args)
        print(flat.encode(enc, errors="replace").decode(enc, errors="replace"), **kwargs)


# ── Required environment variables check ───────────────────────
REQUIRED = [
    "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET",
    "REDIRECT_URI", "SESSION_SECRET",
    "WXO_AGENT_ID", "WXO_ORCHESTRATION_ID", "WXO_AGENT_ENV_ID",
]
for key in REQUIRED:
    if not os.environ.get(key):
        _safe_print(f"❌  Missing environment variable: {key}", file=sys.stderr)
        sys.exit(1)

# ── Configuration ──────────────────────────────────────────────
TENANT_ID             = os.environ["TENANT_ID"]
CLIENT_ID             = os.environ["CLIENT_ID"]
CLIENT_SECRET         = os.environ["CLIENT_SECRET"]
REDIRECT_URI          = os.environ["REDIRECT_URI"]
SESSION_SECRET        = os.environ["SESSION_SECRET"]
WXO_AGENT_ID          = os.environ["WXO_AGENT_ID"]
WXO_ORCHESTRATION_ID  = os.environ["WXO_ORCHESTRATION_ID"]
WXO_AGENT_ENV_ID      = os.environ["WXO_AGENT_ENV_ID"]
WXO_CRN               = os.environ.get("WXO_CRN", "")
WXO_HOST_URL          = os.environ.get("WXO_HOST_URL", "https://us-south.watson-orchestrate.cloud.ibm.com")

# ── RSA key loading ────────────────────────────────────────────
# Client private key (wxo-client-private.pem): signs the JWT sent to wxO.
#   The matching public key must be registered in wxO (embedded chat
#   security) so wxO can verify the signature.
# IBM public key (wxo-ibm-public.pem): provided by wxO, encrypts the sso_token
#   in user_payload; only wxO (which holds the IBM private key) can decrypt it.
try:
    WXO_PRIVATE_KEY    = Path("wxo-client-private.pem").read_text()
    IBM_PUBLIC_KEY_PEM = Path("wxo-ibm-public.pem").read_text()
except FileNotFoundError as e:
    _safe_print(f"❌  Key file not found: {e}", file=sys.stderr)
    _safe_print("    Check that wxo-client-private.pem and wxo-ibm-public.pem", file=sys.stderr)
    _safe_print("    are present at the project root.", file=sys.stderr)
    sys.exit(1)

# ── MSAL client (Entra ID authentication) ──────────────────────
msal_app = msal.ConfidentialClientApplication(
    client_id=CLIENT_ID,
    authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    client_credential=CLIENT_SECRET,
)
# ── Scope for the Authorization Code flow ──────────────────────
# ⚠ The sso_token must be issued FOR your application
#   (aud = api://{CLIENT_ID}/access_as_user) to be usable as an
#   exchange assertion (OBO / Token Exchange) with Entra ID.
#   A token with aud=https://graph.microsoft.com is a Graph resource token
#   signed with the resource keys -> the exchange fails with AADSTS50013
#   "Assertion failed signature validation" (verified experimentally with
#   the /debug route).
# ⚠ Entra ID prerequisite: App Registration -> "Expose an API" -> scope
#   access_as_user defined + admin consent granted.
# Can be overridden with SSO_SCOPE in .env (e.g. https://graph.microsoft.com/.default
# to get a direct Graph token — not usable as an OBO assertion).
SSO_SCOPE = os.environ.get("SSO_SCOPE") or f"api://{CLIENT_ID}/access_as_user"  # empty value -> default
SCOPES = [SSO_SCOPE]

# ── FastAPI application ────────────────────────────────────────
# Environment detection: HTTPS in production (Code Engine), HTTP locally
_is_production = not REDIRECT_URI.startswith("http://localhost")


app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="wxo_session",
    max_age=3600,        # 1 hour
    https_only=_is_production,   # False locally (HTTP), True on Code Engine (HTTPS)
    same_site="lax",
)


# ── Route GET / — Portal main page ────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    sso_token = request.session.get("sso_token")

    if not sso_token:
        # No active session -> redirect to Entra ID for authentication
        auth_url = msal_app.get_authorization_request_url(
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
        )
        return RedirectResponse(url=auth_url)

    # Active session -> build the wxO JWT and serve the HTML with the chat
    try:
        user_email = request.session.get("user_email", "")
        wxo_jwt = build_wxo_jwt(
            sso_token=sso_token,
            user_email=user_email,
        )
        _safe_print(f"🔑 wxO JWT generated for {user_email} ({len(wxo_jwt)} chars)")
        _safe_print(f"   sso_token longueur : {len(sso_token)} chars")
        return HTMLResponse(content=render_portal_page(wxo_jwt, user_email))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return HTMLResponse(
            content=f"Error while generating the wxO JWT: {exc}",
            status_code=500,
        )


# ── Route GET /auth/callback — Entra ID callback ──────────────
@app.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    # Entra ID redirects here with ?code=... after a successful sign-in
    if error:
        return HTMLResponse(
            content=f"Erreur Entra ID : {error} — {error_description}",
            status_code=400,
        )

    try:
        result = msal_app.acquire_token_by_authorization_code(
            code=code,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
        )
        if "error" in result:
            return HTMLResponse(
                content=f"Erreur MSAL : {result['error']} — {result.get('error_description')}",
                status_code=500,
            )

        # access_token = sso_token issued for api://CLIENT_ID/access_as_user (see SSO_SCOPE).
        # This is the token wxO will use as the OBO assertion.
        # Store the sso_token server-side — it NEVER reaches the browser in clear text.
        request.session["sso_token"]  = result["access_token"]
        request.session["user_email"] = result.get("id_token_claims", {}).get(
            "preferred_username", ""
        )

        _safe_print(f"✅  Authenticated (Token Exchange): {request.session['user_email']}")
        return RedirectResponse(url="/")
    except Exception as exc:
        return HTMLResponse(
            content=f"Error while exchanging the Entra code: {exc}",
            status_code=500,
        )


# ── Route GET /debug — Reproduces the two OBO token exchanges done by wxO ────
# Same requests as the wxO "oauth_auth_on_behalf_of_flow" connection:
#   Step 1 (Identity Provider config): sso_token -> token for api://CLIENT_ID
#   Step 2 (Application config):       step-1 token -> Microsoft Graph token
#   Step 3: GET /v1.0/me/mailFolders with the Graph token (what the wxO tool does)
# If both steps succeed here, the values shown are the ones to put in the
# wxO connection.
@app.get("/debug", response_class=HTMLResponse)
async def debug(request: Request):
    import html as _html
    import base64 as _b64
    import json as _json
    import requests as _requests
    from urllib.parse import urlparse, urlencode

    sso_token = request.session.get("sso_token")
    if not sso_token:
        return RedirectResponse(url="/")

    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

    def _claims(tok: str) -> dict:
        try:
            c = _json.loads(_b64.urlsafe_b64decode(tok.split(".")[1] + "=="))
            return {k: c.get(k) for k in ("aud", "scp", "upn", "exp") if k in c}
        except Exception:
            return {}

    # Shell variables used instead of the real secret values in the displayed requests.
    _PLACEHOLDERS = {"client_secret": "$CLIENT_SECRET", "assertion": "$ASSERTION"}

    def _req_post(url: str, data: dict, assertion_var: str) -> dict:
        """Exact form-urlencoded POST, shown as raw HTTP and as a copyable curl
        command. Secrets are replaced by shell variables to define before running."""
        ph = dict(_PLACEHOLDERS, assertion=assertion_var)
        val = lambda k, v: ph.get(k, v)
        parsed = urlparse(url)
        http = [f"POST {parsed.path} HTTP/1.1", f"Host: {parsed.netloc}",
                "Content-Type: application/x-www-form-urlencoded", "",
                "&".join(f"{k}={val(k, v)}" for k, v in data.items())]
        curl = [f"curl -i -X POST '{url}'",
                "  -H 'Content-Type: application/x-www-form-urlencoded'"]
        for k, v in data.items():
            # double quotes so that the shell substitutes the variable
            curl.append(f'  --data-urlencode "{k}={ph[k]}"' if k in ph
                        else f"  --data-urlencode '{k}={v}'")
        return {"method": "POST", "url": url, "http": "\n".join(http),
                "curl": " \\\n".join(curl)}

    def _req_get(url: str, params: dict) -> dict:
        """Exact GET request (query string + bearer header), raw HTTP and curl."""
        full = f"{url}?{urlencode(params)}"
        parsed = urlparse(full)
        http = [f"GET {parsed.path}?{parsed.query} HTTP/1.1", f"Host: {parsed.netloc}",
                "Authorization: Bearer $GRAPH_TOKEN"]
        curl = [f"curl -i -G '{url}'", '  -H "Authorization: Bearer $GRAPH_TOKEN"']
        curl += [f"  --data-urlencode '{k}={v}'" for k, v in params.items()]
        return {"method": "GET", "url": full, "http": "\n".join(http),
                "curl": " \\\n".join(curl)}

    # Entra OBO request (grant jwt-bearer + requested_token_use=on_behalf_of),
    # identical to the one wxO sends at each step of the connection.
    def _exchange(assertion: str, scope: str, assertion_var: str) -> dict:
        payload = {
            "grant_type":          "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id":           CLIENT_ID,
            "client_secret":       CLIENT_SECRET,
            "assertion":           assertion,
            "scope":               scope,
            "requested_token_use": "on_behalf_of",
        }
        result = {"request": _req_post(token_url, payload, assertion_var)}
        try:
            t0 = time.time()
            r = _requests.post(token_url, data=payload, timeout=30)
            resp = r.json()
            result["http_status"] = r.status_code
            result["duration_ms"] = int((time.time() - t0) * 1000)
            result["ok"] = r.status_code == 200 and bool(resp.get("access_token"))
            if result["ok"]:
                result["access_token"] = resp["access_token"]
                result["token_claims"] = _claims(resp["access_token"])
                result["expires_in"] = resp.get("expires_in")
            else:
                result["error"] = resp.get("error")
                result["error_description"] = resp.get("error_description")
        except Exception as e:
            result["ok"] = False
            result["exception"] = f"{type(e).__name__}: {e}"
        return result

    # Step 1 — = "Identity Provider" config of the wxO connection:
    #   assertion = sso_token, scope = api://CLIENT_ID/access_as_user.
    #   No requested_token_type: Entra only accepts saml1/saml2 for this
    #   parameter (otherwise AADSTS70071) -> in wxO it must be left empty.
    step1 = _exchange(sso_token, f"api://{CLIENT_ID}/access_as_user", "$SSO_TOKEN")

    # Step 2 — = "Application" config of the wxO connection:
    #   assertion = step-1 token, scope = Microsoft Graph.
    if step1.get("ok"):
        step2 = _exchange(step1["access_token"], "https://graph.microsoft.com/.default", "$STEP1_TOKEN")
    else:
        step2 = {"ok": False, "skipped": "Step 1 failed, step 2 not run."}

    # Step 3 — the call the wxO tool makes with the Graph token (list mail folders)
    if step2.get("ok"):
        graph_url = "https://graph.microsoft.com/v1.0/me/mailFolders"
        params = {"includeHiddenFolders": "true", "$top": 10}
        step3 = {"request": _req_get(graph_url, params)}
        try:
            t0 = time.time()
            r = _requests.get(graph_url, headers={"Authorization": f"Bearer {step2['access_token']}"},
                              params=params, timeout=30)
            step3["http_status"] = r.status_code
            step3["duration_ms"] = int((time.time() - t0) * 1000)
            step3["ok"] = r.status_code == 200
            body = r.json()
            if step3["ok"]:
                step3["folders"] = [
                    {"displayName": f.get("displayName"), "totalItemCount": f.get("totalItemCount"),
                     "unreadItemCount": f.get("unreadItemCount")}
                    for f in body.get("value", [])
                ]
            else:
                step3["error"] = body.get("error")
        except Exception as e:
            step3["ok"] = False
            step3["exception"] = f"{type(e).__name__}: {e}"
    else:
        step3 = {"ok": False, "skipped": "Step 2 failed, Graph call not run."}

    def _section(title: str, wxo_hint: str, step: dict) -> str:
        # Result without the raw token and without the request (shown separately below)
        shown = {k: v for k, v in step.items() if k not in ("access_token", "request")}
        status = "✅ OK" if step.get("ok") else "❌ FAILED"
        req = step.get("request")
        req_html = ""
        if req:
            req_html = ("<details open class='reqbox'>"
                        f"<summary>📨 Exact request sent — {_html.escape(req['method'])} "
                        f"{_html.escape(req['url'])}</summary>"
                        f"<pre class='req'>{_html.escape(req['http'])}</pre>"
                        "<p class='note'>Copyable curl equivalent — define the shell variables "
                        "($CLIENT_SECRET, $SSO_TOKEN, $STEP1_TOKEN, $GRAPH_TOKEN) first:</p>"
                        f"<pre class='req'>{_html.escape(req['curl'])}</pre></details>")
        return (f"<h2>{_html.escape(title)} — {status}</h2>"
                f"<p class='hint'>{wxo_hint}</p>{req_html}"
                f"<h3>Result</h3>"
                f"<pre>{_html.escape(_json.dumps(shown, indent=2, ensure_ascii=False))}</pre>")

    verdict = ("✅ Both exchanges and the Graph call succeed: these values can be used in the wxO OBO connection."
               if step1.get("ok") and step2.get("ok") and step3.get("ok") else
               "❌ At least one step failed: check the Entra app registration (client secret, "
               "exposed API scope access_as_user, Graph delegated permissions, admin consent).")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Debug — OBO token exchanges</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #f8f9fa; max-width: 1000px; }}
    pre {{ background: #1e293b; color: #e2e8f0; padding: 1rem; border-radius: 8px; overflow-x: auto; font-size: 0.82rem; }}
    .note {{ color: #475569; font-size: 0.85rem; }}
    .reqbox {{ margin: 0.6rem 0; border: 1px solid #cbd5e1; border-radius: 6px; padding: 0.4rem 0.8rem; background: #fff; }}
    .reqbox summary {{ cursor: pointer; font-weight: 600; color: #0f172a; }}
    .req {{ background: #0b1020; color: #a5f3fc; white-space: pre-wrap; word-break: break-all; }}
    .hint {{ background: #eff6ff; border-left: 4px solid #3b82f6; padding: 0.6rem 1rem; border-radius: 4px; }}
  </style>
</head>
<body>
  <p><a href="/">← Back to the portal</a></p>
  <h1>OBO token exchanges (as performed by watsonx Orchestrate)</h1>
  <p>This page replays, with your current session, the two On-Behalf-Of requests that the
  wxO connection <code>oauth_auth_on_behalf_of_flow</code> sends to Microsoft Entra ID.
  It then calls Microsoft Graph with the resulting token, like the wxO Outlook tool.
  Secrets and tokens are masked.</p>
  <p><strong>{verdict}</strong></p>
  {_section("Step 1 — SSO token → application token (api://CLIENT_ID/access_as_user)",
            "wxO connection: <b>Identity Provider</b> settings (token URL, client id, client secret, "
            "grant type jwt-bearer, scope, body requested_token_use=on_behalf_of; "
            "requested_token_type must be empty).", step1)}
  {_section("Step 2 — application token → Microsoft Graph token",
            "wxO connection: <b>Application</b> settings (same token URL and client id, "
            "grant type jwt-bearer, body scope=https://graph.microsoft.com/.default, "
            "client_secret and requested_token_use=on_behalf_of).", step2)}
  {_section("Step 3 — Microsoft Graph call with the Graph token (GET /me/mailFolders)",
            "What the wxO Outlook tool does once the connection has returned the Graph token "
            "(server URL https://graph.microsoft.com).", step3)}
</body>
</html>"""
    return HTMLResponse(html)


# ── Route GET /logout — Sign out ──────────────────────────────
@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    post_logout = REDIRECT_URI.replace("/auth/callback", "/")
    logout_url = (
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri={post_logout}"
    )
    return RedirectResponse(url=logout_url)


# ── Route GET /chat-host — wxO integration with a custom layout ──
@app.get("/chat-host", response_class=HTMLResponse)
async def chat_host(request: Request):
    sso_token = request.session.get("sso_token")

    if not sso_token:
        auth_url = msal_app.get_authorization_request_url(
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
        )
        return RedirectResponse(url=auth_url)

    try:
        user_email = request.session.get("user_email", "")
        wxo_jwt = build_wxo_jwt(
            sso_token=sso_token,
            user_email=user_email,
        )
        _safe_print(f"[chat-host] JWT wxO genere pour {user_email} ({len(wxo_jwt)} chars)")
        return HTMLResponse(content=render_chat_host_page(wxo_jwt))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return HTMLResponse(
            content=f"Erreur lors de la generation du JWT wxO : {exc}",
            status_code=500,
        )


# ── Route GET /api/token — JWT renewal for the embedded widget ─────────
@app.get("/api/token", response_class=PlainTextResponse)
async def api_token(request: Request):
    """Endpoint called by the wxO widget on the 'authTokenNeeded' event
    to get a new JWT before the session expires (7100s)."""
    sso_token = request.session.get("sso_token")
    if not sso_token:
        return PlainTextResponse("unauthorized", status_code=401)
    user_email = request.session.get("user_email", "")
    try:
        new_jwt = build_wxo_jwt(sso_token=sso_token, user_email=user_email)
        _safe_print(f"[api/token] wxO JWT renewed for {user_email} ({len(new_jwt)} chars)")
        return PlainTextResponse(new_jwt)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return PlainTextResponse(f"Erreur : {exc}", status_code=500)


# ── Function: build the secured JWT for wxO ────────────────
def build_wxo_jwt(sso_token: str, user_email: str) -> str:
    """Build the JWT passed to the wxO widget (window.wxOConfiguration.token).

    Claims:
      sub          : user identifier (here the email)
      exp          : mandatory, 7100 s maximum
      user_payload : base64 STRING = RSA-OAEP(IBM public key, '{"sso_token": "..."}')
                     wxO decrypts it and extracts sso_token for the OBO connection
      context      : NON-encrypted data, readable by the agent (never put
                     the sso_token here)
    """
    import base64 as _b64
    import json as _json
    import math
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    # Step 1 — Encrypt the sso_token with the IBM public key
    #
    # Why split into blocks?
    #   RSA-OAEP can only encrypt a message shorter than the key:
    #     max size = k - 2*hLen - 2 = 256 - 2*20 - 2 = 214 bytes
    #     (k = 256 bytes for an RSA-2048 key, hLen = 20 bytes for SHA-1).
    #   An Entra sso_token is ~1,900 characters: single-block encryption
    #   (cryptography.encrypt) raises an error.
    #
    # What the IBM Node.js sample does (ibmRsaKey.encrypt() from the Node-RSA lib),
    # and what wxO knows how to decrypt:
    #   - n = ceil(len(data) / 214) blocks, all of the same size ceil(len(data) / n)
    #     (the last one may be shorter);
    #   - each block is encrypted separately with RSA-OAEP SHA-1 -> 256 bytes;
    #   - encrypted blocks are concatenated, then base64-encoded.
    #   wxO then decrypts 256-byte slices and joins the plaintext back together.
    #   Example: ~1,900 bytes of JSON -> 9 blocks -> 9 x 256 = 2,304 bytes -> ~3,072 base64 chars.
    #
    # OAEP parameters required by Node-RSA (default "pkcs1_oaep"): MGF1-SHA1 + SHA-1, no label.
    data      = _json.dumps({"sso_token": sso_token}, separators=(",", ":")).encode("utf-8")
    ibm_pub   = serialization.load_pem_public_key(IBM_PUBLIC_KEY_PEM.encode())
    oaep_pad  = asym_padding.OAEP(
        mgf=asym_padding.MGF1(algorithm=hashes.SHA1()),
        algorithm=hashes.SHA1(),
        label=None,
    )
    BLOCK_SIZE       = 214                      # 256 - 2*20 - 2 (RSA-2048 / SHA-1)
    n_blocks         = max(1, math.ceil(len(data) / BLOCK_SIZE))          # number of blocks
    chunk_size       = math.ceil(len(data) / n_blocks)                    # equal size per block
    chunks           = [data[i * chunk_size:(i + 1) * chunk_size] for i in range(n_blocks)]
    ciphertext       = b"".join(ibm_pub.encrypt(chunk, oaep_pad) for chunk in chunks)  # n x 256 bytes
    user_payload_b64 = _b64.b64encode(ciphertext).decode("ascii")         # standard base64, no dots

    # Step 2 — Sign the JWT with our RSA private key (RS256).
    #          user_payload is a string (not an object, not a dotted JWE);
    #          context only holds the email: the sso_token must travel
    #          only encrypted inside user_payload.
    #          exp is mandatory — wxO rejects any JWT without expiration.
    #          Maximum allowed by wxO: 7100 seconds (~2 hours).
    wxo_jwt = jwt.encode(
        payload={
            "sub":          user_email,
            "exp":          int(time.time()) + 7100,
            "user_payload": user_payload_b64,   # base64 STRING, not an object
            "context":      {"email": user_email},
        },
        key=WXO_PRIVATE_KEY,
        algorithm="RS256",
    )
    return wxo_jwt   # PyJWT >= 2.x always returns a str


# ── Function: render the portal HTML page with the wxO chat ──
def render_portal_page(wxo_jwt: str, user_email: str = "") -> str:
    # Derive first + last name from the email (e.g. "john.doe@contoso.com" -> "John Doe")
    local = user_email.split("@")[0] if user_email else ""
    parts = [p.capitalize() for p in local.split(".") if p]
    display_name = " ".join(parts)
    greeting = f"Hello {display_name} 👋" if display_name else "Hello 👋"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Outlook Portal — wxO SSO OBO Demo</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #f4f4f4; }}
    header {{
      background: #0f172a; color: #fff;
      padding: 1rem 2rem;
      display: flex; justify-content: space-between; align-items: center;
    }}
    header a {{ color: #94a3b8; font-size: 0.85rem; text-decoration: none; }}
    header a:hover {{ color: #fff; }}
    main {{ padding: 2rem; max-width: 900px; margin: auto; }}
    h2 {{ color: #1e293b; }}
    .btn {{
      display: inline-block;
      margin: 0.2rem 0 1.5rem;
      padding: 0.6rem 1rem;
      background: #0f172a; color: #fff;
      border-radius: 6px;
      font-size: 0.85rem;
      text-decoration: none;
    }}
    .btn:hover {{ background: #1e293b; }}
    #root {{ margin-top: 1.5rem; }}
    table.routes {{ border-collapse: collapse; width: 100%; background: #fff; margin-top: 1rem; }}
    table.routes th, table.routes td {{ border: 1px solid #cbd5e1; padding: 0.6rem 0.8rem; text-align: left; vertical-align: top; }}
    table.routes th {{ background: #0f172a; color: #fff; }}
    table.routes td:first-child {{ white-space: nowrap; font-family: monospace; }}
  </style>
</head>
<body>
  <header>
    <span>🤖 Outlook Portal — wxO SSO On-Behalf-Of Demo</span>
    <a href="/logout">Sign out</a>
  </header>
  <main>
    <h2>{greeting}</h2>
    <p>The watsonx Orchestrate embedded chat is available at the <strong>bottom right</strong> of this page.
    Open it and type a command such as <em>"list the folders in my email box"</em> or
    <em>"list my five last emails"</em>. The assistant reads your Outlook mailbox on your behalf
    (Microsoft Entra ID On-Behalf-Of flow).</p>
    <table class="routes">
      <tr><th>Route</th><th>Description</th></tr>
      <tr><td><a href="/">/</a></td><td>This portal: signs you in with Microsoft Entra ID, builds the signed wxO JWT
        (SSO token encrypted in <code>user_payload</code>) and loads the embedded chat in the bottom-right corner.</td></tr>
      <tr><td><a href="/chat-host">/chat-host</a></td><td>Same chat displayed full screen in a custom layout
        (<code>layout.form = custom</code>, no launcher button).</td></tr>
      <tr><td><a href="/debug">/debug</a></td><td>Replays the two On-Behalf-Of token exchanges performed by the wxO connection
        (SSO token → application token, then application token → Microsoft Graph token), calls Graph to list your mail folders, and shows the values to
        configure in the wxO OBO connection.</td></tr>
    </table>
    <div id="root"></div>
  </main>

  <script>
    function onChatLoad(instance) {{
      window.wxoChatInstance = instance;
      // Automatic JWT renewal when it expires (max 7100s)
      instance.on('authTokenNeeded', async function (event) {{
        const resp = await fetch('/api/token');
        if (!resp.ok) {{ window.location.href = '/'; return; }}
        const newToken = await resp.text();
        event.authToken = newToken;
        window.wxOConfiguration.token = newToken;
      }});
    }}

    window.wxOConfiguration = {{
      orchestrationID: "{WXO_ORCHESTRATION_ID}",
      hostURL: "{WXO_HOST_URL}",
      rootElementID: "root",
      deploymentPlatform: "ibmcloud",
      crn: "{WXO_CRN}",
      chatOptions: {{
        agentId:            "{WXO_AGENT_ID}",
        agentEnvironmentId: "{WXO_AGENT_ENV_ID}",
        onLoad:             onChatLoad
      }},
      token: "{wxo_jwt}"
    }};
    setTimeout(function () {{
      const s = document.createElement('script');
      s.src = window.wxOConfiguration.hostURL + '/wxochat/wxoLoader.js?embed=true';
      s.addEventListener('load', function () {{ wxoLoader.init(); }});
      document.head.appendChild(s);
    }}, 0);
  </script>
</body>
</html>"""


# ── Function: render the chat-host page (full-screen custom layout) ─────────
def render_chat_host_page(wxo_jwt: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Assistant</title>
  <style>
    html, body {{ margin: 0; height: 100%; overflow: hidden; }}
    body {{ display: flex; flex-direction: column; }}
    header {{ padding: 12px 20px; background: #0f62fe; color: #fff; font-family: sans-serif; flex-shrink: 0; }}
    header a {{ color: #c8d7ff; font-size: 0.85rem; text-decoration: none; float: right; }}
    header a:hover {{ color: #fff; }}
    #chat-host {{ flex: 1; min-height: 0; }}
  </style>
</head>
<body>
  <header>My portal - Assistant <a href="/logout">Sign out</a></header>
  <div id="chat-host"></div>
  <script>
    const hostElement = document.getElementById("chat-host");

    function onChatLoad(instance) {{
      window.wxoChatInstance = instance;
      // Automatic JWT renewal when it expires (max 7100s)
      instance.on('authTokenNeeded', async function (event) {{
        const resp = await fetch('/api/token');
        if (!resp.ok) {{ window.location.href = '/'; return; }}
        const newToken = await resp.text();
        event.authToken = newToken;
        window.wxOConfiguration.token = newToken;
      }});
    }}

    window.wxOConfiguration = {{
      orchestrationID: "{WXO_ORCHESTRATION_ID}",
      hostURL: "{WXO_HOST_URL}",
      deploymentPlatform: "ibmcloud",
      crn: "{WXO_CRN}",
      showLauncher: false,
      chatOptions: {{
        agentId:            "{WXO_AGENT_ID}",
        agentEnvironmentId: "{WXO_AGENT_ENV_ID}",
        onLoad:             onChatLoad
      }},
      token: "{wxo_jwt}",
      layout: {{
        form: "custom",
        customElement: hostElement,
        showOrchestrateHeader: true
      }}
    }};
    setTimeout(function () {{
      const s = document.createElement("script");
      s.src = window.wxOConfiguration.hostURL + "/wxochat/wxoLoader.js?embed=true";
      s.addEventListener("load", function () {{ wxoLoader.init(); }});
      document.head.appendChild(s);
    }}, 0);
  </script>
</body>
</html>"""
