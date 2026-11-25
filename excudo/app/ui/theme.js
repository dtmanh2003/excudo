"use strict";
// 起動時にライト表示を指定し、以前保存されたテーマによって表示が変わるのを防ぐ。
(() => {
  document.documentElement.dataset.theme = 'light';
  try { localStorage.removeItem('excudo-appearance-theme'); } catch (_) {}
// WebViewの配色とWindowsタイトルバーを揃え、APIが利用可能になるタイミングでも反映する。
  function syncTitleBar() {
    const api = window.pywebview?.api;
    if (api?.set_window_theme) api.set_window_theme('light').catch(error => console.warn('Title bar update failed', error));
  }
  syncTitleBar();
  window.addEventListener('pywebviewready', syncTitleBar);
})();
