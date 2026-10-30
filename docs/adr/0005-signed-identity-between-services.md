# 0005. The gateway signs the caller's identity; services verify it

## Context
The gateway checks a customer's token and then tells each service who is calling, through the headers `X-User-Id` and
`X-User-Role`. The services believed those headers. That is only safe if nothing but the gateway can ever reach a
service. One mistake (a published port, another compromised container on the network) and anyone could send
`X-User-Role: admin` and be believed.

## Decision
The gateway **signs** the identity with a secret shared only by itself and the services (`INTERNAL_AUTH_SECRET`): an HMAC
over the user id, role, a timestamp, the HTTP method and the path. Each service installs a small middleware
(`ecom_common.identity.VerifyIdentityMiddleware`) that **erases** the identity headers from any request whose
signature is missing or wrong, so the service sees an anonymous caller and its own checks answer 401. The cart signs the
identity too when it calls other services on a customer's behalf.

The signature covers the method and path, and is only valid for a minute, so a captured request cannot be replayed later
or against another endpoint. The gateway also deletes any identity or signature headers a client sends.

## Consequences
* **Good:** reaching a service directly is no longer enough to impersonate anyone; the protection lives in one shared
  middleware instead of in nine copies of access checks.
* **Cost:** every service holds the shared secret, so breaking into *any* service reveals it and allows forging
  identities. Per-service keys or mutual TLS would be stronger and are the next step.
* **Cost:** a replay of the *same* request within the one-minute window is possible for someone who can capture
  internal traffic. Keep internal traffic on a private network (ideally with TLS).
* **Cost:** the secret must be set and kept private. Production refuses to start without a strong one. When it is empty
  (development only) services accept plain headers.
