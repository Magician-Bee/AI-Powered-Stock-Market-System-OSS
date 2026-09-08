#!/bin/bash

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
/bin/bash "${PROJECT_ROOT}/verify-stock-ai-instance.sh"
STATUS=$?
printf '\nPress Return to close...'
read -r _unused
exit "$STATUS"
