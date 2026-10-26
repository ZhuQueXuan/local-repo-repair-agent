// Explicit new continuation identities. No background restart or retry loop.
let checkpointRun='',checkpointValue=null,checkpointBusy=false;
flowNames.paused='已暂停';flowNames.preparing_continuation='续做准备中';stages.paused='安全阶段边界';
Object.assign(eventNames,{pause_requested:'已请求阶段暂停',checkpoint_saved:'检查点已保存',checkpoint_imported:'已导入来源检查点'});
function checkpointChoices(){
  const eligible=$('mode').value==='live'&&$('taskCardEnabled').checked&&['dual_review','document_review'].includes($('collaborationMode').value);
  $('allowPause').disabled=!eligible;if(!eligible)$('allowPause').checked=false;
}
function resetCheckpoint(){checkpointRun='';checkpointValue=null;$('checkpointSection').hidden=true;$('checkpointPause').disabled=true;$('checkpointContinue').disabled=true;$('checkpointNotice').textContent='';}
async function refreshCheckpoint(row,revision){
  const view=await api('/api/runs/'+encodeURIComponent(row.id)+'/checkpoint');
  if(selected!==row.id||selectionRevision!==revision)return;
  checkpointRun=row.id;checkpointValue=view;
  $('checkpointSection').hidden=!view.enabled&&!view.continued_from;
  $('checkpointPause').disabled=!view.can_pause||view.pause_requested||checkpointBusy;
  $('checkpointContinue').disabled=!view.can_continue||checkpointBusy;
  $('checkpointContinueCard').hidden=!view.can_continue;
  text($('checkpointStatus'),view.pause_requested?'已请求暂停：等待当前完整角色结束后保存。':row.status==='paused'?'已暂停；下一阶段：'+(roleNames[view.next_phase]||view.next_phase)+'；剩余运行时间 '+view.remaining_seconds+' 秒。':view.note);
  text($('checkpointLineage'),(view.continued_from?'来源任务：'+view.continued_from+'\n':'')+(view.child_run_id?'已有续做任务：'+view.child_run_id+'\n':'')+'各段费用分别保存，链路累计见全角色用量。继承阶段不是新执行成绩。');
  const chain=window.latestCollaboration?.usage?.lineage;
  if(chain&&row.continued_from&&window.latestCollaboration?.state?.run_id===row.id){
    $('collaborationUsage').append(stat('含前段的累计平台尝试',chain.attempts+' 次 / '+chain.segments+' 段'),
      stat('链路累计已知估算',chain.known_price_estimate_cny+' 元'),
      stat('链路完整费用估算',chain.complete_cost_estimate_cny==null?'未知':chain.complete_cost_estimate_cny+' 元'));
  }
  if(view.can_continue&&!$('checkpointContinueCard').dataset.loadedFor){
    const card=await api('/api/runs/'+encodeURIComponent(row.id)+'/task-card');
    if(selected!==row.id||selectionRevision!==revision)return;
    $('checkpointInstruction').value=card.card.instruction;
    $('checkpointContinueCard').dataset.loadedFor=row.id;
  }
}
$('allowPause').onchange=checkpointChoices;
const previousTaskCardChoices=taskCardChoices;taskCardChoices=function(){previousTaskCardChoices();checkpointChoices();};
$('taskCardEnabled').addEventListener('change',checkpointChoices);
$('collaborationMode').onchange=()=>{collaborationChoices();checkpointChoices();};
$('checkpointPause').onclick=async()=>{
  if(checkpointBusy||checkpointRun!==selected)return;
  const id=selected,rev=selectionRevision;checkpointBusy=true;
  try{await api('/api/runs/'+id+'/pause',{});if(selected===id&&selectionRevision===rev)await refresh()}
  catch(error){notice(error)}finally{checkpointBusy=false}
};
$('checkpointContinue').onclick=async()=>{
  if(checkpointBusy||!checkpointValue?.can_continue||checkpointRun!==selected)return;
  const id=selected,rev=selectionRevision,view=checkpointValue;checkpointBusy=true;
  try{
    const current=await api('/api/runs/'+id+'/task-card');
    if(selected!==id||selectionRevision!==rev)return;
    const card={...current.card,instruction:$('checkpointInstruction').value};
    const signature=JSON.stringify({id,sha256:view.checkpoint_sha256,card});
    let pending=JSON.parse(sessionStorage.getItem('pendingContinuation')||'null');
    if(pending&&pending.signature!==signature)throw Error('上次续做创建结果尚未确认，请恢复原要求并重试；不创建第二个身份。');
    if(!pending){pending={signature,key:crypto.randomUUID()};sessionStorage.setItem('pendingContinuation',JSON.stringify(pending))}
    const child=await api('/api/runs/'+id+'/continue',{request_key:pending.key,checkpoint_sha256:view.checkpoint_sha256,task_card:card});
    sessionStorage.removeItem('pendingContinuation');
    if(selected===id&&selectionRevision===rev)select(child.id);
  }catch(error){if(selected===id&&selectionRevision===rev)text($('checkpointNotice'),error.message)}
  finally{checkpointBusy=false}
};
checkpointChoices();
