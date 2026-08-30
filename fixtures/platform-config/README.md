# platform-config

Platform configuration and schema definitions for the Interlock demo system.

## Overview

This repository holds the canonical database schema for all Interlock services.
It is the **authoritative source of truth** for field names and table structures.

The schema currently uses `customer_id` as the canonical customer identifier
across all tables. This is the field targeted by the pending migration:
`customer_id → account_id`.

## Schema

### `accounts`
Core customer account table. `customer_id` is the primary key.

### `orders`
Order records. References `customer_id` from the `accounts` table.

### `analytics_events`
Event stream. Each event carries `customer_id` directly in the record.

### `fraud_checks`
Fraud assessment results. Keyed by `customer_id`.

## Migration Impact

When the `customer_id → account_id` migration is applied, the following
schema changes are required:
1. Add `account_id` column to `accounts` table
2. Backfill `account_id` from `customer_id`
3. Update all FK references
4. Retain `customer_id` during compatibility window
5. Drop `customer_id` only after all consumers have migrated

## Files

- `schema.sql` — DDL for all tables; references `customer_id` throughout
