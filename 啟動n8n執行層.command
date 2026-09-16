#!/bin/bash

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec /bin/bash "$PROJECT_ROOT/scripts/start-n8n-headless.sh"
