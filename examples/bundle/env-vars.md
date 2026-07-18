---
type: Reference
title: Auth environment variables
description: 'Env keys the auth service reads: SIGNING_KEY_CURRENT, SIGNING_KEY_PREVIOUS, TOKEN_TTL, SESSION_STORE_URL'
tags: [auth, config, environment variables, secrets, ttl]
aliases: [env vars, dotenv, configuration keys]
timestamp: 2026-07-19T00:00:00Z
source: [docs/auth.md#config]
confidence: high
---
- `SIGNING_KEY_CURRENT` — active JWT signing key. Required.
- `SIGNING_KEY_PREVIOUS` — accepted during a [key rotation](./rotate-signing-key.md) overlap. Optional.
- `TOKEN_TTL` — token lifetime; see the [TTL conflict](./caveat-token-ttl-conflict.md).
- `SESSION_STORE_URL` — Redis URL for the session store.
