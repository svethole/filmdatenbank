'use strict';

/* ======================= Grundlagen ======================= */
const $ = (s, el = document) => el.querySelector(s);
const state = {
  films: [], byId: new Map(), shows: {}, epsByShow: new Map(), info: {},
  query: '', sort: 'title', series: true, sidebar: true,
  open: {}, tmpOpen: {},
  openRef: null, pageScroll: 0, pushed: false, polling: false,
};

const store = {
  get(k, d) { try { const v = localStorage.getItem('filmothek.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('filmothek.' + k, JSON.stringify(v)); } catch { /* egal */ } },
};

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  };
  let res;
  try { res = await fetch(path, opts); }
  catch { throw new Error('Die Filmothek antwortet nicht. Läuft das Programm noch?'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || 'Da ist etwas schiefgegangen.');
  return data;
}

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const norm = s => String(s ?? '').toLowerCase().replace(/ß/g, 'ss').normalize('NFD').replace(/[̀-ͯ]/g, '');
const collator = new Intl.Collator('de', { sensitivity: 'base', numeric: true });
const hue = id => [...String(id)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const short = (s, n) => !s ? '' : s.length <= n ? s : s.slice(0, s.lastIndexOf(' ', n)) + ' …';

function runtimeText(min) {
  if (!min) return '';
  const h = Math.floor(min / 60), m = min % 60;
  return h ? `${h} Std. ${m ? m + ' Min.' : ''}`.trim() : `${m} Min.`;
}
const dateText = d => d ? new Date(d).toLocaleDateString('de-DE', { day: 'numeric', month: 'long', year: 'numeric' }) : '';

let toastTimer;
function toast(msg, type = '', ms = 4500) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast ' + type; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

/* ======================= Daten laden ======================= */
const showTitle = sh => sh?.title || sh?.guess_name || 'Serie';
const isPlaceholder = n => /^(folge|episode)\s*\d+$/i.test(n || '');
const epTitle = e => (e.ep_name && !isPlaceholder(e.ep_name)) ? e.ep_name
  : (e.ep_title_guess || e.ep_name || (e.episode != null ? `Folge ${e.episode}` : e.title || e.file));
const epLabel = e => (e.episode != null && !/^folge\s/i.test(epTitle(e)) ? `${e.episode}. ` : '') + epTitle(e);
const epOrder = (a, b) => (a.season ?? 999) - (b.season ?? 999) || (a.episode ?? 999) - (b.episode ?? 999) || collator.compare(epTitle(a), epTitle(b));

function prepare(f) {
  const sh = f.kind === 'episode' ? state.shows[f.show_key] : null;
  const people = [...(f.cast || []), ...(sh?.cast || [])].map(c => c.name);
  const genres = f.genres || sh?.genres || [];
  f._hay = norm([f.title, f.original_title, f.guess_title, f.ep_name, f.ep_title_guess,
    sh?.title, sh?.original_title, sh?.guess_name, ...(f.directors || []), ...(sh?.creators || []),
    ...people, ...(f.keywords || []), ...(sh?.keywords || []), ...genres, f.collection?.name,
    f.tagline, f.overview, f.ep_overview, f.year, f.file].join(' '));
  f._tokens = [...new Set(norm([f.title, f.original_title, f.ep_name, sh?.title, sh?.original_title,
    f.collection?.name, ...people, ...(f.directors || []), ...genres, f.file].join(' '))
    .split(/[^a-z0-9]+/).filter(t => t.length >= 3))];
  return f;
}

async function loadFilms() {
  const data = await api('/api/films');
  state.info = data;
  state.shows = data.shows || {};
  // Dieselbe Serie in unterschiedlich geschriebenen Ordnern -> eine gemeinsame Gruppe
  const canon = {}, byTmdb = {};
  for (const [k, sh] of Object.entries(state.shows).sort(([a], [b]) => a.localeCompare(b))) {
    canon[k] = sh.tmdb_id ? (byTmdb[sh.tmdb_id] ||= k) : k;
  }
  for (const f of data.films) if (f.kind === 'episode' && canon[f.show_key]) f.show_key = canon[f.show_key];
  state.films = data.films.map(prepare);
  state.byId = new Map(state.films.map(f => [f.id, f]));
  state.epsByShow = new Map();
  for (const f of state.films) {
    if (f.kind !== 'episode') continue;
    if (!state.epsByShow.has(f.show_key)) state.epsByShow.set(f.show_key, []);
    state.epsByShow.get(f.show_key).push(f);
  }
  for (const eps of state.epsByShow.values()) eps.sort(epOrder);
  render();
  if (state.openRef && refExists(state.openRef)) showDetail(state.openRef, true);
}

/* ======================= Suche (mit Tippfehler-Toleranz) ======================= */
function lev(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i]; let best = i;
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (cur[j] < best) best = cur[j];
    }
    if (best > max) return max + 1;
    prev = cur;
  }
  return prev[b.length];
}
function fuzzyHit(w, tokens) {
  const tol = w.length >= 8 ? 2 : w.length >= 4 ? 1 : 0;
  if (!tol) return false;
  return tokens.some(t => lev(w, t, tol) <= tol || (t.length > w.length && lev(w, t.slice(0, w.length), tol) <= tol));
}
function filterFilms() {
  const words = norm(state.query).split(/\s+/).filter(Boolean);
  if (!words.length) return { list: state.films, fuzzy: false };
  let list = state.films.filter(f => words.every(w => f._hay.includes(w)));
  if (list.length) return { list, fuzzy: false };
  list = state.films.filter(f => words.every(w => f._hay.includes(w) || fuzzyHit(w, f._tokens)));
  return { list, fuzzy: list.length > 0 };
}

