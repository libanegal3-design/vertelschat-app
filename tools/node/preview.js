// Offline preview of the Shopify theme: renders JSON templates + sections with liquidjs and a small mock of the
// Shopify object model, so every page can be screenshotted without a store. Not part of the theme itself.
const fs = require('fs');
const path = require('path');
const { Liquid, Tag, Hash } = require('liquidjs');

const THEME = process.argv[2] || '/root/work/vt/storefront';
const OUT = process.argv[3] || '/root/work/vt/preview-out';
const read = (p) => fs.readFileSync(path.join(THEME, p), 'utf8');
const locale = JSON.parse(read('locales/nl.default.json'));
const settingsData = JSON.parse(read('config/settings_data.json')).current;

const engine = new Liquid({
  root: [path.join(THEME, 'snippets'), path.join(THEME, 'sections')],
  extname: '.liquid', strictFilters: false, strictVariables: false, jsTruthy: false,
});

function kw(args) { // liquidjs passes `key: value` filter args as [key, value] pairs
  const o = {}; for (const a of args) if (Array.isArray(a)) o[a[0]] = a[1]; return o;
}
engine.registerFilter('t', (key, ...args) => {
  const v = key.split('.').reduce((o, k) => (o || {})[k], locale);
  if (typeof v !== 'string') return `[missing ${key}]`;
  const vars = kw(args); return v.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, k) => vars[k] ?? '');
});
engine.registerFilter('asset_url', (n) => `/assets/${n}`);
engine.registerFilter('stylesheet_tag', (u) => `<link rel="stylesheet" href="${u}">`);
engine.registerFilter('preload_tag', (u, ...a) => { const o = kw(a); return `<link rel="preload" href="${u}" as="${o.as}" type="${o.type || ''}" crossorigin>`; });
engine.registerFilter('image_url', (img) => (img && img.src) || '');
engine.registerFilter('image_tag', (src, ...a) => `<img src="${src}" alt="${(kw(a).alt || '')}">`);
engine.registerFilter('money', (cents) => '€' + (Number(cents) / 100).toFixed(2).replace('.', ','));
engine.registerFilter('money_without_trailing_zeros', (cents) => '€' + (Number(cents) / 100).toFixed(2).replace('.00', '').replace('.', ','));
engine.registerFilter('json', (v) => JSON.stringify(v === undefined ? null : v));
engine.registerFilter('default_pagination', () => '');

class SchemaTag extends Tag {
  constructor(token, remainTokens, liquid) {
    super(token, remainTokens, liquid);
    while (remainTokens.length) { const t = remainTokens.shift(); if (t.name === 'endschema') return; }
  }
  * render() { return ''; }
}
engine.registerTag('schema', SchemaTag);

function blockTag(open, close, render) {
  return class extends Tag {
    constructor(token, remainTokens, liquid) {
      super(token, remainTokens, liquid);
      this.args = token.args; this.tpls = [];
      const stream = this.liquid.parser.parseStream(remainTokens)
        .on('tag:' + close, () => stream.stop())
        .on('template', (tpl) => this.tpls.push(tpl))
        .on('end', () => { throw new Error(`tag ${token.getText()} not closed`); });
      stream.start();
    }
    * render(ctx, emitter) {
      const inner = yield this.liquid.renderer.renderTemplates(this.tpls, ctx);
      emitter.write(render(this.args, inner, ctx));
    }
  };
}
engine.registerTag('style', blockTag('style', 'endstyle', (a, inner) => `<style>${inner}</style>`));
engine.registerTag('form', blockTag('form', 'endform', (args, inner) => {
  const type = (args.match(/'([^']+)'/) || [])[1] || 'form';
  const id = (args.match(/id:\s*'([^']+)'/) || [])[1];
  const cls = (args.match(/class:\s*'([^']+)'/) || [])[1];
  const data = [...args.matchAll(/(data-[\w-]+):\s*''/g)].map(m => m[1]).join(' ');
  const action = type === 'product' ? '/cart/add' : type === 'contact' ? '/contact' : '/';
  return `<form method="post" action="${action}"${id ? ` id="${id}"` : ''}${cls ? ` class="${cls}"` : ''} ${data}>${inner}</form>`;
}));
engine.registerTag('paginate', blockTag('paginate', 'endpaginate', (a, inner) => inner));

function schemaOf(src) {
  const m = src.match(/\{%\s*schema\s*%\}([\s\S]*?)\{%\s*endschema\s*%\}/);
  return m ? JSON.parse(m[1]) : {};
}
function defaults(settings) { const o = {}; for (const s of settings || []) if ('default' in s) o[s.id] = s.default; return o; }

