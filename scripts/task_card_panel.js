// Human-owned task plans. Polling never replaces an unsaved edit or another run.
let taskCardView=null,taskCardRun='',taskCardDirty=false,taskCardSaving=false;
stages.queued='等待执行';actorNames.user='用户';
Object.assign(eventNames,{task_card_created:'任务卡已创建',task_card_updated:'任务卡已更新',task_card_input_prepared:'已准备当前任务卡输入',task_card_input_finished:'任务卡调用记录已更新'});
const taskCardStates={pending:'未开始',in_progress:'进行中',blocked:'受阻',done:'已完成（人工记录）'};
const taskCardInputStates={prepared:'已准备；是否发出尚未确认',not_dispatched:'本次未发出',outcome_unknown:'已尝试调用，响应未知',response_received:'已有模型响应；不代表已经遵循'};
function taskCardCreation(){
  if($('mode').value!=='live'||!$('taskCardEnabled').checked)return undefined;
  const titles=$('taskCardDraftSteps').value.split('\n').map(s=>s.trim()).filter(Boolean);
  return {schema_version:1,goal:$('taskCardDraftGoal').value.trim(),constraints:$('taskCardDraftConstraints').value.trim(),
    blocker:'',next_action:'',instruction:'',steps:titles.map((title,i)=>({id:'step-'+(i+1),title,status:'pending'}))};
}
function taskCardChoices(){
  const live=$('mode').value==='live';$('taskCardEnabled').disabled=!live;
  $('taskCardDraftBox').hidden=!live||!$('taskCardEnabled').checked;
}
function resetTaskCard(){
  taskCardView=null;taskCardRun='';taskCardDirty=false;taskCardSaving=false;
  $('taskCardSection').hidden=true;$('taskCardSteps').replaceChildren();
  $('taskCardDelivery').replaceChildren();$('taskCardAudit').textContent='';$('taskCardNotice').textContent='';
  for(const id of ['taskCardGoal','taskCardConstraints','taskCardBlocker','taskCardNext','taskCardInstruction'])$(id).value='';
  for(const id of ['taskCardSave','taskCardDiscard','taskCardAddStep'])$(id).disabled=true;
}
function taskCardMarkDirty(){
  if(!taskCardView?.can_edit)return;taskCardDirty=true;
  $('taskCardNotice').textContent='有未保存修改。保存后在后续模型调用读取；不打断正在进行的请求。';
}
function taskCardStep(step,editable){
  const row=node('div','','task-card-step');row.dataset.stepId=step.id;
  const title=document.createElement('input');title.maxLength=100;title.value=step.title;title.disabled=!editable;title.setAttribute('aria-label','计划步骤名称');
  const status=document.createElement('select');status.setAttribute('aria-label','步骤状态');status.disabled=!editable;
  for(const [value,label] of Object.entries(taskCardStates)){const option=node('option',label);option.value=value;status.append(option)}status.value=step.status;
  const remove=node('button','移除此步骤');remove.disabled=!editable;
  remove.onclick=()=>{row.remove();taskCardMarkDirty()};title.oninput=taskCardMarkDirty;status.onchange=taskCardMarkDirty;
  row.append(title,status,remove);return row;
}
function renderTaskCard(view,runId){
  taskCardView=view;taskCardRun=runId;taskCardDirty=false;$('taskCardSection').hidden=false;
  $('taskCardEditor').hidden=!view.enabled;$('taskCardNotice').textContent=view.note;
  if(!view.enabled)return;
  text($('taskCardVersion'),'任务卡第 '+view.revision+' 版 · '+(view.can_edit?'可修改':'只读')+' · 实际运行：'+(flowNames[view.runtime_status]||view.runtime_status)+' / '+(stages[view.runtime_stage]||view.runtime_stage));
  const fields={taskCardGoal:'goal',taskCardConstraints:'constraints',taskCardBlocker:'blocker',taskCardNext:'next_action',taskCardInstruction:'instruction'};
  for(const [id,key] of Object.entries(fields)){$(id).value=view.card[key];$(id).disabled=!view.can_edit}
  $('taskCardSteps').replaceChildren(...view.card.steps.map(step=>taskCardStep(step,view.can_edit)));
  for(const id of ['taskCardSave','taskCardDiscard','taskCardAddStep'])$(id).disabled=!view.can_edit;
  const inputs=view.inputs.filter(input=>input.revision===view.revision);
  const receipts=inputs.length?inputs.map(input=>node('p',(roleNames[input.phase]||input.phase)+' · 第'+input.revision+'版 · '+(taskCardInputStates[input.status]||input.status),'hint')):[node('p','当前版本尚无调用记录；任务仍需处于执行阶段才会被读取。','hint')];
  $('taskCardDelivery').replaceChildren(...receipts);
  text($('taskCardAudit'),JSON.stringify({versions:view.versions,inputs:view.inputs},null,2));
}
async function refreshTaskCard(row,revision){
  const runId=row.id;if(taskCardDirty&&taskCardRun===runId)return;
  const view=await api('/api/runs/'+encodeURIComponent(runId)+'/task-card');
  if(selected!==runId||selectionRevision!==revision||taskCardDirty&&taskCardRun===runId)return;
  renderTaskCard(view,runId);
}
for(const id of ['taskCardGoal','taskCardConstraints','taskCardBlocker','taskCardNext','taskCardInstruction'])$(id).oninput=taskCardMarkDirty;
$('taskCardEnabled').onchange=taskCardChoices;
$('taskCardAddStep').onclick=()=>{
  if(!taskCardView?.can_edit)return;
  if($('taskCardSteps').children.length>=8){$('taskCardNotice').textContent='最多保留8个步骤。';return}
  $('taskCardSteps').append(taskCardStep({id:'step-'+crypto.randomUUID().slice(0,8),title:'',status:'pending'},true));taskCardMarkDirty();
};
$('taskCardDiscard').onclick=async()=>{
  const runId=selected,revision=selectionRevision;taskCardDirty=false;
  try{await refreshTaskCard(items.find(row=>row.id===runId),revision)}catch(error){notice(error)}
};
$('taskCardSave').onclick=async()=>{
  if(taskCardSaving||!taskCardView?.can_edit||taskCardRun!==selected)return;
  const runId=selected,revision=selectionRevision;
  const card={schema_version:1,goal:$('taskCardGoal').value,constraints:$('taskCardConstraints').value,blocker:$('taskCardBlocker').value,
    next_action:$('taskCardNext').value,instruction:$('taskCardInstruction').value,
    steps:Array.from($('taskCardSteps').children,row=>({id:row.dataset.stepId,title:row.querySelector('input').value,status:row.querySelector('select').value}))};
  taskCardSaving=true;$('taskCardSave').disabled=true;
  try{
    const view=await api('/api/runs/'+encodeURIComponent(runId)+'/task-card',{revision:taskCardView.revision,card});
    if(selected!==runId||selectionRevision!==revision)return;
    renderTaskCard(view,runId);$('taskCardNotice').textContent='已保存第'+view.revision+'版；后续调用读取最新整张卡，不会重启、补发或增加一轮模型调用。';
  }catch(error){if(selected===runId&&selectionRevision===revision)$('taskCardNotice').textContent=error.message}
  finally{taskCardSaving=false;if(selected===runId&&selectionRevision===revision)$('taskCardSave').disabled=!taskCardView?.can_edit}
};
taskCardChoices();
