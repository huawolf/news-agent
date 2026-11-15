const portalI18n = window.i18next.createInstance();

const NEWS_LANGUAGE_KEY = 'news-agent-language';
const MANAI_LANGUAGE_KEY = 'language';

const normalizeLanguage = value => {
  const language = String(value || '').trim().toLowerCase();
  if (language === 'zh' || language.startsWith('zh-')) return 'zh';
  if (language === 'en' || language.startsWith('en-')) return 'en';
  return '';
};

const manaiLanguage = () => normalizeLanguage(localStorage.getItem(MANAI_LANGUAGE_KEY));

// The portal and manai-pic share an origin in production, so keep their
// language preferences aligned even though they use different value formats.
const syncLanguage = language => {
  const normalized = normalizeLanguage(language);
  if (!normalized) return;
  localStorage.setItem(NEWS_LANGUAGE_KEY, normalized);
  localStorage.setItem(MANAI_LANGUAGE_KEY, normalized === 'en' ? 'en-US' : 'zh-CN');
  const url = new URL(window.location.href);
  if (url.searchParams.get('lng') !== normalized) {
    url.searchParams.set('lng', normalized);
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  }
};

portalI18n.on('languageChanged', syncLanguage);

if (!normalizeLanguage(new URLSearchParams(window.location.search).get('lng'))) {
  const shared = manaiLanguage();
  if (shared) localStorage.setItem(NEWS_LANGUAGE_KEY, shared);
}

window.portalI18nReady = portalI18n
  .use(window.i18nextHttpBackend)
  .use(window.i18nextBrowserLanguageDetector)
  .init({
    fallbackLng: 'zh',
    supportedLngs: ['zh', 'en'],
    load: 'languageOnly',
    ns: ['translation'],
    defaultNS: 'translation',
    backend: {
      loadPath:
        'static/locales/{{lng}}/{{ns}}.json?v=' + (window.portalAssetVersion || '1'),
    },
    detection: {
      order: ['querystring', 'localStorage', 'navigator', 'htmlTag'],
      lookupQuerystring: 'lng',
      lookupLocalStorage: NEWS_LANGUAGE_KEY,
      caches: ['localStorage'],
    },
    interpolation: { escapeValue: false },
  })
  .then(() => {
    syncLanguage(portalI18n.resolvedLanguage || portalI18n.language);
    window.portalI18n = {
      i18n: portalI18n,
      t: (key, vars) => portalI18n.t(key, vars),
      get language() {
        return portalI18n.resolvedLanguage || portalI18n.language;
      },
    };
    return window.portalI18n;
  })
  .catch(error => {
    console.error('Portal localization failed to initialize', error);
    window.portalI18n = {
      i18n: portalI18n,
      t: (key, vars) => {
        const value = portalI18n.t(key, vars);
        if (!vars) return key;
        return Object.entries(vars).reduce(
          (text, [name, replacement]) => text.replaceAll(`{{${name}}}`, String(replacement)),
          value,
        );
      },
      get language() {
        return 'zh';
      },
    };
    return window.portalI18n;
  });
