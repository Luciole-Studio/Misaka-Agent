'use strict';
// Side-panel derivations from production code: patches, project paths, per-file changes and context usage.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../misaka/ui/gui/static/app.js'),'utf8');
const ctx=vm.createContext({console,JSON,Map,Set,WeakMap,Number,String,Array,Object,navigator:{platform:'Win32'},workspace:'D:\\Research\\项目'});
function load(begin,end){const start=source.indexOf(begin);assert.ok(start>=0,begin);vm.runInContext(source.slice(start,source.indexOf(end,start)),ctx);}
load('// Persisted calls carry', 'function toolIcon(');
load('function chatSteps(', 'function stepKind(');
load('function chatUsage(', 'function usageTone(');
const patch=['--- final/report.md','+++ final/report.md','@@ -9,4 +9,5 @@',' 上文',' ','-待补充。','+第一句。','+第二句。',' 下文',''].join('\n');
const lines=vm.runInContext('parsePatch(p)',Object.assign(ctx,{p:patch}));
assert.deepEqual([...lines].map(l=>l.type),['hunk','ctx','ctx','del','add','add','ctx'],'headers are skipped, hunks parsed');
assert.equal(lines[3].a,11);assert.equal(lines[4].b,11);assert.equal(lines[6].a,12);assert.equal(lines[6].b,13);
ctx.p='diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\ndiff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -0,0 +1 @@\n+c\n';
assert.deepEqual([...vm.runInContext('parsePatch(p)',ctx)].map(l=>l.type),['hunk','del','add','hunk','add'],'a second file header never counts as a change');
const rel=p=>vm.runInContext('projectRelative(q)',Object.assign(ctx,{q:p}));
assert.equal(rel('D:\\research\\项目\\资料\\笔记.md'),'资料/笔记.md','Windows paths fold case and separators');
assert.equal(rel('./final/report.md'),'final/report.md');
assert.equal(rel('D:\\Research\\项目'),'.');
assert.equal(rel('C:\\elsewhere\\a.md'),'C:/elsewhere/a.md','outside paths stay absolute');
const ch={items:[{kind:'user',text:'改报告'},{kind:'assistant',blocks:[
  {type:'toolCall',id:'e1',name:'edit',arguments:{path:'D:\\Research\\项目\\final\\report.md',edits:[{oldText:'a',newText:'b'}]}},
  {type:'toolCall',id:'w1',name:'write',arguments:{path:'nodes/表.csv',content:'x,y\n1,2\n'}},
  {type:'toolCall',id:'e2',name:'edit',arguments:{path:'bad.md',edits:[{oldText:'q',newText:'r'}]}},
  {type:'toolCall',id:'e3',name:'edit',arguments:{path:'final/report.md',edits:[{oldText:'one\ntwo',newText:'three'}]}},
  {type:'toolCall',id:'r1',name:'read',arguments:{path:'资料/笔记.md'}},
  {type:'toolCall',id:'t1',name:'misaka_todo',arguments:{ops:[]}}]}],
  results:{e1:{content:[],details:{patch,firstChangedLine:11}},w1:{content:[]},e2:{content:[],isError:true},r1:{content:[]},t1:{content:[]}},exec:{e3:{status:'running'}},meta:{workspace:'D:\\Research\\项目'},streaming:true};
ctx.ch=ch;
const changes=vm.runInContext('chatChanges(ch)',ctx);
assert.deepEqual([...changes].map(c=>c.path),['final/report.md','nodes/表.csv'],'failed edits and non-file tools are left out; one card per file');
const report=changes[0];
assert.equal(report.edits.length,2);assert.equal(report.state,'running');assert.equal(report.adds,3);assert.equal(report.dels,3);
assert.equal(report.edits[0].exact,true);assert.equal(report.edits[0].firstLine,11);assert.equal(report.edits[1].exact,false,'running edit falls back to its arguments');
assert.equal(changes[1].writes,1);assert.equal(changes[1].adds,2);
assert.deepEqual([...vm.runInContext('chatReads(ch)',ctx)].map(e=>[...e]),[['资料/笔记.md',1]]);
ch.items[1].usage={input:1000,output:200,cacheRead:3000,cacheWrite:0,cost:{total:.01}};
ch.items.push({kind:'assistant',blocks:[],usage:{input:500,output:100,cacheRead:4000,cacheWrite:100,cost:{total:.02}}});
ch.meta.model={contextWindow:20000};
const usage=vm.runInContext('chatUsage(ch)',ctx);
assert.equal(usage.used,4700,'context size comes from the latest reply');assert.equal(usage.total.input,1500);assert.equal(usage.total.output,300);
assert.ok(Math.abs(usage.total.cost-.03)<1e-9);assert.ok(Math.abs(usage.ratio-.235)<1e-9);
console.log('Side-panel checks passed: patch parsing, project-relative paths, per-file changes, reads and context usage.');
