"use strict";
// New chats must consult current credentials, even with absent or stale browser storage.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const source=fs.readFileSync(require("node:path").join(__dirname,"../../misaka/ui/gui/static/app.js"),"utf8");
const begin=source.indexOf("async function showModelPicker("),end=source.indexOf("function showThinkingPicker(",begin);
const requests=[],menus=[];
class Node {constructor(){this.children=[];}append(...items){this.children.push(...items);}}
const anchor={isConnected:true};
const ctx=vm.createContext({selectedChat:null,workspace:"project",homeModel:null,state:{model:"default"},
  $:()=>anchor,togglePopover:()=>false,el:()=>new Node(),menuSearch:()=>new Node(),
  menuItem:options=>options,openPopover:(a,box)=>menus.push(box),updateChatHeader(){},
  fmtTokens:n=>n,openSettings(){},toast(){},drawContextThread(){},
  sapi:async(op,params)=>{requests.push({op,params});return {models:[{provider:"google",id:"gemini-test"},{provider:"openai-codex",id:"gpt-test"}]};},
  jsonStored:()=>{throw Error("must not use a stale browser model cache");},
  chatState:()=>({meta:{workspace:"project",capabilities:["models"]}}),
  isSnapshot:()=>false,sessionSupports:()=>true,
  sessionApi:async(id,op,params)=>{requests.push({id,op,params});return {shortlisted:true,models:[{provider:"gateway",id:"vendor/model"}]};}});
vm.runInContext(source.slice(begin,end),ctx);
(async()=>{
  await vm.runInContext("showModelPicker()",ctx);
  assert.equal(requests[0].op,"models");assert.equal(requests[0].params.configured_only,true);
  const choices=menus[0].children.filter(x=>x.onClick);
  assert.equal(choices.length,4);await choices.find(x=>x.label==="gpt-test").onClick();
  assert.equal(ctx.homeModel.provider,"openai-codex");assert.equal(ctx.homeModel.id,"gpt-test");
  requests.length=0;menus.length=0;ctx.selectedChat="sister-session";
  await vm.runInContext("showModelPicker()",ctx);
  await menus[0].children.find(x=>x.label==="vendor/model").onClick();
  assert.equal(requests[1].id,"sister-session");assert.equal(requests[1].op,"set_model");
  assert.equal(requests[1].params.provider,"gateway");assert.equal(requests[1].params.model,"vendor/model");
  requests.length=0;menus.length=0;
  ctx.sessionApi=async()=>({models:[{provider:"google",id:"gemini-test"},{provider:"old-service",id:"not-enabled"}]});
  await vm.runInContext("showModelPicker()",ctx);
  assert.equal(requests[0].op,"models");assert.equal(requests[0].params.configured_only,true);
  const legacy=menus[0].children.filter(x=>x.onClick).map(x=>x.label);
  assert(legacy.includes("gemini-test"));assert(!legacy.includes("not-enabled"));
  console.log("Model picker checks passed: fresh catalogue, pre-chat selection, Sister routing and legacy-owner filtering.");
})().catch(e=>{console.error(e);process.exitCode=1;});
