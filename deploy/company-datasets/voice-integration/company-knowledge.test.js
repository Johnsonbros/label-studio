'use strict';
const test=require('node:test');const assert=require('node:assert/strict');
const {createKnowledgeLookup}=require('./company-knowledge');
test('filters stale, revoked and irrelevant matches; only returns scoped public facts',async()=>{
 const fact={id:'public',text:'Company fact',source:'https://example.com',approved:1,expires:Date.now()/1000+60,similarity:.8};
 const lookup=createKnowledgeLookup({url:'http://internal',token:'test',fetchImpl:async(url,options)=>{
  assert.equal(options.headers.Authorization,'Bearer test');assert.equal(JSON.parse(options.body).limit,3);
  return {ok:true,json:async()=>({scope:'approved_public_company_facts',results:[fact,{...fact,approved:0},{...fact,expires:0},{...fact,similarity:.1}]})};
 }});
 const result=await lookup({query:'Hours'});assert.equal(result.facts.length,1);assert.equal(result.facts[0].fact_id,'public');
});
test('fails closed on backend errors and wrong scope',async()=>{
 for(const fetchImpl of [async()=>{throw Error('private detail')},async()=>({ok:false}),async()=>({ok:true,json:async()=>({scope:'customer',results:[]})})]){
  const result=await createKnowledgeLookup({url:'http://internal',token:'test',fetchImpl})({query:'Hours'});
  assert.equal(result.success,false);assert.ok(!JSON.stringify(result).includes('private detail'));
 }
});
test('rejects invalid queries without accessing backend',async()=>{
 const lookup=createKnowledgeLookup({url:'http://internal',token:'test',fetchImpl:async()=>assert.fail('Should not fetch')});
 for(const query of ['',null,'x'.repeat(501)])assert.equal((await lookup({query})).success,false);
});
