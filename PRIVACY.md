# Privacy

Sabasakal Mini Screen 3.5″ is a local desktop application.

- Settings, diagnostics and short-lived sensor caches are stored locally
  under `%APPDATA%\CS2Screen`.
- Weather lookup uses Open-Meteo. Selected city names are geocoded and the
  resulting coordinates are used for forecast requests.
- Media metadata is read from Windows Global System Media Transport Controls.
- Battery data is read from Windows device properties and, when installed,
  the local Logitech G HUB service.
- The CS2 theme only checks whether the `cs2.exe` process is running; no CS2
  data is read.
- The application contains no analytics, advertising, account system or custom
  telemetry upload.

Runtime logs can contain hardware model names, city choices, application names
or flight-simulator diagnostics. Review them before sharing in a public issue.

