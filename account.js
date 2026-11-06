(() => {
  const PREFIX = 'dobrytalerz:';
  const STATE_KEYS = ['recipes','plans','pantry','checked','hiddenDietMeals','dietWeekAssignments','householdSize'];
  let user = null;
  let writeQueue = Promise.resolve();

  const parseStored = key => {
    const raw = localStorage.getItem(PREFIX + key);
    if (raw === null) return undefined;
    if (key === 'householdSize') return raw;
    try { return JSON.parse(raw); } catch { return undefined; }
  };
  const localState = () => Object.fromEntries(STATE_KEYS.map(key => [key, parseStored(key) ?? (key === 'householdSize' ? '2' : [])]));
  const writeLocalState = state => {
    let changed = false;
    for (const key of STATE_KEYS) {
      const fallback = key === 'householdSize' ? '2' : [];
      const value = state[key] ?? fallback;
      const raw = key === 'householdSize' ? String(value) : JSON.stringify(value);
      if (localStorage.getItem(PREFIX + key) !== raw) changed = true;
      localStorage.setItem(PREFIX + key, raw);
    }
    return changed;
  };
  const request = async (url, options={}) => {
    const response = await fetch(url, {credentials:'same-origin', ...options, headers:{'Content-Type':'application/json', ...(options.headers||{})}});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Błąd serwera (${response.status}).`);
    return data;
  };
  const defaultAssignments = () => {
    const monday = new Date();
    monday.setHours(12,0,0,0);
    monday.setDate(monday.getDate() - ((monday.getDay()+6)%7));
    const key = date => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
    const next = new Date(monday); next.setDate(next.getDate()+7);
    return {1:[key(monday)], 2:[key(next)]};
  };
  const normalizeAssignments = value => {
    const result = {};
    for (const week of [1,2]) {
      const stored = value && !Array.isArray(value) ? value[week] : null;
      const dates = Array.isArray(stored) ? stored : typeof stored === 'string' && stored ? [stored] : [];
      result[week] = [...new Set(dates.filter(Boolean).map(date => {
        const monday = new Date(`${date}T12:00:00`);
        if (Number.isNaN(monday.getTime())) return null;
        monday.setDate(monday.getDate() - ((monday.getDay()+6)%7));
        return `${monday.getFullYear()}-${String(monday.getMonth()+1).padStart(2,'0')}-${String(monday.getDate()).padStart(2,'0')}`;
      }).filter(Boolean))];
    }
    return result;
  };
  const initialServerState = () => ({
    recipes:(window.DIETA_RECIPES||[]).map(recipe=>({...recipe,id:globalThis.crypto?.randomUUID?.()||`id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,10)}`})),
    plans:[], pantry:[], checked:[], hiddenDietMeals:[], dietWeekAssignments:defaultAssignments(), householdSize:'2'
  });
  const setMessage = message => {
    const node = document.querySelector('#account-message');
    if (node) node.textContent = message;
  };
  const ensureScreen = () => {
    let root = document.querySelector('#account-root');
    if (!root) { root = document.createElement('div'); root.id = 'account-root'; document.body.append(root); }
    return root;
  };
  const showGate = (message='') => {
    const root = ensureScreen();
    root.innerHTML = `<main class="account-screen"><section class="account-card"><div class="account-brand"><span class="brand-mark">✳</span><span>dobry<span class="brand-light">talerz</span></span></div><span class="eyebrow">PRYWATNA APLIKACJA</span><h1 id="account-title">Zaloguj się</h1><p id="account-description">Zaloguj się na konto właściciela, aby otworzyć swoje przepisy i plany.</p><form id="account-form"><label>Adres e-mail<input type="email" name="email" autocomplete="username" required maxlength="254" /></label><label>Hasło<input type="password" name="password" autocomplete="current-password" required minlength="10" maxlength="128" /></label><button class="button primary account-submit" type="submit">Zaloguj się</button></form><p class="account-message" id="account-message" role="status">${message}</p></section></main>`;
    const form = root.querySelector('#account-form');
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const submit = root.querySelector('.account-submit');
      submit.disabled = true;
      setMessage('Łączenie z serwerem…');
      try {
        const values = new FormData(form);
        const payload = {email:values.get('email'), password:values.get('password')};
        await request('/api/login', {method:'POST', body:JSON.stringify(payload)});
        const data = await request('/api/state');
        if (!Object.keys(data.state||{}).length && !signup) {
          const state = initialServerState();
          await request('/api/state', {method:'PUT', body:JSON.stringify({state})});
          writeLocalState(state);
        } else {
          writeLocalState(data.state);
        }
        location.reload();
      } catch (error) {
        setMessage(error.message || 'Nie udało się połączyć z serwerem.');
        submit.disabled = false;
      }
    });
    document.body.classList.add('account-ready');
  };
  const renderAccountBar = () => {
    const target = document.querySelector('.topbar-right');
    if (!target || document.querySelector('#account-bar')) return;
    const bar = document.createElement('div');
    bar.id = 'account-bar';
    bar.innerHTML = `<span class="account-email"></span><button class="account-logout" id="account-logout" type="button">Wyloguj</button><span class="sync-status" id="sync-status" aria-live="polite">Zapis na serwerze</span>`;
    bar.querySelector('.account-email').textContent = user.email;
    target.prepend(bar);
    bar.querySelector('#account-logout').addEventListener('click', async () => {
      const button = bar.querySelector('#account-logout'); button.disabled = true;
      try {
        await writeQueue;
        await request('/api/logout', {method:'POST', body:'{}'});
        for (const key of STATE_KEYS) localStorage.removeItem(PREFIX + key);
        location.reload();
      } catch (error) {
        button.disabled = false;
        const status = document.querySelector('#sync-status');
        if (status) status.textContent = error.message || 'Nie udało się wylogować';
      }
    });
  };
  const boot = async () => {
    document.body.classList.remove('account-ready');
    document.body.classList.add('account-pending');
    try {
      const auth = await request('/api/auth');
      if (!auth.authenticated) { document.body.classList.remove('account-pending'); showGate(); return {authenticated:false}; }
      user = auth.user;
      const result = await request('/api/state');
      let state = result.state || {};
      if (!Object.keys(state).length) {
        state = initialServerState();
        await request('/api/state', {method:'PUT', body:JSON.stringify({state})});
      } else {
        const defaults = initialServerState(), missing = {};
        for (const key of STATE_KEYS) {
          if (state[key] === undefined || state[key] === null) {
            state[key] = defaults[key];
            missing[key] = state[key];
          }
        }
        const assignments = normalizeAssignments(state.dietWeekAssignments);
        if (JSON.stringify(assignments) !== JSON.stringify(state.dietWeekAssignments)) {
          state.dietWeekAssignments = assignments;
          missing.dietWeekAssignments = assignments;
        }
        if (Object.keys(missing).length) {
          await request('/api/state', {method:'PUT', body:JSON.stringify({state:missing})});
        }
      }
      const changed = writeLocalState(state);
      if (changed) return {authenticated:true,reload:true};
      document.body.classList.remove('account-pending');
      document.body.classList.add('account-ready');
      renderAccountBar();
      return {authenticated:true,reload:false};
    } catch (error) {
      document.body.classList.remove('account-pending');
      showGate(`Nie można połączyć się z serwerem: ${error.message}`);
      return {authenticated:false};
    }
  };
  const persist = (key, value) => {
    if (!user || !STATE_KEYS.includes(key)) return;
    writeQueue = writeQueue.catch(()=>{}).then(() => request('/api/state', {method:'PUT',body:JSON.stringify({key,value})}));
    writeQueue.then(() => {
      const status = document.querySelector('#sync-status'); if (status) status.textContent = 'Zapisano';
    }).catch(error => {
      const status = document.querySelector('#sync-status'); if (status) status.textContent = 'Błąd zapisu';
      console.error('Nie udało się zapisać danych na serwerze:', error.message);
    });
  };
  window.DietAccount = {boot,persist};
})();
