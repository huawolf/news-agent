# Portal localization runtime

The portal uses the standard i18next browser stack, vendored so the portal stays usable offline and does not rely on a public CDN.

| Package | Version | Role |
| --- | --- | --- |
| `i18next` | 26.4.2 | Translation runtime |
| `i18next-http-backend` | 4.0.2 | Loads locale JSON resources |
| `i18next-browser-languagedetector` | 8.2.1 | Detects query, saved, and browser language preferences |

To upgrade, update the pinned versions in `../package.json`, then run `npm install` and `npm run vendor` from `src/api/static`. This refreshes browser bundles and their licenses from `node_modules`. Keep translations in `../locales/<language>/translation.json`; do not add user-facing copy to portal scripts.
