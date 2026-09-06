#!/usr/bin/env bash
# Run the Meridian FastAPI backend for local development (beta, SPEC.md §14).
set -euo pipefail
cd "$(dirname "$0")/.."
exec uvicorn src.api.main:app --reload --port 8000
