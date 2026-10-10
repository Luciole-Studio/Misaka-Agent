'use strict';
// Exercise production history, row and queue functions independently of the UI boot.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../misaka/ui/gui/static/app.js'),'utf8');
class Node {
  constructor(cls=''){this.cls=cls;this.children=[];this.classList={contains:c=>cls.split(' ').includes(c)};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
}
const actions=[],requests=[],recovered=[];
const ch={items:[],results:{},exec:{},meta:{capabilities:['models','queue','withdraw'],workspace:'project',pendingPrompts:[]},outbox:[],streaming:true};
const ctx=vm.createContext({console,Date,Set,JSON,chats:{side:ch},ch,selectedChat:'main',workspace:'project',
  crypto:require('node:crypto'),chatState:id=>ctx.chats[id],
  el:(tag,cls)=>new Node(cls),icon:()=>new Node(),button:(label,action)=>{const n=new Node();n.label=label;n.action=action;actions.push(n);return n;},
  sessionApi:async(id,op,data)=>{requests.push({id,op,...data});return {text:'recovered',images:[{data:'image',mime:'image/png'}],files:['notes.md'],pendingPrompts:[]};},
  restoreWithdrawn:(...args)=>recovered.push(args),scheduleRender(){},toast(){},pruneOutbox(){},
  isAttached:id=>id.startsWith('attached:'),isSnapshot:id=>id.startsWith('snapshot:')});
function load(begin,end){vm.runInContext(source.slice(source.indexOf(begin),source.indexOf(end,source.indexOf(begin))),ctx);}
load('function blockText(', 'function ensureAssistant(');
load('function buildRows(', '// Persisted calls');
load('function sessionSupports(', 'function sessionApi(');
load('async function resumeForSend(', 'function setContextOpen(');
load('async function sendQueuedNow(', 'function renderQueue(');
load('function renderQueue(', 'function drawMessageQueue(');
(async()=>{
  ctx.history=[{role:'user',content:'hello'},{role:'custom',customType:'agent-messages',display:true,content:'mail',details:{count:1}},{role:'custom',display:false,content:'hidden'}];
  vm.runInContext('buildItemsFromHistory(ch,history)',ctx);
  assert.equal(ch.items.length,2);assert.equal(ch.items[1].title,'agent-messages');assert.equal(ch.items[1].details.count,1);
  ch.outbox=[{text:'queued',queued:true,sent:false},{text:'sending',queued:false,sent:false}];ch.streaming=true;
  const rows=vm.runInContext('buildRows(ch)',ctx);
  assert.equal(rows.filter(r=>r.type==='user').length,2,'pending follow-up never appears as a normal user bubble');
  assert.equal(vm.runInContext("sessionSupports('attached:old','models')",ctx),false,'legacy owner lacks live settings');
  assert.equal(vm.runInContext("sessionSupports('side','models')",ctx),true);
  ch.outbox=[];ch.meta.pendingPrompts=[{id:'p',text:'queued'}];ctx.root=new Node('context-queue');
  vm.runInContext("renderQueue(root,'side')",ctx);
  await actions.find(a=>a.label==='撤回').action();
  assert.deepEqual(requests,[{id:'side',op:'withdraw',message_id:'p'}]);assert.equal(recovered[0][0],'side');assert.equal(recovered[0][2],true);
  assert.equal(recovered[0][1].images.length,1);assert.equal(ch.meta.pendingPrompts.length,0);
  ctx.chats['attached:old']={items:[],results:{},exec:{},meta:{workspace:'project'},outbox:[],streaming:true};
  vm.runInContext("applyAttachedSnapshot(chats['attached:old'],{messages:[],state:'working',paused:true})",ctx);
  assert.equal(ctx.chats['attached:old'].streaming,false,'a paused owner never renders as running');
  ctx.selectedChat='attached:old';ctx.readOnlyView=null;ctx.pendingImages=[];
  const input={value:''},send={classList:{toggle:(name,on)=>{send.stop=on;}}};
  ctx.$=id=>id==='#chat-input'?input:send;
  load('function chatFlags(', 'function updateChatHeader(');
  vm.runInContext('updateSendButton()',ctx);
  assert.equal(send.stop,false);assert.equal(send.disabled,true);
  input.value='resume with new input';vm.runInContext('updateSendButton()',ctx);
  assert.equal(send.disabled,false);assert.equal(send.title,'发送并恢复任务（Enter）');
  requests.length=0;
  ctx.local={text:'local draft',images:[],files:[],sent:false,queued:true};
  await vm.runInContext("sendQueuedNow('attached:old',{local})",ctx);
  assert.equal(requests[0].op,'resume');assert.equal(requests[1].op,'input');
  assert.equal(requests[1].streamingBehavior,'steer');assert.equal(ctx.local.queued,false);
  requests.length=0;ctx.local={text:'local draft',sent:false,queued:true};
  await vm.runInContext("sendQueuedNow('side',{local})",ctx);
  assert.equal(requests.length,1,'local promotion uses one atomic request');assert.equal(requests[0].op,'send_now');
  assert.equal(ctx.local.sent,true);assert.equal(ctx.local.queued,false);
  console.log('Frontend workspace checks passed: custom history, hidden context, queued rows, legacy capabilities, side withdrawal routing and attachments.');
})().catch(e=>{console.error(e);process.exitCode=1;});
