// Predictions form: client-side enhancements.
//   1. Duplicate-driver validation within constrained groups (race top 10,
//      quali top 3, sprint top 3). Drivers may repeat across groups but
//      not within one.
//   2. Dirty-state badge that reflects whether the form differs from
//      what's currently saved on the server.

(function () {
  const form = document.querySelector('.predictions-form');
  if (!form) return;

  // ----- Duplicate-driver validation -----------------------------------

  const GROUPS = [
    { prefix: 'top10_',         max: 10, label: 'race top 10' },
    { prefix: 'quali_top3_',    max: 3,  label: 'quali top 3' },
    { prefix: 'sprint_top3_',   max: 3,  label: 'sprint top 3' },
  ];

  const submit = form.querySelector('button[type="submit"]');
  const hint = form.querySelector('.sticky-save p');
  const defaultHint = hint ? hint.textContent : '';

  function selectsForGroup(group) {
    const out = [];
    for (let i = 1; i <= group.max; i++) {
      const sel = form.querySelector(`select[name="${group.prefix}${i}"]`);
      if (sel) out.push(sel);
    }
    return out;
  }

  function clearError(field) {
    field.classList.remove('field--error');
    const msg = field.querySelector('.field__error');
    if (msg) msg.remove();
  }

  function setError(field, text) {
    field.classList.add('field--error');
    let msg = field.querySelector('.field__error');
    if (!msg) {
      msg = document.createElement('div');
      msg.className = 'field__error';
      field.appendChild(msg);
    }
    msg.textContent = text;
  }

  function validateGroup(group) {
    const selects = selectsForGroup(group);
    const counts = {};
    selects.forEach(sel => {
      if (sel.value) counts[sel.value] = (counts[sel.value] || 0) + 1;
    });
    let groupHasDupes = false;
    selects.forEach(sel => {
      const field = sel.closest('.field');
      if (!field) return;
      const isDupe = sel.value && counts[sel.value] > 1;
      if (isDupe) {
        groupHasDupes = true;
        setError(field, 'Already picked elsewhere in ' + group.label);
      } else {
        clearError(field);
      }
    });
    return groupHasDupes;
  }

  function validateAll() {
    let totalDupes = 0;
    GROUPS.forEach(g => { if (validateGroup(g)) totalDupes += 1; });
    if (submit) submit.disabled = totalDupes > 0;
    if (hint) {
      hint.textContent = totalDupes > 0
        ? 'Resolve duplicate drivers to save.'
        : defaultHint;
    }
  }

  // ----- Dirty-state badge ---------------------------------------------

  const badgeEl = form.querySelector('[data-form-badge]');
  const statusEl = form.querySelector('[data-form-status]');
  const hasSubmitted = statusEl && statusEl.dataset.hasSubmitted === 'true';

  const BADGE = {
    saved: { cls: 'pill pill--status-completed', text: 'Saved' },
    dirty: { cls: 'pill pill--neg',   text: 'Unsaved changes' },
    fresh: { cls: 'pill pill--status-upcoming',  text: 'New predictions' },
  };

  function snapshot() {
    const data = {};
    form.querySelectorAll('input[name], select[name]').forEach(el => {
      if (el.type === 'hidden') return;
      data[el.name] = el.value || '';
    });
    return data;
  }

  const initial = snapshot();

  function isDirty() {
    const current = snapshot();
    const keys = new Set(Object.keys(initial).concat(Object.keys(current)));
    for (const k of keys) {
      if ((initial[k] || '') !== (current[k] || '')) return true;
    }
    return false;
  }

  function setBadge(state) {
    if (!badgeEl) return;
    const b = BADGE[state];
    badgeEl.className = b.cls;
    badgeEl.textContent = b.text;
  }

  function updateBadge() {
    if (isDirty()) setBadge('dirty');
    else if (hasSubmitted) setBadge('saved');
    else setBadge('fresh');
  }

  // ----- Wire up -------------------------------------------------------

  form.addEventListener('input',  () => { validateAll(); updateBadge(); });
  form.addEventListener('change', () => { validateAll(); updateBadge(); });

  validateAll();
  updateBadge();
})();

// Live countdown next to the deadline label.
(function () {
  const el = document.querySelector("[data-countdown]");
  if (!el) return;

  const target = new Date(el.dataset.countdown).getTime();

  function fmt(ms) {
    if (ms <= 0) return "locked";
    const totalSeconds = Math.floor(ms / 1000);
    const days = Math.floor(totalSeconds / 86400);
    const hours = Math.floor((totalSeconds % 86400) / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    if (days > 0) return `${days}d ${hours}h ${minutes}m`;
    if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
    if (minutes > 0) return `${minutes}m ${seconds}s`;
    return `${seconds}s`;
  }

  function tick() {
    const remaining = target - Date.now();
    el.textContent = `(${fmt(remaining)})`;
    if (remaining <= 0) {
      clearInterval(timer);
      // Reload after a moment so the server-side lock state takes over.
      setTimeout(() => location.reload(), 1500);
    }
  }
  tick();
  const timer = setInterval(tick, 1000);
})();
