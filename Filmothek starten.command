#!/bin/bash
# Startet die Filmothek und öffnet sie im Browser.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display alert "Python 3 fehlt" message "Bitte einmalig im Terminal  xcode-select --install  ausführen oder Python von python.org installieren."'
  exit 1
fi
python3 server.py --browser
