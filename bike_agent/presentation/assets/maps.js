/* Optional online basemaps. Business values and geometry stay run-bound.
 * Standard OSM tiles are requested only for the visible map, never prefetched.
 */
(function () {
  'use strict';
  var active = null, loader = null, converted = {};
  function dispose() {if(active){try{active.remove?active.remove():active.destroy();}catch(e){}active=null;}}
  function loadAmap(cfg){
    if(window.AMap)return Promise.resolve(window.AMap);
    if(loader)return loader;
    loader=new Promise(function(resolve,reject){
      if(!cfg.amapKey){reject(new Error('未配置高德 JS Key'));return;}
      if(cfg.securityJsCode)window._AMapSecurityConfig={securityJsCode:cfg.securityJsCode};
      var done=false, script=document.createElement('script');
      var timeout=setTimeout(function(){if(!done){done=true;reject(new Error('高德加载超时'));}},8000);
      window.BIKE_AMAP_READY=function(){if(!done){done=true;clearTimeout(timeout);resolve(window.AMap);}};
      script.onerror=function(){if(!done){done=true;clearTimeout(timeout);reject(new Error('高德脚本不可用'));}};
      script.src='https://webapi.amap.com/maps?v=2.0&key='+encodeURIComponent(cfg.amapKey)+'&callback=BIKE_AMAP_READY';
      document.head.appendChild(script);
    });
    return loader;
  }
  function coordinates(A,features){
    var key=JSON.stringify(features.map(function(f){return [f.id,f.center,f.polygon];}));
    if(converted[key])return Promise.resolve(converted[key]);
    var unique=[], indices={};
    features.forEach(function(f){[f.center].concat(f.polygon).forEach(function(p){var id=p.join(',');if(indices[id]===undefined){indices[id]=unique.length;unique.push(p);}});});
    var output=[];
    var chain=Promise.resolve();
    for(var i=0;i<unique.length;i+=40)(function(batch){
      chain=chain.then(function(){return new Promise(function(resolve,reject){
        var done=false,t=setTimeout(function(){if(!done){done=true;reject(new Error('高德坐标转换超时，安全配置可能缺失'));}},6000);
        A.convertFrom(batch,'gps',function(status,result){
          if(done)return;done=true;clearTimeout(t);
          if(status!=='complete'||!result||!result.locations){reject(new Error('高德坐标转换或鉴权失败'));return;}
          result.locations.forEach(function(p){output.push([p.getLng(),p.getLat()]);});resolve();
        });
      });});
    })(unique.slice(i,i+40));
    return chain.then(function(){
      var transformed=features.map(function(f){
        return Object.assign({},f,{center:output[indices[f.center.join(',')]],
          polygon:f.polygon.map(function(p){return output[indices[p.join(',')]];})});
      });
      converted[key]=transformed;return transformed;
    });
  }
  function mount(host,data,provider,onCell,onStatus){
    if(provider==='offline'){onStatus('离线地理底图');return;}
    var cfg=window.BIKE_MAP_CONFIG||{};
    var mountId=Symbol('map');host._mountId=mountId;
    function current(){return host.isConnected&&host._mountId===mountId;}
    function layer(){var n=document.createElement('div');n.className='online-map';n.style.opacity='0';n.style.pointerEvents='none';host.appendChild(n);return n;}
    function reveal(n,label){
      if(!current())return;
      Array.from(host.children).forEach(function(child){if(child!==n)child.remove();});
      n.style.opacity='1';n.style.pointerEvents='auto';onStatus(label);
    }
    function osm(reason){
      if(!current())return;
      if(provider==='amap'){onStatus('高德不可用，保留离线底图');return;}
      if(!window.L){onStatus('在线地图不可用，保留离线底图');return;}
      onStatus((reason?reason+'；':'')+'正在尝试 OpenStreetMap…');
      var n=layer(), map=L.map(n,{zoomControl:true,scrollWheelZoom:true,attributionControl:true});
      active=map;
      var tiles=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{
        maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
        keepBuffer:0,updateWhenIdle:true,
      }).addTo(map);
      var loaded=0,timer=setTimeout(function(){
        if(current()&&!loaded){map.remove();if(active===map)active=null;n.remove();onStatus('在线瓦片不可用，保留离线地理底图');}
      },9000);
      tiles.on('tileload',function(){if(!loaded++){clearTimeout(timer);reveal(n,'OpenStreetMap · 在线底图');}});
      var centers={};
      data.features.forEach(function(f){
        centers[f.id]=[f.center[1],f.center[0]];
        L.polygon(f.polygon.map(function(p){return [p[1],p[0]];}),{
          color:f.selected?'#123f3a':'#d2dfd5',weight:f.selected?2:.4,
          fillColor:f.color,fillOpacity:f.opacity,
        }).bindTooltip(f.id+' · '+f.value).on('click',function(){onCell(f.id);}).addTo(map);
      });
      data.moves.forEach(function(m){
        if(centers[m.from_cell]&&centers[m.to_cell])L.polyline([centers[m.from_cell],centers[m.to_cell]],{
          color:'#286e8a',weight:1+Math.log1p(m.bikes)/2,
        }).bindTooltip(m.from_cell+' → '+m.to_cell+'：'+m.bikes+' 辆').addTo(map);
      });
      map.fitBounds(L.latLngBounds(Object.values(centers)),{padding:[12,12],animate:false});
    }
    if(provider==='osm'||!cfg.amapKey){osm();return;}
    onStatus('正在验证高德地图…');
    loadAmap(cfg).then(function(A){return coordinates(A,data.features).then(function(features){return {A:A,features:features};});})
      .then(function(result){
        if(!current())return;
        var A=result.A,n=layer(), features=result.features;
        var map=new A.Map(n,{zoom:12,center:features[0].center,viewMode:'2D',resizeEnable:true});
        active=map;var completed=false;
        var timeout=setTimeout(function(){
          if(!completed&&current()){map.destroy();if(active===map)active=null;n.remove();osm('高德底图加载超时');}
        },9000);
        map.on('complete',function(){if(completed)return;completed=true;clearTimeout(timeout);reveal(n,'高德地图 · 在线底图（已转换坐标）');});
        var centers={};
        features.forEach(function(f){
          centers[f.id]=f.center;
          var polygon=new A.Polygon({path:f.polygon,strokeColor:f.selected?'#12433e':'#d2dfd5',
            strokeWeight:f.selected?2:.5,fillColor:f.color,fillOpacity:f.opacity});
          polygon.on('click',function(){onCell(f.id);});map.add(polygon);
        });
        data.moves.forEach(function(m){if(centers[m.from_cell]&&centers[m.to_cell])map.add(new A.Polyline({
          path:[centers[m.from_cell],centers[m.to_cell]],strokeColor:'#286e8a',
          strokeWeight:2,showDir:true,
        }));});
        map.setFitView(null,false,[12,12,12,12]);
      }).catch(function(){if(current())osm('高德鉴权或安全配置不可用');});
  }
  window.BikeMaps={mount:mount,dispose:dispose};
})();
