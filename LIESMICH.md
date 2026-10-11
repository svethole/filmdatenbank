# Filmothek

Eine einfache Film- und Serienbibliothek im Browser – abgespielt wird mit dem installierten **VLC**.

## Was gebraucht wird
- macOS mit **Python 3** und **Git** (einmalig im Terminal `xcode-select --install` ausführen, falls sie fehlen)
- **VLC** im Programme-Ordner
- Ein kostenloser **TMDB-Schlüssel** für Cover und Infos: Konto auf themoviedb.org anlegen → Einstellungen → API → „API-Schlüssel“ oder „Lesezugriffs-Token“ kopieren

## Installation für mehrere Benutzerkonten
Die Filmothek liegt zentral unter **/Users/Shared/Filmothek** (im Finder: *Macintosh HD → Benutzer → Für alle Benutzer*).
Bibliothek, Cover und Einstellungen werden von allen Konten gemeinsam genutzt, die „gesehen“-Markierungen hat jedes Konto für sich.

1. **Im eigenen Konto** im Terminal:
   ```
   git clone https://github.com/svethole/filmdatenbank.git /Users/Shared/Filmothek
   ```
2. Im Finder `/Users/Shared/Filmothek` öffnen und **`Autostart einrichten.command`** doppelklicken
   (beim ersten Mal ggf. Rechtsklick → Öffnen). Im Browser öffnet sich die Filmothek:
   über das **Zahnrad** den Filmordner und den TMDB-Schlüssel eintragen → **Speichern & Filme einlesen**.
3. **Im Konto der Mama** anmelden, im Finder *Gehe zu → Gehe zum Ordner …* → `/Users/Shared/Filmothek`
   und dort ebenfalls **`Autostart einrichten.command`** doppelklicken. Die Einstellungen sind schon da.
4. In jedem Konto in Safari **Ablage → Zum Dock hinzufügen** – dann gibt's ein eigenes Filmothek-Symbol im Dock.

Jedes Konto bekommt automatisch eine eigene Adresse (z. B. `localhost:8642` und `localhost:8643`),
damit VLC immer im richtigen Konto aufgeht.

### Aktualisieren
```
cd /Users/Shared/Filmothek && git pull
```
Die Filmothek merkt das Update und startet sich in allen Konten nach etwa 15 Sekunden von selbst neu.

## Serien
Serienfolgen werden automatisch erkannt, z. B. an
- `S01E02`, `1x02`, `Folge 12`, `Staffel 1 Folge 2` im Dateinamen,
- Ordnern wie `Staffel 1` / `Season 1`,
- einem Oberordner `Serien` (dann ist der nächste Ordner der Serienname),
- oder mindestens drei Dateien `Serienname - Folgentitel` im selben Ordner (wird bei TMDB geprüft).

Am zuverlässigsten ist: `Serien/Alf/Staffel 1/Alf S01E03.avi`.
Folgen ohne Nummer werden über ihren Titel gesucht.

## Gut zu wissen
- Beim Start werden neue Filme automatisch gesucht, sofern die Festplatte angesteckt ist. Sonst: **Bibliothek aktualisieren**.
- Schon geladene Infos und Cover bleiben gespeichert (Ordner `daten/`) – auch ohne Festplatte bleibt die Übersicht sichtbar.
- Falscher oder kein Treffer: Film bzw. „Infos zur Serie“ öffnen → ganz unten **Zuordnung korrigieren**.
- Dateinamen mit Zusätzen wie `Doris Day - Pillow Talk (1959)` werden trotzdem erkannt; der Name bleibt durchsuchbar.
- Die Suche verzeiht kleine Tippfehler („Sean Conery“ findet Sean Connery).
- Dateien in Ordnern wie `Extras`, `Trailer`, `Sample` sowie macOS-Hilfsdateien (`._…`) werden ignoriert.
- Fragt macOS, ob „python3“ auf Dateien auf einem Wechseldatenträger zugreifen darf: **Erlauben** (in jedem Konto einmal).
- Die Filmothek ist nur auf diesem Mac erreichbar (nicht im Netzwerk).
- Ohne Autostart: `Filmothek starten.command` doppelklicken. Autostart abschalten: `Autostart entfernen.command`.
- Fehlermeldungen landen in `~/Library/Logs/Filmothek.log`.