/* ======================= Sortieren & Gruppieren ======================= */
function comparator(sort) {
  const byTitle = (a, b) => collator.compare(a.title || '', b.title || '');
  switch (sort) {
    case 'year_desc': return (a, b) => (b.year || 0) - (a.year || 0) || byTitle(a, b);
    case 'year_asc': return (a, b) => (a.year || 9999) - (b.year || 9999) || byTitle(a, b);
    case 'added': return (a, b) => (b.added || 0) - (a.added || 0) || byTitle(a, b);
    default: return byTitle;
  }
}

/* Einträge: {type:'film'} | {type:'coll'} (Filmreihe) | {type:'show'} (Serie) */
function buildItems(films) {
  const items = [], colls = new Map(), shows = new Map();
  for (const f of films) {
    if (f.kind === 'episode') {
      if (!shows.has(f.show_key)) shows.set(f.show_key, []);
      shows.get(f.show_key).push(f);
    } else if (state.series && f.collection) {
      if (!colls.has(f.collection.id)) colls.set(f.collection.id, []);
      colls.get(f.collection.id).push(f);
    } else items.push({ type: 'film', film: f, key: f, genre: f.genres?.[0] });
  }
  for (const arr of colls.values()) {
    if (arr.length < 2) { items.push({ type: 'film', film: arr[0], key: arr[0], genre: arr[0].genres?.[0] }); continue; }
    arr.sort((a, b) => (a.year || 9999) - (b.year || 9999) || collator.compare(a.title, b.title));
    const years = arr.map(f => f.year).filter(Boolean);
    items.push({
      type: 'coll', id: 'c' + arr[0].collection.id, name: arr[0].collection.name, films: arr, genre: arr[0].genres?.[0],
      key: {
        title: arr[0].collection.name, minYear: Math.min(9999, ...years), maxYear: Math.max(0, ...years),
        added: Math.max(...arr.map(f => f.added || 0)),
      },
    });
  }
  for (const [k, eps] of shows) {
    const sh = state.shows[k] || { key: k };
    eps.sort(epOrder);
    items.push({
      type: 'show', id: k, show: sh, eps, genre: sh.genres?.[0],
      key: { title: showTitle(sh), minYear: sh.year || 9999, maxYear: sh.last_year || sh.year || 0, added: Math.max(...eps.map(e => e.added || 0)) },
    });
  }
  return items;
}

function sortItems(items, sort) {
  const cmp = comparator(sort);
  const k = it => it.type === 'film' ? it.key
    : { ...it.key, year: sort === 'year_desc' ? it.key.maxYear : it.key.minYear };
  return items.sort((a, b) => cmp(k(a), k(b)));
}

function sections(items) {
  if (state.sort !== 'genre') return [{ title: null, items: sortItems(items, state.sort) }];
  const map = new Map();
  for (const it of items) {
    const g = it.genre || 'Ohne Kategorie';
    if (!map.has(g)) map.set(g, []);
    map.get(g).push(it);
  }
  return [...map.entries()]
    .sort(([a], [b]) => (a === 'Ohne Kategorie') - (b === 'Ohne Kategorie') || collator.compare(a, b))
    .map(([title, list]) => ({ title, items: sortItems(list, 'title') }));
}

/* ======================= Auf- und Zuklappen ======================= */
function isOpen(it) {
  if (it.id in state.tmpOpen) return state.tmpOpen[it.id];
  if (state.query) return true;                       // bei einer Suche alle Treffer zeigen
  if (it.id in state.open) return state.open[it.id];
  return it.type === 'coll' ? it.films.length <= 4 : false;
}
function setOpen(id, open) {
  if (state.query) state.tmpOpen[id] = open;
  else { state.open[id] = open; store.set('open', state.open); }
}

/* ======================= Übersicht zeichnen ======================= */
function phHTML(text, h) {
  return `<div class="ph" style="--h:${h}"><span>${esc(text)}</span></div>`;
}
function posterHTML(f) {
  return f.poster
    ? `<img src="/bilder/${encodeURIComponent(f.poster)}" alt="" loading="lazy" data-title="${esc(f.title)}" data-hue="${hue(f.id)}">`
    : phHTML(f.title, hue(f.id));
}
function showPosterHTML(sh, key) {
  const t = showTitle(sh);
  return sh?.poster
    ? `<img src="/bilder/${encodeURIComponent(sh.poster)}" alt="" loading="lazy" data-title="${esc(t)}" data-hue="${hue(key)}">`
    : phHTML(t, hue(key));
}
function stillHTML(e) {
  const label = e.episode != null ? `Folge ${e.episode}` : epTitle(e);
  return e.still
    ? `<img src="/bilder/${encodeURIComponent(e.still)}" alt="" loading="lazy" data-title="${esc(label)}" data-hue="${hue(e.show_key)}">`
    : phHTML(label, hue(e.show_key));
}

