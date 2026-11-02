"""F21: the geographic-view script, kept as its own asset.

Separate from ``workbench.js`` for one measured reason: the rule-grid delta map
must be updated in place, never rebuilt (a second renderer there is exactly the
defect that once shipped zero-size ``NaN`` cells past four green gates).  The
geographic view *does* have to build its SVG, because the basemap exists only as
data -- so it lives in its own file and ``workbench.js`` keeps its
no-construction property.  Every coordinate it writes is read straight out of
the published ``basemap.js``; none is computed here.
"""

from __future__ import annotations

GEO_JS = """/* F21: the geographic view.  Its own file on purpose.

   The rule-grid delta map is updated in place by workbench.js -- no element is
   ever constructed there, which is what keeps its geometry single-sourced in
   Python.  The geographic view is a different thing: it has to *create* an SVG
   per source, because the basemap's paths only exist as data.  Putting that
   construction here means workbench.js keeps its no-construction property, and
   the geometry below still comes entirely from the published ``basemap.js``:
   every x/y/width/height is a direct read of ``row[1..4]``, never a calculation.

   Cells are addressed by ``data-cell`` exactly like the rule-grid rects, so the
   selection survives a projection switch by construction rather than by copying
   state around. */
(function () {
  'use strict';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var built = false;
  var rects = {};
  var B = null;

  function q(selector) { return document.querySelector(selector); }
  function pack() { return window.PRESENTATION_DATA[document.body.getAttribute('data-source')]; }
  function replay() { return window.REPLAY_DATA_SOURCES[document.body.getAttribute('data-source')]; }

  function el(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    for (var key in attrs) if (attrs.hasOwnProperty(key)) node.setAttribute(key, attrs[key]);
    return node;
  }

  function build() {
    if (!B || built) return;
    var views = {
      rule: q('[data-geo-strategy="rule"]'),
      optimized: q('[data-geo-strategy="optimized"]'),
      delta: q('[data-geo-strategy="delta"]')
    };
    var W = B.projection.width, H = B.projection.height;
    Object.keys(views).forEach(function (key) {
      var host = views[key];
      if (!host) return;
      var svg = el('svg', {
        'class': 'grid geo-map', viewBox: '0 0 ' + W + ' ' + H,
        role: 'img', 'aria-label': '地理底图 · ' + key,
        'data-projection': 'geo', 'data-strategy': key
      });
      var back = el('g', { 'class': 'geo-back' });
      B.layers.land.forEach(function (d) { back.appendChild(el('path', { d: d, 'class': 'geo-land' })); });
      B.layers.water.forEach(function (d) { back.appendChild(el('path', { d: d, 'class': 'geo-water' })); });
      B.layers.admin.forEach(function (d) { back.appendChild(el('path', { d: d, 'class': 'geo-admin' })); });
      svg.appendChild(back);

      var cells = el('g', { 'class': 'geo-cells' });
      rects[key] = {};
      B.cells.forEach(function (row) {
        var rect = el('rect', {
          'class': 'cell', 'data-cell': row[0],
          x: row[1], y: row[2], width: row[3], height: row[4]
        });
        rect.appendChild(el('title', {}));
        cells.appendChild(rect);
        rects[key][row[0]] = rect;
      });
      svg.appendChild(cells);
      svg.appendChild(el('g', { 'class': 'geo-moves' }));

      // Scale bar: its pixel length is published with the arithmetic that
      // produced it, so a reader can recompute it from metres-per-pixel.
      var bar = B.scale_bar;
      if (bar) {
        var by = H - 14, bx = 14;
        var scale = el('g', { 'class': 'geo-scale', 'data-metres': bar.metres, 'data-pixels': bar.pixels });
        scale.appendChild(el('line', { x1: bx, y1: by, x2: bx + bar.pixels, y2: by, 'class': 'scale-line' }));
        scale.appendChild(el('line', { x1: bx, y1: by - 4, x2: bx, y2: by + 4, 'class': 'scale-line' }));
        scale.appendChild(el('line', { x1: bx + bar.pixels, y1: by - 4, x2: bx + bar.pixels, y2: by + 4, 'class': 'scale-line' }));
        var label = el('text', { x: bx + 4, y: by - 6, 'class': 'scale-label' });
        label.textContent = bar.label;
        scale.appendChild(label);
        var tip = el('title', {});
        tip.textContent = bar.recompute;
        scale.appendChild(tip);
        svg.appendChild(scale);
      }
      var credit = el('text', { x: W - 8, y: H - 8, 'class': 'geo-credit', 'text-anchor': 'end' });
      credit.textContent = B.attribution.land + ' · ' + B.attribution.water_admin;
      svg.appendChild(credit);
      host.appendChild(svg);
    });
    built = true;
  }

  // Move lines run cell centre to cell centre: the dispatch relation carries no
  // road geometry, and drawing a route would claim one it does not have.
  function centre(cellId) {
    var rect = rects.rule && rects.rule[cellId];
    if (!rect) return null;
    var w = parseFloat(rect.getAttribute('width'));
    var h = parseFloat(rect.getAttribute('height'));
    return [
      parseFloat(rect.getAttribute('x')) + w / 2,
      parseFloat(rect.getAttribute('y')) + h / 2
    ];
  }

  function render(state) {
    if (!B) return;
    build();
    var data = pack();
    var frame = data.frames[state.frame];
    var ids = data.cell_ids;
    var maxInv = data.color_domain.inventory.max;
    var maxAbs = data.color_domain.delta.max_abs;
    var values = replay().frames[state.frame];

    ['rule', 'optimized'].forEach(function (strategy) {
      var host = q('[data-geo-strategy="' + strategy + '"]');
      if (!host) return;
      var inventory = values.strategies[strategy].inventory_end;
      var index = 0;
      ids.forEach(function (cellId) {
        var rect = rects[strategy] && rects[strategy][cellId];
        if (!rect) return;
        var value = inventory[index];
        rect.setAttribute('data-inventory-end', value);
        // A gentler ramp than the rule grid: this view exists to show *where*,
        // so a saturated cell would hide the coastline it is drawn on.
        rect.setAttribute('fill-opacity', (0.10 + 0.45 * (Number(value) / (maxInv || 1))).toFixed(3));
        rect.setAttribute('class', 'cell' + (cellId === state.cell ? ' selected' : ''));
        var title = rect.querySelector('title');
        if (title) title.textContent = cellId + ' 期末库存 ' + value;
        index++;
      });
      var group = host.querySelector('.geo-moves');
      while (group && group.firstChild) group.removeChild(group.firstChild);
      (values.moves[strategy] || []).forEach(function (move) {
        var from = centre(move.from_cell), to = centre(move.to_cell);
        if (!from || !to) return;
        var line = el('line', {
          x1: from[0], y1: from[1], x2: to[0], y2: to[1],
          'class': 'geo-move', 'data-from': move.from_cell, 'data-to': move.to_cell,
          'data-bikes': move.bikes
        });
        var tip = el('title', {});
        tip.textContent = move.from_cell + ' → ' + move.to_cell + '：' + move.bikes +
          ' 辆（格心到格心，非道路轨迹）';
        line.appendChild(tip);
        group.appendChild(line);
      });
    });

    var deltaHost = q('[data-geo-strategy="delta"]');
    if (deltaHost) {
      var k = 0;
      ids.forEach(function (cellId) {
        var rect = rects.delta && rects.delta[cellId];
        if (rect) {
          var value = frame.delta[k];
          var kind = value < 0 ? 'improved' : (value > 0 ? 'worsened' : 'unchanged');
          rect.setAttribute('class', 'cell delta ' + kind + (cellId === state.cell ? ' selected' : ''));
          rect.setAttribute('data-delta', value);
          rect.setAttribute('fill-opacity', (0.15 + 0.85 * (maxAbs ? Math.abs(value) / maxAbs : 0)).toFixed(3));
        }
        k++;
      });
      deltaHost.setAttribute('data-scale-delta', maxAbs);
    }
  }

  window.WorkbenchGeo = {
    attach: function (basemap) { B = basemap || null; },
    render: render
  };
})();

"""
