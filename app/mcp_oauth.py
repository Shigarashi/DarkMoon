"""OAuth sign-in for remote MCP connectors (Linear, Notion, Sentry, Supabase…).

Implements the MCP authorization flow:
  1. The server answers 401 → find its Protected Resource Metadata
     (WWW-Authenticate resource_metadata=…, or /.well-known/oauth-protected-resource)
  2. Read the authorization server's metadata (/.well-known/oauth-authorization-server
     or /.well-known/openid-configuration)
  3. Dynamic Client Registration (RFC 7591) as a public client
  4. Authorization Code + PKCE (S256) in your browser, redirect to Darkmoon's
     loopback callback http://127.0.0.1:<port>/oauth/callback
  5. Token exchange with the RFC 8707 `resource` parameter; refresh tokens kept
Tokens are stored in oauth_tokens.json in the data folder.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from . import paths

log = logging.getLogger("darkmoon.oauth")
STORE = paths.DATA_DIR / "oauth_tokens.json"


class OAuthError(Exception):
    pass


def _load() -> dict[str, Any]:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, Any]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(STORE, 0o600)
    except OSError:
        pass


def _canonical(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc.lower()}{u.path.rstrip('/') or ''}"


def parse_www_authenticate(header: str) -> dict[str, str]:
    return {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', header or "")}


class OAuthManager:
    def __init__(self):
        self.pending: dict[str, dict[str, Any]] = {}   # state -> flow info
        self.redirect_base = ""                        # set by the server: http://127.0.0.1:<port>
        self.on_complete = None                        # async callback(server_name, ok, message)

    # Token store -------------------------------------------------------------
    def tokens(self, name: str) -> dict[str, Any] | None:
        return _load().get(name)

    def sign_out(self, name: str) -> None:
        data = _load()
        data.pop(name, None)
        _save(data)

    def rename(self, old: str, new: str) -> None:
        data = _load()
        if old in data:
            data[new] = data.pop(old)
            _save(data)

    async def access_token(self, name: str) -> str | None:
        t = self.tokens(name)
        if not t:
            return None
        if t.get("expires_at") and time.time() > t["expires_at"] - 60 and t.get("refresh_token"):
            try:
                await self.refresh(name)
                t = self.tokens(name) or {}
            except OAuthError as e:
                log.warning("refresh for %s failed: %s", name, e)
                return None
        return t.get("access_token")

    async def refresh(self, name: str) -> None:
        t = self.tokens(name)
        if not t or not t.get("refresh_token"):
            raise OAuthError("no refresh token")
        form = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": t["client_id"],
                "resource": t.get("resource", "")}
        if t.get("client_secret"):
            form["client_secret"] = t["client_secret"]
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(t["token_endpoint"], data={k: v for k, v in form.items() if v},
                             headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise OAuthError(f"refresh failed: HTTP {r.status_code} {r.text[:200]}")
        self._store_tokens(name, t, r.json())

    def _store_tokens(self, name: str, base: dict[str, Any], tok: dict[str, Any]) -> None:
        data = _load()
        rec = dict(base)
        rec["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            rec["refresh_token"] = tok["refresh_token"]
        rec["expires_at"] = time.time() + int(tok["expires_in"]) if tok.get("expires_in") else None
        rec["scope"] = tok.get("scope", rec.get("scope", ""))
        data[name] = rec
        _save(data)

    # Discovery ---------------------------------------------------------------
    async def _discover(self, url: str, www_auth: str) -> dict[str, Any]:
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        hdr = parse_www_authenticate(www_auth)
        prm_urls = ([hdr["resource_metadata"]] if hdr.get("resource_metadata") else []) + [
            f"{origin}/.well-known/oauth-protected-resource{u.path.rstrip('/')}",
            f"{origin}/.well-known/oauth-protected-resource"]
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            prm: dict[str, Any] = {}
            for pu in prm_urls:
                try:
                    r = await c.get(pu, headers={"Accept": "application/json"})
                    if r.status_code == 200:
                        prm = r.json()
                        break
                except (httpx.HTTPError, ValueError):
                    continue
            issuer = (prm.get("authorization_servers") or [origin])[0].rstrip("/")
            iu = urlparse(issuer)
            ipath = iu.path.rstrip("/")
            io = f"{iu.scheme}://{iu.netloc}"
            asm: dict[str, Any] = {}
            for mu in (f"{io}/.well-known/oauth-authorization-server{ipath}", f"{io}/.well-known/openid-configuration{ipath}",
                       f"{issuer}/.well-known/openid-configuration", f"{io}/.well-known/oauth-authorization-server"):
                try:
                    r = await c.get(mu, headers={"Accept": "application/json"})
                    if r.status_code == 200 and "authorization_endpoint" in r.text:
                        asm = r.json()
                        break
                except (httpx.HTTPError, ValueError):
                    continue
        if not asm:
            # Legacy servers: default endpoints at the origin.
            asm = {"authorization_endpoint": f"{io}/authorize", "token_endpoint": f"{io}/token",
                   "registration_endpoint": f"{io}/register"}
        return {"prm": prm, "as": asm, "scope": hdr.get("scope") or " ".join(prm.get("scopes_supported") or []),
                "resource": prm.get("resource") or _canonical(url)}

    async def _register(self, asm: dict[str, Any], redirect_uri: str) -> dict[str, Any]:
        reg = asm.get("registration_endpoint")
        if not reg:
            raise OAuthError("This server doesn't allow automatic app registration. Use an API key/token header "
                             "instead, or connect through `npx mcp-remote <url>` as a local connector.")
        body = {"client_name": "Darkmoon", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"], "token_endpoint_auth_method": "none", "application_type": "native"}
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(reg, json=body, headers={"Accept": "application/json"})
        if r.status_code not in (200, 201):
            raise OAuthError(f"App registration was refused (HTTP {r.status_code}): {r.text[:300]}")
        return r.json()

    # Flow --------------------------------------------------------------------
    async def begin(self, name: str, url: str) -> str:
        """Return the URL to open in the browser to sign in."""
        if not self.redirect_base:
            raise OAuthError("sign-in callback isn't ready yet")
        redirect_uri = f"{self.redirect_base}/oauth/callback"
        www = ""
        try:  # probe for the WWW-Authenticate hint
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Darkmoon", "version": "1"}}},
                    headers={"Accept": "application/json, text/event-stream"})
                www = r.headers.get("www-authenticate", "")
        except httpx.HTTPError:
            pass
        disc = await self._discover(url, www)
        asm = disc["as"]
        old = self.tokens(name) or {}
        if old.get("client_id") and old.get("redirect_uri") == redirect_uri and old.get("token_endpoint") == asm.get("token_endpoint"):
            client = {"client_id": old["client_id"], "client_secret": old.get("client_secret")}
        else:
            client = await self._register(asm, redirect_uri)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                  "resource": disc["resource"]}
        if disc["scope"]:
            params["scope"] = disc["scope"]
        self.pending[state] = {"name": name, "verifier": verifier, "redirect_uri": redirect_uri, "client": client,
                               "token_endpoint": asm["token_endpoint"], "resource": disc["resource"], "created": time.time()}
        for s in [s for s, v in self.pending.items() if time.time() - v["created"] > 900]:
            self.pending.pop(s, None)
        sep = "&" if "?" in asm["authorization_endpoint"] else "?"
        return f"{asm['authorization_endpoint']}{sep}{urlencode(params)}"

    async def finish(self, state: str, code: str | None, error: str | None) -> tuple[str, bool, str]:
        flow = self.pending.pop(state, None)
        if not flow:
            return "", False, "This sign-in link expired or was already used. Start again from Darkmoon."
        if error or not code:
            return flow["name"], False, f"Sign-in was cancelled or refused ({error or 'no code'})."
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": flow["redirect_uri"],
                "client_id": flow["client"]["client_id"], "code_verifier": flow["verifier"], "resource": flow["resource"]}
        if flow["client"].get("client_secret"):
            form["client_secret"] = flow["client"]["client_secret"]
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(flow["token_endpoint"], data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            return flow["name"], False, f"Token exchange failed: {e}"
        if r.status_code != 200:
            return flow["name"], False, f"Token exchange failed (HTTP {r.status_code}): {r.text[:200]}"
        self._store_tokens(flow["name"], {"client_id": flow["client"]["client_id"],
                                          "client_secret": flow["client"].get("client_secret"),
                                          "token_endpoint": flow["token_endpoint"], "redirect_uri": flow["redirect_uri"],
                                          "resource": flow["resource"]}, r.json())
        return flow["name"], True, "Signed in."


oauth = OAuthManager()
_ = asyncio
