const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const packages = [
  {
    name: 'i18next',
    source: 'dist/umd/i18next.min.js',
    output: 'i18next.min.js',
    license: 'LICENSE',
    licenseOutput: 'i18next.LICENSE',
  },
  {
    name: 'i18next-http-backend',
    source: 'i18nextHttpBackend.min.js',
    output: 'i18next-http-backend.min.js',
    license: 'licence',
    licenseOutput: 'i18next-http-backend.LICENSE',
  },
  {
    name: 'i18next-browser-languagedetector',
    source: 'dist/umd/i18nextBrowserLanguageDetector.min.js',
    output: 'i18next-browser-language-detector.min.js',
    license: 'LICENSE',
    licenseOutput: 'i18next-browser-language-detector.LICENSE',
  },
];

for (const dependency of packages) {
  const packageRoot = path.join(root, 'node_modules', dependency.name);
  fs.copyFileSync(
    path.join(packageRoot, dependency.source),
    path.join(root, 'vendor', dependency.output),
  );
  fs.copyFileSync(
    path.join(packageRoot, dependency.license),
    path.join(root, 'vendor', dependency.licenseOutput),
  );
}