function tileHTML(f) {
  const meta = [f.year, runtimeText(f.runtime)].filter(Boolean).join(' · ');
  return `<button class="tile${f.last_played ? ' played' : ''}" data-id="${f.id}">
    <div class="poster">${posterHTML(f)}</div>
    <div class="tile-title">${esc(f.title)}</div>
    <div class="tile-meta">${esc(meta) || '&nbsp;'}</div>
  </button>`;
}

function epTileHTML(e) {
  const meta = [(e.air_date || '').slice(0, 4), runtimeText(e.runtime)].filter(Boolean).join(' · ');
  return `<button class="tile ep${e.last_played ? ' played' : ''}" data-id="${e.id}">
    <div class="poster wide">${stillHTML(e)}</div>
    <div class="tile-title">${esc(epLabel(e))}</div>
    <div class="tile-meta">${esc(meta) || '&nbsp;'}</div>
  </button>`;
}

const CHEVRON = '<svg class="tri" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg>';

function yearsText(a, b) {
  if (!a || a === 9999) return '';
  return b && b !== a ? `${a}–${b}` : String(a);
}
function seasonsOf(eps) { return new Set(eps.map(e => e.season).filter(s => s != null && s > 0)); }

function groupHTML(it) {
  const open = isOpen(it), isShow = it.type === 'show';
  const name = isShow ? showTitle(it.show) : it.name;
  let count;
  if (isShow) {
    const ns = seasonsOf(it.eps).size;
    count = [plural(it.eps.length, 'Folge', 'Folgen'), ns > 1 ? `aus ${ns} Staffeln` : '',
      yearsText(it.show.year, it.show.last_year)].filter(Boolean).join(' · ');
  } else {
    count = [plural(it.films.length, 'Film', 'Filme'), yearsText(it.key.minYear, it.key.maxYear)].filter(Boolean).join(' · ');
  }
  const head = `<div class="series-head">
    <button class="disclose" data-toggle="${esc(it.id)}" aria-expanded="${open}" title="${open ? 'Zuklappen' : 'Aufklappen'}">
      ${CHEVRON}<span class="series-badge">${isShow ? 'Serie' : 'Filmreihe'}</span>
      <h3>${esc(name)}</h3><span class="series-count">${esc(count)}</span>
    </button>
    ${isShow ? `<button class="btn small" data-show="${esc(it.id)}">Infos zur Serie</button>` : ''}
  </div>`;
  const body = open ? (isShow ? showBodyHTML(it) : `<div class="tiles">${it.films.map(tileHTML).join('')}</div>`) : peekHTML(it);
  return `<section class="series ${isShow ? 'is-show' : 'is-coll'}${open ? ' open' : ''}" data-group="${esc(it.id)}">${head}${body}</section>`;
}

function peekHTML(it) {
  if (it.type === 'show') {
    const sh = it.show;
    return `<button class="peek show-peek" data-toggle="${esc(it.id)}">
      <div class="poster mini">${showPosterHTML(sh, it.id)}</div>
      <div class="peek-text">${sh.overview ? `<p>${esc(short(sh.overview, 240))}</p>` : ''}
        <span class="peek-more">${CHEVRON} Alle ${plural(it.eps.length, 'Folge', 'Folgen')} anzeigen</span></div>
    </button>`;
  }
  return `<button class="peek" data-toggle="${esc(it.id)}">
    <div class="peek-strip">${it.films.slice(0, 14).map(f => `<div class="poster mini">${posterHTML(f)}</div>`).join('')}</div>
    <span class="peek-more">${CHEVRON} Alle ${it.films.length} Filme anzeigen</span>
  </button>`;
}

function showBodyHTML(it) {
  const bySeason = new Map();
  for (const e of it.eps) {
    const s = e.season ?? -1;
    if (!bySeason.has(s)) bySeason.set(s, []);
    bySeason.get(s).push(e);
  }
  const keys = [...bySeason.keys()].sort((a, b) => (a < 0 ? 999 : a === 0 ? 998 : a) - (b < 0 ? 999 : b === 0 ? 998 : b));
  const single = keys.length === 1 && keys[0] === -1;
  return keys.map(s => {
    const eps = bySeason.get(s);
    const title = s === -1 ? (single ? '' : 'Weitere Folgen') : s === 0 ? 'Specials' : `Staffel ${s}`;
    return (title ? `<h4 class="season-title">${title} <span>${plural(eps.length, 'Folge', 'Folgen')}</span></h4>` : '') +
      `<div class="tiles episodes">${eps.map(epTileHTML).join('')}</div>`;
  }).join('');
}

