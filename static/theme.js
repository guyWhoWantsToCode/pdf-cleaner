// Applies the saved theme before the page paints, to avoid a flash.
(function () {
  var saved = null;
  try { saved = JSON.parse(localStorage.getItem('pdfc.theme')); } catch (e) { /* ignore */ }
  var dark = saved ? saved === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
})();
