# Build tools

| File | Use |
|---|---|
| `node/check.js` | Shopify Theme Check (recommended config): `cd node && npm install && npm run check` |
| `node/preview.js` | Offline Liquid preview of the theme with mock products and variants → static HTML (`npm run preview`); also writes `/products/<handle>.js` so the checkout bridge can be previewed |
| `theme_templates.py` | Generates the theme's JSON templates, settings and nl/en/fr locales. **All storefront copy lives here**: edit, then `python theme_templates.py` |
| `shoot.py` | Playwright screenshots: `VT_BASE=http://localhost:8000 VT_SHOTS=out python shoot.py jobs.json` (job = name, path, widths, login e-mail or null, full page, options: wait, click[], fill{}, scroll, element) |
| `sheet.py`, `overview_sheets.py` | Combine screenshots into review and overview sheets |
