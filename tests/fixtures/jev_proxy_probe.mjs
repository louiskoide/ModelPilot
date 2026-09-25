// Offline only: actual pinned Jev proxy/policy, stub scoring, loopback provider.
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import http from 'node:http';
const [checkout, upstreamURL, scenario] = process.argv.slice(2);
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(upstreamURL)) throw Error('Loopback only');
const {startProxy}=await import(pathToFileURL(resolve(checkout,'src/proxy.mjs')));
let calls=0;
const route=async ({models})=>{
  calls++;
  if(scenario==='fallback') return null;
  const chosen=models.find(m=>m.tier==='sonnet');
  return {choice:chosen.id,confidence:.99,request:{fixture:true},response:{fixture:true},ms:1};
};
const {port,close}=await startProxy({upstreamURL,route});
function request(method,path,body){
 return new Promise((done,fail)=>{
  const req=http.request({host:'127.0.0.1',port,method,path,headers:{'content-type':'application/json','x-api-key':'sk-ant-offline'}},res=>{
   let text='';res.on('data',c=>text+=c);res.on('end',()=>done({status:res.statusCode,body:JSON.parse(text)}));
  });req.on('error',fail); req.end(body?JSON.stringify(body):undefined);
 });
}
try {
 await request('GET','/v1/models');
 const first={role:'user',content:'Read sample.txt'};
 const tail={role:'system',content:'# Environment'};
 const body={model:'jev-router',max_tokens:32,tools:[{name:'Read',input_schema:{type:'object'}}],
  metadata:{user_id:JSON.stringify({session_id:'offline-session'})},messages:[first,tail]};
 const replies=[];
 replies.push(await request('POST','/v1/messages',body));
 body.messages=[first,{role:'assistant',content:[{type:'tool_use',id:'t1',name:'Read',input:{file_path:'sample.txt'}}]},
  {role:'user',content:[{type:'tool_result',tool_use_id:'t1',content:'fixture'}]},tail];
 replies.push(await request('POST','/v1/messages',body));
 const afterContinuation=calls;
 body.messages.splice(-1,0,{role:'assistant',content:'Done.'},{role:'user',content:'Now read second.txt'});
 if(scenario==='reject') body.metadata.test_error=true;
 replies.push(await request('POST','/v1/messages',body));
 console.log(JSON.stringify({calls,afterContinuation,replies}));
} finally {close();}
