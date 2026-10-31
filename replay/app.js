(function(){
  var node = document.getElementById('frame-data');
  if(!node) return;
  var data = JSON.parse(node.textContent);
  var NS = 'http://www.w3.org/2000/svg';
  // Pitch comes from the payload so the rects stay aligned with the arrows,
  // which Python positions using the same CELL_PX + CELL_GAP.
  var step = data.cell_px + data.cell_gap;
  document.querySelectorAll('svg.grid').forEach(function(svg){
    var payload = data.strategies[svg.getAttribute('data-strategy')];
    if(!payload) return;
    var layer = svg.querySelector('g.cells');
    var maxInventory = Math.max(1, payload.max_inventory);
    for(var i = 0; i < data.ids.length; i++){
      var cellId = data.ids[i], ix = data.geom[i][0], iy = data.geom[i][1];
      var invEnd = payload.values[i][0], unmet = payload.values[i][1], moved = payload.values[i][2];
      var rect = document.createElementNS(NS, 'rect');
      rect.setAttribute('x', (ix * step).toString());
      rect.setAttribute('y', (iy * step).toString());
      rect.setAttribute('width', data.cell_px);
      rect.setAttribute('height', data.cell_px);
      rect.setAttribute('rx', 1.5);
      rect.setAttribute('opacity', (0.15 + 0.85 * (invEnd / maxInventory)).toFixed(3));
      rect.setAttribute('data-source', svg.getAttribute('data-source'));
      rect.setAttribute('data-cell', cellId);
      rect.setAttribute('data-inventory-end', invEnd);
      rect.setAttribute('data-unmet', unmet);
      rect.setAttribute('data-moved', moved);
      if (unmet > 0) rect.setAttribute('class', 'unmet');
      var title = document.createElementNS(NS, 'title');
      title.textContent = cellId + ' 期末 ' + invEnd + ' 未满足 ' + unmet + ' 搬出 ' + moved;
      rect.appendChild(title);
      layer.appendChild(rect);
    }
  });
})();
