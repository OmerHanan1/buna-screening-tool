"""Single-tenant, explicitly owner-allowlisted Microsoft access-token validation."""
from dataclasses import dataclass
import hashlib
import os
from uuid import UUID

from fastapi import HTTPException
import jwt


@dataclass(frozen=True)
class TeamConfig:
    tenant: str
    client: str
    owner_oid: str

    @classmethod
    def from_environment(cls):
        values = [os.environ.get(key, "") for key in
                  ("BUNA_TEAM_TENANT", "BUNA_TEAM_CLIENT", "BUNA_TEAM_OWNER_OID")]
        if not any(values):
            return None
        if not all(values):
            raise RuntimeError("Incomplete team authentication configuration; refusing startup.")
        return cls(*(str(UUID(value)) for value in values))

    @property
    def issuer(self):
        return f"https://login.microsoftonline.com/{self.tenant}/v2.0"

    def public(self):
        return {"mode": "team", "client_id": self.client,
                "authority": f"https://login.microsoftonline.com/{self.tenant}",
                "scope": f"api://{self.client}/access_as_user"}


class TeamAuthenticator:
    def __init__(self, config: TeamConfig, key_client=None):
        self.config = config
        self.keys = key_client or jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{config.tenant}/discovery/v2.0/keys",
            timeout=5, cache_jwk_set=True, lifespan=3600,
        )

    def authenticate(self, authorization: str):
        if not authorization.startswith("Bearer ") or not 8 < len(authorization) <= 16384:
            raise HTTPException(401, "Microsoft sign-in required.")
        token = authorization[7:]
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise jwt.InvalidTokenError()
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.config.client,
                                issuer=self.config.issuer, leeway=30,
                                options={"require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "sub"]})
            if (claims.get("ver") != "2.0" or claims["tid"] != self.config.tenant
                    or claims.get("azp") != self.config.client
                    or "access_as_user" not in str(claims.get("scp", "")).split()):
                raise jwt.InvalidTokenError()
        except jwt.PyJWKClientConnectionError as exc:
            raise HTTPException(503, "Sign-in verification is temporarily unavailable; retry shortly.") from exc
        except jwt.PyJWTError as exc:
            raise HTTPException(401, "Invalid or expired Microsoft access token.") from exc
        if claims["oid"] != self.config.owner_oid:
            raise HTTPException(403, "This Microsoft account is not on the team allowlist.")
        # Job owners are stable validated identities, never an email or a client-selected ID.
        return hashlib.sha256((claims["iss"] + "|" + claims["oid"]).encode()).hexdigest()
