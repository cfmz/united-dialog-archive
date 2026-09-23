#!/bin/bash
while true; do
  sleep 600
  /root/united-dialog-archive/publish.sh >> /root/united-dialog-archive/cron.log 2>&1
  echo "--- $(date) ---" >> /root/united-dialog-archive/cron.log
done
