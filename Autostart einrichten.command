#!/bin/bash
# Richtet ein, dass die Filmothek beim Anmelden automatisch im Hintergrund startet.
# Muss in JEDEM Benutzerkonto einmal doppelgeklickt werden, das die Filmothek nutzt.
cd "$(dirname "$0")" || exit 1
APP_DIR="$(pwd)"
PY="$(command -v python3)"
if [ -z "$PY" ]; then
  echo "Python 3 fehlt. Bitte zuerst  xcode-select --install  ausführen."; read -r -p "Enter drücken …"; exit 1
fi
PLIST="$HOME/Library/LaunchAgents/de.filmothek.server.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>de.filmothek.server</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$APP_DIR/server.py</string></array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/Filmothek.log</string>
</dict></plist>
EOF
launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
URL="$("$PY" server.py --url)"
sleep 2
open "$URL"
echo ""
echo "Fertig! Die Filmothek startet in diesem Benutzerkonto ab jetzt automatisch."
echo "Adresse: $URL"
echo "Tipp: In Safari  Ablage → Zum Dock hinzufügen  wählen, dann gibt's ein eigenes Symbol im Dock."
read -r -p "Enter drücken zum Schließen …"
