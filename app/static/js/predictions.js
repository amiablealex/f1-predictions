// Predictions form: client-side validation for duplicate drivers
// within a constrained group. Drivers can repeat across groups (e.g.
// quali P1 and race P3 can be the same driver) but not within the
// same group (e.g. race P1 and race P3 must differ).

(function () {
  const form = document.querySelector('.predictions-form');
  if (!form) return;

  // Each group is a set of <select> elements that must hold distinct
  // drivers. The `prefix` matches the input `name` attribute.
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

  // Wire up
  form.querySelectorAll('select').forEach(sel => {
    sel.addEventListener('change', validateAll);
  });
  // Initial pass — covers the case where the server re-rendered the
  // form with a known-bad submission.
  validateAll();
})();
