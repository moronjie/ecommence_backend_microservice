# 0006. Login tokens use RS256 so only the auth service can sign

## Context
A login token (JWT) must be signed so it cannot be forged. With HS256, one shared secret both signs and verifies, so
every service that can *check* a token can also *create* one. The gateway, which faces the internet, would hold
everything needed to mint an admin token; breaking into it would mean owning the whole shop.

## Decision
Production uses **RS256**, a key pair. The **auth service** holds the private key and is the only thing that can sign.
The **gateway** holds only the public key, which can verify tokens but cannot create them. The docker setup mounts each
key into only the container that needs it (`.keys/auth` and `.keys/gateway`). `scripts/generate_keys.py` creates the pair.

Verification pins the algorithm (`algorithms=["RS256"]`), which blocks two classic attacks: tokens with `alg: none`,
and the "algorithm confusion" attack of signing with HS256 using the public key as if it were a shared secret. Both are
covered by tests, including a hand-built forged token against the running stack.

HS256 stays available for development (`JWT_ALGORITHM=HS256`), and production refuses to start with it.

## Consequences
* **Good:** compromising the gateway no longer lets an attacker forge tokens, and the public key is safe to copy anywhere.
* **Cost:** key management: the private key must be protected and backed up, and replacing it logs everyone out. There is
  no automatic rotation, and the tokens carry no key id (`kid`), so a graceful two-key rotation would need that added.
* **Cost:** RS256 is a little slower to verify than HS256 (microseconds, not noticeable here).
* **Cost:** the auth service verifies its own refresh tokens with the public key derived from the private one.
