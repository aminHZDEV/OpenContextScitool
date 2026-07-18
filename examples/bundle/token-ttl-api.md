---
type: Reference
title: Token TTL per the API reference
description: 'The API reference states TOKEN_TTL defaults to 15 minutes (900 seconds)'
tags: [auth, ttl, token, api]
aliases: [15 minute token, TOKEN_TTL 900]
timestamp: 2026-07-19T00:00:00Z
source: [docs/api.md#tokens]
conflicts_with: [./token-ttl-ops.md]
confidence: high
---
Per `docs/api.md`, `TOKEN_TTL` defaults to **900 seconds (15 minutes)**.
Contradicted by the [operations guide](./token-ttl-ops.md); see the
[conflict caveat](./caveat-token-ttl-conflict.md).
