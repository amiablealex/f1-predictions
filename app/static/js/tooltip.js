// Generic tooltip component.
//
// Mark any element with data-tooltip="explanation text" to make it a
// tooltip trigger. On tap, a small popup appears below the element with
// that text. Tapping the trigger again or tapping anywhere outside
// dismisses it. Only one tooltip is ever open at a time.
//
// Positioning: below the trigger, horizontally aligned to its centre but
// clamped to stay within the viewport. Pure CSS classes drive the styling
// — see .tooltip and friends in main.css.

(function () {
  let current = null;  // { trigger, bubble }

  function close() {
    if (!current) return;
    current.bubble.remove();
    current.trigger.setAttribute('aria-expanded', 'false');
    current = null;
  }

  function position(bubble, trigger) {
    const tr = trigger.getBoundingClientRect();
    const margin = 8;
    bubble.style.visibility = 'hidden';
    document.body.appendChild(bubble);
    const br = bubble.getBoundingClientRect();

    // Centre under the trigger, clamp to viewport.
    let left = tr.left + tr.width / 2 - br.width / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - br.width - margin));
    let top = tr.bottom + 6 + window.scrollY;

    bubble.style.left = `${left}px`;
    bubble.style.top = `${top}px`;
    bubble.style.visibility = '';
  }

  function open(trigger) {
    const text = trigger.getAttribute('data-tooltip');
    if (!text) return;
    const bubble = document.createElement('div');
    bubble.className = 'tooltip';
    bubble.setAttribute('role', 'tooltip');
    bubble.textContent = text;
    position(bubble, trigger);
    trigger.setAttribute('aria-expanded', 'true');
    current = { trigger, bubble };
  }

  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-tooltip]');
    if (trigger) {
      if (current && current.trigger === trigger) {
        close();
      } else {
        close();
        open(trigger);
      }
      e.stopPropagation();
      return;
    }
    // Click anywhere else dismisses.
    if (current && !e.target.closest('.tooltip')) {
      close();
    }
  });

  // Reposition or dismiss on resize/scroll — simplest is to dismiss.
  window.addEventListener('resize', close);
  window.addEventListener('scroll', close, { passive: true });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
  });
})();
