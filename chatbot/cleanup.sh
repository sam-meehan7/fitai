#!/bin/bash

# Find and kill any existing Python processes running your bot
pkill -f "python3 bot.py"

# Wait a moment to ensure processes have time to shut down
sleep 5

# Double-check and forcefully kill if any are still running
pkill -9 -f "python3 bot.py"

echo "Cleanup completed. Old processes terminated."