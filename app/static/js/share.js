// Share-invite buttons: native share sheet where available, clipboard otherwise.
(function () {
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

      // Clipboard fallback
      const flash = () => {
        const original = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = original; }, 2000);
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
