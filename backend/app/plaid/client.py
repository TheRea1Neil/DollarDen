"""Plaid API client.

Deliberately a thin httpx wrapper rather than the generated plaid-python SDK:
the surface we need is six endpoints, the exact wire shapes were validated
against the live sandbox during planning, and webhook JWT verification has to
be written by hand regardless. Fewer dependencies on an arm64 Pi, and the
request/response shapes stay legible. See docs/adr/0006.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings

log = logging.getLogger("nnf.plaid")


class PlaidError(RuntimeError):
    """A structured error from Plaid.

    `error_code` is the part worth branching on -- ITEM_LOGIN_REQUIRED means
    re-auth via update mode, which does NOT consume a connection credit.
    """

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.error_code: str = payload.get("error_code", "UNKNOWN")
        self.error_type: str = payload.get("error_type", "UNKNOWN")
        self.message: str = payload.get("error_message", "")
        self.request_id: str | None = payload.get("request_id")
        super().__init__(f"{self.error_code}: {self.message}")

    @property
    def needs_reauth(self) -> bool:
        return self.error_code in {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION"}


class PlaidClient:
    def __init__(self, settings: Settings, timeout: float = 60.0) -> None:
        self.s = settings
        self.base = settings.plaid_host
        self.timeout = timeout

    def _auth(self) -> dict[str, str]:
        return {"client_id": self.s.plaid_client_id, "secret": self.s.plaid_secret}

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = {**self._auth(), **body}
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base}{path}", json=payload)
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "error_code" in data:
            err = PlaidError(r.status_code, data)
            # Never log the payload: it contains client_id/secret/access_token.
            log.warning("plaid %s failed: %s (request_id=%s)", path, err.error_code, err.request_id)
            raise err
        return data

    # ---------------- Link ----------------
    async def create_link_token(
        self,
        user_id: str,
        *,
        access_token: str | None = None,
        webhook: str | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        """Mint a link_token.

        Passing `access_token` switches Link into UPDATE MODE: it
        re-authenticates an existing Item in place rather than creating a new
        one, so no connection credit is consumed. This is the path for
        ITEM_LOGIN_REQUIRED. See docs/04.

        `platform` decides how OAuth gets the user back from the bank, and
        the two mechanisms are MUTUALLY EXCLUSIVE. Plaid's docs, of the
        Android SDK: "you must generate a Link token by calling
        /link/token/create and passing in an `android_package_name` ... Do
        not pass a `redirect_uri` into the /link/token/create call." iOS is
        the opposite -- a registered universal link, no package name.

        Sending the wrong one does not raise. The user picks their accounts,
        gets dropped back at the bank's login, and no Item is ever created
        (observed against ins_127989, docs/04). So this is worth getting
        right rather than sending both and hoping.
        """
        body: dict[str, Any] = {
            "client_name": "no-nonsense-finance",
            "language": "en",
            "country_codes": [c.strip() for c in self.s.plaid_country_codes.split(",")],
            "user": {"client_user_id": user_id},
        }
        if access_token:
            # Update mode: products must NOT be sent alongside an access_token.
            body["access_token"] = access_token
        else:
            body["products"] = [p.strip() for p in self.s.plaid_products.split(",")]

        hook = webhook or self.s.plaid_webhook_url
        if hook:
            body["webhook"] = hook

        self._apply_oauth_return_path(body, platform)
        return await self._post("/link/token/create", body)

    def _apply_oauth_return_path(self, body: dict[str, Any], platform: str | None) -> None:
        """Attach exactly one of android_package_name / redirect_uri.

        In production a missing value is fatal rather than omitted. Every
        large US bank is OAuth, so shipping without it means the link
        silently dead-ends at the bank's login page -- which reads as "the
        app is broken", costs an afternoon, and is indistinguishable from a
        Plaid outage. Failing here names the missing setting instead.

        In sandbox it is only a warning: the non-OAuth test institutions
        (First Platypus and friends) link fine without either.
        """
        production = self.s.plaid_env == "production"

        if platform == "android":
            pkg = self.s.plaid_android_package_name
            if pkg:
                body["android_package_name"] = pkg
                return
            missing = "PLAID_ANDROID_PACKAGE_NAME"
        else:
            uri = self.s.plaid_redirect_uri
            if uri:
                body["redirect_uri"] = uri
                return
            missing = "PLAID_REDIRECT_URI"

        where = platform or "an unspecified platform"
        if production:
            raise RuntimeError(
                f"{missing} is not set, but a production link token was requested "
                f"for {where}. OAuth institutions (Bank of America among them) "
                f"would dead-end at the bank's login without it. See docs/04."
            )
        log.warning("%s unset; OAuth institutions will not work for %s", missing, where)

    async def exchange_public_token(self, public_token: str) -> dict[str, Any]:
        """public_token -> access_token. The access_token is long-lived."""
        return await self._post("/item/public_token/exchange", {"public_token": public_token})

    # ---------------- Data ----------------
    async def get_accounts(self, access_token: str) -> dict[str, Any]:
        """Accounts with Plaid's CACHED balances.

        Cheap, and fine for discovering which accounts exist -- but the
        balance can be badly stale. Measured against a real Bank of
        America item: cached said $9,267.70 while the bank actually held
        $8,707.67, a $560 gap. Use get_balances when the number is going
        to be shown as money available to spend.
        """
        return await self._post("/accounts/get", {"access_token": access_token})

    async def get_balances(self, access_token: str) -> dict[str, Any]:
        """Accounts with LIVE balances, fetched from the institution.

        Same response shape as /accounts/get, so it is a drop-in
        replacement, but it actually reaches the bank rather than
        returning whatever Plaid last cached.
        """
        return await self._post("/accounts/balance/get", {"access_token": access_token})

    async def get_item(self, access_token: str) -> dict[str, Any]:
        return await self._post("/item/get", {"access_token": access_token})

    async def get_institution(self, institution_id: str) -> dict[str, Any]:
        return await self._post(
            "/institutions/get_by_id",
            {
                "institution_id": institution_id,
                "country_codes": [c.strip() for c in self.s.plaid_country_codes.split(",")],
            },
        )

    async def transactions_sync(
        self, access_token: str, cursor: str | None, count: int = 100
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"access_token": access_token, "count": count}
        if cursor:
            body["cursor"] = cursor
        return await self._post("/transactions/sync", body)

    async def webhook_verification_key(self, key_id: str) -> dict[str, Any]:
        return await self._post("/webhook_verification_key/get", {"key_id": key_id})

    # ---------------- Sandbox only ----------------
    async def sandbox_create_public_token(
        self, institution_id: str = "ins_109508", products: list[str] | None = None
    ) -> dict[str, Any]:
        self._require_sandbox()
        return await self._post(
            "/sandbox/public_token/create",
            {
                "institution_id": institution_id,
                "initial_products": products or ["transactions"],
            },
        )

    async def sandbox_fire_webhook(
        self, access_token: str, code: str = "SYNC_UPDATES_AVAILABLE"
    ) -> dict[str, Any]:
        self._require_sandbox()
        return await self._post(
            "/sandbox/item/fire_webhook",
            {"access_token": access_token, "webhook_code": code},
        )

    async def sandbox_reset_login(self, access_token: str) -> dict[str, Any]:
        """Force ITEM_LOGIN_REQUIRED so the update-mode path can be tested."""
        self._require_sandbox()
        return await self._post("/sandbox/item/reset_login", {"access_token": access_token})

    def _require_sandbox(self) -> None:
        if self.s.plaid_env != "sandbox":
            raise RuntimeError("sandbox-only endpoint called outside the sandbox environment")
