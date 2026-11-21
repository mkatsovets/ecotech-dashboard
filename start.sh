#!/bin/sh
# Container entry point. A script (instead of `sh -c "..."` in CMD) because V/AM turns the
# CMD array into a plain command line and can lose the quoting.
cd /app || exit 1
exec python -m uvicorn app.api:app --host 0.0.0.0 --port "${PORT:-80}" \
  --proxy-headers --forwarded-allow-ips "*"
