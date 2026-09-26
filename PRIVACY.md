# Privacy

Sabasakal Mini Screen 3.5″ is a local desktop application.

- Settings, diagnostics and short-lived sensor/game caches are stored locally
  under `%APPDATA%\CS2Screen`.
- Weather lookup uses Open-Meteo. Selected city names are geocoded and the
  resulting coordinates are used for forecast requests.
- Valve relay configuration is downloaded from Valve's public Steam endpoint.
- Media metadata is read from Windows Global System Media Transport Controls.
- Battery data is read from Windows device properties and, when installed,
  the local Logitech G HUB service.
- CS2 data comes from Valve Game State Integration and local console output.
- The application contains no analytics, advertising, account system or custom
  telemetry upload.

Runtime logs can contain hardware model names, city choices, application names
or game-state diagnostics. Review them before sharing in a public issue.

