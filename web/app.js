'use strict';

/* ======================= Grundlagen ======================= */
const $ = (s, el = document) => el.querySelector(s);
const state = {
  films: [], byId: new Map(), info: {},
  query: '', sort: 'title', series: true, sidebar: true,
  openId: null, pageScroll: 0, pushed: false, polling: false,
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

function runtimeText(min) {
  if (!min) return '';
  const h = Math.floor(min / 60), m = min % 60;
  return h ? `${h} Std. ${m ? m + ' Min.' : ''}`.trim() : `${m} Min.`;
}

let toastTimer;
function toast(msg, type = '', ms = 4500) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast ' + type; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

/* ======================= Daten laden ======================= */
function prepare(f) {
  const cast = (f.cast || []).map(c => c.name).join(' ');
  f._hay = norm([f.title, f.original_title, f.guess_title, (f.directors || []).join(' '), cast,
    (f.keywords || []).join(' '), (f.genres || []).join(' '), f.collection?.name, f.tagline,
    f.overview, f.year, f.file].join(' '));
  return f;
}

async function loadFilms() {
  const data = await api('/api/films');
  state.info = data;
  state.films = data.films.map(prepare);
  state.byId = new Map(state.films.map(f => [f.id, f]));
  render();
  if (state.openId && state.byId.has(state.openId)) showDetail(state.byId.get(state.openId), true);
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

/* Liefert Einträge: {film} oder {group:true, name, films} */
function arrange(films, sort) {
  const cmp = comparator(sort);
  if (!state.series) return films.slice().sort(cmp).map(film => ({ film }));
  const groups = new Map(), items = [];
  for (const f of films) {
    if (f.collection) {
      if (!groups.has(f.collection.id)) groups.set(f.collection.id, []);
      groups.get(f.collection.id).push(f);
    } else items.push({ film: f, key: f });
  }
  for (const arr of groups.values()) {
    if (arr.length < 2) { items.push({ film: arr[0], key: arr[0] }); continue; }
    arr.sort((a, b) => (a.year || 9999) - (b.year || 9999) || collator.compare(a.title, b.title));
    const years = arr.map(f => f.year).filter(Boolean);
    items.push({
      group: true, name: arr[0].collection.name, films: arr,
      key: {
        title: arr[0].collection.name,
        year: sort === 'year_desc' ? Math.max(0, ...years) : Math.min(9999, ...years),
        added: Math.max(...arr.map(f => f.added || 0)),
      },
    });
  }
  return items.sort((a, b) => cmp(a.key, b.key));
}

function sections(films) {
  if (state.sort !== 'genre') return [{ title: null, films }];
  // Eine Filmreihe bleibt in der Kategorie ihres ersten Films beisammen
  const seriesGenre = new Map();
  if (state.series) {
    films.slice().sort((a, b) => (a.year || 9999) - (b.year || 9999)).forEach(f => {
      if (f.collection && !seriesGenre.has(f.collection.id)) seriesGenre.set(f.collection.id, f.genres?.[0]);
    });
  }
  const map = new Map();
  for (const f of films) {
    const g = (f.collection && seriesGenre.get(f.collection.id)) || f.genres?.[0] || 'Ohne Kategorie';
    if (!map.has(g)) map.set(g, []);
    map.get(g).push(f);
  }
  return [...map.entries()]
    .sort(([a], [b]) => (a === 'Ohne Kategorie') - (b === 'Ohne Kategorie') || collator.compare(a, b))
    .map(([title, list]) => ({ title, films: list }));
}

/* ======================= Übersicht zeichnen ======================= */
function posterHTML(f) {
  const ph = `<div class="ph" style="--h:${hue(f.id)}"><span>${esc(f.title)}</span></div>`;
  return f.poster ? `<img src="/bilder/${encodeURIComponent(f.poster)}" alt="" loading="lazy" data-title="${esc(f.title)}" data-hue="${hue(f.id)}">` : ph;
}

function tileHTML(f) {
  const meta = [f.year, runtimeText(f.runtime)].filter(Boolean).join(' · ');
  return `<button class="tile${f.last_played ? ' played' : ''}" data-id="${f.id}">
    <div class="poster">${posterHTML(f)}</div>
    <div class="tile-title">${esc(f.title)}</div>
    <div class="tile-meta">${esc(meta) || '&nbsp;'}</div>
  </button>`;
}

function groupHTML(g) {
  return `<section class="series">
    <div class="series-head"><span class="series-badge">Filmreihe</span><h3>${esc(g.name)}</h3>
      <span class="series-count">${g.films.length} Filme</span></div>
    <div class="tiles">${g.films.map(tileHTML).join('')}</div>
  </section>`;
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

function render() {
  const grid = $('#grid'), count = $('#count');
  if (!state.films.length) { grid.innerHTML = emptyHTML(); count.textContent = ''; return; }

  const words = norm(state.query).split(/\s+/).filter(Boolean);
  const list = words.length ? state.films.filter(f => words.every(w => f._hay.includes(w))) : state.films;

  if (!list.length) {
    count.textContent = '';
    grid.innerHTML = `<div class="empty"><div class="big">🔍</div><h2>Nichts gefunden</h2>
      <p>Zu „${esc(state.query)}“ gibt es leider keinen Film.</p>
      <button class="btn primary" data-action="clear">Suche löschen</button></div>`;
    return;
  }
  count.textContent = words.length
    ? `${list.length} von ${state.films.length} Filmen passen zu „${state.query}“`
    : `${state.films.length} Filme`;

  grid.innerHTML = sections(list).map(sec =>
    (sec.title ? `<h2 class="section-title">${esc(sec.title)} <span>${sec.films.length}</span></h2>` : '') +
    `<div class="tiles">${arrange(sec.films, state.sort === 'genre' ? 'title' : state.sort)
      .map(it => it.group ? groupHTML(it) : tileHTML(it.film)).join('')}</div>`
  ).join('');
}

/* Kaputte Cover durch Platzhalter ersetzen */
document.addEventListener('error', e => {
  const img = e.target;
  if (img.tagName === 'IMG' && img.dataset.hue) {
    img.outerHTML = `<div class="ph" style="--h:${img.dataset.hue}"><span>${esc(img.dataset.title)}</span></div>`;
  }
}, true);

/* ======================= Detailansicht ======================= */
const PLAY_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5Z"/></svg>';

function detailHTML(f, mode) {
  const facts = [
    f.year && `<span class="fact">${f.year}</span>`,
    f.runtime && `<span class="fact">${runtimeText(f.runtime)}</span>`,
    f.fsk && `<span class="fact fsk">FSK ${esc(f.fsk)}</span>`,
    f.rating && `<span class="fact">★ ${String(f.rating).replace('.', ',')}</span>`,
  ].filter(Boolean).join('');

  const hero = mode === 'page' && f.backdrop
    ? `<div class="hero" style="background-image:url('/bilder/${encodeURIComponent(f.backdrop)}')"></div>` : '';

  const genres = (f.genres || []).length ? `<h2>Kategorie</h2><div class="chips">${
    f.genres.map(g => `<button class="chip" data-search="${esc(g)}">${esc(g)}</button>`).join('')}</div>` : '';

  const directors = (f.directors || []).length ? `<h2>Regie</h2><div class="chips">${
    f.directors.map(d => `<button class="chip" data-search="${esc(d)}">${esc(d)}</button>`).join('')}</div>` : '';

  const series = f.collection ? `<h2>Filmreihe</h2><div class="series-link">
      <span>Gehört zur Reihe <b>${esc(f.collection.name)}</b></span>
      <button class="btn" data-search="${esc(f.collection.name)}">Alle Filme der Reihe</button></div>` : '';

  const cast = (f.cast || []).length ? `<h2>Darsteller</h2><ul class="cast">${
    f.cast.map(c => `<li><button data-search="${esc(c.name)}" title="Alle Filme mit ${esc(c.name)}">
      <b>${esc(c.name)}</b>${c.role ? `<span>als ${esc(c.role)}</span>` : ''}</button></li>`).join('')}</ul>` : '';

  const played = f.last_played ? `<p class="played-note">Zuletzt abgespielt am ${
    new Date(f.last_played * 1000).toLocaleDateString('de-DE', { day: 'numeric', month: 'long', year: 'numeric' })}.</p>` : '';

  const overview = f.overview
    ? `<p class="overview">${esc(f.overview)}</p>`
    : `<p class="overview" style="color:var(--muted)">Zu diesem Film sind leider noch keine Infos vorhanden.</p>`;

  return `${hero}<div class="detail mode-${mode}${hero ? '' : ' nohero'}">
    <div class="detail-top">
      <div class="detail-poster"><div class="poster">${posterHTML(f)}</div></div>
      <div class="detail-head">
        <h1>${esc(f.title)}</h1>
        ${f.original_title && norm(f.original_title) !== norm(f.title) ? `<div class="orig">${esc(f.original_title)}</div>` : ''}
        <div class="facts">${facts}</div>
        <button class="play-btn" data-play="${f.id}">${PLAY_ICON}<span>Film abspielen</span></button>
        ${played}
      </div>
    </div>
    ${f.tagline ? `<p class="tagline">„${esc(f.tagline)}“</p>` : ''}
    <h2>Worum geht's?</h2>${overview}
    ${genres}${series}${directors}${cast}
    <details class="fix"${f.matched ? '' : ' open'}>
      <summary>${f.matched ? 'Falscher Film? Zuordnung korrigieren' : 'Keine Infos gefunden – hier den richtigen Film suchen'}</summary>
      <form class="fix-form" data-fix="${f.id}">
        <input name="q" value="${esc(f.guess_title || f.title)}" placeholder="Filmtitel">
        <input name="year" value="${esc(f.guess_year || '')}" placeholder="Jahr" inputmode="numeric">
        <button class="btn" type="submit">Suchen</button>
      </form>
      <div class="picks"></div>
    </details>
    <p class="fileinfo">Datei: ${esc(f.rel)}</p>
  </div>`;
}

function showDetail(f, refreshOnly = false) {
  state.openId = f.id;
  if (state.sidebar) {
    if (!$('#page').hidden) {
      $('#page').hidden = true; $('#main').hidden = false; window.scrollTo(0, state.pageScroll);
    }
    $('#drawerBody').innerHTML = detailHTML(f, 'drawer');
    const d = $('#drawer');
    if (!refreshOnly) d.scrollTop = 0;
    d.classList.add('open'); d.setAttribute('aria-hidden', 'false');
    $('#scrim').hidden = false;
    document.body.classList.add('lock');
  } else {
    hideDrawer();
    if ($('#page').hidden) state.pageScroll = window.scrollY;
    $('#pageBody').innerHTML = detailHTML(f, 'page');
    $('#main').hidden = true; $('#page').hidden = false;
    if (!refreshOnly) window.scrollTo(0, 0);
  }
}

function hideDrawer() {
  const d = $('#drawer');
  d.classList.remove('open'); d.setAttribute('aria-hidden', 'true');
  $('#scrim').hidden = true;
  document.body.classList.remove('lock');
}

function hideDetail() {
  state.openId = null;
  hideDrawer();
  if (!$('#page').hidden) {
    $('#page').hidden = true; $('#main').hidden = false;
    window.scrollTo(0, state.pageScroll);
  }
}

/* Navigation über die Adresse (#film/…), damit auch der Zurück-Knopf des Browsers geht */
function openFilm(id) { state.pushed = true; location.hash = 'film/' + id; }
function closeFilm() {
  if (!state.openId) return;
  if (state.pushed) { state.pushed = false; history.back(); }
  else { history.replaceState(null, '', location.pathname); route(); }
}
function route() {
  const m = location.hash.match(/^#film\/(\w+)/);
  const f = m && state.byId.get(m[1]);
  if (f) showDetail(f); else { state.pushed = false; hideDetail(); }
}
window.addEventListener('hashchange', route);

/* ======================= Aktionen ======================= */
async function play(id, btn) {
  btn.disabled = true;
  try {
    await api('/api/play', { id });
    toast('Der Film wird in VLC geöffnet … Viel Vergnügen! 🍿');
    const f = state.byId.get(id);
    if (f) f.last_played = Date.now() / 1000;
  } catch (e) { toast(e.message, 'error', 8000); }
  setTimeout(() => { btn.disabled = false; }, 2500);
}

function setQuery(q) {
  state.query = q;
  $('#search').value = q;
  $('#clearSearch').hidden = !q;
  render();
}

async function searchTmdb(form) {
  const picks = form.nextElementSibling;
  const q = form.q.value.trim(), year = form.year.value.trim();
  picks.innerHTML = '<p class="hint">Suche läuft …</p>';
  try {
    const { results } = await api(`/api/tmdb-search?q=${encodeURIComponent(q)}&year=${encodeURIComponent(year)}`);
    picks.innerHTML = results.length ? results.map(r => `<button class="pick" data-assign="${r.id}" data-film="${form.dataset.fix}">
        ${r.thumb ? `<img src="${esc(r.thumb)}" alt="">` : '<span class="noimg"></span>'}
        <span><b>${esc(r.title)}</b> ${r.year ? `(${esc(r.year)})` : ''}<br><small>${esc(r.original_title || '')}</small></span>
      </button>`).join('') : '<p class="hint">Nichts gefunden. Vielleicht mit dem Originaltitel probieren?</p>';
  } catch (e) { picks.innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}

async function assign(filmId, tmdbId) {
  try {
    const { film } = await api('/api/assign', { id: filmId, tmdb_id: tmdbId });
    const f = prepare(Object.assign(state.byId.get(filmId), film));
    render(); showDetail(f, true);
    toast('Danke, die Infos wurden aktualisiert.');
  } catch (e) { toast(e.message, 'error'); }
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
    t = setTimeout(() => { state.query = e.target.value; render(); }, 150);
  });
  $('#search').addEventListener('keydown', e => { if (e.key === 'Escape') setQuery(''); });
  $('#clearSearch').addEventListener('click', () => { setQuery(''); $('#search').focus(); });
  $('#sort').addEventListener('change', e => { state.sort = e.target.value; store.set('sort', state.sort); render(); });
  $('#series').addEventListener('change', e => { state.series = e.target.checked; store.set('series', state.series); render(); });
  $('#sidebar').addEventListener('change', e => {
    state.sidebar = e.target.checked; store.set('sidebar', state.sidebar);
    if (state.openId) { const f = state.byId.get(state.openId); hideDrawer(); if (!state.sidebar) state.pageScroll = window.scrollY; showDetail(f); }
  });
  $('#logo').addEventListener('click', e => { e.preventDefault(); closeFilm(); setQuery(''); window.scrollTo(0, 0); });
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
    const el = e.target.closest('[data-id],[data-play],[data-search],[data-close],[data-action],[data-assign],[data-vol]');
    if (!el) return;
    if (el.dataset.play) return play(el.dataset.play, el);
    if (el.dataset.assign) return assign(el.dataset.film, Number(el.dataset.assign));
    if (el.dataset.vol) { $('#cfgPath').value = el.dataset.vol; return; }
    if (el.dataset.search) { closeFilm(); setQuery(el.dataset.search); window.scrollTo(0, 0); return; }
    if (el.hasAttribute('data-close')) return closeFilm();
    if (el.dataset.action === 'settings') return openSettings();
    if (el.dataset.action === 'scan') return startScan();
    if (el.dataset.action === 'clear') return setQuery('');
    if (el.dataset.id) return openFilm(el.dataset.id);
  });
  document.addEventListener('submit', e => {
    if (e.target.dataset.fix) { e.preventDefault(); searchTmdb(e.target); }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && state.openId && !$('#settings').open) closeFilm();
  });

  loadFilms().then(() => {
    route();
    pollStatus();
  }).catch(e => toast(e.message, 'error', 10000));
}

init();
