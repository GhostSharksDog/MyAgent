/* Public, browser-only fixtures. No API request reaches the application server. */
(() => {
  const originalFetch = window.fetch.bind(window);
  const now = 1770000000;
  const root = 'C:/Legacy/demo';
  const model = {id:'public-demo',label:'公开演示模型',provider:'custom',base_url:'https://example.invalid/v1',model:'legacy-demo',api_key_masked:'demo-***',api_key_set:true,temperature:0.2,active:true};
  const models = [model,{...model,id:'public-check',label:'公开核验模型',model:'legacy-check',active:false}];
  const currentModel = () => models.find(m=>m.active);
  const settings = {llm:{...model},agent:{profile:'general',workspace_root:root,corpus_paths:[],corpus_include_seed:false,file_max_chars:20000,file_write_enabled:false,file_allow_secrets:false,corpus_loaded:false,corpus_doc_count:0,plan_max_total_tokens:60000,multi_max_total_tokens:80000},env_path:'临时演示配置（不写入磁盘）'};
  settings.run_history={backend:'memory',active_backend:'memory',restart_required:false,max_records:200,max_events:256};
  Object.assign(settings.agent,{file_approval_required:true,file_approval_timeout:300});
  const makeRun = (id,reason='finished') => ({run_id:id,session_id:'public-session',mode:'react',source:'agent',started_at:'2026-10-06T04:00:00Z',finished_at:'2026-10-06T04:00:01Z',duration_ms:1000,stopped_reason:reason,usage:{prompt_tokens:128,completion_tokens:48,total_tokens:176},usage_complete:reason==='finished',steps_used:2,tool_calls:1,tool_results:1,tool_failures:reason==='error'?1:0,context_trimmed:reason==='token_budget',context_tokens:128,events_dropped:reason==='token_budget'?2:0,events:[{kind:'tool_result',elapsed_ms:48,step:1,scope:'child',tool_name:'calculator',ok:reason!=='error',duration_ms:8,truncated:true,counts:null}]});
  const runs=[makeRun('public-archive-error','error'),makeRun('public-archive-budget','token_budget'),makeRun('public-archive-finished')];
  const sessions = [
    {id:'public-session',title:'整理一个清晰的行动计划',created_at:now,updated_at:now,turn_count:2,total_tokens:142},
    {id:'public-notes',title:'核验一组计算结果',created_at:now-200,updated_at:now-200,turn_count:1,total_tokens:86},
    ...Array.from({length:24},(_,i)=>({id:'scroll-'+i,title:'公开示例对话 '+(i+1),created_at:now-1000-i,updated_at:now-1000-i,turn_count:1,total_tokens:64}))
  ];
  const tools = [
    {name:'calculator',description:'计算数学表达式并返回可核验的结果。',parameters:{type:'object',properties:{expression:{type:'string'}},required:['expression']}},
    {name:'read_file',description:'只读预览用户明确选中的工作区文件。',parameters:{type:'object',properties:{path:{type:'string'}},required:['path']}},
    {name:'get_current_time',description:'读取当前时间。',parameters:{type:'object',properties:{}}}
  ];
  const currentTools = () => settings.agent.file_write_enabled ? [...tools,{name:'write_file',description:'演示中的写入工具；不会执行真实写入。',parameters:{type:'object',properties:{path:{type:'string'},content:{type:'string'}},required:['path','content']}}] : tools;
  window.__fixture = {case:'finished',cancelled:false,requests:[],blocked:[],settings,model,settingsFailure:sessionStorage.getItem('smoke-settings-failure')==='true',healthCase:sessionStorage.getItem('smoke-health-case')||'ready',delay:35,deferPreview:false,deferListing:false,pendingFiles:[],releasedFiles:0};
  Object.assign(window.__fixture,{runs,runsFailure:false,detailFailure:false,deferRunDetail:false,pendingRuns:[]});
  window.__fixture.releaseRuns=()=>{for(const release of window.__fixture.pendingRuns.splice(0))release();};
  window.__fixture.releaseFiles = () => {
    for (const release of window.__fixture.pendingFiles.splice(0)) {
      release();window.__fixture.releasedFiles++;
    }
  };
  const json = (body,status=200) => new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
  const plan = status => ({goal:'把工作拆成可核验的三个步骤',reasoning:'先核验数据，再整理资料，最后给出行动建议。',steps:[{id:1,description:'核验公开样本中的计算结果',expected:'得到可复算的结果',status,result:'24 × 24 = 576'},{id:2,description:'整理资料与约束',status,result:'只使用公开示例'},{id:3,description:'形成简洁行动计划',status,result:'以一周为单位复盘'}]});
  function stream(options) {
    const request = JSON.parse(options.body || '{}');
    const selected = window.__fixture.case;
    const run=makeRun('public-run-'+runs.length,'running');
    run.session_id=request.session_id||null;run.mode=request.mode||'react';run.finished_at=null;run.events=[];run.duration_ms=0;run.events_dropped=0;
    runs.unshift(run);
    window.__fixture.cancelled = false;
    const frames = [{type:'start',step:0}];
    if (request.mode === 'plan') frames.push({type:'plan',plan:plan('running')});
    if (request.mode === 'multi') {
      for (const name of ['资料分析员','方案分析员','结果核验员']) frames.push({type:'delegate',specialist:name,content:'核验公开资料并给出简洁建议'});
    }
    frames.push({type:'step',step:1},{type:'token',step:1,content:'我先核验计算，再整理成可执行的建议。'},{type:'tool_call',step:1,tool_name:'calculator',tool_args:{expression:'24 * 24'}},{type:'tool_result',step:1,tool_name:'calculator',tool_ok:true,content:'576',duration_ms:8,truncated:false});
    if (selected !== 'cancel') {
      if (request.mode === 'plan') frames.push({type:'plan_step',plan:plan('done')});
      if (request.mode === 'multi') {
        for (const name of ['资料分析员','方案分析员','结果核验员']) frames.push({type:'delegate_result',specialist:name,tool_ok:true,content:'核验完成：计算结果为 576；建议先明确目标，再安排每周复盘。'});
      }
      frames.push({type:'step',step:2},{type:'final',step:2,content:selected === 'finished' ? '计算已核验：**24 × 24 = 576**。\n\n可以把工作安排成三个步骤：\n\n1. 明确目标和完成标准。\n2. 每天推进一个小任务，记录结果。\n3. 周末复盘，根据证据调整下一步。\n\n先完成今天最小的一步就好。' : '已核验：**24 × 24 = 576**。\n\n这是已有的部分结论，后续调用已停止。'});
      if (selected !== 'finished') frames.push({type:'error',content:'预算已用尽，保留已有结论，不启动后续模型调用。'});
      frames.push({type:'done',steps_used:2,stopped_reason:selected,usage_complete:selected === 'finished',context_trimmed:selected === 'timeout',context_tokens:128,usage:{prompt_tokens:128,completion_tokens:48,total_tokens:176}});
    }
    let timer;
    let cursor = 0;
    const encode = e => {
      if(['start','step','tool_call','tool_result','plan','plan_step','delegate','delegate_result','done','error'].includes(e.type))
        run.events.push({kind:e.type,elapsed_ms:cursor*35,step:e.step||0,scope:'main',tool_name:e.tool_name||null,ok:e.tool_ok??null,duration_ms:e.duration_ms??null,truncated:e.truncated??null,counts:null});
      if(e.type==='done')Object.assign(run,{stopped_reason:e.stopped_reason,finished_at:new Date().toISOString(),duration_ms:cursor*35,usage:e.usage,usage_complete:e.usage_complete,context_trimmed:e.context_trimmed||false});
      return new TextEncoder().encode('event: '+e.type+'\ndata: '+JSON.stringify({...e,run_id:run.run_id,...(e.type==='done'?{record_saved:true}:{})})+'\n\n');
    };
    const body = new ReadableStream({
      start(controller) {
        const send = () => {
          if (cursor < frames.length) {
            controller.enqueue(encode(frames[cursor++]));
            timer = setTimeout(send,window.__fixture.delay);
          } else if (selected !== 'cancel') controller.close();
        };
        send();
      },
      cancel() {clearTimeout(timer);window.__fixture.cancelled = true;if(run.stopped_reason==='running')Object.assign(run,{stopped_reason:'cancelled',finished_at:new Date().toISOString(),duration_ms:cursor*35,usage_complete:false});}
    });
    return new Response(body,{headers:{'Content-Type':'text/event-stream','X-Run-Id':run.run_id}});
  }
  window.fetch = async (input,options={}) => {
    const url = new URL(typeof input === 'string' ? input : input.url,location.href);
    const path = url.pathname;
    if (!path.startsWith('/api/') && path !== '/healthz') return originalFetch(input,options);
    const method = options.method || 'GET';
    window.__fixture.requests.push({path,method});
    if (path === '/api/chat/stream') return stream(options);
    if(path==='/api/runs') {
      if(window.__fixture.runsFailure)return json({detail:'公开演示：运行记录读取失败，请重试'},503);
      const filtered=runs.filter(r=>(!url.searchParams.get('session_id')||r.session_id===url.searchParams.get('session_id'))&&(!url.searchParams.get('stopped_reason')||r.stopped_reason===url.searchParams.get('stopped_reason')));
      const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||50);
      return json({runs:filtered.slice(offset,offset+limit).map(({events,...r})=>r),backend:'memory',total:filtered.length,limit,offset,max_records:200,summary_only:true});
    }
    if(path.startsWith('/api/runs/')) {
      const id=decodeURIComponent(path.split('/').pop());
      const index=runs.findIndex(r=>r.run_id===id);
      if(index<0)return json({detail:'运行记录不存在或已淘汰'},404);
      if(method==='DELETE') {runs.splice(index,1);return json({deleted:true});}
      if(window.__fixture.detailFailure)return json({detail:'公开演示：执行摘要读取失败'},503);
      const snapshot=structuredClone(runs[index]);
      if(window.__fixture.deferRunDetail){window.__fixture.deferRunDetail=false;return new Promise(resolve=>window.__fixture.pendingRuns.push(()=>resolve(json(snapshot))));}
      return json(snapshot);
    }
    if (path === '/healthz') {
      if (window.__fixture.healthCase === 'offline') throw new TypeError('Synthetic offline');
      return json({status:'ok',env:'demo',llm_configured:window.__fixture.healthCase!=='missing-model',auth_required:window.__fixture.healthCase==='missing-key',model:currentModel().model,tools:currentTools().map(t=>t.name),session_backend:'memory'});
    }
    if (path === '/api/meta') return json({service:'Legacy',version:'0.1.0',env:'demo',model:currentModel().model,max_steps:12,tool_count:currentTools().length,session_backend:'memory',agent_modes:['react','plan','multi']});
    if (path === '/api/tools') return json(currentTools());
    if (path === '/api/settings') {
      if (window.__fixture.settingsFailure) return json({detail:'公开演示：设置暂时读取失败'},503);
      if (method === 'PUT') {
        const updates = JSON.parse(options.body || '{}');
        Object.assign(settings.agent,updates);
        if(updates.run_history_backend){settings.run_history.backend=updates.run_history_backend;settings.run_history.restart_required=updates.run_history_backend!==settings.run_history.active_backend;}
      }
      return json(settings);
    }
    if (path === '/api/settings/test') return json({ok:true,model:model.model,latency_ms:12,error:'',hint:'合成连接结果，未调用模型'});
    if (path.startsWith('/api/models/') && path.endsWith('/activate')) {
      const id = path.split('/')[3];
      for (const item of models) item.active=item.id===id;
      settings.llm.model=currentModel().model;
    }
    if (path === '/api/models' || path === '/api/models/import-current' || path.startsWith('/api/models/')) return json({models,current_unsaved:false,current:currentModel()});
    if (path === '/api/sessions' && method === 'POST') {
      const session={id:'created-'+sessions.length,title:'新建对话',created_at:now,updated_at:now,turn_count:0,total_tokens:0};
      sessions.unshift(session);
      return json(session);
    }
    if (path === '/api/sessions') return json({sessions,backend:'memory'});
    if (path.startsWith('/api/sessions/')) {
      if (method === 'DELETE') return json({deleted:true});
      return json({id:path.split('/').pop(),title:'公开示例',created_at:now,updated_at:now,total_tokens:142,turns:[{role:'user',content:'帮我整理一个清晰的行动计划。'},{role:'assistant',content:'明确一个目标，每天推进一个小任务，每周根据记录复盘。'}]});
    }
    if (path === '/api/files/workspace') return json({configured:settings.agent.workspace_root!=='',root:settings.agent.workspace_root,reason:''});
    if (path === '/api/files/picker') return json({kind:'browse',detail:'演示使用应用内目录选择，不弹系统窗口'});
    if (path === '/api/files/browse') return json({path:root,parent:'C:/Legacy',entries:[{name:'demo',path:root,child_count:3}],roots:[{name:'C:',path:'C:/',child_count:1}]});
    if (path === '/api/files/list') {
      const switched = settings.agent.workspace_root !== root;
      const listing = {path:'.',entries:switched ? [{name:'new-workspace.md',path:'new-workspace.md',is_dir:false,size:128}] : [{name:'demo-notes.md',path:'demo-notes.md',is_dir:false,size:128},{name:'sample-data.txt',path:'sample-data.txt',is_dir:false,size:64},{name:'notes',path:'notes',is_dir:true,size:0}],truncated:false,can_go_up:false};
      if (window.__fixture.deferListing) {
        window.__fixture.deferListing=false;
        return new Promise(resolve=>window.__fixture.pendingFiles.push(()=>resolve(json(listing))));
      }
      return json(listing);
    }
    if (path === '/api/files/content') {
      const switched = settings.agent.workspace_root !== root;
      const content={path:url.searchParams.get('path')||'demo-notes.md',content:(switched ? '# 新工作区资料' : '# 公开演示资料')+'\n\n这些内容来自浏览器内合成样本。\n\n- 明确目标\n- 每天记录结果\n- 每周复盘\n\n计算：24 × 24 = 576。',size:128,truncated:false,is_binary:false};
      if (window.__fixture.deferPreview) {
        window.__fixture.deferPreview=false;
        return new Promise(resolve=>window.__fixture.pendingFiles.push(()=>resolve(json(content))));
      }
      return json(content);
    }
    window.__fixture.blocked.push({path,method});
    return json({detail:'视觉测试未声明此请求，已拦截，未发送到后端。'},501);
  };
})();
