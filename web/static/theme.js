// Thème choisi dans le menu (auto, clair, sombre), appliqué avant l'affichage pour éviter un flash de couleurs.
(function () {
  let theme = 'auto';
  try { theme = localStorage.getItem('cockpit_theme') || 'auto'; } catch { /* navigation privée */ }
  if (theme !== 'auto') document.documentElement.dataset.theme = theme;
}());
