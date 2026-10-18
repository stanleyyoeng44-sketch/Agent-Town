#!/usr/bin/env node
/* renderSummary + the assistant model label: headless tests.
 *
 * NO BROWSER AND NO DOCKER ON THIS BOX. The console is a Python string, so
 * the JS is extracted (ui/extract_page_js.py) and run under node with a tiny
 * DOM shim. This proves the render functions; it proves NOTHING about layout.
 *
 *   node ui/extract_page_js.py ui/control.py /tmp/x && \
 *   node ui/test_summary.js /tmp/x/console.js
 *
 * Each case below fails on the pre-change code, which is the only thing that
 * makes it a test rather than a demonstration.
 */
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// A REAL shim: my first version returned '' from every element, so esc()
// returned '' and every escaping assertion passed vacuously.
function mkEl(){ let _t='',_h='';
  return { style:{}, value:'', checked:true, scrollHeight:0, scrollTop:0, clientHeight:0,
    appendChild(){}, remove(){}, classList:{add(){},remove(){}}, onclick:null,
    insertAdjacentHTML(){}, addEventListener(){}, removeEventListener(){},
    get textContent(){return _t;}, set textContent(v){_t=String(v);
      _h=_t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');},
    get innerHTML(){return _h;}, set innerHTML(v){_h=String(v);} }; }
global.document={querySelector:()=>mkEl(),querySelectorAll:()=>[],createElement:()=>mkEl(),
  getElementById:()=>null,body:mkEl()};
global.localStorage={getItem:()=>'',setItem:()=>{}};
global.fetch=async()=>({json:async()=>({}),status:200});
global.setInterval=()=>0; global.window=global;

const E={}; new Function('module','exports',src +
  '\n;module.exports.renderSummary = renderSummary;' +
  '\n;module.exports.renderEvents = renderEvents;')({exports:E},E);

let f=0,c=0;
const ok=(n,cond,d)=>{c++; if(!cond){f++;console.log('FAIL  '+n+(d?' :: '+d:''));}
                      else console.log('ok    '+n);};
const txt=h=>h.replace(/<[^>]*>/g,' ').replace(/\s+/g,' ').trim();

// ---- empty cohort: must name the state, not render a blank strip ----
ok('zero agents names the state', /no agents/.test(txt(E.renderSummary([],{}))));

// ---- eleven agents, nine up ----
const A=[]; for(let i=0;i<11;i++) A.push({name:'a'+i, status:i<9?'Up 2 hours':'Exited (0) 1 minute ago'});
const B={}; for(let i=0;i<11;i++) B['a'+i]=1000;
const t=txt(E.renderSummary(A,B));
ok('eleven agents: total present', /\b11\b/.test(t), t);
ok('eleven agents: nine up',       /\b9\b/.test(t), t);
ok('eleven agents: two asleep',    /\b2\b/.test(t), t);
ok('eleven agents: 11,000 coins',  /11,000/.test(t), t);

// ---- an unread balance is not a zero balance ----
const B2={a0:500};
const t2=txt(E.renderSummary(A,B2));
ok('unknown balances excluded from the total', /500/.test(t2) && !/5,500/.test(t2), t2);
ok('unknown balances are flagged with ?', /\?/.test(t2), t2);
const t3=txt(E.renderSummary(A,{}));
ok('all-unknown total says unknown, not 0', /unknown/.test(t3), t3);

// ---- a wholly dead cohort must not read as fine ----
const D=A.map(a=>({name:a.name,status:'Exited (0) 3 hours ago'}));
ok('all-down carries the warn class', /warn/.test(E.renderSummary(D,{})));

// ---- the docker-missing payload must render, not throw ----
ok('error payload does not throw', typeof E.renderSummary([{error:'no docker'}],{})==='string');
ok('error payload reads as no agents',
   /no agents/.test(txt(E.renderSummary([{error:'x'}],{}))));

// ---- the assistant header must not assert a model it did not read ----
const hdr = E.renderEvents([{kind:'assistant', text:'x', time:'t'}]);
ok('assistant header does not say glm-5.3', hdr.indexOf('glm-5.3') === -1, hdr.slice(0,120));
ok('assistant header falls back to a neutral label', /agent/.test(hdr));

console.log(`\n${f?'FAIL':'PASS'}  ${c-f}/${c} checks`);
process.exit(f?1:0);
