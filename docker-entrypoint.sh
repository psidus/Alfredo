#!/bin/bash
# Start Xvfb in the background to provide a virtual display for screenshot tools
Xvfb :99 -screen 0 1920x1080x24 > /dev/null 2>&1 &
export DISPLAY=:99

# Populate initial database with the Startapp Workflow example
echo "Seeding example workflow..."
python seed_startapp_example.py || true

# Execute the passed command
exec "$@"