async function renderSection(id, def, scope) {
  const src = read(`sections/${def.type}.liquid`).replace(/posted_successfully\?/g, 'posted_successfully');
  const schema = schemaOf(src);
  const settings = { ...defaults(schema.settings), ...(def.settings || {}) };
  for (const st of schema.settings || []) {  // product pickers hold a handle in JSON templates
    if (st.type === 'product' && typeof settings[st.id] === 'string') settings[st.id] = PRODUCTS[settings[st.id]] || null;
  }
  const blockSchemas = Object.fromEntries((schema.blocks || []).map(b => [b.type, b]));
  const blocks = (def.block_order || []).map(bid => {
    const b = def.blocks[bid];
    return { id: bid, type: b.type, shopify_attributes: '', settings: { ...defaults((blockSchemas[b.type] || {}).settings), ...(b.settings || {}) } };
  });
  const html = await engine.parseAndRender(src, { ...scope, section: { id, settings, blocks } });
  return `<div id="shopify-section-${id}" class="shopify-section">${html}</div>`;
}

class SectionsTag extends Tag {
  constructor(token, remainTokens, liquid) { super(token, remainTokens, liquid); this.group = token.args.replace(/['"]/g, '').trim(); }
  * render(ctx, emitter) {
    const group = JSON.parse(read(`sections/${this.group}.json`));
    const scope = ctx.getAll();
    let out = '';
    for (const id of group.order) out += yield renderSection(id, group.sections[id], scope);
    emitter.write(out);
  }
}
engine.registerTag('sections', SectionsTag);

const v = (id, sku, price, title) => ({ id, sku, price, title, public_title: title, available: true });
const PRODUCTS = {
  verteljaar: { title: 'Het verteljaar', handle: 'verteljaar', url: '/products/verteljaar', price: 12900,
    description: '<p>Een jaar lang vragen via WhatsApp voor je vader, moeder, opa of oma. Zij antwoorden met een gewoon spraakbericht; jij krijgt de verhalen, de stem en aan het eind een hardcover boek met bij elk verhaal een QR-code.</p>',
    featured_image: null, images: [],
    variants: [v(4400000001, 'VT-VERTELJAAR', 12900, 'Eén verteller'), v(4400000002, 'VT-VERTELJAAR-DUO', 22800, 'Twee vertellers')] },
  'nog-een-verteljaar': { title: 'Nog een verteljaar', handle: 'nog-een-verteljaar', price: 7900, featured_image: null, images: [],
    description: '<p>Nog twaalf maanden vragen voor dezelfde verteller, met een boektegoed voor deel 2 of een extra exemplaar.</p>',
    variants: [v(4400000011, 'VT-VERLENGING-BOEK', 7900, 'Met boektegoed'), v(4400000012, 'VT-VERLENGING', 6900, 'Zonder boektegoed (tarief 2026)')] },
  'extra-verteller': { title: 'Extra verteller', handle: 'extra-verteller', price: 9900, featured_image: null, images: [],
    description: '<p>Nog een verteller in jullie familie, bijvoorbeeld de andere ouder of een grootouder.</p>',
    variants: [v(4400000021, 'VT-EXTRA-VERTELLER', 9900, 'Default Title')] },
  cadeaupakket: { title: 'Cadeaupakket', handle: 'cadeaupakket', price: 1795, featured_image: null, images: [],
    description: '<p>Iets om echt uit te pakken bij het verteljaar.</p>',
    variants: [v(4400000031, 'VT-CADEAUPAKKET', 1795, 'Default Title')] },
  boekexemplaar: { title: 'Extra exemplaar', handle: 'boekexemplaar', price: 4500, featured_image: null, images: [],
    description: '<p>Een extra exemplaar van een goedgekeurd boek.</p>',
    variants: [v(4400000041, 'VT-BOEK-1', 4500, 'Eerste in het pakket / tot 240'), v(4400000042, 'VT-BOEK-N', 3900, 'Volgende in hetzelfde pakket / tot 240'),
               v(4400000043, 'VT-BOEK-1-320', 6000, 'Eerste in het pakket / tot 320'), v(4400000044, 'VT-BOEK-N-320', 5400, 'Volgende in hetzelfde pakket / tot 320'),
               v(4400000045, 'VT-BOEK-1-400', 7400, 'Eerste in het pakket / tot 400'), v(4400000046, 'VT-BOEK-N-400', 6800, 'Volgende in hetzelfde pakket / tot 400')] },
  'familie-exemplaar': { title: 'Familie-exemplaar', handle: 'familie-exemplaar', price: 4500, featured_image: null, images: [],
    description: '<p>Een eigen exemplaar van het boek, besteld via een privélink.</p>',
    variants: [v(4400000051, 'VT-FAMILIE-1', 4500, 'Eerste in het pakket / tot 240'), v(4400000052, 'VT-FAMILIE-N', 3900, 'Volgende in hetzelfde pakket / tot 240'),
               v(4400000053, 'VT-FAMILIE-1-320', 6000, 'Eerste in het pakket / tot 320'), v(4400000054, 'VT-FAMILIE-N-320', 5400, 'Volgende in hetzelfde pakket / tot 320')] },
  'dikker-boek': { title: 'Dikker boek', handle: 'dikker-boek', price: 1500, featured_image: null, images: [],
    description: '<p>Toeslag voor een boek met meer dan 240 pagina\'s.</p>',
    variants: [v(4400000061, 'VT-OMVANG-320', 1500, 'Tot 320 pagina\'s'), v(4400000062, 'VT-OMVANG-400', 2900, 'Tot 400 pagina\'s')] },
};
for (const p of Object.values(PRODUCTS)) p.selected_or_first_available_variant = p.variants[0];
const product = PRODUCTS.verteljaar;
const PAGES = [
  ['home', 'index', {}],
  ['product', 'product', { product }],
  ['hoe-het-werkt', 'page.hoe-het-werkt', {}], ['cadeau', 'page.cadeau', {}], ['voorbeelden', 'page.voorbeelden', {}],
  ['prijzen', 'page.prijzen', {}], ['veelgestelde-vragen', 'page.veelgestelde-vragen', {}], ['over-ons', 'page.over-ons', {}],
  ['contact', 'page.contact', {}], ['privacy', 'page.privacy', {}], ['voorwaarden', 'page.voorwaarden', {}],
  ['retourneren', 'page.retourneren', {}], ['verzending', 'page.verzending', {}], ['na-het-verteljaar', 'page.na-het-verteljaar', {}],
  ['404', '404', {}],
  ['cart', 'cart', { cart: { item_count: 2, total_price: 22800 + 1795, items: [
      { product, variant: product.variants[1], quantity: 1, final_line_price: 22800, properties: { Cadeau: 'Ja', Voor: 'Marijke en Henk', _project: '' } },
      { product: PRODUCTS.cadeaupakket, quantity: 1, final_line_price: 1795, properties: {} }] } }],
  ['boek-afrekenen', 'page.boek-afrekenen', {}],
  ['nog-een-verteljaar', 'product.verlenging', { product: PRODUCTS['nog-een-verteljaar'] }],
  ['extra-verteller', 'product.extra-verteller', { product: PRODUCTS['extra-verteller'] }],
  ['cadeaupakket', 'product.cadeaupakket', { product: PRODUCTS.cadeaupakket }],
  ['boekexemplaar', 'product.boekexemplaar', { product: PRODUCTS.boekexemplaar }],
  ['familie-exemplaar', 'product.familie-exemplaar', { product: PRODUCTS['familie-exemplaar'] }],
  ['dikker-boek', 'product.dikker-boek', { product: PRODUCTS['dikker-boek'] }],
];

(async () => {
  fs.mkdirSync(path.join(OUT, 'assets'), { recursive: true });
  fs.mkdirSync(path.join(OUT, 'products'), { recursive: true });
  for (const [h, p] of Object.entries(PRODUCTS)) {  // what /products/<handle>.js returns in a real store
    fs.writeFileSync(path.join(OUT, 'products', `${h}.js`), JSON.stringify({ title: p.title, handle: h, variants: p.variants }));
  }
  for (const f of fs.readdirSync(path.join(THEME, 'assets'))) fs.copyFileSync(path.join(THEME, 'assets', f), path.join(OUT, 'assets', f));
  const layout = read('layout/theme.liquid');
  for (const [name, tpl, extra] of PAGES) {
    const [tname, suffix] = tpl.split('.');
    const template = JSON.parse(read(`templates/${tpl}.json`));
    const scope = {
      shop: { name: 'Vertelschat', description: settingsData.default_description }, settings: settingsData,
      request: { locale: { iso_code: 'nl' }, page_type: tname }, routes: { root_url: '/', cart_url: '/cart', search_url: '/search' },
      canonical_url: 'https://vertelschat.nl/', page_title: name === 'home' ? 'Vertelschat' : name, page_description: settingsData.default_description,
      template: { name: tname, suffix: suffix || null }, content_for_header: '', cart: { item_count: 0, items: [] },
      page: { title: name, content: '', handle: name }, search: { performed: false }, collection: { title: 'Alles', products: [product] },
      product: undefined,
      form: { posted_successfully: false, errors: null }, ...extra,
    };
    let body = '';
    for (const id of template.order) body += await renderSection(id, template.sections[id], scope);
    const html = await engine.parseAndRender(layout, { ...scope, content_for_layout: body });
    fs.writeFileSync(path.join(OUT, `${name}.html`), html);
    console.log('rendered', name, html.length);
  }
})().catch(e => { console.error(e); process.exit(1); });
