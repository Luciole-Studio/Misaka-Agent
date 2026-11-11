'use strict';
// Exercise the production notification/event handlers without browser permissions or models.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../misaka/ui/gui/static/app.js'), 'utf8');
const sent = [], notices = [], store = new Map(), expanded = [];
let permissions = 0, polls = 0;
class Notification {
  static permission = 'default';
  static async requestPermission() { permissions++; return this.permission = 'granted'; }
  constructor(title, options) { sent.push({title, ...options}); }
}
const chat = {cursor:4, live:true, items:[], meta:{name:'后台对话',workspace:'project'},outbox:[]};
const context = vm.createContext({Notification,window:{Notification,focus(){}},localStorage:{getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)},
  $:()=>({setAttribute(){}}),toast:(...args)=>notices.push(args),chats:{background:chat},
  selectedChat:'other',workspace:'project',samePath:(a,b)=>a===b,chatTitle:m=>m.name,guard:fn=>fn(),
  switchProject:async()=>{},selectChat:()=>{},view:'settings',guiClosed:false,
  isAttached:()=>false,isSnapshot:()=>false,chatState:()=>chat,failOutbox(){},flushOutbox(){},scheduleRender(){},
  sleep:async()=>{},api:async()=>{polls++;return {reset:false,events:[{type:'run_settled'}],meta:{streaming:false},status:'ready',cursor:5};},
  contextOpen:false,contextTab:'files',contextThread:null,contextTaskSeen:new Map(),contextMemberSeen:new Map(),
  projectKey:x=>x,activateContext:tab=>expanded.push(tab),drawContextTasks(){},drawContextSisters(){},
  state:{cards:[],chats:[],sessions:[]},Object,Set,Date,String,console});
vm.runInContext(source.slice(source.indexOf('function notificationEnabled()'),source.indexOf("$('#context-toggle').onclick")),context);
const eventStart=source.indexOf('function applyChatEvent('),eventEnd=source.indexOf('\n}\n',eventStart)+3;
vm.runInContext(source.slice(eventStart,eventEnd),context);
const pollStart=source.indexOf('async function pollOneChat('),pollEnd=source.indexOf('\n}\n',pollStart)+3;
vm.runInContext(source.slice(pollStart,pollEnd),context);
const observeStart=source.indexOf('function observeContextTasks('),observeEnd=source.indexOf('\nfunction drawContextTasks(',observeStart);
vm.runInContext(source.slice(observeStart,observeEnd),context);
(async()=>{
  vm.runInContext("systemNotify('完成','消息','first')",context);
  assert.equal(permissions,0,'background work never requests permission');
  assert.equal(sent.length,0,'ungranted notifications are quiet');
  await vm.runInContext('toggleNotifications()',context);
  assert.equal(permissions,1,'permission is requested by the user action');
  await vm.runInContext("pollOneChat('background')",context);
  assert.equal(polls,1,'background chat is polled while another page is visible');
  assert.equal(sent[0].title,'对话已完成 · MISAKA');
  vm.runInContext("applyChatEvent(chats.background,{type:'ui_request',id:'q',title:'确认计划'})",context);
  assert.equal(sent[1].title,'需要你的确认 · MISAKA');
  vm.runInContext("systemNotify('x','x','unique');systemNotify('x','x','unique')",context);
  assert.equal(sent.length,3,'replayed notifications are deduplicated');
  await vm.runInContext('toggleNotifications()',context);
  vm.runInContext("systemNotify('静默','x','muted')",context);
  assert.equal(sent.length,3,'muted notifications are suppressed');
  Notification.permission='denied';
  await vm.runInContext('toggleNotifications()',context);
  assert.equal(store.get('misaka-notifications'),'off','denied permissions never enable notifications');
  assert.ok(notices.some(n=>String(n[0]).includes('浏览器阻止')));
  context.view='chat';
  vm.runInContext('observeContextTasks()',context);
  assert.deepEqual(expanded,[],'existing state leaves the panel closed by default');
  context.state.cards=[{id:'task',status:'running',title:'Task'}];
  vm.runInContext('observeContextTasks();observeContextTasks()',context);
  assert.deepEqual(expanded,['tasks'],'a new task expands the panel once');
  context.state.chats=[{id:'sister',role:'10086',status:'ready'}];
  vm.runInContext('observeContextTasks()',context);
  assert.deepEqual(expanded,['tasks','sisters'],'a new Sister conversation expands the panel');
  console.log('Frontend checks passed: consent, background completion, confirmation, deduplication, mute, denial, default sidebar state, automatic task and Sister expansion.');
})().catch(error=>{console.error(error);process.exitCode=1;});
