// Runs Shopify Theme Check (recommended config) on the theme and prints offenses.
const { themeCheckRun } = require('@shopify/theme-check-node');
const root = process.argv[2];
(async () => {
  const res = await themeCheckRun(root, undefined, () => {});
  const offenses = res.offenses || res;
  const sev = { 0: 'error', 1: 'warning', 2: 'info' };
  const counts = { error: 0, warning: 0, info: 0 };
  for (const o of offenses) {
    const s = sev[o.severity] || String(o.severity); counts[s] = (counts[s] || 0) + 1;
    console.log(`${s.toUpperCase()} ${o.check} ${o.uri.replace('file://' + root, '')}:${o.start ? o.start.line + 1 : ''} ${o.message}`);
  }
  console.log('SUMMARY', JSON.stringify(counts), 'total', offenses.length);
})().catch(e => { console.error('theme check failed', e); process.exit(2); });
