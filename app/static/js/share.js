// Share-invite buttons: native share sheet where available, clipboard otherwise.
(function () {
  const TICK = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  document.querySelectorAll('[data-share-league]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const url = btn.dataset.shareUrl;
      const text = btn.dataset.shareText;
      const fullText = text + url;

      if (navigator.share) {
        try {
          await navigator.share({ url, text });
          return;
        } catch (e) {
          return; // user cancelled — leave the page alone
        }
      }

      // Clipboard fallback. Icon buttons swap to a tick; text buttons say Copied.
      const flash = () => {
        if (btn.dataset.flashing) return;
        btn.dataset.flashing = '1';
        const original = btn.innerHTML;
        btn.innerHTML = btn.classList.contains('icon-btn') ? TICK : 'Copied';
        setTimeout(() => { btn.innerHTML = original; delete btn.dataset.flashing; }, 2000);
      };

      try {
        await navigator.clipboard.writeText(fullText);
        flash();
      } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = fullText;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (_) {}
        document.body.removeChild(ta);
        flash();
      }
    });
  });
})();
