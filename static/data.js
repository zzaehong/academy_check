'use strict';
// CSV 내보내기·가져오기. 기존 API로 저장하므로 서버 검증이 그대로 적용된다.
const who=id=>{const s=state.students.find(s=>s.id===id);return [s.name,s.school,s.grade];};
const categoryName=id=>state.categories.find(c=>c.id===id)?.name??'';
const newest=(a,b)=>b.date.localeCompare(a.date)||b.id-a.id;
const PEOPLE=['이름','학교','학년'];
const CSV={
 students:{label:'원생',head:PEOPLE,need:['이름'],rows:()=>state.students.map(s=>[s.name,s.school,s.grade])},
 books:{label:'교재',head:[...PEOPLE,'교재명','상태'],need:['이름','교재명','상태'],rows:()=>state.books.map(b=>[...who(b.student_id),b.title,b.status])},
 notes:{label:'일지·상담',head:[...PEOPLE,'날짜','내용'],need:['이름','날짜','내용'],rows:()=>[...state.notes].sort(newest).map(n=>[...who(n.student_id),n.date,n.content])},
 scores:{label:'성적',head:[...PEOPLE,'카테고리','날짜','시험명','점수','난이도','객관식점수','객관식만점','서술형점수','서술형만점'],need:['이름','카테고리','날짜','시험명','점수'],
  rows:()=>[...state.scores].sort(newest).map(r=>[...who(r.student_id),categoryName(r.category_id),r.date,r.title,r.score,r.difficulty,r.objective_score,r.objective_max,r.written_score,r.written_max].map(v=>v??''))},
};
const cell=v=>/[",\r\n]/.test(String(v))?'"'+String(v).replaceAll('"','""')+'"':String(v);
// shortcut: 엑셀 수식 주입(=,+,-,@로 시작하는 값) 방어는 없음, 본인 데이터만 다루므로. 파일을 타인과 공유하게 되면 접두어 처리 필요.
function toCSV(head,rows){return '﻿'+[head,...rows].map(r=>r.map(cell).join(',')).join('\r\n')+'\r\n';}
function parseCSV(text){
 const rows=[];let row=[],field='',quoted=false;
 for(let i=0;i<text.length;i++){
  const c=text[i];
  if(quoted){if(c==='"'){if(text[i+1]==='"'){field+='"';i++;}else quoted=false;}else field+=c;}
  else if(c==='"')quoted=true;
  else if(c===',')row.push(field),field='';
  else if(c==='\n'||c==='\r'){if(c==='\r'&&text[i+1]==='\n')i++;row.push(field);field='';rows.push(row);row=[];}
  else field+=c;
 }
 if(field!==''||row.length){row.push(field);rows.push(row);}
 return rows.filter(r=>r.some(v=>v.trim()!==''));
}
function exportCSV(kind){
 const spec=CSV[kind];
 const url=URL.createObjectURL(new Blob([toCSV(spec.head,spec.rows())],{type:'text/csv;charset=utf-8'}));
 const link=Object.assign(document.createElement('a'),{href:url,download:`원생노트_${spec.label}_${today()}.csv`});
 link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
// 엑셀이 날짜를 2026/1/5처럼 바꿔 저장해도 받아 준다.
function normalizeDate(v){const m=/^(\d{4})[-./](\d{1,2})[-./](\d{1,2})/.exec(v);return m?`${m[1]}-${m[2].padStart(2,'0')}-${m[3].padStart(2,'0')}`:v;}
function decodeCSV(buffer){
 // 한국어 엑셀의 기본 "CSV" 저장은 UTF-8이 아닌 CP949(euc-kr)다.
 try{return new TextDecoder('utf-8',{fatal:true}).decode(buffer);}catch{return new TextDecoder('euc-kr').decode(buffer);}
}
async function runImport(text){
 const rows=parseCSV(text.replace(/^﻿/,'')),header=(rows.shift()||[]).map(h=>h.trim());
 const kind=header.includes('교재명')?'books':header.includes('내용')?'notes':header.includes('시험명')?'scores':'students';
 const spec=CSV[kind],missing=spec.need.filter(h=>!header.includes(h));
 if(missing.length)throw new Error(`첫 줄에 필요한 항목이 없습니다: ${missing.join(', ')}. 내보내기한 파일의 첫 줄을 그대로 유지해 주세요.`);
 await refresh();
 if(!confirm(`${spec.label} ${rows.length}건을 가져옵니다. 이미 있는 기록은 건너뛰고, 없는 원생은 새로 등록합니다. 계속할까요?`))return null;
 const get=(r,name)=>(r[header.indexOf(name)]??'').trim();
 const person=s=>[s.name,s.school,s.grade].join('|');
 const seen=new Set({students:[],books:state.books.map(b=>b.student_id+'|'+b.title),notes:state.notes.map(n=>n.student_id+'|'+n.date+'|'+n.content),scores:state.scores.map(r=>[r.student_id,r.category_id,r.date,r.title].join('|'))}[kind]);
 const result={added:0,skipped:0,errors:[]};
 for(const [i,r] of rows.entries()){
  try{
   const info={name:get(r,'이름'),school:get(r,'학교'),grade:get(r,'학년')};
   let student=state.students.find(s=>person(s)===person(info));
   if(kind==='students'){if(student){result.skipped++;continue;}await api('students','POST',info);result.added++;continue;}
   let payload,key;
   if(kind==='books'){payload={title:get(r,'교재명'),status:get(r,'상태')};key=payload.title;}
   if(kind==='notes'){payload={date:normalizeDate(get(r,'날짜')),content:get(r,'내용')};key=payload.date+'|'+payload.content;}
   if(kind==='scores'){
    const category=state.categories.find(c=>c.name===get(r,'카테고리'));
    if(!category)throw new Error(`없는 카테고리입니다: ${get(r,'카테고리')}`);
    const score=get(r,'점수'),parts=['객관식점수','객관식만점','서술형점수','서술형만점'].map(h=>get(r,h));
    payload={category_id:category.id,date:normalizeDate(get(r,'날짜')),title:get(r,'시험명'),difficulty:get(r,'난이도'),score:score===''?NaN:Number(score)};
    if(parts.some(v=>v!=='')){
     Object.assign(payload,{score_mode:'split',objective_score:Number(parts[0]||NaN),objective_max:Number(parts[1]||NaN),written_score:Number(parts[2]||NaN),written_max:Number(parts[3]||NaN)});
    }
    key=[category.id,payload.date,payload.title].join('|');
   }
   if(!student){student=await api('students','POST',info);state.students.push(student);}
   if(seen.has(student.id+'|'+key)){result.skipped++;continue;}
   await api(kind,'POST',{...payload,student_id:student.id});seen.add(student.id+'|'+key);result.added++;
  }catch(err){result.errors.push(`${i+2}행: ${err.message}`);}
 }
 return result;
}
function importCSV(){
 const input=Object.assign(document.createElement('input'),{type:'file',accept:'.csv,text/csv'});
 input.onchange=async()=>{
  try{
   const result=await runImport(decodeCSV(await input.files[0].arrayBuffer()));
   if(!result)return;
   await refresh();
   const errors=result.errors.slice(0,10).map(e=>`<li>${escapeHTML(e)}</li>`).join('');
   $('#modal').innerHTML=`<h2>가져오기 결과</h2><p><strong>${result.added}건 추가, ${result.skipped}건 건너뜀, ${result.errors.length}건 오류</strong></p>${errors?`<ul>${errors}</ul>${result.errors.length>10?`<p class="muted">외 ${result.errors.length-10}건</p>`:''}<p class="muted">오류 행은 저장되지 않았습니다. 파일을 고쳐 다시 가져오면 이미 저장된 행은 건너뜁니다.</p>`:''}<div class="actions"><button class="btn btn-primary primary" data-action="close">확인</button></div>`;
   $('#modal').showModal();
  }catch(err){notify(err.message);}
 };
 input.click();
}
