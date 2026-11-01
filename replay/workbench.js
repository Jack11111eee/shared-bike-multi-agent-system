/* Phase 4: DOM wiring only.  All navigation rules live in workbench-core.js. */
(function () {
  'use strict';
  var Core = window.WorkbenchCore;
  var P = window.PRESENTATION_DATA;
  var R = window.REPLAY_DATA_SOURCES;
  if (!Core || !P || !R) return;

  var sourceNames = Object.keys(P);
  var core = Core.create({ source: document.body.getAttribute('data-source'), sources: P });
  var timer = null;

  function q(selector) { return document.querySelector(selector); }
  function qa(selector) { return Array.prototype.slice.call(document.querySelectorAll(selector)); }
  function fmtInt(v) { return Number(v).toLocaleString('en-US'); }
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

  function renderAll() {
    var state = core.get();
    renderGrids(state);
    renderSummary(state);
    renderFrameLabels(state);
    renderState(state);
    renderLedger(state);
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
