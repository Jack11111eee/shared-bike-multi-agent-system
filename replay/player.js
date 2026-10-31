(function(){
  var D = window.REPLAY_DATA;
  if(!D || !D.frames || !D.frames.length) return;
  var STRATS = ['rule','optimized'];
  var n = D.n_frames;
  var NS = 'http://www.w3.org/2000/svg';
  var index = 0, playing = false, speed = 1, loop = false, timer = null;
  var reduced = false;
  try {
    reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch(e) { reduced = false; }

  var playBtn = document.querySelector('[data-control="play"]');
  var scrub = document.querySelector('[data-control="scrub"]');
  var loopBox = document.querySelector('[data-control="loop"]');
  var frameLabel = document.getElementById('frame-label');
  var bucketLabel = document.getElementById('bucket-label');
  var trend = document.querySelector('svg.trend');
  var step = (D.cell_px || 12) + (D.cell_gap || 1);
  var colorMax = (D.color_domain && D.color_domain.max) || 1;
  // The pause comes from the page Python wrote, so the documented default and
  // the running animation cannot disagree.
  var controls = document.querySelector('.controls');
  var baseMs = parseInt(controls ? controls.getAttribute('data-step-ms') : '', 10) || 900;

  function fmtInt(v){ return Number(v).toLocaleString('en-US'); }
  function fmtFloat(v){ return Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
  function sectionFor(strategy){
    return document.querySelector('section.strategy[data-strategy="' + strategy + '"]');
  }
  function opacityFor(value){ return (0.12 + 0.88 * (Number(value) / (colorMax || 1))).toFixed(3); }
  function centreOf(cellId){
    var i = D.cell_ids.indexOf(cellId);
    if(i < 0) return null;
    var ix = D.geom[i][0], iy = D.geom[i][1];
    return [ix * step + (D.cell_px || 12) / 2, iy * step + (D.cell_px || 12) / 2];
  }

  function updateGrid(section, strategy, strat){
    var rects = section.querySelectorAll('rect.cell');
    for(var k = 0; k < rects.length && k < D.cell_ids.length; k++){
      var inv = strat.inventory_end[k], unmet = strat.unmet[k], moved = strat.moved[k];
      var rect = rects[k];
      rect.setAttribute('data-inventory-end', inv);
      rect.setAttribute('data-unmet', unmet);
      rect.setAttribute('data-moved', moved);
      rect.setAttribute('fill-opacity', opacityFor(inv));
      rect.setAttribute('class', unmet > 0 ? 'cell unmet' : 'cell');
      var title = rect.querySelector('title');
      if(title) title.textContent = D.cell_ids[k] + ' 期末 ' + inv + ' 未满足 ' + unmet + ' 搬出 ' + moved;
    }
  }

  function updateArrows(section, strategy, moves){
    var group = section.querySelector('g.arrows');
    while(group.firstChild) group.removeChild(group.firstChild);
    for(var i = 0; i < moves.length; i++){
      var move = moves[i];
      var a = centreOf(move.from_cell), b = centreOf(move.to_cell);
      if(!a || !b) continue;
      var x1 = a[0], y1 = a[1], x2 = b[0], y2 = b[1];
      var span = Math.sqrt((x2-x1)*(x2-x1) + (y2-y1)*(y2-y1));
      if(span > 0){
        var shrink = Math.min((D.cell_px || 12) * 0.45, span / 3);
        x2 -= (x2-x1)/span * shrink;
        y2 -= (y2-y1)/span * shrink;
      }
      var line = document.createElementNS(NS, 'line');
      line.setAttribute('class', 'arrow');
      line.setAttribute('x1', x1.toFixed(2));
      line.setAttribute('y1', y1.toFixed(2));
      line.setAttribute('x2', x2.toFixed(2));
      line.setAttribute('y2', y2.toFixed(2));
      line.setAttribute('marker-end', 'url(#arrow-' + strategy + ')');
      line.setAttribute('data-source', 'outputs/sim_moves.' + strategy + '.' + D.source + '.json');
      line.setAttribute('data-from', move.from_cell);
      line.setAttribute('data-to', move.to_cell);
      line.setAttribute('data-bikes', move.bikes);
      line.setAttribute('stroke-width', (0.6 + Math.min(2.4, move.bikes / 30)).toFixed(2));
      var title = document.createElementNS(NS, 'title');
      title.textContent = move.from_cell + ' → ' + move.to_cell + ': ' + move.bikes + ' 辆（' +
        Math.round(move.distance_m) + ' m）';
      line.appendChild(title);
      group.appendChild(line);
    }
  }

  function updateKpis(section, strategy, frame){
    var kpi = frame.strategies[strategy].kpi;
    var moves = frame.moves[strategy] || [];
    var movedBikes = 0;
    for(var i = 0; i < moves.length; i++) movedBikes += moves[i].bikes;
    var values = {
      borrow_requests: kpi.borrow_requests, borrow_met: kpi.borrow_met,
      unmet_requests: kpi.unmet_requests, returns: kpi.returns,
      returns_applied: kpi.returns_applied, returns_overflow: kpi.returns_overflow,
      bikes_moved: kpi.bikes_moved, distance_bike_km: kpi.distance_bike_km,
      inventory_start: kpi.inventory_start, inventory_end: kpi.inventory_end,
      n_moves: moves.length, moves_bikes: movedBikes, n_cells: D.cell_ids.length
    };
    var previous = index > 0 ? D.frames[index-1].strategies[strategy].kpi : null;
    for(var name in values){
      if(!values.hasOwnProperty(name)) continue;
      var dd = section.querySelector('dd[data-metric="' + name + '"]');
      if(!dd) continue;
      var value = values[name];
      dd.setAttribute('data-value', String(value));
      // The element carries its own format, so the JS does not need to know
      // which metric is measured in bike-km.
      var isFloat = dd.getAttribute('data-format') === 'float';
      var span = dd.querySelector('.value');
      if(span) span.textContent = isFloat ? fmtFloat(value) : fmtInt(value);
      var delta = dd.querySelector('.delta');
      if(delta){
        if(!previous || previous[name] === undefined){
          delta.textContent = '—';
          delta.setAttribute('data-prev', '');
          delta.setAttribute('class', 'delta');
        } else {
          var diff = value - previous[name];
          var text = (diff > 0 ? '+' : '') + (isFloat ? diff.toFixed(2) : fmtInt(diff));
          delta.textContent = text;
          delta.setAttribute('data-prev', String(previous[name]));
          delta.setAttribute('class', 'delta' + (diff > 0 ? ' up' : ''));
        }
      }
    }
  }

  function updateDetail(section, strategy, frame){
    var moves = frame.moves[strategy] || [];
    var moveList = section.querySelector('.move-list');
    var heading = section.querySelector('.details h3');
    if(heading) heading.textContent = '本帧搬运明细（' + moves.length + ' 条）';
    while(moveList.firstChild) moveList.removeChild(moveList.firstChild);
    if(!moves.length){
      var empty = document.createElement('li');
      empty.className = 'empty';
      empty.textContent = '本帧没有搬运记录。';
      moveList.appendChild(empty);
    } else {
      for(var i = 0; i < moves.length; i++){
        var move = moves[i];
        var li = document.createElement('li');
        li.className = 'move-row';
        li.setAttribute('data-from', move.from_cell);
        li.setAttribute('data-to', move.to_cell);
        li.setAttribute('data-bikes', move.bikes);
        li.setAttribute('data-source', 'outputs/sim_moves.' + strategy + '.' + D.source + '.json');
        li.textContent = move.from_cell + ' → ' + move.to_cell + '：' + move.bikes + ' 辆';
        moveList.appendChild(li);
      }
    }
    var unmetList = section.querySelector('.unmet-list');
    while(unmetList.firstChild) unmetList.removeChild(unmetList.firstChild);
    var any = false;
    for(var k = 0; k < frame.strategies[strategy].unmet.length; k++){
      var unmet = frame.strategies[strategy].unmet[k];
      if(unmet <= 0) continue;
      any = true;
      var item = document.createElement('li');
      item.className = 'unmet-row';
      item.setAttribute('data-cell', D.cell_ids[k]);
      item.setAttribute('data-unmet', unmet);
      item.setAttribute('data-source', 'outputs/replay.' + strategy + '.' + D.source + '.parquet');
      item.textContent = D.cell_ids[k] + '：' + unmet;
      unmetList.appendChild(item);
    }
    if(!any){
      var none = document.createElement('li');
      none.className = 'empty';
      none.textContent = '本帧没有未满足请求。';
      unmetList.appendChild(none);
    }
  }

  function updateTrend(){
    var bars = document.querySelectorAll('svg.trend [data-seek]');
    for(var i = 0; i < bars.length; i++){
      var current = parseInt(bars[i].getAttribute('data-frame-index'), 10) === index;
      bars[i].classList.toggle('is-current', current);
    }
    var cursor = document.querySelector('svg.trend .trend-cursor');
    if(cursor){
      cursor.setAttribute('data-frame-index', String(index));
      var points = document.querySelectorAll('svg.trend .trend-point');
      var x = points[index] ? points[index].getAttribute('cx') : '0';
      cursor.setAttribute('x1', x);
      cursor.setAttribute('x2', x);
    }
  }

  function render(){
    var frame = D.frames[index];
    if(frameLabel){
      frameLabel.textContent = String(index + 1);
      frameLabel.setAttribute('data-value', String(index + 1));
    }
    if(bucketLabel){
      bucketLabel.textContent = frame.time_bucket;
      bucketLabel.setAttribute('data-value', frame.time_bucket);
    }
    if(scrub) scrub.value = String(index);
    for(var s = 0; s < STRATS.length; s++){
      var strategy = STRATS[s];
      var section = sectionFor(strategy);
      if(!section) continue;
      updateGrid(section, strategy, frame.strategies[strategy]);
      updateArrows(section, strategy, frame.moves[strategy] || []);
      updateKpis(section, strategy, frame);
      updateDetail(section, strategy, frame);
    }
    updateTrend();
  }

  function goto(i){
    if(n <= 0) return;
    if(i < 0) i = 0;
    if(i > n - 1) i = n - 1;
    index = i;
    render();
  }

  function schedule(){
    if(timer) clearTimeout(timer);
    if(!playing) return;
    var pause = baseMs / speed;
    timer = setTimeout(function(){
      if(index >= n - 1){
        if(loop){ goto(0); schedule(); }
        else { playing = false; reflectPlayState(); }
      } else {
        goto(index + 1);
        schedule();
      }
    }, pause);
  }

  function reflectPlayState(){
    if(!playBtn) return;
    playBtn.textContent = playing ? '暂停' : '播放';
    playBtn.setAttribute('data-state', playing ? 'playing' : 'paused');
  }

  function setPlaying(value){
    playing = !!value;
    reflectPlayState();
    if(playing) schedule(); else if(timer) clearTimeout(timer);
  }

  if(playBtn) playBtn.addEventListener('click', function(){ setPlaying(!playing); });
  var prevBtn = document.querySelector('[data-control="prev"]');
  if(prevBtn) prevBtn.addEventListener('click', function(){ setPlaying(false); goto(index - 1); });
  var nextBtn = document.querySelector('[data-control="next"]');
  if(nextBtn) nextBtn.addEventListener('click', function(){ setPlaying(false); goto(index + 1); });
  if(scrub) scrub.addEventListener('input', function(){ setPlaying(false); goto(parseInt(scrub.value, 10)); });
  if(loopBox) loopBox.addEventListener('change', function(){ loop = !!loopBox.checked; schedule(); });

  var speedButtons = document.querySelectorAll('[data-control="speed"]');
  for(var b = 0; b < speedButtons.length; b++){
    speedButtons[b].addEventListener('click', function(){
      speed = parseFloat(this.getAttribute('data-speed')) || 1;
      for(var k = 0; k < speedButtons.length; k++){
        speedButtons[k].classList.toggle('active', speedButtons[k] === this);
      }
      if(playing) schedule();
    });
  }

  if(trend){
    trend.addEventListener('click', function(event){
      var target = event.target;
      while(target && target !== trend && !target.getAttribute('data-seek')) target = target.parentNode;
      if(target && target !== trend && target.getAttribute('data-seek') !== null){
        setPlaying(false);
        goto(parseInt(target.getAttribute('data-seek'), 10));
      }
    });
  }

  document.addEventListener('keydown', function(event){
    var tag = (event.target && event.target.tagName || '').toLowerCase();
    if(tag === 'input' || tag === 'select' || tag === 'textarea') return;
    if(event.key === ' ' || event.key === 'Spacebar'){
      // A focused button already activates on Space (native click); acting on
      // it here as well would toggle twice and look like the key did nothing.
      if(tag === 'button') return;
      event.preventDefault();
      setPlaying(!playing);
    } else if(event.key === 'ArrowLeft'){
      event.preventDefault(); setPlaying(false); goto(index - 1);
    } else if(event.key === 'ArrowRight'){
      event.preventDefault(); setPlaying(false); goto(index + 1);
    } else if(event.key === 'Home'){
      event.preventDefault(); setPlaying(false); goto(0);
    } else if(event.key === 'End'){
      event.preventDefault(); setPlaying(false); goto(n - 1);
    }
  });

  render();
  // NF-20: honour reduced motion by never starting on its own.
  if(!reduced) setPlaying(true);
})();
