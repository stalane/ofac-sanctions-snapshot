(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.CountryMatch = factory();
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var ALIASES = {
    'myanmar': 'burma',
    'palestine': 'palestinian',
    'turkiye': 'turkey',
    'democratic peoples republic of korea': 'korea north',
    'syrian arab republic': 'syria',
    'cabo verde': 'cape verde',
    'ivory coast': 'cote d ivoire'
  };

  function norm(s) {
    return String(s == null ? '' : s)
      .toLowerCase()
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .replace(/[^a-z0-9 ]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function tokens(s) {
    return norm(s).split(' ').filter(Boolean);
  }

  function match(geoCountry, available) {
    var g = tokens(geoCountry);
    var alias = ALIASES[g.join(' ')];
    if (alias) g = tokens(alias);
    if (!g.length) return null;

    var best = null;
    var bestScore = -1;

    for (var i = 0; i < available.length; i++) {
      var a = tokens(available[i]);
      if (!a.length) continue;

      if (g.join(' ') === a.join(' ')) return available[i];

      var common = 0;
      for (var j = 0; j < g.length; j++) {
        if (a.indexOf(g[j]) !== -1) common++;
      }
      if (common !== Math.min(g.length, a.length)) continue;

      if (common > bestScore) {
        bestScore = common;
        best = available[i];
      }
    }
    return best;
  }

  return { norm: norm, tokens: tokens, match: match, ALIASES: ALIASES };
});