function emptyHTML() {
  const info = state.info;
  if (!info.library_path) {
    return `<div class="empty"><div class="big">🎬</div><h2>Willkommen in der Filmothek!</h2>
      <p>Bitte zuerst in den Einstellungen den Ordner mit den Filmen auswählen.</p>
      <button class="btn primary" data-action="settings">Einstellungen öffnen</button></div>`;
  }
  if (!info.drive_ok) {
    return `<div class="empty"><div class="big">💽</div><h2>Festplatte nicht gefunden</h2>
      <p>Bitte die Festplatte mit den Filmen anstecken und dann auf „Bibliothek aktualisieren“ klicken.</p>
      <button class="btn primary" data-action="scan">Bibliothek aktualisieren</button></div>`;
  }
  return `<div class="empty"><div class="big">🍿</div><h2>Noch keine Filme</h2>
    <p>Klicke auf „Bibliothek aktualisieren“, damit die Filme eingelesen werden.</p>
    <button class="btn primary" data-action="scan">Bibliothek aktualisieren</button></div>`;
}

function countText(list) {
  const movies = list.filter(f => f.kind !== 'episode').length;
  const shows = new Set(list.filter(f => f.kind === 'episode').map(f => f.show_key)).size;
  return [movies ? plural(movies, 'Film', 'Filme') : '', shows ? plural(shows, 'Serie', 'Serien') : ''].filter(Boolean).join(' · ');
}

function render() {
  const grid = $('#grid'), count = $('#count');
  if (!state.films.length) { grid.innerHTML = emptyHTML(); count.textContent = ''; return; }

  const { list, fuzzy } = filterFilms();
  if (!list.length) {
    count.textContent = '';
    grid.innerHTML = `<div class="empty"><div class="big">🔍</div><h2>Nichts gefunden</h2>
      <p>Zu „${esc(state.query)}“ gibt es leider keinen Film und keine Serie.</p>
      <button class="btn primary" data-action="clear">Suche löschen</button></div>`;
    return;
  }
  count.textContent = state.query ? `${countText(list)} zu „${state.query}“` : countText(list);

  const hint = fuzzy ? `<p class="fuzzy-hint">Keine genauen Treffer für „${esc(state.query)}“ – hier sind ähnliche Ergebnisse:</p>` : '';
  grid.innerHTML = hint + sections(buildItems(list)).map(sec =>
    (sec.title ? `<h2 class="section-title">${esc(sec.title)} <span>${sec.items.length}</span></h2>` : '') +
    `<div class="tiles">${sec.items.map(it => it.type === 'film' ? tileHTML(it.film) : groupHTML(it)).join('')}</div>`
  ).join('');
}

/* Kaputte Bilder durch Platzhalter ersetzen */
document.addEventListener('error', e => {
  const img = e.target;
  if (img.tagName === 'IMG' && img.dataset.hue) img.outerHTML = phHTML(img.dataset.title, img.dataset.hue);
}, true);

/* ======================= Detailansicht ======================= */
const PLAY_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5Z"/></svg>';

const chips = (list, title) => (list || []).length ? `<h2>${title}</h2><div class="chips">${
  list.map(x => `<button class="chip" data-search="${esc(x)}">${esc(x)}</button>`).join('')}</div>` : '';

function castHTML(cast) {
  return (cast || []).length ? `<h2>Darsteller</h2><ul class="cast">${
    cast.map(c => `<li><button data-search="${esc(c.name)}" title="Alles mit ${esc(c.name)}">
      <b>${esc(c.name)}</b>${c.role ? `<span>als ${esc(c.role)}</span>` : ''}</button></li>`).join('')}</ul>` : '';
}

function factsHTML(list) {
  return `<div class="facts">${list.filter(Boolean).join('')}</div>`;
}
const fact = (t, cls = '') => t ? `<span class="fact ${cls}">${esc(t)}</span>` : '';
const rating = r => r ? `★ ${String(r).replace('.', ',')}` : '';

function heroHTML(img, mode) {
  return mode === 'page' && img
    ? `<div class="hero" style="background-image:url('/bilder/${encodeURIComponent(img)}')"></div>` : '';
}

function fixHTML(open, label, attr, q, year) {
  return `<details class="fix"${open ? ' open' : ''}>
    <summary>${label}</summary>
    <form class="fix-form" ${attr}>
      <input name="q" value="${esc(q)}" placeholder="Titel">
      <input name="year" value="${esc(year || '')}" placeholder="Jahr" inputmode="numeric">
      <button class="btn" type="submit">Suchen</button>
    </form>
    <div class="picks"></div>
  </details>`;
}

