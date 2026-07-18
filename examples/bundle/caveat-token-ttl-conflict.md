---
type: Caveat
title: Docs disagree on the token TTL
description: 'The API reference says TOKEN_TTL is 15 minutes; the operations guide says 24 hours — the two docs contradict and neither notes the other'
tags: [auth, ttl, token, conflict, documentation]
aliases: [token lifetime, how long is a token valid, TOKEN_TTL value]
timestamp: 2026-07-19T00:00:00Z
source: [docs/api.md#tokens, docs/ops.md#sessions]
confidence: high
---
Two sources give different values for `TOKEN_TTL` and neither acknowledges the
other: the [API reference](./token-ttl-api.md) says **15 minutes**, the
[operations guide](./token-ttl-ops.md) says **24 hours**. This concept exists so
a reader searching the TTL learns *both* claims and that they conflict — the
lexical index cannot otherwise surface a rival answer.
