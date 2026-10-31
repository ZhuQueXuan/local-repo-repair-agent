// Goal-based repair and reference records on the existing local workbench.
let currentWorkflowRow=null, repairPending=false, runtimeTimer=null;
function clearWorkflowSelection(){currentWorkflowRow=null;byId('repairControls').hidden=true;byId('repairAck').checked=false;byId('repairGoal').value=''}
function analysisReferenceFields(){const mode=byId('referenceMode').value;return {reference_mode:mode,...(mode==='web'?{reference_ack:byId('ack').checked}:{})}}
function renderReferenceConsultations(records,parent){
 if(!records?.length)return;
 parent.append(make('h3','资料助手查阅记录'));
 for(const record of records){const section=make('div','','finding');section.append(make('p',record.question),make('p',record.answer),make('p',record.note,'hint'));
  for(const unknown of record.unknowns||[])section.append(make('p','尚不确定：'+unknown,'hint'));
  for(const source of record.sources||[]){if(!record.source_refs.includes(source.source_ref))continue;
   if(source.url&&source.url.startsWith('https://')){const link=make('a',source.url);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';section.append(link)}
   else section.append(make('p',source.path||'所选项目资料'));
   section.append(make('pre',source.excerpt));
  }parent.append(section);
 }
}
function renderSourceRepair(report){
 byId('reportSection').hidden=false;byId('evidencePreview').textContent='本次修改只在副本中进行；请选择代码副本或修改对照下载。';
 byId('reportTitle').textContent=report.repair_result==='checks_passed_pending_user'?'修复结果 · 待你最终验收':'修复未完成 · 已有成果保留';
 byId('coverage').textContent=report.note;
 const box=byId('report');box.replaceChildren();box.append(make('h3','这次要做到什么'),make('p',report.goal),make('p',report.reason));
 if(['offline_substitute','actual_runtime_synthetic_models'].includes(report.validation_scope))box.append(make('p',report.validation_scope==='actual_runtime_synthetic_models'?'程序检查实际运行，模型回复是替身；不能当作真实模型能力验收。':'离线替身演示，不能当作真实运行或模型能力验收。','notice'));
 box.append(make('h3','完成条件'));for(const value of report.criteria)box.append(make('p',value));
 for(const item of report.rounds){const section=make('div','','finding');section.append(make('h3','第 '+item.round+' 轮'));
  section.append(make('p',item.checks?(item.checks.passed?'运行检查通过':'运行检查没有全部通过'):'运行检查未完成'));
  for(const check of item.checks?.checks||[])section.append(make('p',(check.language==='python'?'Python':'JavaScript')+'：'+(check.report?.tests??0)+'项检查，失败 '+(check.report?.failures??0)+'，错误 '+(check.report?.errors??0)+'，跳过 '+(check.report?.skipped??0)));
  section.append(make('p',item.review?item.review.reason:'本轮没有获得完整复核意见。'));
  for(const problem of item.review?.blockers||[])section.append(make('p','仍需处理：'+problem));
  for(const unknown of item.review?.unknowns||[])section.append(make('p','尚未验证：'+unknown,'hint'));
  for(const limitation of item.review?.limitations||[])section.append(make('p','适用范围：'+limitation,'hint'));
  box.append(section);
 }
 for(const snapshot of report.snapshots){const line=make('div','','row');line.append(make('span',(snapshot.round?'第'+snapshot.round+'轮':'原始')+'代码副本 · 修改 '+snapshot.changed.length+' 个文件'));
  for(const [label,name]of [['下载代码副本',snapshot.archive],['下载修改对照',snapshot.diff]]){const button=make('button',label);const id=selectedRun;button.onclick=()=>download(id,name);line.append(button)}box.append(line);
 }
 for(const limitation of report.acceptance_limitations||[])box.append(make('p','检查范围：'+limitation,'hint'));
 renderReferenceConsultations(report.reference_consultations,box);
}
function updateWorkflowControls(row){
 const previous=currentWorkflowRow?.id;currentWorkflowRow=row;
 byId('repairControls').hidden=row.kind!=='analysis'||!['finished','failed','cancelled','interrupted'].includes(row.status)||!row.artifacts?.['report.json']&&!row.artifacts?.['draft-report.json'];
 if(previous!==row.id){byId('repairGoal').value=row.kind==='analysis'?(row.focus||''):'';byId('repairAck').checked=false;notice('repairNotice','')}
 if(row.kind==='source_repair')byId('runRoleInfo').textContent='围绕整体目标：修改 → 运行检查 → 独立复核，需要时查资料。最多3轮、修复累计15分钟，连续2轮无实质改善停止；不按模型调用总次数或固定费用截断，结果由你最终验收。';
 byId('roleMetrics').replaceChildren();
 for(const [role,value]of Object.entries(row.analysis_roles||{}))byId('roleMetrics').append(make('p',({analyst:'分析者',reviewer:'核对者',repairer:'修改者',reference:'资料助手'}[role]||role)+' · '+({submitted:'已提交',consulted:'已查阅',running:'进行中',yielded_to_checks:'停止空转，程序接手检查',not_submitted:'未完成',failed:'停止',interrupted:'中断',cancelled:'取消'}[value.status]||value.status)+' · 平台尝试 '+(value.metrics?.requests??0)));
}
async function runtimeProgress(){try{const value=await request('/api/source-repair-runtime');notice('repairRuntimeNotice',value.notice);if(value.status==='preparing')runtimeTimer=setTimeout(runtimeProgress,2000)}catch(error){notice('repairRuntimeNotice',error)}}
byId('inspectRepairRuntime').onclick=async()=>{notice('repairRuntimeNotice','正在检查隔离运行环境，不调用模型…');try{const value=await request('/api/source-repair-runtime/inspect',{});notice('repairRuntimeNotice',value.notice+' '+value.limits)}catch(error){notice('repairRuntimeNotice',error)}};
byId('prepareRepairRuntime').onclick=async()=>{try{clearTimeout(runtimeTimer);const value=await request('/api/source-repair-runtime/prepare',{});notice('repairRuntimeNotice',value.notice);runtimeTimer=setTimeout(runtimeProgress,2000)}catch(error){notice('repairRuntimeNotice',error)}};
byId('startSourceRepair').onclick=async()=>{
 if(repairPending||currentWorkflowRow?.kind!=='analysis')return;
 const goal=byId('repairGoal').value.trim();if(!goal){notice('repairNotice','请说明修好后希望达到什么效果。');return}
 if(!byId('repairAck').checked){notice('repairNotice','请确认修改副本、模型调用及隔离运行许可。');return}
 const body={analysis_id:currentWorkflowRow.id,goal,external_processing_ack:true,execution_ack:true};
 const signature=JSON.stringify(body);let pending;try{pending=JSON.parse(sessionStorage.getItem('pendingSourceRepair')||'null')}catch{}
 if(pending&&pending.signature!==signature){notice('repairNotice','上次创建结果尚不明确，请保留原目标重试或先查看历史，避免重复创建。');return}
 const key=pending?.key||crypto.randomUUID();sessionStorage.setItem('pendingSourceRepair',JSON.stringify({signature,key}));
 repairPending=true;byId('startSourceRepair').disabled=true;notice('repairNotice','正在检查环境并创建修复任务…');
 try{const row=await request('/api/source-repair',{...body,request_key:key});sessionStorage.removeItem('pendingSourceRepair');selectRun(row.id)}
 catch(error){if(error.definite)sessionStorage.removeItem('pendingSourceRepair');notice('repairNotice',error)}
 finally{repairPending=false;byId('startSourceRepair').disabled=false}
};