function filmDetailHTML(f, mode) {
  const hero = heroHTML(f.backdrop, mode);
  const series = f.collection ? `<h2>Filmreihe</h2><div class="series-link">
      <span>Gehört zur Reihe <b>${esc(f.collection.name)}</b></span>
      <button class="btn" data-search="${esc(f.collection.name)}">Alle Filme der Reihe</button></div>` : '';
  const played = f.last_played ? `<p class="played-note">Zuletzt abgespielt am ${dateText(f.last_played * 1000)}.</p>` : '';
  const overview = f.overview
    ? `<p class="overview">${esc(f.overview)}</p>`
    : `<p class="overview muted">Zu diesem Film sind leider noch keine Infos vorhanden.</p>`;

  return `${hero}<div class="detail mode-${mode}${hero ? '' : ' nohero'}">
    <div class="detail-top">
      <div class="detail-poster"><div class="poster">${posterHTML(f)}</div></div>
      <div class="detail-head">
        <h1>${esc(f.title)}</h1>
        ${f.original_title && norm(f.original_title) !== norm(f.title) ? `<div class="orig">${esc(f.original_title)}</div>` : ''}
        ${factsHTML([fact(f.year), fact(runtimeText(f.runtime)), f.fsk && fact('FSK ' + f.fsk, 'fsk'), fact(rating(f.rating))])}
        <button class="play-btn" data-play="${f.id}">${PLAY_ICON}<span>Film abspielen</span></button>
        ${played}
      </div>
    </div>
    ${f.tagline ? `<p class="tagline">„${esc(f.tagline)}“</p>` : ''}
    <h2>Worum geht's?</h2>${overview}
    ${chips(f.genres, 'Kategorie')}${series}${chips(f.directors, 'Regie')}${castHTML(f.cast)}
    ${fixHTML(!f.matched, f.matched ? 'Falscher Film? Zuordnung korrigieren' : 'Keine Infos gefunden – hier den richtigen Film suchen',
      `data-fix="${f.id}"`, f.guess_title || f.title, f.guess_year)}
    <p class="fileinfo">Datei: ${esc(f.rel)}</p>
  </div>`;
}

function seasonEpText(e) {
  return [e.season > 0 ? `Staffel ${e.season}` : e.season === 0 ? 'Special' : '', e.episode != null ? `Folge ${e.episode}` : '']
    .filter(Boolean).join(', ');
}

function episodeDetailHTML(e, mode) {
  const sh = state.shows[e.show_key] || {};
  const eps = state.epsByShow.get(e.show_key) || [];
  const i = eps.indexOf(e), prev = eps[i - 1], next = eps[i + 1];
  const played = e.last_played ? `<p class="played-note">Zuletzt abgespielt am ${dateText(e.last_played * 1000)}.</p>` : '';
  const overview = e.ep_overview
    ? `<h2>Worum geht's in dieser Folge?</h2><p class="overview">${esc(e.ep_overview)}</p>`
    : sh.overview ? `<h2>Worum geht's in der Serie?</h2><p class="overview">${esc(sh.overview)}</p>`
    : `<h2>Worum geht's?</h2><p class="overview muted">Zu dieser Folge sind leider noch keine Infos vorhanden.</p>`;

  return `<div class="detail mode-${mode} nohero episode-detail">
    <div class="ep-top">
      <div class="poster wide ep-still">${stillHTML(e)}</div>
      <div class="detail-head">
        <div class="ep-show"><button class="linklike" data-show="${esc(e.show_key)}">${esc(showTitle(sh))}</button>
          ${seasonEpText(e) ? ` · ${esc(seasonEpText(e))}` : ''}</div>
        <h1>${esc(epTitle(e))}</h1>
        ${factsHTML([fact(dateText(e.air_date)), fact(runtimeText(e.runtime)), sh.fsk && fact('FSK ' + sh.fsk, 'fsk')])}
        <button class="play-btn" data-play="${e.id}">${PLAY_ICON}<span>Folge abspielen</span></button>
        ${played}
        <div class="ep-nav">
          ${prev ? `<button class="btn" data-id="${prev.id}" title="${esc(epLabel(prev))}">← Vorherige Folge</button>` : ''}
          ${next ? `<button class="btn" data-id="${next.id}" title="${esc(epLabel(next))}">Nächste Folge →</button>` : ''}
        </div>
      </div>
    </div>
    ${overview}
    <h2>Serie</h2><div class="series-link show-link">
      <span><b>${esc(showTitle(sh))}</b> – ${plural(eps.length, 'Folge', 'Folgen')} auf der Festplatte</span>
      <button class="btn" data-show="${esc(e.show_key)}">Infos zur Serie</button></div>
    ${castHTML(sh.cast)}
    <p class="fileinfo">Datei: ${esc(e.rel)}</p>
  </div>`;
}

function nextToWatch(eps) {
  const last = eps.filter(e => e.last_played).sort((a, b) => b.last_played - a.last_played)[0];
  if (!last) return { ep: eps[0], resume: false };
  const i = eps.indexOf(last);
  return { ep: eps[i + 1] || eps[0], resume: !!eps[i + 1] };
}

