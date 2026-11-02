(function () {
  'use strict';
  var runs = window.BIKE_RUNS || [], config = window.BIKE_CONSOLE_CONFIG || {};
  var params = new URLSearchParams(location.hash.slice(1));
  var pathRun = location.pathname.match(/\/console\/run\/([A-Za-z0-9_-]+)/);
  var state = {
    runId: params.get('run') || (pathRun && pathRun[1]) || (runs[0] && runs[0].identity.run_id),
    view: ['overview','workflow','result'].includes(params.get('view')) ? params.get('view') : 'overview', cursor: Number(params.get('event') || 0)||0,
    tab: 'candidates', candidateId: params.get('candidate') || 'baseline',
    frame: Number(params.get('frame') || 0)||0, cell: params.get('cell'),
    layer: 'gain', mapProvider: 'auto', playing: false, speed: 1
  };
  var timer = null, app = document.getElementById('app');
  if ('scrollRestoration' in history) history.scrollRestoration='manual';
  var roles = {
    coordinator: {name:'协调智能体', icon:'◎', job:'分派任务 · 反馈修订 · 最终决策'},
    diagnosis: {name:'诊断智能体', icon:'⌕', job:'查询证据 · 定位问题 · 说明缺口'},
    dispatch: {name:'方案智能体', icon:'⇄', job:'提出候选 · 权衡资源 · 响应反馈'},
    evaluation: {name:'评估智能体', icon:'✓', job:'调用仿真 · 核验约束 · 评审方案'}
  };
  var terminalLabels = {accepted:'找到可行候选', baseline_retained:'保留基准',
    no_feasible_candidate:'本次未找到可行方案', failed:'执行失败', limit_reached:'达到运行上限'};
  var eventLabels = {run_started:'任务启动',role_started:'开始处理',model_request_started:'请求模型',
    model_request_completed:'模型已响应',model_request_failed:'模型请求失败',tool_started:'调用工具',
    tool_completed:'工具已返回',tool_failed:'工具执行失败',role_output_submitted:'交付结果',
    action_rejected:'动作被校验器拒绝',candidate_proposed:'提出候选',candidate_evaluated:'仿真结果已生成',
    revision_requested:'要求修订方案',decision_finalized:'提交最终决策',run_failed:'运行终止',
    run_cancelled:'运行已取消',artifacts_published:'展示产物已发布'};
  function esc(v) {return String(v == null ? '' : v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function fmt(v,d) {return v == null ? '—' : Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d == null ? 0:d,minimumFractionDigits:d || 0});}
  function pct(v) {return v == null ? '—' : (v*100).toFixed(2)+'%';}
  function date(v) {return v?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v)):'';}
  function sign(v,d) {return v == null ? '—' : (v>0?'+':'')+fmt(v,d);}
  function run() {return runs.find(function(r){return r.identity.run_id===state.runId;}) || runs[0];}
  function badge(s,cls) {return '<span class="badge '+(cls||'')+'">'+esc(s)+'</span>';}
  function strategy(s) {return s==='optimized'?'约束优化策略':'规则策略';}
  function candidateLabel(cid){var match=String(cid).match(/^cand-r(\d+)-(\d+)$/);return cid==='baseline'?'基准方案':match?'第 '+match[1]+' 轮 · 方案 '+match[2]:cid;}
  function fieldLabel(fields){return (fields||[]).map(function(f){return f==='strategy'?'调度策略':'每小时搬运上限';}).join(' / ');}
  function decisionSummary(r){
    var c=r.decision.selected;
    if(!c)return terminalLabels[r.decision.terminal_state]+'。本次没有可签收的最终方案。'+
      (r.decision.execution_status==='completed'?'结论仅限本次已评估候选，不代表所有合法组合都不可行。':r.decision.reason);
    var result=c.result, base=r.candidates.find(function(x){return x.is_baseline;});
    return '选择'+strategy(c.strategy)+'，每小时搬运上限 '+c.budget+' 辆。模拟借车满足率 '+pct(result.metrics.service_rate)+
      (base&&base.result?'，基准为 '+pct(base.result.metrics.service_rate):'')+'；工具约束校验通过。效果仅适用于本次历史回放与已评估候选。';
  }
  function deliverySummary(role,p){
    if(role==='coordinator'){
      if(p.action==='finalize')return p.candidate_id?'提交 '+candidateLabel(p.candidate_id)+' 作为最终选择，由程序核验可行性与排序。':'提交“本次未找到可行候选”，保留评估证据。';
      if(p.action==='request_revision')return '要求修订 '+candidateLabel(p.candidate_id)+'，修改 '+fieldLabel(p.revision_fields)+'，并保留原候选和评估来源。';
      return '将任务交给'+(roles[p.assignee]?roles[p.assignee].name:'下一角色')+'，依据已有证据继续处理。';
    }
    if(role==='diagnosis')return '交付 '+p.focus_buckets.length+' 个关注时段和 '+p.focus_cells.length+' 个待核查区域。关注时段：'+p.focus_buckets.map(function(b){return b.slice(11,16);}).join('、')+'。区域原因仍需结合格级账本核查。';
    if(role==='dispatch')return '提出 '+p.candidates.map(function(c){return strategy(c.strategy)+' / '+c.budget+' 辆上限';}).join('；')+
      (p.revision_of?'。修订自 '+candidateLabel(p.revision_of)+'，实际修改 '+fieldLabel(p.changed_fields):'。实际效果交由仿真工具评估')+'。';
    return p.assessments.map(function(a){return candidateLabel(a.candidate_id)+'：'+(a.verdict==='feasible'?'通过硬约束':'未通过硬约束');}).join('；')+'。评审引用实际仿真结果。';
  }
  function visibleEvents() {var r=run();return r ? r.events.slice(0,state.view==='workflow'?state.cursor+1:r.events.length) : [];}
  function candidates() {
    var r=run(); if(!r)return [];
    if(state.view!=='workflow')return r.candidates;
    var known={}, evaluations={};
    visibleEvents().forEach(function(e){
      if(e.kind==='candidate_proposed')known[e.candidate_id]=true;
      if(e.kind==='candidate_evaluated')evaluations[e.candidate_id]=e.result;
    });
    return r.candidates.filter(function(c){return c.is_baseline||known[c.candidate_id];}).map(function(c){
      var copy=Object.assign({},c);delete copy.result;delete copy.assessment;
      if(evaluations[c.candidate_id])copy.result=evaluations[c.candidate_id];
      return copy;
    });
  }
  function selectedCandidate() {
    var cs=candidates();return cs.find(function(c){return c.candidate_id===state.candidateId;}) || cs[0];
  }
  function latestOutput(role) {
    return visibleEvents().filter(function(e){return e.role===role&&e.kind==='role_output_submitted';}).slice(-1)[0];
  }
  function evidenceLabel(ref){
    var r=run(), available=visibleEvents();
    var completed=new Set(available.filter(function(e){return e.kind==='tool_completed';}).map(function(e){return e.tool_call_id;}));
    var tool=r.tool_calls.find(function(t){return completed.has(t.tool_call_id)&&(t.tool_call_id===ref||t.output.result_id===ref||t.output.artifact_set_id===ref);});
    if(tool){
      if(tool.tool==='get_forecast')return '预测查询 · '+String(tool.input.time_bucket).slice(11,16);
      if(tool.tool==='get_risk_ranking')return tool.input.direction==='clear'?'清运风险清单':'补车风险清单';
      return '仿真 · '+strategy(tool.output.strategy)+' / '+tool.output.budget;
    }
    var msg=available.find(function(e){return e.message_id===ref;});
    return msg&&roles[msg.role]?roles[msg.role].name+'交付':ref;
  }
  function keyEvents() {
    return run().events.map(function(e,i){return {e:e,i:i};}).filter(function(x){
      return ['run_started','role_started','role_output_submitted','candidate_evaluated','revision_requested',
        'decision_finalized','run_failed','run_cancelled','action_rejected'].includes(x.e.kind);
    });
  }
  function saveHash() {
    var q=new URLSearchParams({run:state.runId,view:state.view,event:String(state.cursor),
      candidate:state.candidateId,frame:String(state.frame)});
    if(state.cell)q.set('cell',state.cell);
    history.replaceState(null,'','#'+q.toString());
  }
  function stop(){state.playing=false;if(timer)clearInterval(timer);timer=null;}
  function setView(view){stop();state.view=view;if(view==='result'){var d=run().decision;state.candidateId=d.selected?d.selected.candidate_id:'baseline';}render();window.scrollTo(0,0);}
  function step(dir){
    var keys=keyEvents(), next=dir>0?keys.find(function(k){return k.i>state.cursor;}):keys.slice().reverse().find(function(k){return k.i<state.cursor;});
    if(next)state.cursor=next.i;else stop();
    var current=run().events[state.cursor];
    if(current&&current.candidate_id)state.candidateId=current.candidate_id;
    render();
  }
  function play(){
    if(state.playing){stop();render();return;}
    if(state.cursor>=run().events.length-1)state.cursor=0;
    state.playing=true;
    timer=setInterval(function(){step(1);},2200/state.speed);
    render();
  }
  function stat(label,value,unit,note,cls){
    return '<div class="stat"><div class="stat-label">'+esc(label)+'</div><div class="stat-value '+(cls||'')+'">'+esc(value)+'<small>'+esc(unit||'')+'</small></div><div class="stat-note">'+esc(note||'')+'</div></div>';
  }
  function exportLink(name,label){
    var base=config.standalone?'':'exports/'+encodeURIComponent(state.runId)+'/';
    return '<a class="chip" href="'+base+name+'" '+(name.endsWith('.csv')||name.endsWith('.md')?'download':'target="_blank" rel="noopener"')+'>'+esc(label)+'</a>';
  }
  function roleCards(){
    return '<div class="role-summary">'+Object.keys(roles).map(function(k,i){
      return '<article><h3><span class="num-circle">0'+(i+1)+'</span>'+roles[k].name+'</h3><p>'+roles[k].job+'</p></article>';
    }).join('')+'</div>';
  }
  function header(r){
    var source=r.task.source==='sh'?'上海':'厦门';
    return '<div class="page-heading"><div><div class="eyebrow">URBAN MOBILITY / DECISION WORKSPACE</div><h1>'+source+'早高峰调度复盘</h1><p class="subtext">从问题诊断到方案评审，让每一次调度都有依据。</p></div>'+
      '<select id="run-select" class="run-select" aria-label="选择运行">'+runs.map(function(v){
        return '<option value="'+esc(v.identity.run_id)+'" '+(v.identity.run_id===state.runId?'selected':'')+'>'+
          esc((v.task.source==='sh'?'上海':'厦门')+' · '+terminalLabels[v.decision.terminal_state]+' · '+date(v.execution.started_at))+'</option>';
      }).join('')+'</select></div>';
  }
  function overview(r){
    var task=r.task,d=r.decision,selected=d.selected, s=selected&&selected.result;
    var baseline=r.candidates.find(function(c){return c.is_baseline;}), b=baseline&&baseline.result;
    var stats=stat('本次目标',task.min_service_rate==null?'未设置':pct(task.min_service_rate),'','模拟借车满足率下限')+
      stat('搬运资源上限',fmt(task.max_budget),'辆 / 小时','与实际模拟搬运量分别展示')+
      stat('协作角色',fmt(Object.keys(d.model_roles||{}).length),'个','真实角色交付数量，使用独立上下文')+
      stat('真实执行耗时',fmt((r.execution.timing||{}).task_elapsed_ms/1000,1),'秒','回放停留时长与真实耗时独立');
    return '<section class="hero"><div class="eyebrow">ONE TASK. FOUR AGENTS. TRACEABLE DECISIONS.</div><h2>在有限搬运资源下，<br>找到有证据支持的调度方案。</h2><p>读取历史订单形成的预测与风险证据，由四个角色协作提出候选、调用仿真、评审约束，并提交最终决策。</p>'+
      '<div class="actions"><button class="primary" data-view="workflow">查看协作回放 →</button><button class="secondary" data-view="result">查看决策结果</button></div></section>'+
      '<div class="stats">'+stats+'</div><div class="overview-grid"><section class="panel"><div class="panel-head"><h2>四个角色，明确交接</h2><small>模型决策 × 工具验证</small></div><div class="panel-body">'+roleCards()+'</div></section>'+
      '<section class="panel"><div class="panel-head"><h2>这次任务解决什么</h2>'+badge(terminalLabels[d.terminal_state],d.execution_status==='completed'?'':'warn')+'</div><div class="panel-body"><div class="fact-list">'+
      '<div class="fact-row"><span>分析方式</span><b>历史回放与模拟决策</b></div>'+
      '<div class="fact-row"><span>允许策略</span><b>规则调度 / 约束优化</b></div>'+
      '<div class="fact-row"><span>预算档位</span><b>'+esc((task.budget_options||[]).join(' / '))+' 辆 / 小时</b></div>'+
      '<div class="fact-row"><span>本次结果</span><b>'+(s?strategy(selected.strategy)+' · '+selected.budget+' 辆上限':'无有效最终选择')+'</b></div>'+
      '<div class="fact-row"><span>基准 → 选择满足率</span><b>'+pct(b&&b.metrics.service_rate)+' → '+pct(s&&s.metrics.service_rate)+'</b></div></div>'+
      '<p class="subtext">'+esc(decisionSummary(r))+'</p></div></section></div>';
  }
  function flow(r){
    var current=r.events[state.cursor]||{}, active=current.role;
    var nodeHtml=Object.keys(roles).map(function(role){
      var started=visibleEvents().some(function(e){return e.kind==='role_started'&&e.role===role;});
      var out=latestOutput(role), cls=(active===role?' selected':'')+(out?' done':'');
      return '<button class="flow-node'+cls+'" data-role="'+role+'"><span class="node-icon">'+roles[role].icon+'</span><span class="node-state">'+(active===role?'当前步骤':out?'已交付':started?'已开始':'待执行')+'</span><strong>'+roles[role].name+'</strong><small>'+roles[role].job+'</small></button>';
    }).join('');
    var direction=active==='coordinator'?((current.payload&&current.payload.assignee)||(current.kind==='revision_requested'?'dispatch':null)):active;
    var returning=active!=='coordinator'&&current.kind==='role_output_submitted';
    return '<div class="flow-area"><svg viewBox="0 0 800 400" aria-hidden="true"><defs><marker id="edgeArrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto-start-reverse"><path d="M0 0L6 3L0 6" fill="#6ba89c"/></marker></defs>'+
      ['diagnosis','dispatch','evaluation'].map(function(role,i){
        var y=[65,195,325][i];
        var marker=direction===role?(returning?' marker-start="url(#edgeArrow)"':' marker-end="url(#edgeArrow)"'):'';
        return '<path class="flow-line '+(direction===role?'active':'')+'" d="M350 195 C400 195 400 '+y+' 440 '+y+'"'+marker+'/>';
      }).join('')+'</svg>'+nodeHtml+'<span class="flow-caption">'+
      (direction&&roles[direction]?(returning?roles[direction].name+' → 协调智能体 · 交付结果':'协调智能体 → '+roles[direction].name+' · 任务与反馈'):'交付与反馈经协调角色流转 · 工具执行由程序完成')+'</span></div>';
  }
  function detail(r){
    var current=r.events[state.cursor]||{}, role=current.role;
    if(!roles[role]){
      var last=visibleEvents().filter(function(e){return roles[e.role];}).slice(-1)[0];role=last&&last.role;
    }
    if(!role)return '<div class="detail-area"><div class="eyebrow">TASK READY</div><h2>从任务开始，逐步查看决策</h2><p class="subtext">点击播放或下一步。每个节点可查看输入、行动、产出与证据。</p></div>';
    var started=visibleEvents().filter(function(e){return e.role===role&&e.kind==='role_started';}).slice(-1)[0];
    var occurrences=visibleEvents().filter(function(e){return e.role===role&&e.kind==='role_started';});
    var output=visibleEvents().filter(function(e){return e.role===role&&e.kind==='role_output_submitted'&&(!started||e.sequence>started.sequence);}).slice(-1)[0];
    var p=output?output.payload:null, context=started?started.context:{};
    var assignment=(context.recent_messages||[]).filter(function(m){return m.sender==='coordinator';}).slice(-1)[0];
    var tools=visibleEvents().filter(function(e){return e.role===role&&e.kind==='tool_completed'&&(!started||e.sequence>started.sequence);});
    return '<div class="detail-area"><div class="detail-role"><span class="detail-title">'+roles[role].name+'</span>'+
      '<select id="role-occurrence" class="occurrence-select" aria-label="角色执行轮次">'+occurrences.map(function(o,i){return '<option value="'+(o.sequence-1)+'" '+(started&&o.sequence===started.sequence?'selected':'')+'>第 '+(i+1)+' 次 · 第 '+o.round+' 轮</option>';}).join('')+'</select>'+badge(output?'已交付':'回放到此步骤')+'</div>'+
      '<div class="detail-section"><span class="detail-label">01 / 收到了什么</span><p>'+esc(role!=='coordinator'&&assignment?assignment.payload.reason:context.instruction||'根据任务和上游交付继续处理。')+'</p></div>'+
      '<div class="detail-section"><span class="detail-label">02 / 做了什么</span><p>'+esc(tools.length?'调用 '+tools.map(function(t){return ({get_forecast:'需求预测查询',get_risk_ranking:'失衡风险清单',evaluate_dispatch_candidate:'候选仿真评估'})[t.tool]||t.tool;}).join('、'):roles[role].job)+'</p></div>'+
      '<div class="detail-section"><span class="detail-label">03 / 交付了什么 · 按结构化字段整理</span><p class="output-text">'+esc(p?deliverySummary(role,p):'此回放位置尚未产生角色交付。')+'</p></div>'+
      '<div class="detail-section"><span class="detail-label">04 / 证据与交接</span>'+
      (p&&p.evidence_refs.length?p.evidence_refs.map(function(ref){return '<button class="chip" title="'+esc(ref)+'" data-evidence="'+esc(ref)+'">'+esc(evidenceLabel(ref))+' ↗</button>';}).join(''):'<span class="tiny">证据随工具调用与角色交付逐步形成</span>')+'</div>'+
      (p?'<details><summary>展开模型的原始解释</summary><p class="subtext">'+esc(p.summary||p.reason)+'</p><p class="tiny">模型解释保留原文，因果判断和推测不作为已核验指标。</p></details>':'')+
      '<details><summary>查看当前步骤的结构化记录</summary><pre class="raw">'+esc(JSON.stringify(p||current,null,2))+'</pre></details></div>';
  }
  function table(r){
    var cs=candidates();
    if(!cs.length)return '<div class="empty">此步骤尚未提出候选。</div>';
    return '<div class="table-wrap"><table><thead><tr><th>方案</th><th>策略 / 每小时上限</th><th>模拟满足率</th><th>未满足请求</th><th>搬运工作量</th><th>综合代价</th><th>约束</th></tr></thead><tbody>'+cs.map(function(c){
      var result=c.result,m=result?result.metrics:{}, cid=c.candidate_id;
      return '<tr tabindex="0" data-candidate="'+esc(cid)+'" class="'+(cid===state.candidateId?'selected':'')+'"><td><b>'+esc(candidateLabel(cid))+'</b><small>'+esc(c.revision_of?'修订自 '+candidateLabel(c.revision_of):c.is_baseline?'冻结任务指定':'独立新提案')+'</small></td>'+
        '<td>'+strategy(c.strategy)+'<small>'+fmt(c.budget)+' 辆 / 小时</small></td><td class="num">'+pct(m.service_rate)+'</td><td class="num">'+fmt(m.unmet_requests)+'<small>次</small></td><td class="num">'+fmt(m.distance_bike_km,2)+'<small>车·公里</small></td><td class="num">'+fmt(m.composite_cost,2)+'</td><td>'+
        (result?'<span class="status-dot '+(!result.feasible?'bad':'')+'"></span>'+(result.feasible?'达标':'未达标'):'待评估')+'</td></tr>';
    }).join('')+'</tbody></table></div><p class="table-note">点击方案查看其空间结果。每小时上限是资源约束；增加上限带来的改善包含资源效应。综合代价为加权分值。</p>';
  }
  function spatial(r){
    var c=selectedCandidate(), data=c&&c.result&&r.spatial[c.candidate_id];
    if(!data)return '<div class="empty">当前候选尚无已发布的空间产物。请先推进到评估完成，或选择已有结果的候选。</div>';
    state.frame=Math.max(0,Math.min(state.frame,data.frames.length-1));
    var bucket=String(data.frames[state.frame].time_bucket).replace('T',' ');
    var rows=data.states.filter(function(x){return String(x.time_bucket).replace('T',' ')===bucket;});
    if(!rows.length)return '<div class="empty">当前时间单元没有空间状态。</div>';
    var baselineRows={}, baseData=r.spatial.baseline;
    if(baseData)baseData.states.forEach(function(x){if(String(x.time_bucket).replace('T',' ')===bucket)baselineRows[x.cell_id]=x;});
    if(!rows.some(function(x){return x.cell_id===state.cell;})){
      state.cell=rows.slice().sort(function(a,b){
        if(state.layer==='gain'){
          var ag=baselineRows[a.cell_id]?Math.abs(baselineRows[a.cell_id].unmet_requests-a.unmet_requests):0;
          var bg=baselineRows[b.cell_id]?Math.abs(baselineRows[b.cell_id].unmet_requests-b.unmet_requests):0;
          if(ag!==bg)return bg-ag;
        }
        return b.unmet_requests-a.unmet_requests;
      })[0].cell_id;
    }
    var row=rows.find(function(x){return x.cell_id===state.cell;});
    var moves=data.moves.filter(function(x){return String(x.time_bucket).replace('T',' ')===bucket;});
    var inbound=moves.filter(function(m){return m.to_cell===state.cell;}).reduce(function(s,m){return s+m.bikes;},0);
    var outbound=moves.filter(function(m){return m.from_cell===state.cell;}).reduce(function(s,m){return s+m.bikes;},0);
    var base=r.spatial.baseline, brow=base&&base.states.find(function(x){return x.cell_id===state.cell&&String(x.time_bucket).replace('T',' ')===bucket;});
    var frameGain=rows.reduce(function(total,x){return total+(baselineRows[x.cell_id]?baselineRows[x.cell_id].unmet_requests-x.unmet_requests:0);},0);
    return '<div class="panel-body"><div class="map-controls"><div><b style="font-size:12px">'+esc(candidateLabel(c.candidate_id))+' · '+strategy(c.strategy)+' · '+c.budget+' 辆 / 小时</b><div class="tiny" style="margin-top:6px">本时段未满足 '+fmt(data.frames[state.frame].unmet_requests)+' 次 · 相对基准减少 '+sign(frameGain)+' 次</div><div class="tiny" style="margin-top:5px">结果 '+esc(c.result.result_id)+'</div></div><select id="map-layer" aria-label="地图指标">'+
      [['gain','相对基准改善'],['unmet','未满足请求'],['inventory','期末库存'],['moves','搬运关系']].map(function(x){return '<option value="'+x[0]+'" '+(state.layer===x[0]?'selected':'')+'>'+x[1]+'</option>';}).join('')+'</select></div>'+
      '<div class="map-provider-row"><select id="map-provider" aria-label="地图来源">'+[['auto','自动：高德 → OpenStreetMap → 离线'],['amap','尝试高德地图'],['osm','OpenStreetMap（无需 Key）'],['offline','离线地理底图']].map(function(x){return '<option value="'+x[0]+'" '+(state.mapProvider===x[0]?'selected':'')+'>'+x[1]+'</option>';}).join('')+'</select><span id="map-provider-status" class="map-provider-status">离线地理底图</span></div>'+
      '<div class="map-layout"><div><div id="geo-map" class="geo-map" data-result-id="'+esc(c.result.result_id)+'"></div><div class="map-legend">'+
      (state.layer==='gain'?'<span><i class="legend-key" style="background:#3e9b7d"></i>未满足减少 / 改善为正</span><span><i class="legend-key" style="background:#cb775e"></i>未满足增加</span>':'<span>颜色越深，当前指标越高</span>')+'<span>北向朝上 · 区域网格</span></div>'+
      '<div class="actions"><button data-frame="-1" '+(state.frame===0?'disabled':'')+'>←</button><span class="tiny">历史数据时段</span><select id="frame-select" class="cell-picker" aria-label="历史时段" style="margin:0">'+data.frames.map(function(f,i){return '<option value="'+i+'" '+(i===state.frame?'selected':'')+'>'+esc(f.time_bucket)+'</option>';}).join('')+'</select><button data-frame="1" '+(state.frame===data.frames.length-1?'disabled':'')+'>→</button></div>'+
      '<p class="map-attribution">离线地理底图：GSHHG © NOAA / University of Hawaii · © OpenStreetMap contributors，ODbL。'+
      '搬运连线为区域间调拨关系，不是道路导航路线。业务坐标按项目 WGS-84 口径显示。</p></div>'+
      '<div><h3 class="cell-title">区域库存账本</h3><select id="cell-select" class="cell-picker" aria-label="选择区域">'+rows.map(function(x){return '<option '+(x.cell_id===state.cell?'selected':'')+'>'+esc(x.cell_id)+'</option>';}).join('')+'</select><div class="fact-list">'+
      [['期初库存',row.inventory_start,'辆'],['模拟借车请求',row.borrow_requests,'次'],['调入 / 调出',inbound+' / '+outbound,'辆'],
       ['接纳归还',row.returns_applied,'辆'],['满足 / 未满足',row.borrow_met+' / '+row.unmet_requests,'次'],
       ['期末库存',row.inventory_end,'辆'],['相比基准，未满足减少',brow?brow.unmet_requests-row.unmet_requests:'—','次']].map(function(x){return '<div class="fact-row"><span>'+esc(x[0])+'</span><b>'+esc(x[1])+' '+x[2]+'</b></div>';}).join('')+
      '</div><p class="subtext">'+(moves.length?'本时段共 '+moves.length+' 条真实模拟搬运记录。':'本时段无模拟搬运。')+'</p></div></div></div>';
  }
  function renderMap(){
    var host=document.getElementById('geo-map');if(!host)return;
    var r=run(),c=selectedCandidate(),data=r.spatial[c.candidate_id],bucket=String(data.frames[state.frame].time_bucket).replace('T',' ');
    var b=r.geometry, ns='http://www.w3.org/2000/svg';
    function el(name,attrs){var n=document.createElementNS(ns,name);Object.keys(attrs||{}).forEach(function(k){n.setAttribute(k,attrs[k]);});return n;}
    var W=b?b.projection.width:760,H=b?b.projection.height:600;
    var svg=el('svg',{viewBox:'0 0 '+W+' '+H,role:'img','aria-label':'当前候选的逐格模拟空间结果'});
    var defs=el('defs'),marker=el('marker',{id:'moveArrow',markerWidth:7,markerHeight:7,refX:6,refY:3,orient:'auto'});marker.appendChild(el('path',{d:'M0 0L6 3L0 6',fill:'#286e8a'}));defs.appendChild(marker);svg.appendChild(defs);
    if(b){['land','water','admin'].forEach(function(k){b.layers[k].forEach(function(d){svg.appendChild(el('path',{d:d,'class':'geo-'+k}));});});}
    var rows={},baseline={};data.states.forEach(function(x){if(String(x.time_bucket).replace('T',' ')===bucket)rows[x.cell_id]=x;});
    var base=r.spatial.baseline;if(base)base.states.forEach(function(x){if(String(x.time_bucket).replace('T',' ')===bucket)baseline[x.cell_id]=x;});
    var cells=b?b.cells:(r.cells||[]).map(function(cell){return [cell.cell_id,cell.ix*30,400-cell.iy*30,29,29];});
    var centers={},max=1;
    // Fixed domain across all frames of this candidate; no per-frame rescaling.
    var baseAll={};if(base)base.states.forEach(function(x){baseAll[x.cell_id+'|'+x.time_bucket]=x;});
    data.states.forEach(function(x){var value;
      if(state.layer==='gain'){var old=baseAll[x.cell_id+'|'+x.time_bucket];value=old?old.unmet_requests-x.unmet_requests:0;}
      else value=state.layer==='inventory'?x.inventory_end:x.unmet_requests;
      max=Math.max(max,Math.abs(value));
    });
    cells.forEach(function(cell){
      var row=rows[cell[0]];if(!row)return;var value;
      if(state.layer==='gain')value=baseline[cell[0]]?baseline[cell[0]].unmet_requests-row.unmet_requests:0;
      else value=state.layer==='inventory'?row.inventory_end:row.unmet_requests;
      var color=state.layer==='gain'?(value>0?'#258563':value<0?'#bd604a':'#d8e2de'):'#297b9c';
      var opacity=value?(.15+.65*Math.abs(value)/max):.12;
      var rect=el('rect',{x:cell[1],y:cell[2],width:cell[3],height:cell[4],fill:color,'fill-opacity':opacity,
        stroke:cell[0]===state.cell?'#12443b':'#fff','stroke-width':cell[0]===state.cell?2:.4,'data-cell':cell[0]});
      var title=el('title');title.textContent=cell[0]+' · '+value;rect.appendChild(title);
      rect.addEventListener('click',function(){stop();state.cell=cell[0];render();});svg.appendChild(rect);
      centers[cell[0]]=[cell[1]+cell[3]/2,cell[2]+cell[4]/2];
    });
    if(state.layer==='moves')data.moves.filter(function(m){return String(m.time_bucket).replace('T',' ')===bucket;}).forEach(function(m){
      var a=centers[m.from_cell],z=centers[m.to_cell];if(!a||!z)return;
      var line=el('line',{x1:a[0],y1:a[1],x2:z[0],y2:z[1],stroke:'#286e8a','stroke-width':1+Math.log1p(m.bikes)/2,'marker-end':'url(#moveArrow)'});
      var title=el('title');title.textContent=m.from_cell+' → '+m.to_cell+'：'+m.bikes+' 辆';line.appendChild(title);svg.appendChild(line);
    });
    var north=el('text',{x:W-25,y:30,fill:'#6c8c96','font-size':15});north.textContent='N ↑';svg.appendChild(north);
    if(b&&b.scale_bar){var scale=b.scale_bar,bar=el('line',{x1:20,y1:H-25,x2:20+scale.pixels,y2:H-25,stroke:'#6a7c80','stroke-width':2});svg.appendChild(bar);var txt=el('text',{x:20,y:H-32,fill:'#6a7c80','font-size':12});txt.textContent=scale.label;svg.appendChild(txt);}
    host.replaceChildren(svg);
    if(window.BikeMaps){
      var byId={};(r.cells||[]).forEach(function(cell){byId[cell.cell_id]=cell;});
      var features=cells.filter(function(cell){return rows[cell[0]]&&byId[cell[0]];}).map(function(cell){
        var row=rows[cell[0]], value=state.layer==='gain'?(baseline[cell[0]]?baseline[cell[0]].unmet_requests-row.unmet_requests:0):
          state.layer==='inventory'?row.inventory_end:row.unmet_requests;
        var raw=byId[cell[0]], polygon;
        if(b){var p=b.projection;function inverse(x,y){return [p.bbox[0]+x/p.px_per_degree_lon,p.bbox[3]-y/p.px_per_degree_lat];}
          polygon=[inverse(cell[1],cell[2]),inverse(cell[1]+cell[3],cell[2]),inverse(cell[1]+cell[3],cell[2]+cell[4]),inverse(cell[1],cell[2]+cell[4])];
        }else{
          var dlat=(r.task.source==='xm'?1000:2000)/110540/2,dlon=(r.task.source==='xm'?1000:2000)/(111320*Math.cos(raw.lat*Math.PI/180))/2;
          polygon=[[raw.lon-dlon,raw.lat-dlat],[raw.lon+dlon,raw.lat-dlat],[raw.lon+dlon,raw.lat+dlat],[raw.lon-dlon,raw.lat+dlat]];
        }
        return {id:cell[0],center:[raw.lon,raw.lat],polygon:polygon,value:value,selected:cell[0]===state.cell,
          color:state.layer==='gain'?(value>0?'#258563':value<0?'#bd604a':'#d8e2de'):'#297b9c',
          opacity:value?(.15+.65*Math.abs(value)/max):.1};
      });
      window.BikeMaps.mount(host,{features:features,moves:state.layer==='moves'?data.moves.filter(function(m){return String(m.time_bucket).replace('T',' ')===bucket;}):[]},
        state.mapProvider,function(cell){stop();state.cell=cell;render();},function(label){var n=document.getElementById('map-provider-status');if(n&&host.isConnected)n.textContent=label;});
    }
  }
  function evidenceTab(){
    return '<div class="panel-body"><div class="notice">以下为当前回放位置之前实际发生的事件。模型内部推理不作为展示内容。</div><ol class="timeline-list">'+visibleEvents().map(function(e,i){
      return '<li><button data-event="'+i+'" class="'+(i===state.cursor?'active':'')+'"><small>#'+e.sequence+' · '+fmt(e.elapsed_ms/1000,1)+'s</small><span>'+esc((roles[e.role]?roles[e.role].name+' · ':'')+(eventLabels[e.kind]||e.kind))+'</span></button></li>';
    }).join('')+'</ol></div>';
  }
  function inputTab(r){
    var base=r.candidates.find(function(c){return c.is_baseline;});
    return '<div class="panel-body"><div class="notice">数据接入、清洗聚合、预测与风险计算已在本次智能体决策前完成。模型通过工具读取这些产物，没有在回放时重新训练模型。</div><div class="fact-list">'+
      [['数据来源',r.task.source==='xm'?'厦门历史共享单车订单':'上海历史共享单车订单'],
       ['分析网格',(r.cells||[]).length+' 个'],['候选策略','规则调度 / 约束优化'],
       ['分析时间范围',base&&base.result?base.result.summary.first_bucket+' 至 '+base.result.summary.last_bucket:'查看任务输入清单'],
       ['模拟假设','初始库存、停放容量与成本权重由配置提供，详见报告']].map(function(x){return '<div class="fact-row"><span>'+esc(x[0])+'</span><b>'+esc(x[1])+'</b></div>';}).join('')+'</div></div>';
  }
  function tabs(r){
    var names={candidates:'候选方案',spatial:'空间结果',inputs:'数据与假设',records:'事件记录'};
    return '<section class="panel"><div class="tabbar">'+Object.keys(names).map(function(k){return '<button data-tab="'+k+'" class="'+(state.tab===k?'active':'')+'">'+names[k]+'</button>';}).join('')+'</div>'+
      (state.tab==='candidates'?table(r):state.tab==='spatial'?spatial(r):state.tab==='inputs'?inputTab(r):evidenceTab())+'</section>';
  }
  function workflow(r){
    var ev=r.events[state.cursor]||{}, keyCount=keyEvents().filter(function(k){return k.i<=state.cursor;}).length;
    return '<div class="notice"><b>任务目标：模拟借车满足率 '+(r.task.min_service_rate==null?'不设下限':'≥ '+pct(r.task.min_service_rate))+' · 每小时最多搬运 '+r.task.max_budget+' 辆</b><br>真实运行回放 · '+esc(r.execution.model||'模型记录')+' · 历史数据时段与协作步骤分别控制，原始事件可展开查看。</div>'+
      '<div class="workflow-grid"><section class="panel"><div class="panel-head"><h2>多智能体协作</h2><small>步骤 '+keyCount+' / '+keyEvents().length+'</small></div>'+flow(r)+'</section>'+
      '<section class="panel"><div class="panel-head"><h2>当前步骤</h2><small>'+esc(eventLabels[ev.kind]||'准备就绪')+'</small></div>'+detail(r)+'</section></div>'+
      '<div class="playbar"><button id="play" class="primary">'+(state.playing?'Ⅱ 暂停':'▶ 播放')+'</button><button id="prev" '+(state.cursor===0?'disabled':'')+'>上一步</button><button id="next" '+(state.cursor>=r.events.length-1?'disabled':'')+'>下一步</button>'+
      '<input id="event-range" aria-label="协作事件进度" type="range" min="0" max="'+Math.max(0,r.events.length-1)+'" value="'+state.cursor+'"><label>'+Math.min(state.cursor+1,r.events.length)+' / '+r.events.length+' 事件</label>'+
      '<select id="speed" aria-label="演示速度">'+[.5,1,2].map(function(s){return '<option '+(s===state.speed?'selected':'')+' value="'+s+'">'+s+'× 演示</option>';}).join('')+'</select></div>'+tabs(r);
  }
  function result(r){
    var d=r.decision, c=d.selected, m=c&&c.result&&c.result.metrics, delta=c&&c.result&&c.result.deltas;
    var success=d.execution_status==='completed'&&c;
    var cards=stat('模拟借车满足率',m?pct(m.service_rate):'—','',delta?'相对基准 '+sign(delta.service_rate_delta_pp,2)+' 个百分点':'见已评估候选')+
      stat('模拟未满足请求',m?fmt(m.unmet_requests):'—','次',delta?'相对基准减少 '+fmt(delta.unmet_gain)+' 次':'不等于实测缺车数量')+
      stat('搬运工作量',m?fmt(m.distance_bike_km,2):'—','车·公里',delta?'相对基准 '+sign(delta.distance_delta,2)+' 车·公里':'不是运输车辆行驶里程')+
      stat('综合代价',m?fmt(m.composite_cost,2):'—','',delta?'相对基准 '+sign(delta.cost_delta,2):'未满足惩罚与距离代价之和');
    return '<section class="result-verdict '+(success?'':'warn')+'"><div class="result-symbol">'+(success?'✓':'!')+'</div><div><div class="eyebrow">DECISION REPORT</div><h2>'+esc(terminalLabels[d.terminal_state])+'</h2><p>'+esc(decisionSummary(r))+'</p>'+
      (c?'<p>最终选择：'+strategy(c.strategy)+' · 每小时最多搬运 '+c.budget+' 辆</p>':'')+'</div></section><div class="stats">'+cards+'</div>'+
      '<div class="report-grid"><section class="panel"><div class="panel-head"><h2>为什么作出这个决定</h2><small>模型意见与工具事实</small></div><div class="panel-body"><ol class="step-list">'+
      ['diagnosis','dispatch','evaluation','coordinator'].map(function(role,i){var out=latestOutput(role);return '<li><span class="num-circle">'+(i+1)+'</span><div><b>'+roles[role].name+'</b><p class="subtext">'+esc(out?deliverySummary(role,out.payload):'此角色未完成交付。')+'</p>'+(out?'<details><summary>模型原始解释</summary><p class="subtext">'+esc(out.payload.summary||out.payload.reason)+'</p></details>':'')+'</div></li>';}).join('')+'</ol></div></section>'+
      '<section class="panel"><div class="panel-head"><h2>最终交付物</h2>'+badge('可追溯')+'</div><div class="panel-body"><h3>调度决策报告</h3><p class="subtext">目标、约束、候选比较、最终选择与证据引用。</p><div class="actions section-gap">'+exportLink('decision-report.html','打开报告 ↗')+exportLink('decision-report.md','下载 Markdown')+'</div>'+
      (c&&r.spatial[c.candidate_id]?'<h3 class="section-gap">模拟调拨明细</h3><p class="subtext">逐时段、区域间搬运量及工作量，对应本次最终选择。</p>'+exportLink('dispatch-moves.csv','下载调拨 CSV'):'')+
      '<div class="notice warn section-gap">结果来自历史仿真与有限候选。预算上限变化属于资源效应，不将全部改善归因于算法。</div></div></section></div><div class="section-gap">'+tabs(r)+'</div>';
  }
  function render(){
    if(window.BikeMaps)window.BikeMaps.dispose();
    var r=run();
    if(!r){app.innerHTML='<div class="loading"><h1>城市调度决策台</h1><p class="subtext">尚未发布运行。请先执行真实任务，再运行 console publish。</p></div>';return;}
    state.runId=r.identity.run_id;state.cursor=Math.max(0,Math.min(state.cursor,Math.max(0,r.events.length-1)));
    var auth=r.identity.authenticity_status, label=auth==='real_model_complete'?'真实大模型 · 完整协作':auth==='real_model_partial'?'真实大模型 · 部分执行':auth==='test_fixture'?'测试夹具 · 非真实模型':'规则历史记录';
    var nav=[['overview','◫','任务总览'],['workflow','⌘','协作过程'],['result','↗','决策结果']];
    app.innerHTML='<div class="app-shell"><aside class="sidebar"><div class="brand"><span class="brand-mark">v</span><div><b>VELO</b><small>URBAN INTELLIGENCE</small></div></div><div class="nav-label">DECISION WORKSPACE</div>'+
      nav.map(function(n){return '<button class="nav-item '+(state.view===n[0]?'active':'')+'" data-view="'+n[0]+'"><span class="nav-icon">'+n[1]+'</span>'+n[2]+'</button>';}).join('')+
      '<div class="sidebar-bottom"><strong>四角色协作 · 工具验证</strong>历史订单 → 预测与风险<br>模型决策 → 仿真评审<br><br>所有业务指标均为模拟值<br>本地运行 · 记录可追溯</div></aside><main class="main"><header class="topbar"><div class="breadcrumb">工作空间 / <b>'+nav.find(function(n){return n[0]===state.view;})[2]+'</b></div>'+badge(label,auth==='real_model_complete'?'':'warn')+'</header><div class="content">'+header(r)+
      (auth==='test_fixture'?'<div class="error-banner">当前是用于验证程序的测试夹具，不是 DeepSeek 真实运行。</div>':'')+
      (r.events.length?(state.view==='overview'?overview(r):state.view==='workflow'?workflow(r):result(r)):'<div class="empty">该历史记录不包含新版角色事件和候选专属空间产物。<br>请通过原协作页查看；不会套用其他运行的地图。</div>')+
      '<footer class="footer-note">运行 '+esc(r.identity.run_id)+' · '+esc(date(r.execution.started_at))+'（北京时间）<br>历史数据复盘；模型在既有策略与预算范围内决策，具体搬运由仿真引擎计算。</footer></div></main></div>';
    bind();renderMap();saveHash();
  }
  window.addEventListener('hashchange',function(){
    stop();var q=new URLSearchParams(location.hash.slice(1));
    if(q.get('run'))state.runId=q.get('run');
    state.view=['overview','workflow','result'].includes(q.get('view'))?q.get('view'):'overview';
    state.cursor=Number(q.get('event')||0)||0;
    state.candidateId=q.get('candidate')||'baseline';
    state.frame=Number(q.get('frame')||0)||0;
    state.cell=q.get('cell');
    render();
  });
  function bind(){
    app.querySelectorAll('[data-view]').forEach(function(n){n.onclick=function(){setView(n.dataset.view);};});
    app.querySelectorAll('[data-tab]').forEach(function(n){n.onclick=function(){stop();state.tab=n.dataset.tab;render();};});
    app.querySelectorAll('[data-candidate]').forEach(function(n){var choose=function(){stop();state.candidateId=n.dataset.candidate;state.tab='spatial';state.cell=null;render();};n.onclick=choose;n.onkeydown=function(e){if(e.key==='Enter'){e.preventDefault();choose();}};});
    app.querySelectorAll('[data-role]').forEach(function(n){n.onclick=function(){
      stop();var starts=run().events.map(function(e,i){return {e:e,i:i};}).filter(function(x){return x.e.role===n.dataset.role&&x.e.kind==='role_started';});
      var before=starts.filter(function(x){return x.i<=state.cursor;}), target=before.length?before[before.length-1]:starts[0];
      if(target){
        var out=run().events.map(function(e,i){return {e:e,i:i};}).find(function(x){
          return x.i>target.i&&x.i<=state.cursor&&x.e.role===n.dataset.role&&x.e.kind==='role_output_submitted';
        });
        state.cursor=out?out.i:target.i;render();
      }
    };});
    app.querySelectorAll('[data-event]').forEach(function(n){n.onclick=function(){stop();state.cursor=Number(n.dataset.event);state.view='workflow';render();};});
    app.querySelectorAll('[data-frame]').forEach(function(n){n.onclick=function(){stop();state.frame+=Number(n.dataset.frame);render();};});
    app.querySelectorAll('[data-evidence]').forEach(function(n){n.onclick=function(){
      stop();var ref=n.dataset.evidence;
      var allowedTools=new Set(visibleEvents().filter(function(e){return e.kind==='tool_completed';}).map(function(e){return e.tool_call_id;}));
      var record=run().tool_calls.find(function(t){return allowedTools.has(t.tool_call_id)&&(t.tool_call_id===ref||t.output.result_id===ref||t.output.artifact_set_id===ref);})||
        visibleEvents().find(function(e){return e.event_id===ref||e.message_id===ref;});
      var existing=n.parentElement.querySelector('.evidence-detail');if(existing)existing.remove();
      var pre=document.createElement('pre');pre.className='raw evidence-detail';pre.textContent=JSON.stringify(record||{reference:ref},null,2);n.parentElement.appendChild(pre);
    };});
    function on(id,event,cb){var n=document.getElementById(id);if(n)n[event]=cb;}
    on('run-select','onchange',function(e){stop();state.runId=e.target.value;state.cursor=0;state.frame=0;state.cell=null;state.candidateId='baseline';state.tab='candidates';state.view='overview';render();});
    on('play','onclick',play);on('prev','onclick',function(){stop();step(-1);});on('next','onclick',function(){stop();step(1);});
    on('event-range','oninput',function(e){stop();state.cursor=Number(e.target.value);render();});
    on('speed','onchange',function(e){var was=state.playing;stop();state.speed=Number(e.target.value);if(was)play();else render();});
    on('map-layer','onchange',function(e){stop();state.layer=e.target.value;render();});
    on('map-provider','onchange',function(e){stop();state.mapProvider=e.target.value;render();});
    on('frame-select','onchange',function(e){stop();state.frame=Number(e.target.value);render();});
    on('cell-select','onchange',function(e){stop();state.cell=e.target.value;render();});
    on('role-occurrence','onchange',function(e){stop();state.cursor=Number(e.target.value);render();});
  }
  window.BIKE_CONSOLE={state:state,run:run,candidates:candidates,render:render,step:step,stop:stop};
  if(state.view==='result'&&!params.has('candidate')&&run()&&run().decision.selected)state.candidateId=run().decision.selected.candidate_id;
  render();
  window.scrollTo(0,0);
})();
