/* Dashboard and presentation controls. No model calls or external requests. */
(() => {
  'use strict';
  const root = document.documentElement;
  root.classList.add('js');
  const read = key => { try { return localStorage.getItem('kiwm-ui-' + key); } catch { return null; } };
  const write = (key, value) => { try { localStorage.setItem('kiwm-ui-' + key, value); } catch {} };
  const normalizeSize = value => {
    const percent = Number(value);
    return Number.isInteger(percent) && percent >= 50 && percent <= 150 && percent % 10 === 0 ? percent : 100;
  };
  const applySize = value => root.style.setProperty('--ui-size', `${16 * normalizeSize(value) / 100}px`);
  root.dataset.theme = read('theme') === 'dark' ? 'dark' : 'light';
  // App preference wins over OS/browser hints, including when storage is blocked.
  root.dataset.motion = read('motion') === 'off' ? 'off' : 'on';
  const size = read('size');
  applySize(size);

  // Keep the existing keyboard shortcut separate from the breadcrumb links.
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || !['Escape','Backspace'].includes(event.key)) return;
    if (document.querySelector('dialog[open],[role=dialog][aria-modal=true]')) return;
    const dropdown = document.querySelector('[data-tag-dropdown][open]');
    if (event.key === 'Escape' && dropdown) {
      dropdown.open = false; dropdown.querySelector('summary')?.focus(); event.preventDefault(); return;
    }
    const target = event.target instanceof Element ? event.target : document.activeElement;
    if (target?.closest('input,textarea,select,[contenteditable]:not([contenteditable=false]),[role=textbox]')) return;
    if (!document.body || document.body.classList.contains('page-home')) return;
    const path = document.querySelectorAll('.breadcrumbs a');
    if (!path.length) return;
    event.preventDefault();
    const projectPage = ['page-project','page-edit','page-site_access','page-run','page-document'].some(name => document.body.classList.contains(name));
    location.assign(path[projectPage ? 0 : Math.max(0, path.length - 2)].href);
  });

  document.addEventListener('DOMContentLoaded', () => {
    // Use elapsed monotonic time from the server timestamp, not the PC clock.
    // A fresh status response also corrects this anchor after a sleeping tab.
    let serverClock = Date.parse(document.body.dataset.serverNow || '');
    let serverClockAt = performance.now();
    const estimatedServerNow = () => serverClock + performance.now() - serverClockAt;
    const sync = () => {
      document.querySelectorAll('[data-theme-toggle]').forEach(button => {
        const dark = root.dataset.theme === 'dark';
        button.setAttribute('aria-pressed', String(dark));
        button.setAttribute('aria-label', dark ? 'Helle Darstellung einschalten' : 'Dunkle Darstellung einschalten');
        button.title = dark ? 'Helle Darstellung' : 'Dunkle Darstellung';
        const use = button.querySelector('use'); if (use) use.setAttribute('href', dark ? '#i-sun' : '#i-moon');
      });
      document.querySelectorAll('[data-motion-toggle]').forEach(button => {
        const on = root.dataset.motion === 'on';
        button.setAttribute('aria-pressed', String(on));
        button.setAttribute('aria-label', on ? 'Animationen ausschalten' : 'Animationen einschalten');
        button.title = button.getAttribute('aria-label');
        button.disabled = false;
        const label = button.querySelector('span'); if (label) label.textContent = on ? 'Animationen an' : 'Animationen aus';
      });
      document.querySelectorAll('[data-ui-size]').forEach(select => select.value = String(normalizeSize(read('size'))));
    };
    document.querySelectorAll('[data-theme-toggle]').forEach(button => button.addEventListener('click', () => {
      root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark'; write('theme', root.dataset.theme); sync();
    }));
    document.querySelectorAll('[data-motion-toggle]').forEach(button => button.addEventListener('click', () => {
      root.dataset.motion = root.dataset.motion === 'on' ? 'off' : 'on'; write('motion', root.dataset.motion); sync();
    }));
    document.querySelectorAll('[data-ui-size]').forEach(select => select.addEventListener('change', () => {
      const value = String(normalizeSize(select.value));
      applySize(value); write('size', value); sync();
    }));
    sync();

    const maintenanceTable = document.querySelector('[data-maintenance-sortable]');
    if (maintenanceTable) {
      const collator = new Intl.Collator('de', {numeric: true, sensitivity: 'base'});
      const rows = [...maintenanceTable.querySelectorAll('[data-maintenance-project]')];
      const compareNames = (a, b) => collator.compare(a.cells[0].dataset.sortValue, b.cells[0].dataset.sortValue);
      const sortRows = (column, ascending) => {
        const order = ascending ? 1 : -1;
        rows.sort((a, b) => {
          if (column === 0) return order * compareNames(a, b);
          const difference = Number(a.cells[column].dataset.sortValue) - Number(b.cells[column].dataset.sortValue);
          return order * difference || compareNames(a, b);
        });
        if (rows.length) maintenanceTable.tBodies[0].append(...rows);
      };
      sortRows(0, true);
      maintenanceTable.querySelectorAll('[data-maintenance-sort]').forEach(button => button.addEventListener('click', () => {
        const header = button.closest('th');
        const active = maintenanceTable.querySelector('th[aria-sort]');
        const ascending = active === header ? header.getAttribute('aria-sort') === 'descending' : header.cellIndex === 0;
        active?.removeAttribute('aria-sort');
        header.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
        sortRows(header.cellIndex, ascending);
      }));
    }

    // Search only document metadata on the server; leave the search field in
    // place so debounced result updates never steal focus or move its caret.
    const filesForm = document.querySelector('[data-files-search]');
    if (filesForm && window.fetch && window.AbortController) {
      const query = filesForm.querySelector('[data-files-query]');
      const status = document.querySelector('[data-files-request-status]');
      let pending, timer, serial = 0;
      const formURL = () => {
        const url = new URL(filesForm.action, location.href);
        url.search = new URLSearchParams(new FormData(filesForm)).toString();
        return url;
      };
      const showStatus = text => { if (status) { status.textContent = text; status.hidden = !text; } };
      const loadFiles = async (value, historyMode = 'replace') => {
        const url = new URL(value, location.href);
        if (url.origin !== location.origin || url.pathname !== '/files') return;
        clearTimeout(timer); pending?.abort(); pending = new AbortController();
        const request = ++serial;
        const current = document.querySelector('[data-files-results]');
        current?.setAttribute('aria-busy', 'true'); showStatus('Dateien werden gesucht …');
        try {
          const response = await fetch(url.href, {signal: pending.signal, credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}});
          if (!response.ok) throw new Error('Dateisuche fehlgeschlagen');
          const parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
          const replacement = parsed.querySelector('[data-files-results]');
          if (!replacement) throw new Error('Dateisuche ohne Ergebnisansicht');
          if (request !== serial) return;
          const focusedSort = document.activeElement?.closest('[data-files-sort]')?.dataset.filesSortKey;
          current?.replaceWith(document.importNode(replacement, true));
          for (const name of ['sort', 'direction']) {
            const fresh = parsed.querySelector(`[data-files-search] [name="${name}"]`);
            if (fresh && filesForm.elements.namedItem(name)) filesForm.elements.namedItem(name).value = fresh.value;
          }
          if (focusedSort) document.querySelector(`[data-files-sort-key="${focusedSort}"]`)?.focus({preventScroll: true});
          if (historyMode === 'push') history.pushState(null, '', url);
          else if (historyMode === 'replace') history.replaceState(null, '', url);
          showStatus('');
        } catch (error) {
          if (error.name !== 'AbortError' && request === serial) showStatus('Dateisuche nicht erreichbar. Bitte erneut suchen.');
        } finally {
          if (request === serial) document.querySelector('[data-files-results]')?.removeAttribute('aria-busy');
        }
      };
      filesForm.addEventListener('submit', event => { event.preventDefault(); loadFiles(formURL(), 'push'); });
      query?.addEventListener('input', event => {
        if (event.isComposing) return;
        clearTimeout(timer); pending?.abort(); serial++;
        timer = setTimeout(() => loadFiles(formURL()), 320);
      });
      query?.addEventListener('compositionend', () => { clearTimeout(timer); timer = setTimeout(() => loadFiles(formURL()), 320); });
      document.addEventListener('click', event => {
        const link = event.target.closest('[data-files-sort]');
        if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        const url = new URL(link.href); if (query) url.searchParams.set('q', query.value);
        event.preventDefault(); loadFiles(url, 'push');
      });
      window.addEventListener('popstate', () => {
        const url = new URL(location.href);
        if (url.pathname !== '/files') return location.reload();
        if (query) query.value = url.searchParams.get('q') || '';
        loadFiles(url, 'none');
      });
    }

    const sidebar = document.querySelector('.sidebar');
    const menu = document.querySelector('[data-menu-toggle]');
    if (sidebar && menu) {
      sidebar.dataset.collapsed = 'true';
      menu.addEventListener('click', () => {
        const expanded = sidebar.dataset.collapsed === 'true';
        sidebar.dataset.collapsed = String(!expanded); menu.setAttribute('aria-expanded', String(expanded));
      });
    }
    document.querySelectorAll('[data-color-field]').forEach(field => {
      const picker = field.querySelector('[data-color-picker]');
      const colorText = field.querySelector('[data-color-text]');
      picker?.addEventListener('input', () => { if (colorText) colorText.value = picker.value; });
      colorText?.addEventListener('input', () => { if (picker && /^#[0-9a-f]{6}$/i.test(colorText.value)) picker.value = colorText.value; });
    });

    // Color dice: each swatch contrasts at least 3:1 with both theme surfaces.
    const luminance = rgb => rgb.map(c => { c /= 255; return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4; }).reduce((v,c,i) => v + c * [.2126,.7152,.0722][i], 0);
    const contrast = (a,b) => (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
    const randomColor = previous => {
      const dark = luminance([38,38,38]);
      for (let attempt=0; attempt<200; attempt++) {
        const random = new Uint32Array(3); crypto.getRandomValues(random);
        const h=random[0]%360, s=(55+random[1]%24)/100, l=(40+random[2]%16)/100;
        const a=s*Math.min(l,1-l), f=n => { const k=(n+h/30)%12; return Math.round(255*(l-a*Math.max(-1,Math.min(k-3,9-k,1)))); };
        const rgb=[f(0),f(8),f(4)], value='#'+rgb.map(c=>c.toString(16).padStart(2,'0')).join('');
        if(value!==previous && contrast(luminance(rgb),1)>=3 && contrast(luminance(rgb),dark)>=3) return value;
      }
      return previous === '#596fda' ? '#b96939' : '#596fda';
    };
    document.querySelectorAll('[data-random-color]').forEach(button=>button.addEventListener('click',()=>{
      const field=button.closest('[data-color-field]'), picker=field.querySelector('[data-color-picker]'), text=field.querySelector('[data-color-text]');
      const value=randomColor(text.value.toLowerCase()); picker.value=text.value=value;
      text.dispatchEvent(new Event('input',{bubbles:true})); text.dispatchEvent(new Event('change',{bubbles:true}));
      button.classList.remove('is-rolling'); void button.offsetWidth; button.classList.add('is-rolling'); setTimeout(()=>button.classList.remove('is-rolling'),400);
    }));
    document.querySelectorAll('[data-icon-editor]').forEach(editor=>{
      const form=editor.closest('form'), csrf=form.querySelector('[name=csrf]').value;
      const preview=editor.querySelector('[data-icon-preview]'), payload=editor.querySelector('[data-icon-payload]'), action=editor.querySelector('[data-icon-action]');
      const status=editor.parentElement.querySelector('[data-icon-status]'), input=editor.querySelector('[data-icon-file]'), url=editor.querySelector('[name=url]');
      const buttons=[...editor.querySelectorAll('button')]; let objectUrl;
      const show = icon => {
        if(objectUrl) URL.revokeObjectURL(objectUrl);
        const bytes=Uint8Array.from(atob(icon.data),c=>c.charCodeAt(0)); objectUrl=URL.createObjectURL(new Blob([bytes],{type:icon.media}));
        const image=document.createElement('img');image.src=objectUrl;image.alt='Projekticon';preview.replaceChildren(image);
        payload.value=JSON.stringify(icon);action.value='';payload.dispatchEvent(new Event('change',{bubbles:true}));
        status.textContent=icon.mode==='manual'?'Eigenes Icon · bleibt bei Prüfungen erhalten.':'Favicon geladen · wird mit dem Projekt gespeichert.';
      };
      const request = async (endpoint,options) => {
        buttons.forEach(b=>b.disabled=true);status.textContent='Favicon wird verarbeitet …';
        try {
          const response=await fetch(endpoint,{method:'POST',...options,headers:{...options.headers,'x-csrf-token':csrf}});
          const data=await response.json();if(!response.ok) throw new Error(data.detail || data.error || 'Favicon konnte nicht geladen werden.');show(data);
        } catch(error){status.textContent=error.message || 'Favicon konnte nicht geladen werden.';}
        finally{buttons.forEach(b=>b.disabled=false);input.value='';}
      };
      editor.querySelector('[data-icon-fetch]').addEventListener('click',()=>{
        if(!url.reportValidity()) return;
        request('/icons/discover',{headers:{'content-type':'application/json'},body:JSON.stringify({url:url.value})});
      });
      editor.querySelector('[data-icon-upload]').addEventListener('click',()=>input.click());
      input.addEventListener('change',()=>{
        const file=input.files[0];if(!file)return;
        if(!/\.(ico|svg)$/i.test(file.name) || file.size>256*1024){status.textContent='Bitte ICO oder SVG bis 256 KB wählen.';input.value='';return;}
        request('/icons/upload',{headers:{'content-type':'application/octet-stream','x-icon-name':encodeURIComponent(file.name)},body:file});
      });
      editor.querySelector('[data-icon-clear]').addEventListener('click',()=>{
        payload.value='';action.value='clear';preview.replaceChildren();status.textContent='Automatisches Favicon wird beim Speichern erneut geladen.';
        action.dispatchEvent(new Event('change',{bubbles:true}));
      });
    });
    const updateRelativeTimes=()=>document.querySelectorAll('[data-relative-time]').forEach(element=>{
      const stamp=Date.parse(element.dataset.relativeTime);if(!Number.isFinite(stamp))return;
      const reference=Number.isFinite(serverClock)?estimatedServerNow():Date.now();
      const seconds=Math.max(0,Math.floor((reference-stamp)/1000));let text='gerade eben';
      if(seconds>=86400){const n=Math.floor(seconds/86400);text=`vor ${n} Tag${n===1?'':'en'}`;}
      else if(seconds>=3600){const n=Math.floor(seconds/3600);text=`vor ${n} Stunde${n===1?'':'n'}`;}
      else if(seconds>=60){const n=Math.floor(seconds/60);text=`vor ${n} Minute${n===1?'':'n'}`;}
      element.textContent=`(${text})`;
    });
    updateRelativeTimes();setInterval(updateRelativeTimes,60000);

    document.querySelectorAll('[data-schedule-editor]').forEach(editor => {
      const mode = editor.querySelector('[data-schedule-mode]');
      const project = editor.closest('[data-project-schedule]');
      const toggle = project?.querySelector('[data-use-global-schedule]');
      const custom = project?.querySelector('[data-custom-schedule]');
      const updateSchedule = () => {
        const global = !!toggle?.checked;
        if (custom) { custom.hidden = global; custom.disabled = global; }
        editor.querySelectorAll('[data-schedule-when]').forEach(field => {
          const visible = field.dataset.scheduleWhen.split(' ').includes(mode.value);
          field.hidden = !visible;
          field.querySelectorAll('input,select').forEach(input => input.disabled = global || !visible);
        });
      };
      mode?.addEventListener('change', updateSchedule);
      toggle?.addEventListener('change', updateSchedule);
      updateSchedule();
    });

    document.querySelectorAll('[data-access-provider]').forEach(provider => {
      const form = provider.closest('form');
      const syncAccess = () => form?.querySelectorAll('[data-access-fields]').forEach(fields => {
        const active = fields.dataset.accessFields === provider.value;
        fields.hidden = !active; fields.disabled = !active;
      });
      provider.addEventListener('change', syncAccess);
      syncAccess();
    });

    document.querySelectorAll('[data-tag-dropdown]').forEach(dropdown => {
      const summary = dropdown.querySelector('summary');
      const label = dropdown.querySelector('[data-tag-selection-label]');
      const syncTags = () => {
        const names = [...dropdown.querySelectorAll('input[name=tag_ids]:checked')].map(input => input.closest('label').querySelector('span:last-child').textContent.trim());
        label.textContent = names.join(', ') || 'Tags auswählen';
        summary.title = names.join(', ') || 'Tags auswählen';
      };
      dropdown.addEventListener('change', syncTags);
      syncTags();
    });
    document.addEventListener('pointerdown', event => {
      document.querySelectorAll('[data-tag-dropdown][open]').forEach(dropdown => { if (!dropdown.contains(event.target)) dropdown.open = false; });
    });

    const dirtyForms = new Set();
    document.querySelectorAll('form[data-guard-unsaved],form[data-site-access-form]').forEach(form => {
      const markDirty = () => dirtyForms.add(form);
      if (form.hasAttribute('data-restored-input')) markDirty();
      form.addEventListener('input', markDirty); form.addEventListener('change', markDirty);
      form.addEventListener('submit', event => { if (!event.defaultPrevented) dirtyForms.delete(form); });
    });
    window.addEventListener('beforeunload', event => {
      if (!dirtyForms.size) return;
      event.preventDefault(); event.returnValue = '';
    });


    const search = document.querySelector('[data-project-search]');
    const tagFilter = document.querySelector('[data-tag-filter]');
    const tagChoices = [...document.querySelectorAll('[data-tag-choice]')];
    const filters = [...document.querySelectorAll('[data-filter]')];
    const projects = [...document.querySelectorAll('[data-project]')];
    const projectSort = document.querySelector('[data-project-sort]');
    const projectGrid = document.querySelector('.project-grid');
    const projectNames = new Intl.Collator('de', {sensitivity:'base', numeric:true});
    const sortTime = value => {
      const time = Date.parse(value || '');
      return Number.isFinite(time) ? time : -Infinity;
    };
    const sortProjects = prioritizeRunning => {
      if (!projectSort || !projectGrid) return;
      const field = projectSort.value;
      const sorted = [...projects].sort((a, b) => {
        const running = prioritizeRunning ? Number(b.dataset.running) - Number(a.dataset.running) : 0;
        const errors = Number(b.dataset.error) - Number(a.dataset.error);
        const newest = field === 'name' ? 0 : sortTime(b.dataset[field]) - sortTime(a.dataset[field]);
        return running || errors || newest || projectNames.compare(a.dataset.projectName, b.dataset.projectName)
          || a.dataset.projectId.localeCompare(b.dataset.projectId);
      });
      const focused = document.activeElement;
      let next = null;
      for (const card of sorted.reverse()) {
        if (card.nextElementSibling !== next) projectGrid.insertBefore(card, next);
        next = card;
      }
      if (focused !== document.activeElement && projectGrid.contains(focused)) focused.focus({preventScroll:true});
    };
    if (projectSort && projectGrid) {
      const saved = read('project-sort');
      if ([...projectSort.options].some(option => option.value === saved)) projectSort.value = saved;
      projectSort.addEventListener('change', () => {
        write('project-sort', projectSort.value);
        updateProjects();
      });
    }
    let selected = 'all';
    let automationVersion = 0;
    const applyAutomation = (card, enabled) => {
      card.dataset.enabled = enabled ? '1' : '0'; card.classList.toggle('is-paused', !enabled);
      const form = card.querySelector('[data-automation-form]');
      const button = form?.querySelector('.automation-toggle');
      if (button) {
        button.classList.toggle('is-active', enabled); button.classList.toggle('is-paused', !enabled);
        button.setAttribute('aria-pressed', String(enabled));
        button.setAttribute('aria-label', `Automatik für ${form.dataset.projectName} ${enabled ? 'pausieren' : 'aktivieren'}`);
        button.title = `Automatische Prüfungen ${enabled ? 'pausieren' : 'aktivieren'} · ${form.dataset.schedule}`;
        button.querySelector('use')?.setAttribute('href', enabled ? '#i-play' : '#i-pause');
        button.querySelector('[data-automation-label]').textContent = enabled ? 'Automatik aktiv' : 'Pausiert';
        form.elements.namedItem('enabled').value = enabled ? '0' : '1';
      }
      if (!enabled) {
        const retry = card.querySelector('[data-retry-status]');
        if (retry) { retry.hidden = true; retry.dataset.retryState = ''; retry.dataset.retryDue = ''; }
      }
    };
    const matchesStatus = (card, key) => key === 'all' || (key === 'attention' && card.dataset.attention === '1') || (key === 'notices' && card.dataset.notices === '1') || (key === 'active' && card.dataset.enabled === '1') || (key === 'paused' && card.dataset.enabled === '0') || (key === 'error' && card.dataset.error === '1');
    const updateProjects = () => {
      const words = (search?.value || '').trim().toLocaleLowerCase('de').split(/\s+/).filter(Boolean);
      const tag = tagFilter?.value || '';
      const base = projects.filter(card => {
        let tags = []; try { tags = JSON.parse(card.dataset.tags || '[]'); } catch {}
        return words.every(word => card.dataset.search.toLocaleLowerCase('de').includes(word)) && (!tag || tags.includes(tag));
      });
      let count = 0;
      projects.forEach(card => { card.hidden = !base.includes(card) || !matchesStatus(card, selected); if (!card.hidden) count++; });
      filters.forEach(button => {
        const output = button.querySelector('[data-filter-count]');
        const count = base.filter(card => matchesStatus(card, button.dataset.filter)).length;
        if (output) output.textContent = String(count);
        button.setAttribute('aria-pressed', String(button.dataset.filter === selected));
        button.classList.toggle('has-changes', button.dataset.filter === 'attention' && count > 0);
        button.classList.toggle('has-errors', button.dataset.filter === 'error' && count > 0);
        button.classList.toggle('has-paused', button.dataset.filter === 'paused' && count > 0);
      });
      tagChoices.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.tagChoice === tag)));
      const empty = document.querySelector('[data-no-results]'); if (empty) empty.hidden = count > 0 || projects.length === 0;
      const output = document.querySelector('[data-result-count]'); if (output) output.textContent = `${count}/${projects.length}`;
      sortProjects(selected === 'all' && !tag && !words.length);
    };
    search?.addEventListener('input', updateProjects);
    tagFilter?.addEventListener('change', updateProjects);
    tagChoices.forEach(button => button.addEventListener('click', () => {
      if (tagFilter) tagFilter.value = tagFilter.value === button.dataset.tagChoice ? '' : button.dataset.tagChoice;
      updateProjects();
    }));
    if (tagFilter) {
      const requestedTag = new URLSearchParams(location.search).get('tag');
      if (requestedTag && [...tagFilter.options].some(option => option.value === requestedTag)) tagFilter.value = requestedTag;
    }
    filters.forEach(button => button.addEventListener('click', () => {
      selected = selected === button.dataset.filter ? 'all' : button.dataset.filter;
      updateProjects();
    }));
    if (projects.length) updateProjects();
    document.querySelectorAll('[data-review-form]').forEach(form => form.addEventListener('submit', async event => {
      if (!window.fetch || !window.AbortController) return;
      event.preventDefault();
      const card = form.closest('[data-project]');
      const button = form.querySelector('button');
      if (button.disabled) return;
      const focused = document.activeElement === button;
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(form.action, {method:'POST', credentials:'same-origin', signal:controller.signal,
          headers:{'Accept':'application/json'}, body:new URLSearchParams(new FormData(form))});
        if (!response.ok) throw new Error();
        const data = await response.json();
        const changes = data.attention_count;
        const notices = data.notice_count;
        if (data.id !== card.dataset.projectId || ![changes, notices].every(n => Number.isSafeInteger(n) && n >= 0)
            || typeof data.review_through !== 'string') throw new Error();
        card.dataset.attention = changes ? '1' : '0';
        card.dataset.notices = notices ? '1' : '0';
        card.querySelector('.project-facts').classList.toggle('has-change', Boolean(changes));
        const change = card.querySelector('[data-change-signal]');
        change.hidden = !changes;
        change.querySelector('span').textContent = `${changes} Änderung${changes === 1 ? '' : 'en'} erkannt`;
        const notice = card.querySelector('[data-notice-signal]');
        notice.hidden = !notices;
        notice.querySelector('span').textContent = `${notices} Hinweis${notices === 1 ? '' : 'e'}`;
        form.elements.namedItem('through').value = data.review_through;
        form.hidden = !changes;
        updateProjects();
        if (focused && (form.hidden || card.hidden)) {
          const next = card.hidden ? filters.find(filter => filter.dataset.filter === selected) : card.querySelector('.project-title a');
          next?.focus({preventScroll:true});
        }
      } catch {
        const toast = document.createElement('div'); toast.className = 'toast'; toast.setAttribute('role','alert');
        toast.textContent = 'Änderungen konnten nicht bestätigt werden. Bitte erneut versuchen.';
        document.body.append(toast); setTimeout(() => toast.remove(), 8000);
      } finally {
        clearTimeout(timeout);
        button.disabled = false;
        button.removeAttribute('aria-busy');
      }
    }));
    document.querySelectorAll('[data-automation-form]').forEach(form => form.addEventListener('submit', async event => {
      if (!window.fetch || !window.AbortController) return;
      event.preventDefault();
      const card = form.closest('[data-project]'); const button = form.querySelector('button');
      if (card.dataset.automationBusy) return;
      card.dataset.automationBusy = '1'; automationVersion++; button.disabled = true; button.setAttribute('aria-busy','true');
      const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(),10000);
      try {
        const response = await fetch(form.action, {method:'POST',credentials:'same-origin',signal:controller.signal,
          headers:{'Accept':'application/json'},body:new URLSearchParams(new FormData(form))});
        if (!response.ok) throw new Error();
        const data = await response.json();
        if (data.id !== card.dataset.projectId || ![0,1].includes(data.enabled)) throw new Error();
        applyAutomation(card, data.enabled === 1); updateProjects();
      } catch {
        const toast = document.createElement('div'); toast.className = 'toast'; toast.setAttribute('role','alert');
        toast.textContent = 'Automatik konnte nicht bestätigt werden. Bitte erneut versuchen.';
        document.body.append(toast); setTimeout(() => toast.remove(),8000);
      } finally {
        clearTimeout(timeout); delete card.dataset.automationBusy; automationVersion++; button.disabled = false; button.removeAttribute('aria-busy');
      }
    }));
    if (projects.length && window.fetch && window.AbortController) {
      const byId = new Map(projects.map(card => [card.dataset.projectId, card]));
      const labels = {queued:'Wartet',running:'Läuft',success:'Erfolgreich',partial:'Unvollständig',failed:'Fehler',protection:'Bot-Schutz'};
      const updateCountdowns = () => {
        if (document.hidden) return;
        projects.forEach(card => {
          const status = card.querySelector('[data-retry-status]');
          const label = status?.querySelector('[data-retry-label]');
          if (!status || status.hidden || !label) return;
          let text = status.dataset.retryState === 'running' ? 'Wiederholung läuft' : 'Wiederholung wartet';
          const deadline = Date.parse(status.dataset.retryDue || '');
          if (status.dataset.retryState === 'pending' && Number.isFinite(deadline) && Number.isFinite(serverClock)) {
            const seconds = Math.max(0, Math.ceil((deadline - estimatedServerNow()) / 1000));
            if (seconds) text = `Wird wiederholt in ${String(Math.floor(seconds / 60)).padStart(2,'0')}:${String(seconds % 60).padStart(2,'0')}`;
          }
          if (label.textContent !== text) label.textContent = text;
        });
      };
      const timeFormatter = new Intl.DateTimeFormat('de-DE', {timeZone:'Europe/Berlin',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
      const refreshSuccess = (card, stamp) => {
        const facts = card.querySelector('.project-facts > span[title="Letzte erfolgreiche Prüfung"]');
        if (!facts || facts.querySelector('time')?.dateTime === stamp) return;
        const date = new Date(stamp);
        if (typeof stamp !== 'string' || !Number.isFinite(date.getTime())) return;
        const parts = Object.fromEntries(timeFormatter.formatToParts(date).map(part => [part.type,part.value]));
        const time = document.createElement('time'); time.dateTime = stamp;
        time.textContent = `${parts.day}.${parts.month}.${parts.year} · ${parts.hour}:${parts.minute}`;
        const age = document.createElement('span'); age.dataset.relativeTime = stamp;
        facts.replaceChildren(time,document.createTextNode(' '),age);
      };
      let statusPending = false;
      const refreshStatus = async () => {
        if (document.hidden || statusPending) return;
        statusPending = true;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(),8000);
        const sent = performance.now();
        const requestedAutomationVersion = automationVersion;
        try {
          const response = await fetch('/api/project-status', {credentials:'same-origin',cache:'no-store',signal:controller.signal});
          if (!response.ok) return;
          const data = await response.json();
          if (!Array.isArray(data.projects)) return;
          const clock = Date.parse(data.server_now || '');
          if (Number.isFinite(clock)) { serverClock = clock + (performance.now()-sent)/2; serverClockAt = performance.now(); }
          data.projects.forEach(project => {
            const card = byId.get(project.id); if (!card) return;
            const enabled = project.enabled === 1 || project.enabled === true;
            const known = Object.hasOwn(labels,project.status);
            const status = known ? project.status : '';
            if (requestedAutomationVersion === automationVersion && !card.dataset.automationBusy) applyAutomation(card,enabled);
            card.dataset.error = ['failed','partial','protection'].includes(status) ? '1' : '0';
            card.dataset.running = status === 'running' ? '1' : '0';
            card.dataset.lastChecked = project.last_checked || '';
            card.dataset.lastChange = project.last_change || '';
            card.dataset.lastSuccess = project.last_success || '';
            const badge = card.querySelector('.project-run-status > .badge');
            if (badge) {
              badge.className = 'badge' + (status ? ' ' + status : '');
              const dot = document.createElement('span'); dot.className = 'status-dot';
              badge.replaceChildren(dot,document.createTextNode(known ? labels[status] : 'Ungeprüft'));
            }
            const retry = project.retry;
            const indicator = card.querySelector('[data-retry-status]');
            if (indicator) {
              const valid = card.dataset.enabled === '1' && retry && ['pending','queued','running'].includes(retry.state) && Number.isFinite(Date.parse(retry.due_at || ''));
              indicator.hidden = !valid;
              indicator.dataset.retryState = valid ? retry.state : '';
              indicator.dataset.retryDue = valid ? retry.due_at : '';
              if (valid) indicator.title = 'Einmalige Wiederholung · ' + timeFormatter.format(new Date(retry.due_at));
              else indicator.removeAttribute('title');
            }
            refreshSuccess(card,project.last_success);
          });
          updateCountdowns(); updateRelativeTimes(); updateProjects();
        } catch {} // Retain the last confirmed state during network failures.
        finally { clearTimeout(timeout); statusPending = false; }
      };
      updateCountdowns(); setInterval(updateCountdowns,1000);
      setInterval(refreshStatus,20000);
      document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshStatus(); updateCountdowns(); } });
    }
    const revealHash = () => {
      let id; try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
      const target = id && (document.getElementById(id) || (id === 'changes' && document.querySelector('[data-change-notices]')));
      if (target?.tagName === 'DETAILS') { target.open = true; requestAnimationFrame(() => target.scrollIntoView({block:'start'})); }
    };
    revealHash(); window.addEventListener('hashchange', revealHash);

    document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
      const field = document.getElementById(button.dataset.copy); if (!field) return;
      try {
        await navigator.clipboard.writeText(field.value);
        const label = button.querySelector('span'); if (label) { const original = label.textContent; label.textContent = 'Kopiert'; setTimeout(() => label.textContent = original, 2200); }
      } catch { field.focus(); field.select(); }
    }));

    document.addEventListener('pointerdown', event => {
      if (root.dataset.motion === 'off') return;
      const button = event.target.closest('.button,button'); if (!button || button.disabled) return;
      const bounds = button.getBoundingClientRect(); const dot = document.createElement('span');
      dot.className = 'ripple'; dot.setAttribute('aria-hidden', 'true'); dot.style.left = `${event.clientX - bounds.left}px`; dot.style.top = `${event.clientY - bounds.top}px`;
      button.append(dot); setTimeout(() => dot.remove(), 650);
    });
    document.querySelectorAll('form[method=post]').forEach(form => form.addEventListener('submit', event => {
      if (event.defaultPrevented) return;
      if (form.hasAttribute('data-download')) return;
      const button = event.submitter; if (!button || form.dataset.submitting) { if (form.dataset.submitting) event.preventDefault(); return; }
      button.dataset.loadingDisabled = button.disabled ? '1' : '0';
      form.dataset.submitting = 'true'; button.setAttribute('aria-busy','true');
      const spinner = document.createElement('span'); spinner.className='spinner'; spinner.setAttribute('aria-hidden','true'); button.prepend(spinner);
      if (button.dataset.loading) { const label = button.querySelector('[data-button-label]'); if (label) label.textContent = button.dataset.loading; }
      // Native form submission and CSRF fields remain intact.
      setTimeout(() => { if (document.contains(button)) button.disabled = true; }, 0);
    }));
    window.addEventListener('pageshow', event => {
      if (!event.persisted) return;
      document.querySelectorAll('form[data-submitting]').forEach(form => {
        delete form.dataset.submitting;
        form.querySelectorAll('button[data-loading-disabled]').forEach(button => {
          button.disabled = button.dataset.loadingDisabled === '1';
          delete button.dataset.loadingDisabled;
          button.removeAttribute('aria-busy'); button.querySelector('.spinner')?.remove();
        });
      });
    });
    const toast = document.querySelector('.toast');
    if (toast) setTimeout(() => { toast.classList.add('is-leaving'); setTimeout(() => toast.remove(), 250); }, 6500);
  });
})();
