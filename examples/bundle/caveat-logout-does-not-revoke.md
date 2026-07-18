---
type: Caveat
title: Logout does not revoke the token
description: 'Logout only clears the client cookie; the bearer token stays valid until it expires, so a copied token keeps working after logout'
tags: [auth, token, logout, revocation, security, gotcha]
aliases: [logout security, token still valid after logout, revoke token]
timestamp: 2026-07-19T00:00:00Z
source: [docs/auth.md#logout]
confidence: high
---
`POST /logout` deletes the client-side cookie only. The [session token](./session-token.md)
itself is **not** added to any revocation list — it remains valid until `TOKEN_TTL`
elapses. A token captured before logout keeps working. To truly revoke, rotate the
[signing key](./rotate-signing-key.md) or add a server-side denylist (not implemented).
