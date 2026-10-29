# Persistent personal-site monitoring on macOS

The personal monitor runs as a Docker Compose service with a named volume for
health history. `restart: unless-stopped` restarts the process after crashes or
Docker engine restarts. A launchd job runs at login and every five minutes to
ensure the Compose service exists once Docker Desktop is available.

The active local configuration is `config/customer.yaml`, which is intentionally
ignored by Git so credentials can be added later without committing them.

## Install

```bash
chmod +x deploy/macos/start-monitor.sh
docker compose up -d --build
mkdir -p ~/Library/LaunchAgents ~/Library/Logs/AI-SRE \
  ~/Library/'Application Support'/AI-SRE
cp deploy/macos/start-monitor.sh ~/Library/'Application Support'/AI-SRE/
cp deploy/macos/com.abhay.ai-sre-monitor.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.abhay.ai-sre-monitor.plist
```

## Verify

```bash
docker compose ps
docker compose logs --tail=50 monitor
docker compose exec monitor ai-sre dashboard --config /app/config/customer.yaml
launchctl print "gui/$(id -u)/com.abhay.ai-sre-monitor"
```

The container health check becomes healthy after the first website poll and
fails if no health sample has been written for three minutes.

## Remove the login job

```bash
launchctl bootout "gui/$(id -u)/com.abhay.ai-sre-monitor"
rm ~/Library/LaunchAgents/com.abhay.ai-sre-monitor.plist
rm ~/Library/'Application Support'/AI-SRE/start-monitor.sh
```

Removing the launchd job does not delete monitoring history. `docker compose
down` preserves the named volume; `docker compose down -v` deletes it.

## Availability limitation

This setup survives terminal closure, process crashes, Docker restarts, and Mac
logins. It cannot check the website while the Mac is powered off, disconnected,
or fully asleep. A true 24/7 availability measurement requires the same Compose
service on an always-on host outside Vercel's infrastructure.
