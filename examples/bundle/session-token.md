---
type: Concept
title: Session token
description: 'Opaque bearer token issued at login, sent as `Authorization: Bearer`, identifies the user for the request'
tags: [auth, token, session, bearer, login]
aliases: [access token, auth token, bearer token]
timestamp: 2026-07-19T00:00:00Z
source: [docs/auth.md#tokens]
confidence: high
---
A session token is issued at login and presented on every request as
`Authorization: Bearer <token>`. It is opaque — the client never parses it.
See [how to rotate the signing key](./rotate-signing-key.md) and the
[logout caveat](./caveat-logout-does-not-revoke.md).
