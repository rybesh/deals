#!/bin/sh
echo "Starting cron..."
supercronic ./crontab &
echo "Starting web server..."
goStatic -fallback /index.xml
