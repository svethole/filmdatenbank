#!/bin/bash
PLIST="$HOME/Library/LaunchAgents/de.filmothek.server.plist"
launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST"
echo "Autostart der Filmothek wurde entfernt."
read -r -p "Enter drücken zum Schließen …"
