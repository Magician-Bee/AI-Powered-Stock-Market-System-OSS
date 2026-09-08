#!/bin/bash

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec /bin/bash "${PROJECT_ROOT}/stop-stock-ai.sh"
