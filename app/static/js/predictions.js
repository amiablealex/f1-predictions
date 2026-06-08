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

// Shared lap-time mask (M:SS.mmm) for any input[data-lap-time]. Extracted
// from the predictions template so the pole-time field, wildcard lap-time
// fields, and the contributor actual form share one implementation.
(function () {
  const inputs = document.querySelectorAll('input[data-lap-time]');
  if (!inputs.length) return;

  const format = (digits) => {
    digits = digits.slice(0, 6);
    if (digits.length === 0) return '';
    let out = digits[0];
    if (digits.length >= 2) out += ':' + digits.slice(1, 3);
    if (digits.length >= 4) out += '.' + digits.slice(3, 6);
    return out;
  };

  const validate = (value) => {
    const digits = value.replace(/\D/g, '');
    if (digits.length === 0) return { valid: true, normalized: '' };
    if (digits.length < 3) {
      return { valid: false, message: 'Lap time needs at least M:SS (e.g. 1:23).' };
    }
    const seconds = digits.slice(1, 3);
    if (parseInt(seconds, 10) > 59) {
      return { valid: false, message: 'Seconds must be 00–59.' };
    }
    const ms = (digits.slice(3) + '000').slice(0, 3);
    return { valid: true, normalized: `${digits[0]}:${seconds}.${ms}` };
  };

  const errorEl = (field) => {
    let el = field.querySelector('.field__error');
    if (!el) {
      el = document.createElement('small');
      el.className = 'field__error';
      field.appendChild(el);
    }
    return el;
  };
  const setError = (field, msg) => {
    field.classList.add('field--error');
    const el = errorEl(field);
    el.textContent = msg;
    el.hidden = false;
  };
  const clearError = (field) => {
    field.classList.remove('field--error');
    const el = field.querySelector('.field__error');
    if (el) { el.textContent = ''; el.hidden = true; }
  };

  inputs.forEach((input) => {
    const field = input.closest('.field') || input.parentElement;

    input.addEventListener('input', () => {
      input.value = format(input.value.replace(/\D/g, ''));
      if (validate(input.value).valid) clearError(field);
    });

    input.addEventListener('blur', () => {
      const result = validate(input.value);
      if (!result.valid) {
        setError(field, result.message);
      } else {
        clearError(field);
        if (result.normalized !== input.value) input.value = result.normalized;
      }
    });

    const form = input.closest('form');
    if (form) {
      form.addEventListener('submit', (e) => {
        const result = validate(input.value);
        if (!result.valid) {
          e.preventDefault();
          setError(field, result.message);
          input.focus();
          input.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else if (result.normalized !== input.value) {
          input.value = result.normalized;
        }
      });
    }

    // Normalise any server-rendered value on load.
    if (input.value) {
      input.value = format(input.value.replace(/\D/g, ''));
      if (!validate(input.value).valid) setError(field, validate(input.value).message);
    }
  });
})();

// Shared integer-only sanitiser for any input[data-int-only]. Strips on the
// `input` event so it works on mobile virtual keyboards (which don't reliably
// fire keydown); the keydown block is a desktop nicety.
(function () {
  const inputs = document.querySelectorAll('input[data-int-only]');
  if (!inputs.length) return;
  inputs.forEach((input) => {
    input.addEventListener('keydown', (e) => {
      if (['.', ',', 'e', 'E', '+', '-'].includes(e.key)) e.preventDefault();
    });
    input.addEventListener('input', () => {
      const cleaned = input.value.replace(/\D/g, '');
      if (cleaned !== input.value) {
        const pos = input.selectionStart;
        const removed = input.value.length - cleaned.length;
        input.value = cleaned;
        if (pos !== null) {
          const newPos = Math.max(0, pos - removed);
          input.setSelectionRange(newPos, newPos);
        }
      }
    });
  });
})();
