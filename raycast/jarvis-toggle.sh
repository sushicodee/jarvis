#!/bin/bash
# @raycast.schemaVersion 1
# @raycast.title Jarvis toggle
# @raycast.mode silent
# @raycast.packageName Jarvis
curl -s -X POST http://127.0.0.1:8765/toggle >/dev/null || echo "Jarvis not running"
