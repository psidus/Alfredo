#!/bin/bash
# Post-startup script to connect qdrant_server to the Alfredo network.
# Run this after `docker-compose up -d` to enable container-to-container communication.
# 
# Usage: ./connect_qdrant.sh
#   or:  bash connect_qdrant.sh

NETWORK_NAME="alfredo_default"
QDRANT_CONTAINER="qdrant_server"

echo "Connecting $QDRANT_CONTAINER to $NETWORK_NAME..."
docker network connect "$NETWORK_NAME" "$QDRANT_CONTAINER" 2>/dev/null

if [ $? -eq 0 ]; then
    echo "✅ $QDRANT_CONTAINER connected to $NETWORK_NAME"
else
    echo "ℹ️  $QDRANT_CONTAINER already connected to $NETWORK_NAME (or not running)"
fi

# Verify connectivity
echo "Verifying connectivity..."
docker exec alfredo_dashboard python -c "import requests; r = requests.get('http://qdrant_server:6333', timeout=2); print(f'Qdrant server: {r.status_code} - {r.json()[\"version\"]}')" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "✅ Dashboard can reach Qdrant server"
else
    echo "❌ Dashboard cannot reach Qdrant server"
fi
