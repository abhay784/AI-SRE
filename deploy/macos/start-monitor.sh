#!/bin/zsh

set -u

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

# Docker Desktop may still be starting when launchd runs this job. Retry for
# two minutes; launchd also invokes the job every five minutes as a backstop.
for _attempt in {1..24}; do
  if docker info >/dev/null 2>&1; then
    exec docker start ai-sre-personal-monitor
  fi
  sleep 5
done

exit 1
