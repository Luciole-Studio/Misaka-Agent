'use strict';
// Quotes, rewind parsing, fork eligibility and side-chat events from production code.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../misaka/ui/gui/static/app.js'),'utf8');
const ctx=vm.createContext({console,JSON,Map,Set,Number,String,Array,Object,navigator:{platform:'Win32'},workspace:'D:\\Research\\项目',
  chats:{},selectedChat:'main',isAttached:id=>String(id).startsWith('attached:'),isSnapshot:id=>String(id).startsWith('snapshot:'),
  notifyChat(){},autoPreviewTool(){},sessionSupports:(id,op)=>(ctx.chats[id]?.meta.capabilities||[]).includes(op)});
function load(begin,end){const start=source.indexOf(begin);assert.ok(start>=0,begin);const stop=source.indexOf(end,start);assert.ok(stop>start,end);vm.runInContext(source.slice(start,stop),ctx);}
load('function freshChat(', 'function isAttached(');
ctx.chatState=id=>ctx.chats[id]??=vm.runInContext('freshChat()',ctx);
load('function blockText(', 'function ensureAssistant(');
load('function ensureAssistant(', 'function applyChatEvent(');
const eventStart=source.indexOf('function applyChatEvent(');vm.runInContext(source.slice(eventStart,source.indexOf('\n}\n',eventStart)+3),ctx);
load('function projectRelative(', 'function parsePatch(');
load('function isSide(', 'function rewindState(');
load('const REFERENCE_MARK', 'async function rewindLast(');
load('const QUOTE_BLOCK', 'function quoteLabel(');
load('function sideKey(', 'function sideDraftOf(');
const run=code=>vm.runInContext(code,ctx);

// A message carries its quotes after the typed text; the view and a rewind split them back out.
ctx.quotes=[{text:'第一段\n含换行',note:'为什么？\n第二行'},{text:'第二段',note:''}];
const sent=run("composeOutgoing('请对比两段',quotes)");
assert.ok(sent.startsWith('请对比两段\n\n[引用 1：原文]\n第一段\n含换行\n[引用 1：批注]\n为什么？\n第二行\n[引用 1：完]'));
assert.ok(sent.endsWith('[引用 2：原文]\n第二段\n[引用 2：完]'),'a quote without a note has no note block');
ctx.sent=sent+'\n\n[引用的当前项目文件，可按需读取]\n- D:\\Research\\项目\\资料\\笔记.md';
const refs=run('splitReferences(sent)');
assert.deepEqual(JSON.parse(JSON.stringify(refs.references)),[{path:'资料/笔记.md',workspace:'D:\\Research\\项目',token:'@"资料/笔记.md"'}]);
ctx.body=refs.text;const parsed=run('splitQuotes(body)');
assert.equal(parsed.text,'请对比两段');
assert.deepEqual(JSON.parse(JSON.stringify(parsed.quotes)),[{text:'第一段\n含换行',note:'为什么？\n第二行'},{text:'第二段',note:''}]);
assert.equal(run("splitQuotes(composeOutgoing('',[{text:'只有引用',note:''}]))").text,'','a message may be quotes alone');
assert.equal(run("splitQuotes('[引用 1：原文] 不是块')").quotes.length,0,'plain text is never mistaken for a quote');

// History keeps message timestamps: they name a turn for fork and a prompt for rewind.
const main=ctx.chats.main=run('freshChat()');main.meta={status:'ready',capabilities:['fork','rewind','side']};
ctx.history=[{role:'user',content:'问',timestamp:1},{role:'assistant',content:[{type:'text',text:'答'}],timestamp:2}];
run('buildItemsFromHistory(chats.main,history)');
assert.deepEqual([...main.items.map(i=>i.ts)],[1,2]);
ctx.turn={items:[main.items[1]]};
assert.equal(run("forkPoint(turn,'main')"),2);
main.meta.status='starting';assert.equal(run("forkPoint(turn,'main')"),null,'a starting chat cannot fork yet');
ctx.chats['snapshot:a']={meta:{sessionPath:'a.jsonl'}};assert.equal(run("forkPoint(turn,'snapshot:a')"),2,'saved sessions fork from their file');
ctx.chats['snapshot:b']={meta:{}};assert.equal(run("forkPoint(turn,'snapshot:b')"),null);
assert.equal(run("forkPoint(turn,'btw:main:s')"),null,'side answers never fork');

