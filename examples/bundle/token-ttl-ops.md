---
type: Reference
title: Token TTL per the operations guide
description: 'The operations guide states TOKEN_TTL is 24 hours (86400 seconds)'
tags: [auth, ttl, token, operations]
aliases: [24 hour token, day-long session, TOKEN_TTL 86400]
timestamp: 2026-07-19T00:00:00Z
source: [docs/ops.md#sessions]
conflicts_with: [./token-ttl-api.md]
confidence: high
---
Per `docs/ops.md`, `TOKEN_TTL` is **86400 seconds (24 hours)**.
Contradicted by the [API reference](./token-ttl-api.md); see the
[conflict caveat](./caveat-token-ttl-conflict.md).
