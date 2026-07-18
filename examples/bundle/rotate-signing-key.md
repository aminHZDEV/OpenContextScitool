---
type: Process
title: Rotating the signing key
description: 'Steps to rotate the JWT signing key with zero downtime using an overlap window, plus the rollback path'
tags: [auth, jwt, keys, rotation, secrets, runbook, key rotation]
aliases: [rotate signing key, key rollover, JWT key rotation]
timestamp: 2026-07-19T00:00:00Z
source: [docs/auth.md#key-rotation]
confidence: high
---
Rotation is safe during business hours; the overlap window means no live token
is invalidated mid-flight.

1. Set `SIGNING_KEY_PREVIOUS` to the current key.
2. Set `SIGNING_KEY_CURRENT` to the new key. Both are accepted during the overlap.
3. After all old tokens expire (see [session token](./session-token.md)), drop `SIGNING_KEY_PREVIOUS`.

Rollback: swap the two keys back; the overlap makes it non-destructive.