// Side events ride the main chat's stream, survive a replay and disappear when closed.
main.meta.status='ready';
run("applyChatEvent(chats.main,{type:'side_event',side:'s1',event:{type:'agent_start'}})");
run("applyChatEvent(chats.main,{type:'side_event',side:'s1',event:{type:'message_start',message:{role:'user',content:'顺带',timestamp:5}}})");
assert.equal(main.items.length,2,'side events never touch the main transcript');
const side=run("sideThread('main','s1')");
assert.equal(side,ctx.chats['btw:main:s1']);assert.equal(side.items[0].text,'顺带');assert.equal(side.streaming,true);
assert.equal(run('sideActiveId(chats.main)'),'s1','after a reload the newest side thread is shown');
run("applyChatEvent(chats.main,{type:'side_error',side:'s1',message:'没有额度'})");
assert.equal(side.items.at(-1).kind,'error');assert.equal(side.streaming,false);
run("restoreSides(chats.main,[{id:'s2',streaming:true,messages:[{role:'user',content:'另一个',timestamp:6}]}])");
assert.equal(main.sides.s2.items[0].text,'另一个');assert.equal(main.sides.s2.streaming,true);
run("applyChatEvent(chats.main,{type:'side_closed',side:'s2'})");
assert.equal(main.sides.s2,undefined);assert.equal(run('sideActiveId(chats.main)'),null);
main.sideActive='s1';assert.equal(run('sideActiveId(chats.main)'),'s1');
run("applyChatEvent(chats.main,{type:'ready'})");main.rewound={sent:true};
run("applyChatEvent(chats.main,{type:'run_settled'})");assert.equal(main.rewound,null,'a finished reply ends the chance to restore');
main.rewound={sent:false};run("applyChatEvent(chats.main,{type:'run_settled'})");assert.ok(main.rewound,'an unsent rewind stays restorable');

// Sister attachments and side-question quotes render together without replacing either helper.
load('function sideDraft(', 'async function sidePicker(');
load('function sideDraftOf(', 'function sideUnavailable(');
load('function drawSideQuoteChips(', 'function drawSidePanel(');
const node=()=>({children:[],append(...items){this.children.push(...items);},replaceChildren(...items){this.children=[...items];}});
ctx.$=(_selector,root)=>root.chips;ctx.el=node;ctx.icon=node;ctx.button=node;ctx.quoteChip=node;
ctx.chats.sister=run('freshChat()');
ctx.chats.sister.sideDraft={text:'',images:[{data:'AA==',mime:'image/png'}],files:['notes.md']};
main.btwDraft={text:'',quotes:[{text:'Quoted passage',note:''}]};
ctx.sisterRoot={chips:node()};ctx.sideRoot={chips:node()};
run("drawSideChips(sisterRoot,'sister')");assert.equal(ctx.sisterRoot.chips.children.length,2);
run('drawSideQuoteChips(sideRoot,chats.main)');assert.equal(ctx.sideRoot.chips.children.length,1);
run("drawSideChips(sisterRoot,'sister')");assert.equal(ctx.sisterRoot.chips.children.length,2,'side quotes preserve Sister image and file chips');
assert.equal(ctx.chats.sister.sideDraft.images[0].data,'AA==');assert.equal(main.btwDraft.quotes.length,1);
console.log('Frontend branching checks passed: quote round trip, reference parsing, turn timestamps, fork eligibility, side-chat events and Sister attachments.');