function showDetailHTML(key, mode) {
  const sh = state.shows[key] || { key };
  const eps = state.epsByShow.get(key) || [];
  const hero = heroHTML(sh.backdrop, mode);
  const ns = seasonsOf(eps).size;
  const { ep, resume } = nextToWatch(eps);
  const seasonsTxt = sh.number_of_seasons ? plural(sh.number_of_seasons, 'Staffel', 'Staffeln') : '';
  const overview = sh.overview
    ? `<p class="overview">${esc(sh.overview)}</p>`
    : `<p class="overview muted">Zu dieser Serie sind leider noch keine Infos vorhanden.</p>`;

  return `${hero}<div class="detail mode-${mode}${hero ? '' : ' nohero'}">
    <div class="detail-top">
      <div class="detail-poster"><div class="poster">${showPosterHTML(sh, key)}</div></div>
      <div class="detail-head">
        <div class="ep-show"><span class="series-badge">Serie</span></div>
        <h1>${esc(showTitle(sh))}</h1>
        ${sh.original_title && norm(sh.original_title) !== norm(showTitle(sh)) ? `<div class="orig">${esc(sh.original_title)}</div>` : ''}
        ${factsHTML([fact(yearsText(sh.year, sh.last_year)), fact(seasonsTxt), sh.fsk && fact('FSK ' + sh.fsk, 'fsk'), fact(rating(sh.rating))])}
        ${ep ? `<button class="play-btn" data-play="${ep.id}">${PLAY_ICON}<span>${resume ? 'Weiterschauen' : 'Erste Folge abspielen'}</span></button>
          <p class="played-note">${esc(seasonEpText(ep) || '')}${seasonEpText(ep) ? ': ' : ''}„${esc(epTitle(ep))}“</p>` : ''}
      </div>
    </div>
    ${sh.tagline ? `<p class="tagline">„${esc(sh.tagline)}“</p>` : ''}
    <h2>Worum geht's?</h2>${overview}
    <h2>Auf der Festplatte</h2><div class="series-link show-link">
      <span>${plural(eps.length, 'Folge', 'Folgen')}${ns > 1 ? ` aus ${ns} Staffeln` : ''}</span>
      <button class="btn" data-expand="${esc(key)}">Alle Folgen anzeigen</button></div>
    ${chips(sh.genres, 'Kategorie')}${chips(sh.creators, 'Idee')}${castHTML(sh.cast)}
    ${fixHTML(!sh.matched, sh.matched ? 'Falsche Serie? Zuordnung korrigieren' : 'Keine Infos gefunden – hier die richtige Serie suchen',
      `data-fix-show="${esc(key)}"`, sh.guess_name || showTitle(sh), sh.guess_year)}
  </div>`;
}

function refExists(ref) {
  return ref.type === 'show' ? state.epsByShow.has(ref.id) : state.byId.has(ref.id);
}

function detailHTML(ref, mode) {
  if (ref.type === 'show') return showDetailHTML(ref.id, mode);
  const f = state.byId.get(ref.id);
  return f.kind === 'episode' ? episodeDetailHTML(f, mode) : filmDetailHTML(f, mode);
}

function showDetail(ref, refreshOnly = false) {
  const same = state.openRef && state.openRef.type === ref.type && state.openRef.id === ref.id;
  state.openRef = ref;
  if (state.sidebar) {
    if (!$('#page').hidden) {
      $('#page').hidden = true; $('#main').hidden = false; window.scrollTo(0, state.pageScroll);
    }
    $('#drawerBody').innerHTML = detailHTML(ref, 'drawer');
    const d = $('#drawer');
    if (!(refreshOnly && same)) d.scrollTop = 0;
    d.classList.add('open'); d.setAttribute('aria-hidden', 'false');
    $('#scrim').hidden = false;
    document.body.classList.add('lock');
  } else {
    hideDrawer();
    if ($('#page').hidden) state.pageScroll = window.scrollY;
    $('#pageBody').innerHTML = detailHTML(ref, 'page');
    $('#main').hidden = true; $('#page').hidden = false;
    if (!(refreshOnly && same)) window.scrollTo(0, 0);
  }
}

function hideDrawer() {
  const d = $('#drawer');
  d.classList.remove('open'); d.setAttribute('aria-hidden', 'true');
  $('#scrim').hidden = true;
  document.body.classList.remove('lock');
}

function hideDetail() {
  state.openRef = null;
  hideDrawer();
  if (!$('#page').hidden) {
    $('#page').hidden = true; $('#main').hidden = false;
    window.scrollTo(0, state.pageScroll);
  }
}

