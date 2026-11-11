'use strict';
/* HPC Usage Monitor 1.0 — standalone, no network dependencies. */
const HPC = (() => {
  const TYPES = ['user_cpu','queue_started','queue_pending','node_cpu','gpu_user','gpu'];
  const SCHEMA = {
    user_cpu:['timestamp','user','node','partition','cpus_alloc','mem_alloc_mb','n_jobs','job_ids'],
    queue_started:['timestamp','job_id','user','partition','submit_time','start_time','wait_sec','wait_min','alloc_cpus','n_nodes','state'],
    queue_pending:['timestamp','job_id','user','partition','qos','req_cpus','submit_time','wait_sec','wait_min','priority','reason'],
    node_cpu:['timestamp','node','node_type','partitions','state','cpu_total','cpu_alloc','alloc_pct','cpu_load','load_pct','mem_total_mb','mem_alloc_mb','mem_pct'],
    gpu_user:['timestamp','node','gpu_index','user','job_id','pid','mem_used_mb','proc_name'],
    gpu:['timestamp','node','gpu_index','uuid','gpu_name','gpu_util','mem_total_mb','mem_used_mb','mem_pct','temp_c','power_w']
  };
  const NUMBERS = new Set(['cpus_alloc','mem_alloc_mb','n_jobs','wait_sec','wait_min','alloc_cpus','n_nodes','req_cpus','priority','cpu_total','cpu_alloc','alloc_pct','cpu_load','load_pct','mem_total_mb','mem_pct','gpu_util','mem_used_mb','temp_c','power_w']);
  const KEY = {user_cpu:['timestamp','user','node','partition'],queue_started:['timestamp','job_id','start_time','submit_time'],queue_pending:['timestamp','job_id','submit_time'],node_cpu:['timestamp','node'],gpu_user:['timestamp','node','gpu_index','pid','user','job_id'],gpu:['timestamp','node','gpu_index']};
  const DAY = 86400000, HOUR = 3600000;
  const defaults = {offset:540,interval:30,startedMode:'snapshot',nodeClock:true,denominator:'observed',bucket:'day'};
  const num = v => v === null || v === undefined || String(v).trim()==='' || /^(?:N\/A|NA|nan|null|none|-)$/i.test(String(v).trim()) ? null : Number.isFinite(Number(String(v).replace(/%$/,''))) ? Number(String(v).replace(/%$/,'')) : null;
  const ratio = (a,b) => b>0 ? 100*a/b : null;
  const add = (map,key,fn) => { if(!map.has(key)) map.set(key,fn()); return map.get(key); };
  const med = a => { if(!a.length)return null; a=a.slice().sort((x,y)=>x-y);const i=a.length>>1;return a.length%2?a[i]:(a[i-1]+a[i])/2; };
  function time(value,offset=540) {
    if(typeof value==='number') return Number.isFinite(value)?value:null;
    const s=String(value||'').trim();
    if(!s || /^(Unknown|N\/A|None|None assigned|-|0)$/i.test(s)) return null;
    const m=s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(\.\d{1,3})?)?(Z|[+-]\d{2}:?\d{2})?$/i);
    if(!m)return null;
    const y=+m[1],mo=+m[2]-1,d=+m[3],h=+m[4],mi=+m[5],se=+(m[6]||0),ms=Math.round(+(m[7]||0)*1000);
    const t=Date.UTC(y,mo,d,h,mi,se,ms),v=new Date(t);
    if(v.getUTCFullYear()!==y||v.getUTCMonth()!==mo||v.getUTCDate()!==d||h>23||mi>59||se>59)return null;
    if(!m[8])return t-offset*60000;
    if(m[8].toUpperCase()==='Z')return t;
    const z=m[8].replace(':',''),oh=+z.slice(1,3),om=+z.slice(3,5);
    if(oh>14||om>59||(oh===14&&om!==0))return null;
    return t-(z[0]==='-'?-1:1)*(oh*60+om)*60000;
  }
  const iso=(t,offset=540)=>new Date(t+offset*60000).toISOString().slice(0,19);
  const list = v => {
    const s=String(v||'').trim();
    if(!s||/^(?:-|none|N\/A|\(null\))$/i.test(s))return [];
    if(s[0]==='[')try{const a=JSON.parse(s);if(Array.isArray(a))return [...new Set(a.map(String).map(x=>x.trim()).filter(Boolean))];}catch{}
    /* Keep Slurm compact array identifiers intact; never expand unknown ranges. */
    const result=[];let part='',depth=0;
    for(const c of s){if(c==='[')depth++;if(c===']')depth--;if(depth===0&&/[,;|\s]/.test(c)){if(part)result.push(part.replace(/^["']|["']$/g,''));part='';}else part+=c;}
    if(part)result.push(part.replace(/^["']|["']$/g,''));
    return [...new Set(result.filter(Boolean))];
  };
  function fields(line){
    const out=[];let v='',q=false;
    for(let i=0;i<line.length;i++){const c=line[i];if(c==='"'&&(q||v==='')){if(q&&line[i+1]==='"'){v+='"';i++;}else q=!q;}else if(c==='\t'&&!q){out.push(v);v='';}else v+=c;}
    if(q)throw new Error('닫히지 않은 큰따옴표');
    out.push(v);return out;
  }
  function detect(h){
    const set=new Set(h);
    if(!set.has('timestamp'))return null;
    const distinctive = {user_cpu:['cpus_alloc','job_ids','user','node'],queue_started:['start_time','job_id','alloc_cpus'],queue_pending:['req_cpus','reason','job_id'],node_cpu:['cpu_total','cpu_alloc','node'],gpu_user:['pid','user','gpu_index'],gpu:['gpu_util','gpu_index','gpu_name']};
    return TYPES.find(k=>distinctive[k].every(x=>set.has(x)))||null;
  }
  function createStore(){return {tables:Object.fromEntries(TYPES.map(k=>[k,new Map()])),files:[],issues:[],issueTotal:0,duplicates:0,conflicts:0,revision:0,cache:null,demo:false};}
  function issue(store,file,line,message){store.issueTotal++;if(store.issues.length<300)store.issues.push({file,line,message});}
  function insert(store,type,raw,file,line,summary){
    const r=Object.create(null);
    for(const k of SCHEMA[type]){const v=raw[k];r[k]=NUMBERS.has(k)?num(v):String(v??'').trim();}
    if(time(r.timestamp,540)===null){issue(store,file,line,'timestamp 형식 오류: '+String(r.timestamp).slice(0,80));summary.invalid++;return;}
    const required=type.startsWith('queue_')?['job_id','user']:type==='gpu_user'?['node','user','gpu_index','pid']:type==='user_cpu'?['node','user']:type==='gpu'?['node','gpu_index']:['node'];
    if(required.some(k=>r[k]===null||r[k]==='')){issue(store,file,line,'필수 식별자 누락: '+required.join(', '));summary.invalid++;return;}
    for(const k of ['start_time','submit_time'])if(r[k]&&time(r[k],540)===null)issue(store,file,line,k+' 형식 오류; 해당 시각은 집계에서 제외');
    for(const k of SCHEMA[type].filter(k=>NUMBERS.has(k))){if(raw[k]!==undefined&&String(raw[k]).trim()&&!/^(?:-|N\/A|NA|nan|null|none)$/i.test(String(raw[k]).trim())&&r[k]===null)issue(store,file,line,k+' 숫자 형식 오류');}
    for(const k of ['cpus_alloc','cpu_total','cpu_alloc','n_jobs','alloc_cpus','req_cpus','n_nodes','mem_alloc_mb','mem_used_mb','mem_total_mb','wait_sec','wait_min'])if(r[k]!==undefined&&r[k]!==null&&r[k]<0){issue(store,file,line,k+' 음수는 결측값으로 처리');r[k]=null;}
    if(type==='node_cpu'&&r.cpu_total!==null&&r.cpu_alloc>r.cpu_total)issue(store,file,line,'할당 코어가 전체 코어보다 큼; 원본 수치를 유지');
    if(type==='user_cpu'&&r.n_jobs!==null&&r.n_jobs!==list(r.job_ids).length)issue(store,file,line,'n_jobs와 job_ids 개수 불일치; 개별 작업 집계는 job_ids 기준');
    const key=JSON.stringify(KEY[type].map(k=>r[k]));
    const old=store.tables[type].get(key);
    if(old){store.duplicates++;summary.duplicates++;if(JSON.stringify(old)!==JSON.stringify(r)){store.conflicts++;issue(store,file,line,'같은 식별자·시각의 값이 다름; 나중에 불러온 행을 사용');}}
    else summary.rows++;
    store.tables[type].set(key,r);
    summary.types.add(type);store.revision++;store.cache=null;
  }
  function parser(store,file){
    const summary={name:file,rows:0,duplicates:0,invalid:0,types:new Set()},state={header:null,type:null,line:0};
    function feed(s){
      state.line++;s=s.replace(/^\uFEFF/,'').replace(/\r$/,'');
      if(!s.trim())return;
      let f;
      try{f=fields(s);}catch(e){summary.invalid++;issue(store,file,state.line,e.message);return;}
      const h=f.map(x=>x.trim().replace(/^#\s*/,'')),type=detect(h);
      if(type){state.header=h;state.type=type;summary.types.add(type);return;}
      if(s.startsWith('#'))return;
      if(!state.header){summary.invalid++;if(summary.invalid<5)issue(store,file,state.line,'인식 가능한 탭 구분 헤더가 필요합니다.');return;}
      if(f.length!==state.header.length){summary.invalid++;issue(store,file,state.line,'열 개수 불일치 ('+f.length+'/'+state.header.length+')');return;}
      const raw=Object.create(null);state.header.forEach((k,i)=>{raw[k]=f[i];});
      insert(store,state.type,raw,file,state.line,summary);
    }
    function finish(){const s={...summary,types:[...summary.types]};store.files.push(s);return s;}
    return {feed,finish};
  }
  function loadText(store,text,name='input.tsv'){
    if(String(text).trimStart().startsWith('{'))return loadSession(store,JSON.parse(text),name);
    const p=parser(store,name);String(text).split('\n').forEach(p.feed);return p.finish();
  }
  async function loadFile(store,file,progress){
    if(/\.json$/i.test(file.name))return loadSession(store,JSON.parse(await file.text()),file.name);
    const p=parser(store,file.name),reader=file.stream().getReader(),decoder=new TextDecoder('utf-8');
    let pending='',read=0;
    while(true){const {done,value}=await reader.read();if(done)break;read+=value.byteLength;pending+=decoder.decode(value,{stream:true});const lines=pending.split('\n');pending=lines.pop();for(const l of lines)p.feed(l);if(progress)progress(read,file.size);}
    pending+=decoder.decode();if(pending)p.feed(pending);return p.finish();
  }
  function loadSession(store,obj,name){
    if(obj.format!=='hpc-monitor-session'||!obj.tables||typeof obj.tables!=='object')throw new Error('지원하는 통합 JSON 파일이 아닙니다.');
    if(obj.demo===true)store.demo=true;
    if(obj.settings&&typeof obj.settings==='object'){
      const r=obj.settings,v={};
      if([0,540].includes(+r.offset))v.offset=+r.offset;
      if(Number.isFinite(+r.interval)&&+r.interval>=1&&+r.interval<=1440)v.interval=+r.interval;
      if(['snapshot','event'].includes(r.startedMode))v.startedMode=r.startedMode;
      if(typeof r.nodeClock==='boolean')v.nodeClock=r.nodeClock;
      if(['observed','calendar'].includes(r.denominator))v.denominator=r.denominator;
      if(['hour','day','week','month'].includes(r.bucket))v.bucket=r.bucket;
      store.importedSettings=v;
    }
    const summary={name,rows:0,duplicates:0,invalid:0,types:new Set()};
    for(const type of TYPES)if(Array.isArray(obj.tables[type])){summary.types.add(type);obj.tables[type].forEach((r,i)=>{if(r&&typeof r==='object'&&!Array.isArray(r))insert(store,type,r,name,i+1,summary);else{summary.invalid++;issue(store,name,i+1,'JSON 행 형식 오류');}});}
    const s={...summary,types:[...summary.types]};store.files.push(s);return s;
  }
  function session(store,settings){return {format:'hpc-monitor-session',version:1,settings,demo:store.demo,tables:Object.fromEntries(TYPES.map(k=>[k,[...store.tables[k].values()]]))};}
  function normalize(store,opt){
    const key=store.revision+':'+opt.offset;
    if(store.cache?.key===key)return store.cache.value;
    const tables=Object.create(null),times=Object.create(null),nodes=new Map(),users=new Set(),groups=new Set();
    let min=Infinity,max=-Infinity;
    for(const type of TYPES){
      tables[type]=[...store.tables[type].values()].map(r=>({...r,t:time(r.timestamp,opt.offset),sub:time(r.submit_time,opt.offset),start:time(r.start_time,opt.offset)})).sort((a,b)=>a.t-b.t);
      times[type]=[...new Set(tables[type].map(r=>r.t))];
      if(times[type].length){min=Math.min(min,times[type][0]);max=Math.max(max,times[type].at(-1));}
      for(const r of tables[type]){
        if(r.user)users.add(r.user);
        const gs=list(r.partitions||r.partition);gs.forEach(g=>groups.add(g));
        if(r.node){const n=add(nodes,r.node,()=>({name:r.node,groups:new Set(),cores:null,nodeType:'',hasCPU:false}));gs.forEach(g=>n.groups.add(g));if(type==='node_cpu')n.hasCPU=true;if(r.cpu_total!==undefined&&r.cpu_total!==null)n.cores=r.cpu_total;if(r.node_type)n.nodeType=r.node_type;}
      }
    }
    const v={tables,times,nodes,users:[...users].sort(),groups:[...groups].sort(),min:Number.isFinite(min)?min:null,max:Number.isFinite(max)?max:null};
    store.cache={key,value:v};return v;
  }
  function bucketStart(t,mode,offset){
    const local=t+offset*60000;
    if(mode==='hour')return Math.floor(local/HOUR)*HOUR-offset*60000;
    if(mode==='month'){const d=new Date(local);return Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),1)-offset*60000;}
    const day=Math.floor(local/DAY)*DAY-offset*60000;
    return mode==='week'?day-((new Date(local).getUTCDay()+6)%7)*DAY:day;
  }
  function nextBucket(t,mode,offset){
    if(mode==='month'){const d=new Date(t+offset*60000);return Date.UTC(d.getUTCFullYear(),d.getUTCMonth()+1,1)-offset*60000;}
    return t+(mode==='hour'?HOUR:mode==='week'?7*DAY:DAY);
  }
  function splits(a,b,mode,offset,fn){for(let t=a;t<b;){const bs=bucketStart(t,mode,offset),end=Math.min(b,nextBucket(bs,mode,offset));fn(bs,(end-t)/1000,t,end);t=end;}}
  function intervals(rows,entity,clock,step,end){
    const by=new Map(),nextClock=new Map();
    for(let i=0;i<clock.length;i++)nextClock.set(clock[i],clock[i+1]??clock[i]);
    rows.forEach(r=>add(by,entity(r),()=>[]).push(r));
    const out=[];
    for(const arr of by.values())for(let i=0;i<arr.length;i++){
      const r=arr[i],next=arr[i+1]?.t??end,c=nextClock.get(r.t),b=Math.min(next,r.t+step,end,c===undefined?end:c);
      if(b>r.t)out.push({a:r.t,b,r});
    }
    return out;
  }
  function mergeRanges(ranges){
    const a=ranges.filter(x=>x[1]>x[0]).sort((x,y)=>x[0]-y[0]),out=[];
    for(const r of a){const p=out.at(-1);if(p&&r[0]<=p[1])p[1]=Math.max(p[1],r[1]);else out.push(r.slice());}return out;
  }
  const seconds=ranges=>mergeRanges(ranges).reduce((s,r)=>s+(r[1]-r[0])/1000,0);
  function stats(){return {observed:0,busy:0,coreSec:0,capacitySec:0,memSec:0,memWeight:0,loadSec:0,loadWeight:0,gpuSec:0,gpuWeight:0,gpuMemSec:0,tempMax:null,powerSec:0,powerWeight:0,jobs:new Set(),nodes:new Set()};}
  function analyze(store,options={}){
    const opt={...defaults,...options},n=normalize(store,opt),T=n.tables,step=Math.max(1,Number(opt.interval)||30)*60000;
    const available=Object.fromEntries(TYPES.map(k=>[k,store.files.some(f=>f.types.includes(k))||T[k].length>0]));
    const empty={empty:true,meta:metadata(store,n),settings:opt};
    if(n.min===null)return empty;
    const from=Number.isFinite(opt.from)?opt.from:n.min,to=Number.isFinite(opt.to)?opt.to:n.max;
    if(!(to>from))return {...empty,error:'집계에는 서로 다른 시각의 기록이 두 개 이상 필요합니다. 종료 시각은 시작 시각보다 뒤여야 합니다.'};
    if((to-from)/DAY>3653)return {...empty,error:'한 번에 조회할 수 있는 기간은 최대 10년입니다.'};
    if(opt.bucket==='hour'&&(to-from)/HOUR>240)opt.bucket='day';
    if(opt.bucket==='day'&&(to-from)/DAY>240)opt.bucket='week';
    if(opt.bucket==='week'&&(to-from)/DAY/7>240)opt.bucket='month';
    const choose=(a,v)=>!a?.length||a.includes(v),chooseGroups=gs=>!opt.groups?.length||gs.some(g=>opt.groups.includes(g));
    const eligibleNode=name=>choose(opt.nodes,name)&&chooseGroups([...(n.nodes.get(name)?.groups||[])]);
    const nodeNames=[...n.nodes.keys()].filter(eligibleNode);
    const selected=new Set(nodeNames),userMatch=r=>choose(opt.users,r.user);
    const rowGroup=r=>chooseGroups(list(r.partition||r.partitions));
    const nodeStats=new Map(),groupStats=new Map(),userStats=new Map(),gpuStats=new Map(),daily=new Map(),heat=new Map(),gpuHeat=new Map();
    const newDay=t=>({t,submitted:new Set(),started:new Set(),running:new Set(),jobSec:0,coreSec:0,capacitySec:0,observed:0,busy:0,gpuSec:0,gpuWeight:0,pendingMax:0,wait:[]});
    for(let b=bucketStart(from,opt.bucket,opt.offset);b<to;b=nextBucket(b,opt.bucket,opt.offset))daily.set(b,newDay(b));
    const day=t=>add(daily,bucketStart(t,opt.bucket,opt.offset),()=>newDay(bucketStart(t,opt.bucket,opt.offset)));
    const clip=(a,b)=>[Math.max(a,from),Math.min(b,to)];
    const userIntervals=intervals(T.user_cpu,r=>JSON.stringify([r.user,r.node,r.partition]),opt.nodeClock?[...new Set([...n.times.user_cpu,...n.times.node_cpu])].sort((a,b)=>a-b):n.times.user_cpu,step,opt.nodeClock?Math.max(n.times.user_cpu.at(-1)??-Infinity,n.times.node_cpu.at(-1)??-Infinity):n.times.user_cpu.at(-1));
    const uAt=new Map();
    for(const r of T.user_cpu)if(selected.has(r.node)&&userMatch(r)&&rowGroup(r)){
      const a=add(uAt,JSON.stringify([r.t,r.node]),()=>({cpu:0,mem:0,cpuKnown:false,memKnown:false,ids:new Set()}));
      if(r.cpus_alloc!==null){a.cpu+=r.cpus_alloc;a.cpuKnown=true;}if(r.mem_alloc_mb!==null){a.mem+=r.mem_alloc_mb;a.memKnown=true;}list(r.job_ids).forEach(id=>a.ids.add(id));
    }
    const filteringUsers=!!opt.users?.length,filteringGroups=!!opt.groups?.length;
    const userSampleTimes=new Set(n.times.user_cpu);
    const hasJobEvidence=available.user_cpu||(opt.startedMode==='snapshot'&&available.queue_started);
    nodeNames.filter(name=>n.nodes.get(name)?.hasCPU).forEach(name=>nodeStats.set(name,stats()));
    const nodeInt=intervals(T.node_cpu,r=>r.node,n.times.node_cpu,step,n.times.node_cpu.at(-1));
    let physicalCapacity=0;
    for(const it of nodeInt){
      const r=it.r;if(!selected.has(r.node))continue;
      const [a,b]=clip(it.a,it.b);if(b<=a)continue;
      if(r.cpu_total!==null)physicalCapacity+=r.cpu_total*(b-a)/1000;
      const user=uAt.get(JSON.stringify([r.t,r.node]));
      /* User allocation is only attributed at a synchronized sample. */
      const scoped=filteringUsers;
      const absentUserKnown=available.user_cpu&&(userSampleTimes.has(r.t)||(opt.nodeClock&&r.cpu_alloc===0));
      const cpu=scoped?(user?(user.cpuKnown?user.cpu:null):(absentUserKnown?0:null)):r.cpu_alloc;
      const mem=scoped?(user?(user.memKnown?user.mem:null):(absentUserKnown?0:null)):r.mem_alloc_mb;
      const s=add(nodeStats,r.node,stats);
      const gs=[...(n.nodes.get(r.node)?.groups||[])].filter(g=>choose(opt.groups,g)),targets=[s,...gs.map(g=>add(groupStats,g,stats))];
      targets.forEach(v=>v.nodes.add(r.node));
      splits(a,b,opt.bucket,opt.offset,(bt,sec)=>{
        const d=day(bt),h=add(heat,JSON.stringify([r.node,bt]),()=>({node:r.node,t:bt,core:0,cap:0,busy:0,obs:0}));
        for(const v of [...targets,d]){
          if(cpu!==null){v.observed+=sec;if(cpu>0)v.busy+=sec;v.coreSec+=cpu*sec;if(r.cpu_total!==null)v.capacitySec+=r.cpu_total*sec;}
          if(mem!==null&&r.mem_total_mb>0){v.memSec=(v.memSec||0)+(100*mem/r.mem_total_mb)*sec;v.memWeight=(v.memWeight||0)+sec;}
          if(!scoped&&r.load_pct!==null){v.loadSec=(v.loadSec||0)+r.load_pct*sec;v.loadWeight=(v.loadWeight||0)+sec;}
        }
        if(cpu!==null){h.obs+=sec;h.core+=cpu*sec;if(r.cpu_total!==null)h.cap+=r.cpu_total*sec;if(cpu>0)h.busy+=sec;}
      });
    }
    for(const it of userIntervals){
      const r=it.r;if(!selected.has(r.node)||!userMatch(r)||!rowGroup(r))continue;
      const [a,b]=clip(it.a,it.b);if(b<=a)continue;
      const u=add(userStats,r.user,stats);u.nodes.add(r.node);
      const sec=(b-a)/1000;if(r.cpus_alloc!==null){u.coreSec+=r.cpus_alloc*sec;u.observed+=sec;if(!available.node_cpu)splits(a,b,opt.bucket,opt.offset,(bt,s)=>{day(bt).coreSec+=r.cpus_alloc*s;});}
    }
    const jobs=new Map(),byId=new Map();
    function getJob(r,kind){
      const id=String(r.job_id),key=JSON.stringify([id,r.sub??r.start??'unknown']);
      let j=jobs.get(key);
      if(!j){j={key,id,user:r.user||'',partition:r.partition||'',submit:r.sub,start:r.start,state:r.state||'',alloc:r.alloc_cpus??null,nNodes:r.n_nodes??null,wait:r.wait_sec??(r.wait_min!==null&&r.wait_min!==undefined?r.wait_min*60:null),nodes:new Set(),ranges:[],nodeRanges:new Map(),cpuObservations:[],observations:[],source:new Set(),hasCPU:false,lastPending:null};jobs.set(key,j);add(byId,id,()=>[]).push(j);}
      if(r.start!==null&&r.start!==undefined)j.start=r.start;if(r.state)j.state=r.state;
      if(kind==='queue_started'){j.source.add(kind);j.observations.push(r.t);if(r.alloc_cpus!==null)j.alloc=r.alloc_cpus;if(r.n_nodes!==null)j.nNodes=r.n_nodes;if(r.wait_sec!==null)j.wait=r.wait_sec;else if(r.wait_min!==null)j.wait=r.wait_min*60;}
      else if(kind==='queue_pending'){j.source.add(kind);j.lastPending=r;}
      return j;
    }
    T.queue_pending.forEach(r=>getJob(r,'queue_pending'));T.queue_started.forEach(r=>getJob(r,'queue_started'));
    function resolveJob(id,r){
      const candidates=byId.get(id)||[];
      let j=candidates.filter(j=>j.user===r.user&&(j.start??j.submit??-Infinity)<=r.t).sort((a,b)=>(b.start??b.submit??0)-(a.start??a.submit??0))[0];
      if(!j)j=getJob({job_id:id,user:r.user,partition:r.partition,sub:null,start:null,state:''},'user_cpu');
      return j;
    }
    for(const r of T.user_cpu)for(const id of list(r.job_ids)){const j=resolveJob(id,r);j.hasCPU=true;j.nodes.add(r.node);j.source.add('user_cpu');}
    const presenceClock=opt.nodeClock?[...new Set([...n.times.user_cpu,...n.times.node_cpu])].sort((a,b)=>a-b):n.times.user_cpu;
    const clockNext=new Map(presenceClock.map((t,i)=>[t,presenceClock[i+1]??null]));
    const startNext=new Map(n.times.queue_started.map((t,i)=>[t,n.times.queue_started[i+1]??null]));
    for(const it of userIntervals){const r=it.r;for(const id of list(r.job_ids)){
      const j=resolveJob(id,r);j.cpuObservations.push(r.t);
      if(selected.has(r.node)&&userMatch(r)&&rowGroup(r)){j.ranges.push([it.a,it.b]);add(j.nodeRanges,r.node,()=>[]).push([it.a,it.b]);}
    }}
    /* The last point has no duration, but is still evidence that a job exists. */
    for(const r of T.user_cpu)for(const id of list(r.job_ids))resolveJob(id,r).cpuObservations.push(r.t);
    for(const j of jobs.values())if(!j.hasCPU&&opt.startedMode==='snapshot'){
      for(const t of j.observations){const next=startNext.get(t);if(next!==null&&next!==undefined){const b=Math.min(t+step,next);if(b>t)j.ranges.push([t,b]);}}
    }
    const visibleJobs=[],pendingAt=new Map(),reason=new Map();
    const rangeSec=(to-from)/1000;
    let submitted=0,started=0,running=0,inferredEnded=0,totalJobSec=0;const waits=[],activity=new Map();
    const queueClock=[...new Set([...n.times.queue_pending,...n.times.queue_started,...(opt.nodeClock?n.times.node_cpu:[])])].filter(t=>t>=from&&t<=to).sort((a,b)=>a-b);
    const lastQueue=queueClock.at(-1)??null;
    const currentPending=new Map();
    for(const r of T.queue_pending)if(r.t===lastQueue)currentPending.set(getJob(r,'queue_pending').key,r);
    const perNodeJobs=new Map(),perGroupJobs=new Map(),perNodeJobSeconds=new Map(),perGroupJobSeconds=new Map();
    for(const j of jobs.values()){
      if(!choose(opt.users,j.user)||!chooseGroups(list(j.partition)))continue;
      if(opt.nodes?.length&&!([...j.nodes].some(x=>opt.nodes.includes(x))))continue;
      if(filteringGroups&&j.nodes.size&&!([...j.nodes].some(x=>selected.has(x))))continue;
      let ranges=mergeRanges(j.ranges);
      /* Known start time refines only the first observed, contiguous interval. */
      const firstEvidence=(j.hasCPU?j.cpuObservations:j.observations).reduce((a,b)=>Math.min(a,b),Infinity);
      if(ranges.length&&ranges[0][0]===firstEvidence&&j.start!==null&&j.start<=ranges[0][0]&&ranges[0][0]-j.start<=step)ranges[0][0]=j.start;
      ranges=mergeRanges(ranges.map(r=>clip(...r)).filter(r=>r[1]>r[0]));
      const sec=seconds(ranges),isSub=j.submit!==null&&j.submit>=from&&j.submit<to,isStart=j.start!==null&&j.start>=from&&j.start<to;
      const pendingRecord=currentPending.get(j.key);
      const pending=pendingRecord&&!(j.start!==null&&j.start<=lastQueue);
      if(!sec&&!isSub&&!isStart&&!pending)continue;
      const u=add(userStats,j.user||'미상',stats);u.nodes=new Set([...u.nodes,...[...j.nodes].filter(x=>selected.has(x))]);
      if(isSub){submitted++;u.submitted=(u.submitted||0)+1;day(j.submit).submitted.add(j.key);}
      if(isStart){started++;u.started=(u.started||0)+1;day(j.start).started.add(j.key);const w=j.wait??(j.submit!==null?(j.start-j.submit)/1000:null);if(w!==null&&w>=0){waits.push(w);day(j.start).wait.push(w);}}
      if(sec){running++;u.jobs.add(j.key);u.jobSec=(u.jobSec||0)+sec;totalJobSec+=sec;add(activity,j.user,()=>[]).push(...ranges);
        for(const [a,b] of ranges)splits(a,b,opt.bucket,opt.offset,(bt,s)=>{day(bt).running.add(j.key);day(bt).jobSec+=s;});
      }
      const obs=[...new Set(j.hasCPU?j.cpuObservations:j.observations)].sort((a,b)=>a-b),last=obs.filter(t=>t<=to).at(-1);
      const clock=j.hasCPU?clockNext:startNext,nxt=clock.get(last),ended=last!==undefined&&nxt!==null&&nxt!==undefined&&nxt-last<=step*1.1&&nxt<=to;
      const status=pending?'대기':ended?'종료 추정':sec?'종료 미확인':isStart?'시작 기록':'제출 기록';
      if(ended&&sec)inferredEnded++;
      if(pending){pendingAt.set(j.key,{...j,pendingRecord});const r=pendingRecord.reason||'미상';reason.set(r,(reason.get(r)||0)+1);}
      const groupJobRanges=new Map();
      for(const [node,rs]of j.nodeRanges){const refined=mergeRanges(rs);if(refined.length&&refined[0][0]===firstEvidence&&j.start!==null&&j.start<=firstEvidence&&firstEvidence-j.start<=step)refined[0][0]=j.start;const clipped=refined.map(r=>clip(...r)),duration=seconds(clipped);if(duration>0){add(perNodeJobs,node,()=>new Set()).add(j.key);perNodeJobSeconds.set(node,(perNodeJobSeconds.get(node)||0)+duration);for(const g of n.nodes.get(node)?.groups||[]){add(perGroupJobs,g,()=>new Set()).add(j.key);add(groupJobRanges,g,()=>[]).push(...clipped);}}}
      for(const [g,rs]of groupJobRanges)perGroupJobSeconds.set(g,(perGroupJobSeconds.get(g)||0)+seconds(rs));
      const waitValue=j.start!==null&&j.start<=to?(j.wait??(j.submit!==null?(j.start-j.submit)/1000:null)):null;
      visibleJobs.push({id:j.id,user:j.user,partition:j.partition,nodes:[...j.nodes].join(', '),submit:j.submit,start:j.start!==null&&j.start<=to?j.start:null,last:last??null,end:ended?nxt:null,status,state:j.state,wallHours:sec?sec/3600:null,waitMin:waitValue===null?null:waitValue/60,alloc:j.alloc,source:[...j.source].join(', '),key:j.key,ranges});
    }
    const pendingSamples=new Map();
    for(const r of T.queue_pending)if(r.t>=from&&r.t<to&&userMatch(r)&&rowGroup(r)&&!opt.nodes?.length)add(pendingSamples,r.t,()=>new Set()).add(JSON.stringify([r.job_id,r.sub]));
    for(const [t,set] of pendingSamples)day(t).pendingMax=Math.max(day(t).pendingMax,set.size);
    const gpuUserInt=intervals(T.gpu_user,r=>JSON.stringify([r.node,r.gpu_index,r.pid,r.user,r.job_id]),opt.nodeClock?[...new Set([...n.times.gpu_user,...n.times.gpu])].sort((a,b)=>a-b):n.times.gpu_user,step,opt.nodeClock?Math.max(n.times.gpu_user.at(-1)??-Infinity,n.times.gpu.at(-1)??-Infinity):n.times.gpu_user.at(-1));
    const userGPURanges=new Map(),allowedGPUs=new Set(),gpuMem=new Map();
    for(const it of gpuUserInt){const r=it.r;if(!selected.has(r.node)||!userMatch(r))continue;const [a,b]=clip(it.a,it.b);if(b<=a)continue;
      const u=add(userStats,r.user,stats),sec=(b-a)/1000;u.nodes.add(r.node);if(r.mem_used_mb!==null){u.gpuMemSec+=r.mem_used_mb*sec;u.gpuWeight+=sec;}
      const gid=r.node+'/GPU'+r.gpu_index;allowedGPUs.add(gid);add(userGPURanges,JSON.stringify([r.user,gid]),()=>[]).push([a,b]);
      const gm=add(gpuMem,gid,()=>({memSec:0,processSec:0,users:new Set()}));gm.users.add(r.user);if(r.mem_used_mb!==null)gm.memSec+=r.mem_used_mb*sec;gm.processSec+=sec;
    }
    for(const [key,ranges]of userGPURanges){const [user]=JSON.parse(key);add(userStats,user,stats).gpuDeviceSec=(userStats.get(user).gpuDeviceSec||0)+seconds(ranges);}
    const gpuInt=intervals(T.gpu,r=>r.node+'/GPU'+r.gpu_index,n.times.gpu,step,n.times.gpu.at(-1));
    for(const it of gpuInt){const r=it.r,gid=r.node+'/GPU'+r.gpu_index;if(!selected.has(r.node)||(filteringUsers&&!allowedGPUs.has(gid)))continue;const [a,b]=clip(it.a,it.b);if(b<=a)continue;
      const g=add(gpuStats,gid,()=>({...stats(),id:gid,node:r.node,index:r.gpu_index,name:r.gpu_name,uuid:r.uuid,memMax:null}));
      if(r.temp_c!==null)g.tempMax=Math.max(g.tempMax??-Infinity,r.temp_c);if(r.mem_used_mb!==null)g.memMax=Math.max(g.memMax??-Infinity,r.mem_used_mb);
      splits(a,b,opt.bucket,opt.offset,(bt,sec)=>{
        const d=day(bt),h=add(gpuHeat,JSON.stringify([gid,bt]),()=>({node:gid,t:bt,value:0,weight:0}));
        g.observed+=sec;if(r.gpu_util!==null){g.gpuSec+=r.gpu_util*sec;g.gpuWeight+=sec;d.gpuSec+=r.gpu_util*sec;d.gpuWeight+=sec;h.value+=r.gpu_util*sec;h.weight+=sec;}
        if(r.mem_pct!==null){g.memSec+=r.mem_pct*sec;g.memWeight+=sec;}if(r.power_w!==null){g.powerSec+=r.power_w*sec;g.powerWeight+=sec;}
      });
    }
    for(const r of T.gpu){const gid=r.node+'/GPU'+r.gpu_index;if(r.t<from||r.t>=to||!selected.has(r.node)||(filteringUsers&&!allowedGPUs.has(gid)))continue;const g=add(gpuStats,gid,()=>({...stats(),id:gid,node:r.node,index:r.gpu_index,name:r.gpu_name,uuid:r.uuid,memMax:null}));if(r.temp_c!==null)g.tempMax=Math.max(g.tempMax??-Infinity,r.temp_c);if(r.mem_used_mb!==null)g.memMax=Math.max(g.memMax??-Infinity,r.mem_used_mb);}
    const states=new Map();for(const r of T.node_cpu)if(r.t<=to)states.set(r.node,r.state);
    const ns=[...nodeStats].map(([name,s])=>({...s,name,state:states.get(name)||'',groups:[...(n.nodes.get(name)?.groups||[])].join(', '),type:n.nodes.get(name)?.nodeType||'',cores:n.nodes.get(name)?.cores,jobs:hasJobEvidence?(perNodeJobs.get(name)?.size||0):null,jobHours:hasJobEvidence?(perNodeJobSeconds.get(name)||0)/3600:null,coreHours:filteringUsers&&!available.user_cpu?null:s.coreSec/3600,busyHours:s.busy/3600,busyPct:ratio(s.busy,opt.denominator==='calendar'?rangeSec:s.observed),coveredBusyPct:ratio(s.busy,s.observed),allocPct:ratio(s.coreSec,opt.denominator==='calendar'?(n.nodes.get(name)?.cores??0)*rangeSec:s.capacitySec),coverage:ratio(s.observed,rangeSec),memPct:s.memWeight?s.memSec/s.memWeight:null,loadPct:s.loadWeight?s.loadSec/s.loadWeight:null}));
    const capacity=ns.reduce((a,s)=>a+s.capacitySec,0),observed=ns.reduce((a,s)=>a+s.observed,0),busy=ns.reduce((a,s)=>a+s.busy,0),cores=ns.reduce((a,s)=>a+s.coreSec,0);
    const fullNode=rangeSec*nodeNames.filter(x=>n.nodes.get(x)?.hasCPU).length,fullCore=rangeSec*nodeNames.reduce((a,x)=>a+(n.nodes.get(x)?.cores||0),0);
    const users=[...userStats].map(([name,s])=>({name,coreHours:available.user_cpu?s.coreSec/3600:null,jobs:hasJobEvidence?s.jobs.size:null,submitted:available.queue_pending||available.queue_started?s.submitted||0:null,started:available.queue_started?s.started||0:null,jobHours:hasJobEvidence?(s.jobSec||0)/3600:null,activityPct:hasJobEvidence?ratio(seconds(activity.get(name)||[]),rangeSec):null,contribution:available.user_cpu?ratio(s.coreSec,opt.denominator==='calendar'?fullCore:physicalCapacity):null,gpuHours:available.gpu_user?(s.gpuDeviceSec||0)/3600:null,gpuMemHours:available.gpu_user?(s.gpuMemSec||0)/3600:null,nodes:[...s.nodes].join(', ')})).sort((a,b)=>(b.coreHours||b.gpuHours||0)-(a.coreHours||a.gpuHours||0));
    const groups=[...groupStats].map(([name,s])=>({name,nodeCount:s.nodes.size,jobs:hasJobEvidence?(perGroupJobs.get(name)?.size||0):null,jobHours:hasJobEvidence?(perGroupJobSeconds.get(name)||0)/3600:null,coreHours:filteringUsers&&!available.user_cpu?null:s.coreSec/3600,busyHours:s.busy/3600,allocPct:ratio(s.coreSec,opt.denominator==='calendar'?rangeSec*[...s.nodes].reduce((a,x)=>a+(n.nodes.get(x)?.cores||0),0):s.capacitySec),busyPct:ratio(s.busy,opt.denominator==='calendar'?s.nodes.size*rangeSec:s.observed),coverage:ratio(s.observed,s.nodes.size*rangeSec)}));
    const gpu=[...gpuStats.values()].map(g=>({id:g.id,node:g.node,name:g.name,util:g.gpuWeight?g.gpuSec/g.gpuWeight:null,memPct:g.memWeight?g.memSec/g.memWeight:null,tempMax:g.tempMax,power:g.powerWeight?g.powerSec/g.powerWeight:null,energyWh:g.powerWeight?g.powerSec/3600:null,memMax:g.memMax,coverage:ratio(g.observed,rangeSec),users:[...(gpuMem.get(g.id)?.users||[])].join(', ')}));
    const activeClockRows=T.node_cpu.filter(r=>r.t>=from&&r.t<=to&&selected.has(r.node)),lastNodeTs=activeClockRows.at(-1)?.t;
    const latestNodes=lastNodeTs!==undefined?activeClockRows.filter(r=>r.t===lastNodeTs).map(r=>({node:r.node,state:r.state,cpu:r.cpu_alloc,total:r.cpu_total,mem:r.mem_pct})):[]; 
    return {empty:false,settings:opt,meta:metadata(store,n),from,to,summary:{submitted:available.queue_pending||available.queue_started?submitted:null,started:available.queue_started?started:null,running:hasJobEvidence?running:null,jobHours:hasJobEvidence?totalJobSec/3600:null,inferredEnded,coreHours:filteringUsers&&!available.user_cpu?null:available.node_cpu?cores/3600:available.user_cpu?users.reduce((s,u)=>s+(u.coreHours||0),0):null,busyPct:ratio(busy,opt.denominator==='calendar'?fullNode:observed),coveredBusyPct:ratio(busy,observed),allocPct:ratio(cores,opt.denominator==='calendar'?fullCore:capacity),coverage:ratio(observed,fullNode),nodes:nodeNames.length,users:users.length,waitMedian:med(waits),waitMean:waits.length?waits.reduce((a,b)=>a+b,0)/waits.length:null,pending:opt.nodes?.length||!available.queue_pending?null:pendingAt.size,gpuUtil:(()=>{let v=0,w=0;for(const g of gpuStats.values()){v+=g.gpuSec;w+=g.gpuWeight;}return w?v/w:null;})()},
      series:[...daily.values()].sort((a,b)=>a.t-b.t).map(d=>({t:d.t,submitted:available.queue_pending||available.queue_started?d.submitted.size:null,started:available.queue_started?d.started.size:null,running:hasJobEvidence?d.running.size:null,jobHours:hasJobEvidence?d.jobSec/3600:null,coreHours:d.capacitySec||!available.node_cpu&&available.user_cpu?d.coreSec/3600:null,busyPct:ratio(d.busy,d.observed),allocPct:ratio(d.coreSec,d.capacitySec),pendingMax:available.queue_pending&&!opt.nodes?.length?d.pendingMax:null,gpuUtil:d.gpuWeight?d.gpuSec/d.gpuWeight:null,waitMedian:med(d.wait)})),
      nodes:ns.sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true})),groups,users,gpu,jobs:visibleJobs.sort((a,b)=>(b.start??b.submit??b.last??0)-(a.start??a.submit??a.last??0)),
      heat:[...heat.values()].map(h=>({node:h.node,t:h.t,value:ratio(h.core,h.cap),coverage:ratio(h.obs,(Math.min(to,nextBucket(h.t,opt.bucket,opt.offset))-Math.max(from,h.t))/1000)})),
      gpuHeat:[...gpuHeat.values()].map(h=>({node:h.node,t:h.t,value:h.weight?h.value/h.weight:null,coverage:ratio(h.weight,(Math.min(to,nextBucket(h.t,opt.bucket,opt.offset))-Math.max(from,h.t))/1000)})),
      reasons:[...reason].map(([name,count])=>({name,count})),pendingRows:[...pendingAt.values()].map(j=>({id:j.id,user:j.user,partition:j.partition,cpus:j.pendingRecord.req_cpus,waitMin:(j.pendingRecord.wait_sec??(j.pendingRecord.wait_min!==null?j.pendingRecord.wait_min*60:(j.submit!==null?(lastQueue-j.submit)/1000:null)))===null?null:(j.pendingRecord.wait_sec??(j.pendingRecord.wait_min!==null?j.pendingRecord.wait_min*60:(lastQueue-j.submit)/1000))/60,reason:j.pendingRecord.reason,priority:j.pendingRecord.priority})),
      latestNodes,lastQueue,lastNodeTs,
      notices:[
        '30분 사이에 시작하고 끝난 작업은 로그에 잡히지 않을 수 있습니다. 제출·시작 건수는 관측된 작업 기준입니다.',
        '작업 소멸은 종료로 추정하며 성공·실패를 의미하지 않습니다. 종료 시각은 다음 확인 기록 시각, 오차는 최대 기록 주기 수준입니다.',
        '각 상태를 다음 기록까지 유지한다고 가정합니다. 긴 기록 공백은 최대 한 주기까지만 적분하고, 마지막 기록 이후는 연장하지 않습니다.',
        '빈 사용자·대기 기록과 수집 실패는 행만으로 완전히 구분할 수 없습니다. node_cpu 시각을 빈 스냅샷 기준으로 사용할 수 있습니다.',
        filteringUsers?'사용자 필터의 분모는 선택 노드 전체입니다. 전체 사용자 기록이 확인된 시점에서 선택 사용자만 없으면 미사용으로 추정합니다. 사용자 로그 전체가 빠진 가동 시점은 미관측 처리합니다.':'코어 할당률은 계산 효율이 아닙니다. CPU load와 실제 CPU 계산 사용률도 구분합니다.',
        'GPU 사용자 사용시간은 프로세스 관측 기반 GPU·h이며 공유 GPU에서는 사용자 합계가 장치 시간보다 클 수 있습니다.',
        ...(filteringUsers?['GPU 사용률·온도·전력은 선택 사용자가 사용한 GPU 장치 전체 값입니다. 사용자별 계산 사용률로 배분하지 않습니다.']:[]),
        ...(!T.node_cpu.length?['node_cpu가 없어 전체 코어 용량·노드 사용률·관측률을 계산할 수 없습니다.']:[]),
        ...(filteringUsers&&!T.user_cpu.length?['user_cpu가 없어 사용자별 CPU 할당을 계산할 수 없습니다.']:[]),
        ...(filteringGroups?['노드 그래프는 선택 partition에 속한 물리 노드 전체를 보여줍니다. 사용자별 집계는 작업 partition도 적용합니다.']:[]),
        ...(nodeNames.some(x=>(n.nodes.get(x)?.groups.size||0)>1)?['여러 partition에 속한 노드는 그룹별 표에 중복 포함될 수 있습니다. 전체 합계에서는 한 번만 계산합니다.']:[]),
        ...(opt.denominator==='calendar'?['전체 조회기간 기준 비율은 미관측 시간의 작업량을 포함하지 않은 하한값입니다. 관측률과 함께 해석하세요.']:[])
      ]};
  }
  function metadata(store,n){
    return {counts:Object.fromEntries(TYPES.map(k=>[k,store.tables[k].size])),files:store.files,issues:store.issues,issueTotal:store.issueTotal,duplicates:store.duplicates,conflicts:store.conflicts,demo:store.demo,min:n.min,max:n.max,groups:n.groups,users:n.users,nodes:[...n.nodes.keys()].sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}))};
  }
  function demo(){
    const s=createStore();s.demo=true;
    const base=time('2026-11-01T00:00:00'),ns=[['node01','cpu-small',64],['node02','cpu-small',64],['node06','cpu-large',128],['node07','cpu-large',128],['node10','gpu',64],['node11','gpu',64]];
    const owners=['kim','lee','park','kang'],starts=new Map(),submit=new Map();
    const summary={rows:0,duplicates:0,invalid:0,types:new Set()};
    const ins=(type,r)=>insert(s,type,r,'내장 예제',0,summary);
    for(let i=0;i<=7*48;i++){
      if(i>=137&&i<=141)continue;
      const timestamp=iso(base+i*1800000),t=base+i*1800000;
      for(let ni=0;ni<ns.length;ni++){
        const [node,partition,cpu_total]=ns[ni],hour=i%48/2,active=ni===3?i%96<18:hour>5&&hour<23||i%17<4,cycle=Math.floor(i/(ni%2?8:12)),job_id=String(12000+ni*1000+cycle),user=owners[(cycle+ni)%owners.length],cpu_alloc=active?cpu_total*(ni%2?.5:.75):0;
        ins('node_cpu',{timestamp,node,node_type:ni>=4?'GPU':'CPU',partitions:partition,state:active?'MIXED':'IDLE',cpu_total,cpu_alloc,alloc_pct:cpu_alloc/cpu_total*100,cpu_load:cpu_alloc*.86,load_pct:cpu_alloc/cpu_total*86,mem_total_mb:262144,mem_alloc_mb:active?65536:0,mem_pct:active?25:0});
        if(active){
          if(!starts.has(job_id)){starts.set(job_id,t-9*60000);submit.set(job_id,t-(25+ni*13)*60000);}
          ins('user_cpu',{timestamp,user,node,partition,cpus_alloc:cpu_alloc,mem_alloc_mb:65536,n_jobs:1,job_ids:job_id});
          ins('queue_started',{timestamp,job_id,user,partition,submit_time:iso(submit.get(job_id)),start_time:iso(starts.get(job_id)),wait_sec:(starts.get(job_id)-submit.get(job_id))/1000,wait_min:(starts.get(job_id)-submit.get(job_id))/60000,alloc_cpus:cpu_alloc,n_nodes:1,state:'RUNNING'});
        }
        if(ni>=4)for(let gi=0;gi<2;gi++){
          const util=active?35+((i*7+gi*21+ni*3)%64):0,mem=active?8000+gi*4000:0;
          ins('gpu',{timestamp,node,gpu_index:String(gi),uuid:'GPU-example-'+ni+'-'+gi,gpu_name:'Example GPU 48 GB',gpu_util:util,mem_total_mb:49152,mem_used_mb:mem,mem_pct:mem/49152*100,temp_c:active?55+util*.25:35,power_w:active?140+util*2:28});
          if(active)ins('gpu_user',{timestamp,node,gpu_index:String(gi),user,job_id,pid:String(5000+ni*100+gi),mem_used_mb:mem,proc_name:'simulation'});
        }
      }
      for(let pi=0;pi<(i%48>17&&i%48<40?4:1);pi++){
        const id=String(60000+Math.floor(i/10)*10+pi),user=owners[pi%4],sub=base+Math.floor(i/10)*10*1800000-3600000;
        ins('queue_pending',{timestamp,job_id:id,user,partition:pi%2?'cpu-large':'gpu',qos:'normal',req_cpus:64,submit_time:iso(sub),wait_sec:(t-sub)/1000,wait_min:(t-sub)/60000,priority:1000+pi,reason:pi%2?'Priority':'Resources'});
      }
    }
    s.files=[{name:'내장 예제 · 실제 로그가 아닙니다',rows:summary.rows,duplicates:0,invalid:0,types:TYPES.slice()}];return s;
  }
  const describe=(store,options={})=>metadata(store,normalize(store,{...defaults,...options}));
  return {TYPES,SCHEMA,defaults,time,iso,num,list,fields,detect,createStore,loadText,loadFile,loadSession,session,normalize,describe,analyze,demo,mergeRanges,seconds,bucketStart,nextBucket};
})();
if(typeof globalThis!=='undefined')globalThis.HPC=HPC;
