/* Phase 4: DOM wiring only.  All navigation rules live in workbench-core.js. */
(function () {
  'use strict';
  var Core = window.WorkbenchCore;
  var P = window.PRESENTATION_DATA;
  var R = window.REPLAY_DATA_SOURCES;
  if (!Core || !P || !R) return;

  var sourceNames = Object.keys(P);
  var core = Core.create({ source: document.body.getAttribute('data-source'), sources: P });
  // F28: restore an analysis position from a collaboration-page deep link.
  // Invalid values are rejected by the core setters, so a bad cell id leaves
  // the page at its default instead of fabricating a selection.
  function deepParams() {
    var raw = (window.location.hash || '').replace(/^#/, '');
    if (!raw && window.location.search) raw = window.location.search.replace(/^\?/, '');
    var out = {};
    if (!raw) return out;
    raw.split('&').forEach(function (part) {
      var at = part.indexOf('=');
      if (at > 0) out[decodeURIComponent(part.slice(0, at))] = decodeURIComponent(part.slice(at + 1));
    });
    return out;
  }
  var deep = deepParams();
  if (deep.frame !== undefined) { var deepFrame = parseInt(deep.frame, 10); if (isFinite(deepFrame)) core.setFrame(deepFrame); }
  if (deep.cell) core.selectCell(deep.cell);
  if (deep.mode) core.setMode(deep.mode);
  if (deep.projection) core.setProjection(deep.projection);
  if (deep.scenario) core.setScenario(deep.scenario);
  var timer = null;
  // The geographic view lives in its own script: it builds SVG from published
  // geometry, and keeping that out of this file is what lets the rule-grid delta
  // map stay an in-place update with no element construction at all.
  var Geo = window.WorkbenchGeo || null;
  if (Geo) Geo.attach((window.BASEMAP_DATA || {})[document.body.getAttribute('data-source')] || null);
  var F = (window.FORECAST_DATA || {})[document.body.getAttribute('data-source')] || null;

  function q(selector) { return document.querySelector(selector); }
  function qa(selector) { return Array.prototype.slice.call(document.querySelectorAll(selector)); }
  function fmtInt(v) { return Number(v).toLocaleString('en-US'); }
  // A missing prediction is an em dash, never a zero: the chart has to be able
  // to say "no data here" rather than draw a point on the axis.
  function fmtNum(value, digits) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'number' && !isFinite(value)) return '—';
    return digits ? Number(value).toFixed(digits) : fmtInt(Math.round(value));
  }
  function signed(v) { return (v > 0 ? '+' : '') + fmtInt(v); }
  function pack(name) { return P[name]; }
  function replay(name) { return R[name]; }

  function opacity(value, max) { return (0.12 + 0.88 * (Number(value) / (max || 1))).toFixed(3); }

  function renderGrids(state) {
    var data = pack(state.source);
    var shared = replay(state.source);
    var maxInv = data.color_domain.inventory.max;
    var maxAbs = data.color_domain.delta.max_abs;
    var frame = data.frames[state.frame];
    var ids = data.cell_ids;

    ['rule', 'optimized'].forEach(function (strategy) {
      var svg = q('svg.grid[data-strategy="' + strategy + '"]');
      if (!svg) return;
      // 期末库存 comes from the shared player bundle, not a second copy.
      var values = shared.frames[state.frame].strategies[strategy].inventory_end;
      var rects = svg.querySelectorAll('rect.cell');
      for (var i = 0; i < rects.length && i < ids.length; i++) {
        var rect = rects[i];
        rect.setAttribute('data-inventory-end', values[i]);
        rect.setAttribute('fill-opacity', opacity(values[i], maxInv));
        rect.setAttribute('class', 'cell' + (ids[i] === state.cell ? ' selected' : ''));
        var title = rect.querySelector('title');
        if (title) title.textContent = ids[i] + ' 期末库存 ' + values[i];
        rect.setAttribute('aria-label', ids[i] + ' 期末库存 ' + values[i]);
      }
    });

    var deltaSvg = q('svg.delta-map');
    if (deltaSvg) {
      // The rects are prerendered by Python with their geometry; the script only
      // rewrites what changes per frame.  Rebuilding them here is what let a
      // missing geometry field turn every cell into a zero-size NaN rect.
      var deltas = frame.delta;
      var cells = deltaSvg.querySelectorAll('rect.cell');
      var glyphs = deltaSvg.querySelectorAll('text.delta-glyph');
      for (var k = 0; k < cells.length && k < ids.length; k++) {
        var value = deltas[k];
        var kind = value < 0 ? 'improved' : (value > 0 ? 'worsened' : 'unchanged');
        var word = value < 0 ? '改善' : (value > 0 ? '退步' : '无差异');
        var rectEl = cells[k];
        rectEl.setAttribute('class', 'cell delta ' + kind + (ids[k] === state.cell ? ' selected' : ''));
        rectEl.setAttribute('data-delta', value);
        rectEl.setAttribute('data-delta-kind', kind);
        rectEl.setAttribute('fill-opacity', (0.15 + 0.85 * (maxAbs ? Math.abs(value) / maxAbs : 0)).toFixed(3));
        rectEl.setAttribute('aria-label', ids[k] + ' ' + word + ' ' + Math.abs(value) + ' 次');
        var t = rectEl.querySelector('title');
        if (t) t.textContent = ids[k] + ' ' + word + ' ' + Math.abs(value) + ' 次（delta_unmet = ' + signed(value) + '）';
        if (glyphs[k]) glyphs[k].textContent = value < 0 ? '−' : (value > 0 ? '+' : '');
      }
    }
  }

  function renderSummary(state) {
    var frame = pack(state.source).frames[state.frame];
    var s = frame.delta_summary;
    var box = q('#delta-summary');
    if (box) box.setAttribute('data-status', s.status);
    var join = 'time_bucket=' + frame.time_bucket;
    setMetric('delta_net', s.net, signed(s.net), join);
    setMetric('improved_cells', s.improved_cells, String(s.improved_cells), join);
    setMetric('worsened_cells', s.worsened_cells, String(s.worsened_cells), join);
    setMetric('unchanged_cells', s.unchanged_cells, String(s.unchanged_cells), join);
  }

  function setMetric(name, value, display, joinKey) {
    var nodes = qa('[data-metric="' + name + '"]');
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].setAttribute('data-value', String(value));
      if (joinKey) nodes[i].setAttribute('data-join-key', joinKey);
      if (display !== undefined && nodes[i].classList.contains('metric')) nodes[i].textContent = display;
    }
  }

  function renderFrameLabels(state) {
    var frame = pack(state.source).frames[state.frame];
    var label = q('#frame-label');
    if (label) {
      label.textContent = String(state.frame + 1);
      label.setAttribute('data-value', String(state.frame + 1));
      label.setAttribute('data-join-key', 'row ' + (state.frame + 1));
    }
    var bucket = q('#bucket-label');
    if (bucket) {
      bucket.textContent = frame.time_bucket;
      bucket.setAttribute('data-value', frame.time_bucket);
      bucket.setAttribute('data-join-key', 'time_bucket=' + frame.time_bucket);
    }
    var scrub = q('[data-control="scrub"]');
    if (scrub) scrub.value = String(state.frame);
    var playState = q('#play-state');
    if (playState) {
      playState.textContent = state.playing ? '播放中' : '已暂停';
      playState.setAttribute('data-playing', state.playing ? 'true' : 'false');
    }
    var playBtn = q('[data-control="play"]');
    if (playBtn) {
      playBtn.textContent = state.playing ? '暂停' : '播放';
      playBtn.setAttribute('data-state', state.playing ? 'playing' : 'paused');
    }
  }

  function renderState(state) {
    var chapter = state.chapter;
    var src = q('#state-source');
    if (src) { src.textContent = state.source.toUpperCase(); src.setAttribute('data-value', state.source); }
    var mode = q('#state-mode');
    if (mode) {
      mode.textContent = state.mode === 'delta' ? '未满足请求差分' : '并排对照';
      mode.setAttribute('data-value', state.mode);
    }
    var cell = q('#state-cell');
    if (cell) {
      cell.textContent = state.cell || '未选择';
      cell.setAttribute('data-value', state.cell || '');
    }
    var cellSelect = q('[data-cell-select]');
    if (cellSelect && state.cell && cellSelect.value !== state.cell) cellSelect.value = state.cell;
    var scenario = q('#state-scenario');
    if (scenario) { scenario.textContent = state.scenario; scenario.setAttribute('data-value', state.scenario); }
    qa('[data-mode]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-mode') === state.mode);
    });
    qa('[data-source-switch]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-source-switch') === state.source);
    });
    var side = q('[data-view="side"]'), deltaView = q('[data-view="delta"]');
    if (side) side.hidden = state.mode !== 'side';
    if (deltaView) deltaView.hidden = state.mode !== 'delta';
    // Projection and mode are independent switches: the rule grid and the
    // geographic view each honour the current mode, so a reader can compare the
    // same delta picture in both projections without losing the cell.
    var geo = state.projection === 'geo';
    var panes = q('#panes');
    if (panes) panes.hidden = geo;
    var geoPanes = q('[data-geo-panes]');
    if (geoPanes) geoPanes.hidden = !geo;
    qa('[data-geo-strategy]').forEach(function (node) {
      var isDelta = node.getAttribute('data-geo-strategy') === 'delta';
      node.hidden = isDelta !== (state.mode === 'delta');
    });
    qa('[data-projection]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-projection') === state.projection);
    });
    var projectionNote = q('#projection-note');
    if (projectionNote) {
      projectionNote.setAttribute('data-projection', state.projection);
      projectionNote.textContent = geo
        ? '地理底图：按真实经纬度投影，南北方向正确；底图只作位置语境，不参与指标计算。切换回规则网格不改变选中格子。'
        : '规则网格按行列排布，格子等距，只表示相对位置；切换地理底图会保留数据源、时段与选中格子。';
    }
    var guideChapter = q('#guide-chapter');
    if (guideChapter) {
      guideChapter.textContent = chapter < 0 ? '未开始' : String(chapter + 1);
      guideChapter.setAttribute('data-value', String(chapter));
    }
    var guideState = q('#guide-state');
    if (guideState) {
      guideState.textContent = state.started ? ('导览进行中 · 第 ' + (state.chapter + 1) + ' 章') : '未开始';
      guideState.setAttribute('data-state', state.started ? 'active' : 'idle');
    }
    var guideTitle = q('#guide-title');
    if (guideTitle) {
      var chapters = pack(state.source).guide.chapters;
      guideTitle.textContent = chapter >= 0 && chapters[chapter] ? chapters[chapter].title : '未开始';
    }
    qa('.chapter').forEach(function (node) {
      var index = parseInt(node.getAttribute('data-chapter'), 10);
      node.setAttribute('data-active', index === state.chapter ? 'true' : 'false');
      node.hidden = state.started ? index !== state.chapter : false;
    });
    qa('[data-guide-action]').forEach(function (button) {
      var action = button.getAttribute('data-guide-action');
      var n = pack(state.source).guide.n_chapters;
      button.disabled = (action === 'prev' && state.chapter <= 0) ||
        (action === 'next' && state.chapter >= n - 1) ||
        ((action === 'prev' || action === 'next') && !state.started);
    });
    var ledgerCell = q('#ledger-cell');
    if (ledgerCell) {
      var chosen = state.cell || pack(state.source).ledger_top_cell.cell_id;
      ledgerCell.textContent = chosen;
      ledgerCell.setAttribute('data-value', chosen);
    }
    qa('[data-scenario]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-scenario') === state.scenario);
    });
    qa('.scenario-table tr').forEach(function (row) {
      row.classList.toggle('is-current', row.getAttribute('data-scenario') === state.scenario);
    });
    var scope = q('#scenario-scope');
    if (scope) {
      scope.textContent = '当前显示：baseline 回放地图；场景 ' + state.scenario +
        ' 只更新上表数字，不改写地图数据。';
    }
  }

  function renderLedger(state) {
    var data = pack(state.source);
    var cell = state.cell || data.ledger_top_cell.cell_id;
    var index = data.cell_ids.indexOf(cell);
    if (index < 0) return;
    var frame = data.frames[state.frame];
    ['rule', 'optimized'].forEach(function (strategy) {
      var block = frame.strategies[strategy];
      var incoming = Number(block.moved_in[String(index)] || 0);
      var out = block.ledger.out[index];
      var start = block.ledger.start[index];
      var rapp = block.ledger.rapp[index];
      var met = block.ledger.met[index];
      var values = {
        inventory_start: start,
        moved_in: incoming,
        moved_out: out,
        scheduled_inventory: start + incoming - out,
        returns: block.ledger.ret[index],
        returns_applied: rapp,
        returns_overflow: block.ledger.ret[index] - rapp,
        borrow_requests: block.ledger.req[index],
        borrow_met: met,
        unmet_requests: block.ledger.req[index] - met,
        inventory_end: start + incoming - out + rapp - met
      };
      // The evidence trail has to describe the numbers on screen, so it is
      // rewritten from the same state that rewrites the numbers.
      var join = 'cell_id=' + cell + ';time_bucket=' + frame.time_bucket;
      var operands = {
        scheduled_inventory: { inventory_start: start, moved_in: incoming, moved_out: out },
        returns_overflow: { returns: block.ledger.ret[index], returns_applied: rapp },
        unmet_requests: { borrow_requests: block.ledger.req[index], borrow_met: met },
        inventory_end: {
          inventory_start: start, moved_in: incoming, moved_out: out,
          returns_applied: rapp, borrow_met: met
        }
      };
      var pane = q('[data-ledger-strategy="' + strategy + '"]');
      if (!pane) return;
      for (var name in values) {
        if (!values.hasOwnProperty(name)) continue;
        var node = pane.querySelector('[data-metric="' + name + '"]');
        if (!node) continue;
        node.setAttribute('data-value', String(values[name]));
        node.setAttribute('data-join-key', join);
        if (operands[name]) node.setAttribute('data-operands', JSON.stringify(operands[name]));
        else node.removeAttribute('data-operands');
        node.textContent = fmtInt(values[name]);
      }
      var moveList = pane.querySelector('.cell-moves');
      if (moveList) {
        while (moveList.firstChild) moveList.removeChild(moveList.firstChild);
        var records = replay(state.source).frames[state.frame].moves[strategy] || [];
        var listed = 0;
        for (var m = 0; m < records.length; m++) {
          var move = records[m];
          if (move.from_cell !== cell && move.to_cell !== cell) continue;
          var li = document.createElement('li');
          li.className = 'move-row';
          li.setAttribute('data-direction', move.to_cell === cell ? 'in' : 'out');
          li.setAttribute('data-from', move.from_cell);
          li.setAttribute('data-to', move.to_cell);
          li.setAttribute('data-bikes', move.bikes);
          li.setAttribute('data-source', 'outputs/sim_moves.' + strategy + '.' + state.source + '.json');
          li.textContent = move.from_cell + ' → ' + move.to_cell + '：' + move.bikes + ' 辆（' +
            Math.round(move.distance_m) + ' m）';
          moveList.appendChild(li);
          listed++;
        }
        if (!listed) {
          var emptyMove = document.createElement('li');
          emptyMove.className = 'empty';
          emptyMove.textContent = '本帧没有涉及该格的搬运记录。';
          moveList.appendChild(emptyMove);
        }
      }
      var body = pane.querySelector('.history-table tbody');
      if (body) {
        while (body.firstChild) body.removeChild(body.firstChild);
        for (var f = 0; f < data.frames.length; f++) {
          var row = data.frames[f];
          var cellBlock = row.strategies[strategy];
          var rowIn = Number(cellBlock.moved_in[String(index)] || 0);
          var tr = document.createElement('tr');
          tr.setAttribute('data-frame-index', f);
          tr.setAttribute('data-current', f === state.frame ? 'true' : 'false');
          var cells = [
            row.time_bucket,
            cellBlock.ledger.start[index],
            rowIn,
            cellBlock.ledger.out[index],
            cellBlock.ledger.start[index] + rowIn - cellBlock.ledger.out[index] +
              cellBlock.ledger.rapp[index] - cellBlock.ledger.met[index],
            cellBlock.ledger.req[index] - cellBlock.ledger.met[index]
          ];
          for (var c = 0; c < cells.length; c++) {
            var td = document.createElement('td');
            td.textContent = typeof cells[c] === 'number' ? fmtInt(cells[c]) : cells[c];
            tr.appendChild(td);
          }
          body.appendChild(tr);
        }
        var empty = pane.querySelector('.history-empty');
        if (empty) empty.hidden = true;
      }
    });
  }

  // -- F22: prediction series -----------------------------------------------
  var CHART = { width: 720, height: 230, pad: { l: 52, r: 14, t: 12, b: 30 } };

  // The float series are published as base64 little-endian float64 so they
  // round-trip bit-exactly; NaN marks a point the parquet does not have.
  function decodeFloats(block) {
    var raw = atob(block.values);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    var view = new DataView(bytes.buffer);
    var out = new Float64Array(bytes.length / 8);
    for (var j = 0; j < out.length; j++) out[j] = view.getFloat64(j * 8, true);
    return out;
  }

  function seriesFor(metric, name) {
    var block = F.metrics[metric];
    if (!block || !block.series[name]) return null;
    var raw = block.series[name];
    if (raw.kind === 'int') {
      var flat = [];
      for (var r = 0; r < raw.values.length; r++) {
        for (var c = 0; c < raw.values[r].length; c++) flat.push(raw.values[r][c]);
      }
      return flat;
    }
    return decodeFloats(raw);
  }

  function forecastValues(state, metric) {
    var block = F.metrics[metric];
    if (!block) return null;
    if (state.forecastRange === 'source') {
      return {
        actual: block.summary.actual.values,
        baseline: block.summary.baseline.values,
        pred: block.summary.pred.values,
        nCells: block.summary.actual.n_cells,
        local: block.local.source,
        scope: 'source'
      };
    }
    var index = F.cell_ids.indexOf(state.cell || pack(state.source).ledger_top_cell.cell_id);
    if (index < 0) return null;
    var n = F.n_buckets;
    var actual = seriesFor(metric, 'actual');
    var baseline = seriesFor(metric, 'baseline');
    var pred = seriesFor(metric, 'pred');
    var cellNet = function (name) {
      if (metric !== 'net') return seriesFor(metric, name).slice(index * n, index * n + n);
      var outV = seriesFor('out', name).slice(index * n, index * n + n);
      var inV = seriesFor('in', name).slice(index * n, index * n + n);
      return outV.map(function (value, i) { return value - inV[i]; });
    };
    return {
      actual: cellNet('actual'),
      baseline: cellNet('baseline'),
      pred: cellNet('pred'),
      nCells: 1,
      local: block.local.per_cell[index],
      scope: 'cell',
      cell: F.cell_ids[index]
    };
  }

  // The chart is rebuilt as markup, not as elements: workbench.js must never
  // construct SVG nodes (the rule-grid delta map is updated in place, and that
  // property is what keeps its geometry single-sourced in Python).  A string is
  // also the cheaper path for 149 points that all change when the range flips.
  function esc(value) {
    return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function renderForecast(state) {
    var svg = q('#forecast-chart');
    if (!F || !svg) return;
    var metric = state.forecastMetric;
    var values = forecastValues(state, metric);
    var labels = F.buckets;
    var frame = state.frame;
    svg.setAttribute('viewBox', '0 0 ' + CHART.width + ' ' + CHART.height);

    if (!values) {
      svg.innerHTML = '<text x="16" y="28" class="chart-empty">该格没有预测数据</text>';
      // Nothing below runs, so the labels and readouts have to be cleared here:
      // leaving the previous render's numbers on screen would show one cell's
      // MAE next to "this cell has no forecast".
      var emptyRange = q('#forecast-range-label');
      if (emptyRange) {
        emptyRange.setAttribute('data-value', 'none');
        emptyRange.innerHTML = '范围：<b>当前格</b>；该格没有预测数据。';
      }
      var emptyMae = q('#forecast-mae-label');
      if (emptyMae) emptyMae.textContent = '局部 MAE';
      ['forecast_bucket', 'forecast_actual', 'forecast_baseline', 'forecast_pred',
        'forecast_error', 'forecast_abs_error', 'forecast_mae', 'forecast_n'].forEach(function (name) {
        setMetric(name, null, '—', 'scope=none');
      });
      qa('[data-forecast-metric]').forEach(function (button) {
        button.classList.toggle('active', button.getAttribute('data-forecast-metric') === metric);
      });
      qa('[data-forecast-range]').forEach(function (button) {
        button.classList.toggle('active', button.getAttribute('data-forecast-range') === state.forecastRange);
      });
      return;
    }

    var all = values.actual.concat(values.baseline, values.pred).filter(function (v) {
      return v !== null && v !== undefined && isFinite(v);
    });
    var lo = all.length ? Math.min.apply(null, all) : 0;
    var hi = all.length ? Math.max.apply(null, all) : 1;
    if (hi === lo) { hi = lo + 1; lo = lo - 1; }
    var innerW = CHART.width - CHART.pad.l - CHART.pad.r;
    var innerH = CHART.height - CHART.pad.t - CHART.pad.b;
    var x = function (i) { return CHART.pad.l + (labels.length > 1 ? (i / (labels.length - 1)) * innerW : innerW / 2); };
    var y = function (v) { return CHART.pad.t + innerH - ((v - lo) / (hi - lo)) * innerH; };

    var parts = [];
    if (lo < 0 && hi > 0) {
      parts.push('<line x1="' + CHART.pad.l + '" y1="' + y(0).toFixed(1) + '" x2="' +
        (CHART.width - CHART.pad.r) + '" y2="' + y(0).toFixed(1) + '" class="chart-zero"/>');
    }
    parts.push('<line x1="' + CHART.pad.l + '" y1="' + (CHART.pad.t + innerH) + '" x2="' +
      (CHART.width - CHART.pad.r) + '" y2="' + (CHART.pad.t + innerH) + '" class="chart-axis"/>');
    [[hi, 'hi'], [lo, 'lo']].forEach(function (pair) {
      parts.push('<text x="' + (CHART.pad.l - 8) + '" y="' + (y(pair[0]) + 4).toFixed(1) +
        '" class="chart-tick" text-anchor="end">' + esc(fmtNum(pair[0], Math.abs(pair[0]) < 10 ? 2 : 0)) + '</text>');
    });
    parts.push('<text x="' + x(0).toFixed(1) + '" y="' + (CHART.height - 8) +
      '" class="chart-tick" text-anchor="start">' + esc(labels[0].slice(5, 16)) + '</text>');
    if (labels.length > 1) {
      parts.push('<text x="' + x(labels.length - 1).toFixed(1) + '" y="' + (CHART.height - 8) +
        '" class="chart-tick" text-anchor="end">' + esc(labels[labels.length - 1].slice(5, 16)) + '</text>');
    }

    // The cursor marks the frame the map is showing, so the chart and the map
    // can never disagree about which time is on screen.
    if (frame >= 0 && frame < labels.length) {
      parts.push('<line x1="' + x(frame).toFixed(1) + '" y1="' + CHART.pad.t + '" x2="' +
        x(frame).toFixed(1) + '" y2="' + (CHART.pad.t + innerH) + '" class="chart-cursor"/>');
    }

    ['baseline', 'pred', 'actual'].forEach(function (name) {
      var series = values[name];
      var segment = [];
      var flush = function () {
        if (segment.length > 1) {
          parts.push('<polyline points="' + segment.join(' ') + '" class="series ' + name +
            '" fill="none"/>');
        }
        segment = [];
      };
      series.forEach(function (value, i) {
        if (value === null || value === undefined || !isFinite(value)) { flush(); return; }
        segment.push(x(i).toFixed(1) + ',' + y(value).toFixed(1));
      });
      flush();
    });

    // Clickable points: each names its bucket, and the click is handled by the
    // delegated listener below, so the chart and the clock cannot drift apart.
    values.actual.forEach(function (value, i) {
      if (value === null || value === undefined || !isFinite(value)) return;
      parts.push('<circle cx="' + x(i).toFixed(1) + '" cy="' + y(value).toFixed(1) +
        '" r="4" class="chart-point" data-forecast-bucket="' + i + '" data-bucket="' +
        esc(labels[i]) + '"><title>' + esc(labels[i]) + ' 实际 ' + esc(fmtNum(value, 2)) +
        '</title></circle>');
    });
    svg.innerHTML = parts.join('');

    var bucketIndex = frame >= 0 && frame < labels.length ? frame : 0;
    var label = q('#forecast-range-label');
    var scopeText = values.scope === 'source'
      ? '范围：<b>全源汇总</b>（每时段对 ' + values.nCells + ' 个格子求和）'
      : '范围：<b>当前格 ' + esc(values.cell) + '</b>';
    if (label) {
      label.setAttribute('data-value', values.scope);
      label.innerHTML = scopeText + '；指标：<b>' + esc(F.labels.metrics[metric]) + '</b>。' +
        (metric === 'net' ? esc(F.labels.net) + '。' : '');
    }
    var readout = {
      forecast_bucket: labels[bucketIndex],
      forecast_actual: values.actual[bucketIndex],
      forecast_baseline: values.baseline[bucketIndex],
      forecast_pred: values.pred[bucketIndex]
    };
    var join = 'scope=' + values.scope + (values.cell ? ';cell_id=' + values.cell : '') +
      ';metric=' + metric + ';time_bucket=' + labels[bucketIndex];
    setMetric('forecast_bucket', readout.forecast_bucket, readout.forecast_bucket, join);
    setMetric('forecast_actual', readout.forecast_actual, fmtNum(readout.forecast_actual, 2), join);
    setMetric('forecast_baseline', readout.forecast_baseline, fmtNum(readout.forecast_baseline, 2), join);
    setMetric('forecast_pred', readout.forecast_pred, fmtNum(readout.forecast_pred, 2), join);
    var has = readout.forecast_actual !== null && readout.forecast_actual !== undefined &&
      readout.forecast_pred !== null && readout.forecast_pred !== undefined;
    var error = has ? readout.forecast_pred - readout.forecast_actual : null;
    setMetric('forecast_error', error, fmtNum(error, 2), join);
    var absError = error === null ? null : Math.abs(error);
    setMetric('forecast_abs_error', absError, fmtNum(absError, 2), join);
    setMetric('forecast_mae', values.local.mae, fmtNum(values.local.mae, 3), join);
    setMetric('forecast_n', values.local.n, String(values.local.n), join);
    // The number changes meaning with the range, so its label must change too:
    // a source-wide MAE wearing the "局部" label is a different quantity.
    var maeLabel = q('#forecast-mae-label');
    if (maeLabel) maeLabel.textContent = values.scope === 'source' ? '全源 MAE' : '局部 MAE';
    // The page has to show the model-vs-baseline comparison it cites, including
    // when the model loses -- XM's borrowed-bike prediction does.
    var evalNode = q('#forecast-eval');
    if (evalNode && F.evaluation && F.evaluation.metrics) {
      var ev = F.evaluation.metrics;
      var rows = [['借出', 'out_count'], ['归还', 'in_count']].map(function (pair) {
        var base = ev.baseline[pair[1]].mae;
        var model = ev.model[pair[1]].mae;
        return pair[0] + ' MAE 基线 ' + fmtNum(base, 3) + ' / 模型 ' + fmtNum(model, 3) +
          '（' + (model <= base ? '模型更好' : '模型未优于基线') + '）';
      });
      evalNode.innerHTML = '全源评估引用既有产物 <code>outputs/forecast_metrics.' +
        esc(F.source) + '.json</code>：' + rows.join('；') +
        '。MAE 是点估计误差，<strong>不是预测区间</strong>；' +
        '它与本页按格重算的局部 MAE 不是同一口径，标签与样本数分别显示。';
    }
    qa('[data-forecast-metric]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-forecast-metric') === metric);
    });
    qa('[data-forecast-range]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-forecast-range') === state.forecastRange);
    });
  }

  function renderAll() {
    var state = core.get();
    renderGrids(state);
    renderSummary(state);
    renderFrameLabels(state);
    renderState(state);
    renderLedger(state);
    if (Geo) Geo.render(state);
    renderForecast(state);
    if (window.__WORKBENCH_RENDER_COUNT__ !== undefined) window.__WORKBENCH_RENDER_COUNT__++;
  }

  function stop() { if (timer) { clearTimeout(timer); timer = null; } }

  function schedule() {
    stop();
    var state = core.get();
    if (!state.playing) return;
    var ms = parseInt((q('.controls') || {}).getAttribute ? q('.controls').getAttribute('data-step-ms') : '', 10) || 900;
    timer = setTimeout(function () {
      var now = core.get();
      if (!now.playing) return;
      if (now.frame >= pack(now.source).n_frames - 1) {
        core.setPlaying(false);
        renderAll();
        return;
      }
      core.setFrame(now.frame + 1);
      renderAll();
      schedule();
    }, ms);
  }

  function act(fn) { var ok = fn(); renderAll(); if (!core.get().playing) stop(); else schedule(); return ok; }

  qa('[data-guide-action]').forEach(function (button) {
    button.addEventListener('click', function () {
      var action = button.getAttribute('data-guide-action');
      if (action === 'start') act(function () { return core.start(); });
      else if (action === 'next') act(function () { return core.next(); });
      else if (action === 'prev') act(function () { return core.prev(); });
      else if (action === 'restart') act(function () { return core.restart(); });
      else if (action === 'exit') act(function () { return core.exit(); });
      var chapter = q('.chapter[data-active="true"]');
      if (chapter) chapter.scrollIntoView({ block: 'nearest' });
    });
  });

  qa('[data-event]').forEach(function (button) {
    button.addEventListener('click', function () {
      act(function () { return core.jumpToEvent(button.getAttribute('data-event')); });
      var maps = q('[data-maps]');
      if (maps) maps.scrollIntoView({ block: 'start' });
    });
  });

  qa('[data-mode]').forEach(function (button) {
    button.addEventListener('click', function () {
      act(function () { return core.setMode(button.getAttribute('data-mode')); });
    });
  });

  qa('[data-projection]').forEach(function (button) {
    button.addEventListener('click', function () {
      act(function () { return core.setProjection(button.getAttribute('data-projection')); });
    });
  });

  qa('[data-forecast-metric]').forEach(function (button) {
    button.addEventListener('click', function () {
      act(function () { return core.setForecastMetric(button.getAttribute('data-forecast-metric')); });
    });
  });

  qa('[data-forecast-range]').forEach(function (button) {
    button.addEventListener('click', function () {
      act(function () { return core.setForecastRange(button.getAttribute('data-forecast-range')); });
    });
  });

  // Clicking a point on the curve jumps the clock to that bucket and pauses --
  // the same rule as an event jump: the target must not be taken away by
  // playback.  The selected cell is untouched.
  var chart = q('#forecast-chart');
  if (chart) chart.addEventListener('click', function (event) {
    var target = event.target;
    var bucket = target && target.getAttribute ? target.getAttribute('data-forecast-bucket') : null;
    if (bucket === null || bucket === undefined) return;
    var index = parseInt(bucket, 10);
    if (!isFinite(index)) return;
    act(function () { core.setPlaying(false); return core.setFrame(index); });
  });

  qa('[data-scenario]').forEach(function (button) {
    button.addEventListener('click', function () {
      act(function () { return core.setScenario(button.getAttribute('data-scenario')); });
    });
  });

  function selectCell(cellId) {
    act(function () { return core.selectCell(cellId); });
    var ledger = q('#ledger');
    if (ledger) ledger.scrollIntoView({ block: 'nearest' });
  }

  document.addEventListener('click', function (event) {
    var target = event.target;
    while (target && target !== document.body && !target.getAttribute('data-cell')) target = target.parentNode;
    if (target && target.getAttribute && target.getAttribute('data-cell')) selectCell(target.getAttribute('data-cell'));
  });

  document.addEventListener('keydown', function (event) {
    var tag = (event.target && event.target.tagName || '').toLowerCase();
    var onCell = event.target && event.target.getAttribute && event.target.getAttribute('data-cell');
    if (onCell && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      selectCell(onCell);
      return;
    }
    if (tag === 'input' || tag === 'select' || tag === 'textarea' || tag === 'button') return;
    if (event.key === 'ArrowLeft') { event.preventDefault(); act(function () { return core.setFrame(core.get().frame - 1); }); }
    else if (event.key === 'ArrowRight') { event.preventDefault(); act(function () { return core.setFrame(core.get().frame + 1); }); }
    else if (event.key === 'Escape' && core.get().started) { act(function () { return core.exit(); }); }
  });

  var playBtn = q('[data-control="play"]');
  if (playBtn) playBtn.addEventListener('click', function () { act(function () { return core.setPlaying(!core.get().playing); }); });
  var prevBtn = q('[data-control="prev"]');
  if (prevBtn) prevBtn.addEventListener('click', function () {
    act(function () { core.setPlaying(false); return core.setFrame(core.get().frame - 1); });
  });
  var nextBtn = q('[data-control="next"]');
  if (nextBtn) nextBtn.addEventListener('click', function () {
    act(function () { core.setPlaying(false); return core.setFrame(core.get().frame + 1); });
  });
  var scrub = q('[data-control="scrub"]');
  if (scrub) scrub.addEventListener('input', function () {
    act(function () { core.setPlaying(false); return core.setFrame(parseInt(scrub.value, 10)); });
  });

  // A select is the keyboard path to cell selection: a grid with 255 focusable
  // rects would be 255 tab stops, which is not usable navigation.
  var cellSelect = q('[data-cell-select]');
  if (cellSelect) cellSelect.addEventListener('change', function () {
    act(function () { return core.selectCell(cellSelect.value); });
  });

  var reduced = false;
  try { reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) { reduced = false; }
  if (reduced) core.setPlaying(false);

  renderAll();
  window.__WORKBENCH_READY__ = true;
})();