/* Navigation über die Adresse (#film/… bzw. #serie/…), damit auch der Zurück-Knopf des Browsers geht */
function navigate(hash) {
  if (state.openRef) location.replace(hash);          // innerhalb der Detailansicht: kein neuer Verlaufseintrag
  else { state.pushed = true; location.hash = hash; }
}
const openFilm = id => navigate('#film/' + id);
const openShow = key => navigate('#serie/' + encodeURIComponent(key));
function closeDetail() {
  if (!state.openRef) return;
  if (state.pushed) { state.pushed = false; history.back(); }
  else { history.replaceState(null, '', location.pathname); route(); }
}
function route() {
  let m;
  if ((m = location.hash.match(/^#film\/(\w+)/)) && state.byId.has(m[1])) showDetail({ type: 'film', id: m[1] });
  else if ((m = location.hash.match(/^#serie\/(.+)$/)) && state.epsByShow.has(decodeURIComponent(m[1]))) {
    showDetail({ type: 'show', id: decodeURIComponent(m[1]) });
  } else { state.pushed = false; hideDetail(); }
}
window.addEventListener('hashchange', route);

/* ======================= Aktionen ======================= */
async function play(id, btn) {
  btn.disabled = true;
  try {
    await api('/api/play', { id });
    const f = state.byId.get(id);
    toast(f?.kind === 'episode' ? 'Die Folge wird in VLC geöffnet … Viel Vergnügen! 📺' : 'Der Film wird in VLC geöffnet … Viel Vergnügen! 🍿');
    if (f) {
      f.last_played = Date.now() / 1000;
      render();
      if (state.openRef) showDetail(state.openRef, true);
    }
  } catch (e) { toast(e.message, 'error', 8000); }
  setTimeout(() => { btn.disabled = false; }, 2500);
}

function setQuery(q) {
  state.query = q;
  state.tmpOpen = {};
  $('#search').value = q;
  $('#clearSearch').hidden = !q;
  render();
}

function toggleGroup(id) {
  const section = document.querySelector(`[data-group="${CSS.escape(id)}"]`);
  const wasOpen = section?.classList.contains('open');
  setOpen(id, !wasOpen);
  const top = section?.getBoundingClientRect().top;
  render();
  // Die Gruppe bleibt an derselben Stelle auf dem Bildschirm
  const again = document.querySelector(`[data-group="${CSS.escape(id)}"]`);
  if (again && top != null) window.scrollBy(0, again.getBoundingClientRect().top - top);
}

function expandAndShow(key) {
  setOpen(key, true);
  closeDetail();
  setTimeout(() => {
    render();
    const el = document.querySelector(`[data-group="${CSS.escape(key)}"]`);
    if (el) window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - $('#topbar').offsetHeight - 12 });
  }, 80);
}

async function searchTmdb(form) {
  const picks = form.nextElementSibling;
  const q = form.q.value.trim(), year = form.year.value.trim();
  const tv = !!form.dataset.fixShow;
  picks.innerHTML = '<p class="hint">Suche läuft …</p>';
  try {
    const { results } = await api(`/api/tmdb-search?kind=${tv ? 'tv' : 'movie'}&q=${encodeURIComponent(q)}&year=${encodeURIComponent(year)}`);
    const target = tv ? `data-target-show="${esc(form.dataset.fixShow)}"` : `data-target-film="${esc(form.dataset.fix)}"`;
    picks.innerHTML = results.length ? results.map(r => `<button class="pick" data-assign="${r.id}" ${target}>
        ${r.thumb ? `<img src="${esc(r.thumb)}" alt="">` : '<span class="noimg"></span>'}
        <span><b>${esc(r.title)}</b> ${r.year ? `(${esc(r.year)})` : ''}<br><small>${esc(r.original_title || '')}</small></span>
      </button>`).join('') : '<p class="hint">Nichts gefunden. Vielleicht mit dem Originaltitel probieren?</p>';
  } catch (e) { picks.innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}

async function assign(el) {
  const tmdb_id = Number(el.dataset.assign);
  const body = el.dataset.targetShow ? { show: el.dataset.targetShow, tmdb_id } : { id: el.dataset.targetFilm, tmdb_id };
  el.disabled = true;
  try {
    await api('/api/assign', body);
    await loadFilms();
    toast('Danke, die Infos wurden aktualisiert.');
  } catch (e) { toast(e.message, 'error'); el.disabled = false; }
}

/* ======================= Bibliothek einlesen ======================= */
async function startScan() {
  try { await api('/api/scan', {}); } catch (e) { return toast(e.message, 'error'); }
  pollStatus();
}

async function pollStatus() {
  if (state.polling) return;
  state.polling = true;
  let tick = 0;
  const btn = $('#scanBtn'), label = $('#scanLabel'), bar = $('#progress'), fill = $('#progressFill');
  try {
    for (;;) {
      const s = await api('/api/status');
      if (!s.running) {
        btn.disabled = false; btn.classList.remove('spinning');
        label.textContent = 'Bibliothek aktualisieren'; bar.hidden = true;
        if (tick > 0 || s.finished_at && Date.now() / 1000 - s.finished_at < 5) {
          await loadFilms();
          if (s.error) toast((s.message ? s.message + '\n' : '') + s.error, 'error', 9000);
          else if (s.message && tick > 0) toast(s.message);
        }
        break;
      }
      btn.disabled = true; btn.classList.add('spinning'); bar.hidden = false;
      if (s.total) {
        label.textContent = `Lade Infos … ${s.done} / ${s.total}`;
        bar.classList.remove('indeterminate'); fill.style.width = (100 * s.done / s.total) + '%';
      } else {
        label.textContent = 'Suche Filme …';
        bar.classList.add('indeterminate'); fill.style.width = '';
      }
      if (tick % 4 === 3) await loadFilms();
      tick++;
      await new Promise(r => setTimeout(r, 1000));
    }
  } catch (e) {
    toast(e.message, 'error');
  } finally { state.polling = false; }
}

/* ======================= Einstellungen ======================= */
async function openSettings() {
  const dlg = $('#settings'), msg = $('#cfgMsg');
  msg.textContent = ''; msg.className = 'form-msg';
  try {
    const cfg = await api('/api/config');
    $('#cfgPath').value = cfg.library_path || '';
    $('#cfgKey').value = cfg.tmdb_key || '';
    $('#chooseFolder').hidden = cfg.platform !== 'darwin';
    $('#volumes').innerHTML = (cfg.volumes || []).map(v =>
      `<button type="button" class="chip" data-vol="${esc(v.path)}">${esc(v.name)}</button>`).join('');
  } catch (e) { msg.textContent = e.message; }
  dlg.showModal();
}

async function saveSettings(scan) {
  const msg = $('#cfgMsg');
  try {
    const r = await api('/api/config', { library_path: $('#cfgPath').value, tmdb_key: $('#cfgKey').value });
    if (!r.folder_exists) {
      msg.className = 'form-msg';
      msg.textContent = 'Gespeichert – aber diesen Ordner gibt es gerade nicht. Ist die Festplatte angesteckt?';
      await loadFilms();
      return;
    }
    $('#settings').close();
    await loadFilms();
    if (scan) startScan(); else toast('Einstellungen gespeichert.');
  } catch (e) { msg.className = 'form-msg'; msg.textContent = e.message; }
}

/* ======================= Ereignisse ======================= */
function init() {
  state.sort = store.get('sort', 'title');
  state.series = store.get('series', true);
  state.sidebar = store.get('sidebar', true);
  state.open = store.get('open', {});
  $('#sort').value = state.sort;
  $('#series').checked = state.series;
  $('#sidebar').checked = state.sidebar;

  // Höhe der Bedienleiste merken (für Seitenleiste & Zurück-Leiste)
  const setTop = () => document.documentElement.style.setProperty('--top-h', $('#topbar').offsetHeight + 'px');
  new ResizeObserver(setTop).observe($('#topbar')); setTop();

  let t;
  $('#search').addEventListener('input', e => {
    clearTimeout(t);
    $('#clearSearch').hidden = !e.target.value;
    t = setTimeout(() => { state.query = e.target.value; state.tmpOpen = {}; render(); }, 180);
  });
  $('#search').addEventListener('keydown', e => { if (e.key === 'Escape') setQuery(''); });
  $('#clearSearch').addEventListener('click', () => { setQuery(''); $('#search').focus(); });
  $('#sort').addEventListener('change', e => { state.sort = e.target.value; store.set('sort', state.sort); render(); });
  $('#series').addEventListener('change', e => { state.series = e.target.checked; store.set('series', state.series); render(); });
  $('#sidebar').addEventListener('change', e => {
    state.sidebar = e.target.checked; store.set('sidebar', state.sidebar);
    if (state.openRef) { hideDrawer(); if (!state.sidebar) state.pageScroll = window.scrollY; showDetail(state.openRef); }
  });
  $('#logo').addEventListener('click', e => { e.preventDefault(); closeDetail(); setQuery(''); window.scrollTo(0, 0); });
  $('#scanBtn').addEventListener('click', startScan);
  $('#settingsBtn').addEventListener('click', openSettings);

  $('#cfgCancel').addEventListener('click', () => $('#settings').close());
  $('#cfgSave').addEventListener('click', () => saveSettings(false));
  $('#cfgSaveScan').addEventListener('click', () => saveSettings(true));
  $('#chooseFolder').addEventListener('click', async () => {
    try { const r = await api('/api/choose-folder', {}); if (r.path) $('#cfgPath').value = r.path; }
    catch (e) { $('#cfgMsg').textContent = e.message; }
  });

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-id],[data-play],[data-search],[data-close],[data-action],[data-assign],[data-vol],[data-toggle],[data-show],[data-expand]');
    if (!el) return;
    if (el.dataset.play) return play(el.dataset.play, el);
    if (el.dataset.assign) return assign(el);
    if (el.dataset.vol) { $('#cfgPath').value = el.dataset.vol; return; }
    if (el.dataset.toggle) return toggleGroup(el.dataset.toggle);
    if (el.dataset.expand) return expandAndShow(el.dataset.expand);
    if (el.dataset.show) return openShow(el.dataset.show);
    if (el.dataset.search) { closeDetail(); setQuery(el.dataset.search); window.scrollTo(0, 0); return; }
    if (el.hasAttribute('data-close')) return closeDetail();
    if (el.dataset.action === 'settings') return openSettings();
    if (el.dataset.action === 'scan') return startScan();
    if (el.dataset.action === 'clear') return setQuery('');
    if (el.dataset.id) return openFilm(el.dataset.id);
  });
  document.addEventListener('submit', e => {
    if (e.target.dataset.fix || e.target.dataset.fixShow) { e.preventDefault(); searchTmdb(e.target); }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && state.openRef && !$('#settings').open) closeDetail();
  });

  loadFilms().then(() => {
    route();
    pollStatus();
  }).catch(e => toast(e.message, 'error', 10000));
}

init();
