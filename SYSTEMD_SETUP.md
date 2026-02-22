# Persistent Service Setup

This guide shows how to run the Claude Code Slack Bot as a persistent service.

**SECURITY NOTE:** Before setting up the service, ensure your `.env` file has `DEVELOPMENT_MODE=false` and `ENVIRONMENT=production` for secure operation.

## Option A: systemd (Linux)

### 1. Create the service file

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/claude-slack-bot.service
```

Add this content:

```ini
[Unit]
Description=Claude Code Slack Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/claude-code-slack
ExecStart=/usr/local/bin/poetry run claude-slack-bot
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Environment — ensure CLAUDECODE is unset so nested sessions work
Environment="PATH=/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=default.target
```

**Note:** Update `WorkingDirectory` and the Poetry path for your system.

### 2. Enable and start

```bash
systemctl --user daemon-reload
systemctl --user enable claude-slack-bot.service
systemctl --user start claude-slack-bot.service
```

### 3. Verify

```bash
systemctl --user status claude-slack-bot
```

### 4. Verify secure configuration

```bash
journalctl --user -u claude-slack-bot -n 50 | grep -i "environment\|development"
# Should show: "environment": "production"
```

### Common commands

```bash
systemctl --user start claude-slack-bot
systemctl --user stop claude-slack-bot
systemctl --user restart claude-slack-bot
systemctl --user status claude-slack-bot
journalctl --user -u claude-slack-bot -f        # Live logs
journalctl --user -u claude-slack-bot -n 50     # Recent logs
```

**Service stops after logout?** Enable lingering:
```bash
loginctl enable-linger $USER
```

## Option B: launchd (macOS)

### 1. Create the plist file

```bash
nano ~/Library/LaunchAgents/com.claude-slack-bot.plist
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude-slack-bot</string>
    <key>WorkingDirectory</key>
    <string>/path/to/claude-code-slack</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/poetry</string>
        <string>run</string>
        <string>claude-slack-bot</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude-slack-bot.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-slack-bot.err</string>
</dict>
</plist>
```

**Note:** Update `WorkingDirectory` and Poetry path for your system.

### 2. Load and start

```bash
launchctl load ~/Library/LaunchAgents/com.claude-slack-bot.plist
```

### 3. Common commands

```bash
# Start
launchctl start com.claude-slack-bot

# Stop
launchctl stop com.claude-slack-bot

# Unload (disable)
launchctl unload ~/Library/LaunchAgents/com.claude-slack-bot.plist

# Check status
launchctl list | grep claude

# View logs
tail -f /tmp/claude-slack-bot.log
```

## Option C: tmux (Quick & Simple)

For development or quick deployment without a service manager:

```bash
make run-remote    # Starts in tmux session 'claude-bot'
make remote-attach # Attach to session
make remote-stop   # Stop session
```

This persists after SSH disconnect but not after reboot.
