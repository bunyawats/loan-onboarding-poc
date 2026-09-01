#!/usr/bin/env bash
# Runs automatically on first container start (official postgres image
# behavior for anything in /docker-entrypoint-initdb.d, alphabetical
# order). Creates the second `temporal` database alongside the
# POSTGRES_DB-created `loan_onboarding` one, then applies schema.sql to
# loan_onboarding only -- same two-database-one-container pattern as
# review-approval-temporal (see CLAUDE.md "Data storage").
#
# Compose wiring this script assumes (set up in Phase 0's
# docker-compose.yml):
#   volumes:
#     - ./db/init:/docker-entrypoint-initdb.d   # this script runs from here
#     - ./db:/db-source:ro                       # schema.sql read from here
#
# schema.sql is deliberately NOT placed directly under db/init/ -- the
# postgres image auto-runs every *.sql file it finds there too, which
# would double-apply it (once automatically, once from this script) and
# fail on "relation already exists". Keeping it one level up in
# db/schema.sql and reading it via the separate /db-source mount avoids
# that trap entirely.

set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE DATABASE temporal;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -f /db-source/schema.sql
