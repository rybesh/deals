#!/bin/sh
echo "Starting cron..."
supercronic -split-logs ./crontab 1> cron.log &
echo "Starting web server..."
goStatic -fallback /index.xml
