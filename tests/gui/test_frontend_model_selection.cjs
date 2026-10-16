"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../../misaka/ui/gui/static/app.js"),"utf8");
const begin=source.indexOf("async function testProvider("),end=source.indexOf("function apiKeyDialog(",begin);
class Node {
  constructor(tag,cls="",text=""){this.tag=tag;this.className=cls;this.textContent=text;this.children=[];this.value="";this.attrs={};}
  append(...items){this.children.push(...items);}replaceChildren(...items){this.children=[...items];}
  setAttribute(key,value){this.attrs[key]=value;}
}
const el=(tag,cls="",text="",...rest)=>{const n=new Node(tag,cls,typeof text==="string"?text:"");if(text instanceof Node)n.append(text);n.append(...rest);return n;};
const all=n=>[n,...n.children.filter(x=>x instanceof Node).flatMap(all)];
let body,closed=false,requests=[];
const ctx=vm.createContext({el,fmtTokens:n=>n,textInput:(value,placeholder)=>Object.assign(new Node("input"),{value,placeholder}),
  button:(text,action,cls)=>Object.assign(el("button",cls,text),{action}),modal:(_title,n)=>{body=n;},
  closeDialog:()=>{closed=true;},toast(){},drawSettingsShell(){},view:"settings",settingsCache:{},
  sapi:async(op,params)=>{requests.push({op,params});if(op==="provider_models")return {models:[{id:"a",name:"Alpha"},{id:"b",name:"Beta"}],selected:["a"],enabled:true,message:"saved"};return {message:"saved"};},
  api:async(_endpoint,params)=>{requests.push(params);return {models:[{id:"a",name:"Alpha"},{id:"b",name:"Beta"},{id:"c",name:"Beta Plus"}],message:"fresh"};}});
vm.runInContext(source.slice(begin,end),ctx);
(async()=>{
 await vm.runInContext("manageProviderModels({id:'provider',name:'Provider'})",ctx);
 const find=text=>all(body).find(n=>n.tag==="button"&&n.textContent===text);
 const search=all(body).find(n=>n.attrs["aria-label"]==="搜索可启用模型");
 search.value="Beta";search.oninput();
 let rows=all(body).find(n=>n.className==="provider-model-list");assert.equal(rows.children.length,1);
 let cb=all(rows).find(n=>n.type==="checkbox");cb.checked=true;cb.onchange();
 await find("获取模型列表").action();
 assert.equal(rows.children.length,2); // Search remains active after catalogue refresh.
 await find("选择筛选结果").action();
 await find("保存启用选择").action();
 const saved=requests.find(r=>r.op==="save_model_selection");
 assert.deepEqual(Array.from(saved.params.models),["a","b","c"]);assert.equal(saved.params.enabled,true);assert.equal(closed,true);
 console.log("Model selection checks passed: search, draft preservation, filtered selection and persistence payload.");
})().catch(e=>{console.error(e);process.exitCode=1;});
