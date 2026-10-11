# Filmothek

Eine einfache Filmbibliothek im Browser – Filme werden mit dem installierten **VLC** abgespielt.

## Was gebraucht wird
- macOS mit **Python 3** (einmalig im Terminal `xcode-select --install` ausführen, falls `python3` fehlt)
- **VLC** im Programme-Ordner
- Ein kostenloser **TMDB-Schlüssel** für Cover und Infos: Konto auf themoviedb.org anlegen → Einstellungen → API → „API-Schlüssel“ oder „Lesezugriffs-Token“ kopieren

## Einrichten (einmalig)
1. Ordner `Filmothek` an einen festen Platz legen, z. B. in den Benutzerordner.
2. **`Autostart einrichten.command`** doppelklicken (beim ersten Mal ggf. Rechtsklick → Öffnen).
   Die Filmothek läuft ab dann im Hintergrund, auch nach einem Neustart.
3. Im Browser öffnet sich `http://localhost:8642`. Über das **Zahnrad** den Filmordner auswählen und den TMDB-Schlüssel eintragen → **Speichern & Filme einlesen**.
4. In Safari **Ablage → Zum Dock hinzufügen** – dann hat die Mama ein eigenes Filmothek-Symbol im Dock.

Ohne Autostart geht's auch: `Filmothek starten.command` doppelklicken (dann bleibt ein Terminal-Fenster offen).

## Gut zu wissen
- Beim Start werden neue Filme automatisch gesucht, sofern die Festplatte angesteckt ist. Sonst: **Bibliothek aktualisieren**.
- Schon geladene Infos und Cover werden gespeichert (Ordner `daten/`) – auch ohne Festplatte bleibt die Übersicht sichtbar.
- Hat ein Film falsche oder keine Infos: Film öffnen → ganz unten **Zuordnung korrigieren**.
- Dateien in Ordnern wie `Extras`, `Trailer`, `Sample` sowie macOS-Hilfsdateien (`._…`) werden ignoriert.
- Findet die Filmothek im Autostart-Betrieb keine Filme auf der externen Platte, unter
  *Systemeinstellungen → Datenschutz & Sicherheit → Festplattenvollzugriff* `python3` erlauben.
- Die Filmothek ist nur auf diesem Mac erreichbar (nicht im Netzwerk).
- Autostart wieder abschalten: `Autostart entfernen.command`.
