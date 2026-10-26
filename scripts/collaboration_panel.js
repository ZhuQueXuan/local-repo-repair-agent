// Read-only version/evidence views. All model text is rendered via textContent.
let collaborationCandidate='',candidateRevision=0,collaborationAvailable=false,candidateDataSignature='';
stages.consulting='查阅文档';
const roleNames={consulting:'文档查阅',repairing:'初修',reviewing:'独立首审',revising:'修订',final_review:'最终核查'};
function phaseName(value,role){return value.mode.startsWith('self_review_')?({repairing:'初修',reviewing:'自查（只读）',revising:'修订',final_review:'自查复核（只读）'}[role.phase]):value.mode==='dual_review_diagnostic'&&role.phase==='repairing'?'人工候选确认':roleNames[role.phase];}
const verdicts={approve:'审查通过',request_changes:'请求修订',insufficient_evidence:'证据不足',unavailable:'审查不可用',not_run:'尚未审查'};
function roleReason(role){const value=role.reason||role.result?.exit_reason||role.status;return ({Submitted:'正常提交',ReviewSubmitted:'已提交审查',pending:'未启动',running:'正在执行',succeeded:'已完成',skipped:'已跳过','Not required by this feedback branch':'本次分支无需启动','Cancelled before role start':'启动前已取消','Cancelled; no subsequent role or request is started.':'已取消，后续角色不再启动'})[value]||value;}
function collaborationChoices(){
  const offline=$('mode').value==='replay', selectMode=$('collaborationMode');
  selectMode.disabled=offline||!collaborationAvailable;
  if(offline)selectMode.value='single';
  const diagnostic=selectMode.value==='dual_review_diagnostic', selfCheck=selectMode.value==='self_review', dual=['dual_review','dual_review_diagnostic','self_review','document_review'].includes(selectMode.value);
  for(const option of $('task').options)option.disabled=diagnostic&&option.value!=='split-after-boundary';
  if(diagnostic)$('task').value='split-after-boundary';
  for(const option of $('profile').options)option.disabled=dual&&option.value!=='adaptive_history';
  if(dual)$('profile').value='adaptive_history';
  $('collaborationHint').textContent=diagnostic?'人工提供已知缺陷候选 → 模型原会话确认 → 独立审查 → 至多一次返工 → 新版核查。额外检查公开；这是接线诊断，不代表模型发现新问题或效果提升。总时限30分钟，全部角色计费。':dual?'修复 → 独立审查 → 至多一次修订 → 必要的最终核查。总时限30分钟；全部角色均计费，北邮费用可能未知。当前仅支持上下文与任务状态策略。':'单 Agent 沿用原策略；离线重放不产生新的模型审查。';
  const documents=selectMode.value==='document_review';$('documentQuestionBox').hidden=!documents;
  if(documents)$('collaborationHint').textContent='文档查阅者先回答一个明确问题并提供原文 → 修复者 → 独立审查 → 至多一次返工/复核。文档说明不代替测试，缺资料明确返回未知；全部角色计费。这是实验功能，实际完成情况和失败原因按各次运行展示，尚无新题效果比较结论。';
  if(selfCheck)$('collaborationHint').textContent='S+：同一 Agent 保留修复和自查历史。初修 → 六个只读工具自查 → 至多一次修订 → 只读复核。时间与评分机会对齐双 Agent，实际用量分别记账。当前仅离线接线验收，尚无真实模型/新题效果证据。';
}
function resetCollaboration(){collaborationCandidate='';candidateDataSignature='';candidateRevision++;$('collaboration').hidden=true;$('candidateContent').textContent='请选择候选版本。';$('evidenceContent').textContent='点击证据查看原文；意见不是独立裁决。';$('candidateReviews').replaceChildren();}
async function showEvidence(runId,id,revision){
  let version=candidateRevision;
  try{let value=await api('/api/runs/'+runId+'/evidence/'+encodeURIComponent(id));
    if(selected!==runId||selectionRevision!==revision||candidateRevision!==version)return;
    text($('evidenceContent'),JSON.stringify(value,null,2));
  }catch(e){if(selected===runId&&selectionRevision===revision&&candidateRevision===version)text($('evidenceContent'),e.message);}
}
async function showAdjudication(runId,batchId,revision){
  const version=candidateRevision;
  try{const value=await api('/api/runs/'+runId+'/adjudications/'+encodeURIComponent(batchId)+'/result.json');
    if(selected!==runId||selectionRevision!==revision||candidateRevision!==version)return;
    text($('evidenceContent'),JSON.stringify(value,null,2));
  }catch(e){if(selected===runId&&selectionRevision===revision&&candidateRevision===version)text($('evidenceContent'),e.message);}
}
function adjudicationText(decision){
  const truth={confirmed:'已登记检查支持',refuted:'已登记检查反驳',unresolved:'无法判断'};
  const resolution={resolved_on_checked_cases:'对应具体用例已修复',not_resolved:'对应具体用例未修复',not_a_confirmed_defect:'未证实为缺陷',unresolved:'修复情况未知'};
  const impact={harmful_on_checked_cases:'出现已覆盖回归',improved_on_checked_cases:'已覆盖用例有改善',changed_without_checked_improvement:'有改动，未见已覆盖收益',unchanged:'版本未变',unresolved:'影响未知'};
  return '独立裁决：'+truth[decision.verdict]+'；'+resolution[decision.resolution]+'；'+impact[decision.impact]+
    '\n原意见版本 '+decision.reviewed_candidate_id+' → 检查目标 '+decision.target_candidate_id+
    '\n覆盖：'+decision.coverage_scope+'\n检查：'+(decision.check_ids.join('、')||'无精确映射')+
    (decision.prior_visibility==='already_reported'?'\n这些用例此前已公开，不计为新增发现。':'')+
    '\n说明：'+decision.note;
}
async function showCandidate(runId,id,revision){
  collaborationCandidate=id;const version=++candidateRevision;
  $('candidateReviews').replaceChildren();
  text($('candidateContent'),'正在核验并加载所选版本…');text($('evidenceContent'),'本次候选已切换，请重新选择证据。');
  try{const value=await api('/api/runs/'+runId+'/candidates/'+encodeURIComponent(id));
    if(selected!==runId||selectionRevision!==revision||candidateRevision!==version)return;
    text($('candidateContent'),(value.is_final?'最终采用版本\n':'历史/待定版本\n')+id+'\n'+value.note+'\n\n'+value.diff+'\n\n此版本独立报告：\n'+JSON.stringify(value.reports,null,2));
    $('candidateReviews').replaceChildren();
    for(const review of value.reviews){const card=node('div','','stat');card.append(node('b',verdicts[review.verdict]||review.verdict),node('p','版本 '+review.candidate_id,'hint'));
      for(const finding of review.findings){card.append(node('p',finding.severity+' · '+finding.claim));
        const responses=(window.latestCollaboration?.responses||[]).filter(r=>r.review_id===review.review_id&&r.finding_id===finding.finding_id);
        card.append(node('p','修复者响应：'+(responses.length?responses.map(r=>r.disposition+' · '+r.reason+' · 目标版本 '+r.target_candidate_id).join('\n'):'尚无'),'hint'));
        const decisions=(value.adjudications||[]).filter(d=>d.review_id===review.review_id&&d.finding_id===finding.finding_id);
        if(!decisions.length)card.append(node('p','独立裁决：未核实，尚无可用的登记检查结果。','hint'));
        for(const decision of decisions){card.append(node('pre',adjudicationText(decision)));
          const b=node('button','查看裁决检查凭证');b.onclick=()=>showAdjudication(runId,decision.batch_id,revision);card.append(b);}
        for(const ref of finding.evidence_ids){const b=node('button','证据 '+ref);b.onclick=()=>showEvidence(runId,ref,revision);card.append(b);}}
      if(review.unknowns.length)card.append(node('pre','未知：'+review.unknowns.join('\n')));
      $('candidateReviews').append(card);}
    if(!value.reviews.length)$('candidateReviews').append(node('p','此版本尚无审查，不套用其他版本意见。','hint'));
  }catch(e){if(selected===runId&&selectionRevision===revision&&candidateRevision===version)text($('candidateContent'),'此候选不可用：'+e.message);}
}
async function refreshCollaboration(row,revision){
  if(!row.kind.startsWith('collaboration_')){resetCollaboration();return;}
  const value=await api('/api/runs/'+row.id+'/collaboration');
  if(selected!==row.id||selectionRevision!==revision)return;
  window.latestCollaboration=value;$('collaboration').hidden=false;
  text($('exit'),value.roles.filter(r=>r.started).map(r=>phaseName(value,r)+'：'+roleReason(r)).join('；')||'阶段尚未启动');
  text($('collaborationStatus'),(value.synthetic?'离线人工夹具 · 无模型效果\n':value.mode==='dual_review_diagnostic'?'人工缺陷候选 · 真实模型反馈诊断（非效果比较）\n':'双角色真实运行\n')+'协作：'+value.collaboration_result+'；'+(verdicts[value.review_verdict]||value.review_verdict)+'\n最终候选：'+(value.final_candidate_id||'尚无最终验收版本')+(row.cancel_requested?'\n已请求取消；等待当前请求返回或命令收尾后停止。':'')+(row.status==='interrupted'?'\n执行中断，已保留证据，不自动续跑。':''));
  if(value.mode.startsWith('self_review_'))text($('collaborationStatus'),'S+同一 Agent 分阶段自查 · 同一会话、分阶段权限/费用；不是独立审查。\n'+$('collaborationStatus').textContent.replace('双角色真实运行\n',''));
  if(value.mode.startsWith('document_review_'))text($('collaborationStatus'),'三角色：文档查阅 → 修复 → 审查（实验）；完成情况见各角色状态。\n'+$('collaborationStatus').textContent.replace('双角色真实运行\n',''));
  if(row.continued_from)text($('collaborationStatus'),$('collaborationStatus').textContent+'\n关联续做：已完成阶段来自 '+row.continued_from+'，并非本段重新执行。');
  text($('consultationResults'),value.consultations?.length?JSON.stringify(value.consultations,null,2):'尚无文档查阅结果；不补造依据。');
  if(value.adjudications?.length){const all=value.adjudications.flatMap(b=>b.decisions);
    text($('collaborationStatus'),$('collaborationStatus').textContent+'\n追加裁决 '+value.adjudications.length+' 批：具体检查支持 '+all.filter(d=>d.verdict==='confirmed').length+' 条，反驳 '+all.filter(d=>d.verdict==='refuted').length+' 条，无法判断 '+all.filter(d=>d.verdict==='unresolved').length+' 条。仅限已登记用例；切换历史候选查看该版本的意见。');}
  $('roles').replaceChildren();const usage=value.usage;
  for(const role of value.roles){const u=usage.by_role?.[role.role_run_id]||(usage.available?{attempts:0,known_price_estimate_cny:0,complete_cost_estimate_cny:0}:null);const card=node('div','','stat');
    if(role.inherited_from)card.append(node('p','继承来源 '+role.inherited_from+'；原阶段凭证保留，不计为新模型成功。','hint'));
    card.append(node('b',phaseName(value,role)+' · '+(flowNames[role.status]||({succeeded:'已完成',skipped:'已跳过',pending:'未启动'}[role.status])||role.status)),
      node('p',roleReason(role),'hint'),
      node('p','活动 '+(role.elapsed_seconds??'未知')+' 秒 · 调用 '+(u?.attempts??'未知')+' 次','hint'),
      node('p','已知估算 '+(u?.known_price_estimate_cny??'未知')+' 元；完整费用 '+(u?.complete_cost_estimate_cny??'未知'),'hint'));
    const details=node('details');details.append(node('summary','时间与版本明细'),node('pre',JSON.stringify({开始:role.started??'未知/未启动',结束:role.ended??'未知/未结束',角色:role.role_run_id,候选:role.result?.candidate_id??'见交接包'},null,2)));card.append(details);
    $('roles').append(card);}
  $('collaborationUsage').replaceChildren();
  if(usage.available){$('collaborationUsage').append(stat('全角色逻辑请求 / 平台尝试',usage.logical_requests+' / '+usage.attempts),stat('已知输入 / 输出 token',usage.known_prompt_tokens+' / '+usage.known_completion_tokens),stat('已知价格估算',usage.known_price_estimate_cny+' 元'),stat('完整费用估算',usage.complete_cost_estimate_cny==null?'未知':usage.complete_cost_estimate_cny+' 元'),stat('未知占位 · 不是实际收费',usage.unknown_placeholder_cny+' 元'),stat(value.elapsed_scope,value.total_elapsed_seconds??'未知'));
    text($('providerUsage'),(value.synthetic?'以下用量为人工合成，非API消耗。\n':'估算不是平台扣费或余额；已知token不含未知请求。\n')+JSON.stringify(usage.providers,null,2));
  }else text($('providerUsage'),usage.reason||'用量未知，不能按零展示。');
  const selectVersion=$('candidateVersion'), oldSignature=selectVersion.dataset.signature;
  const signature=JSON.stringify(value.candidates.map(c=>c.candidate_id));
  if(signature!==oldSignature){selectVersion.replaceChildren();value.candidates.forEach((c,i)=>{const o=node('option','v'+(i+1)+' · '+c.candidate_id);o.value=c.candidate_id;selectVersion.append(o)});selectVersion.dataset.signature=signature;}
  const chosen=value.candidates.some(c=>c.candidate_id===collaborationCandidate)?collaborationCandidate:value.final_candidate_id||value.candidates.at(-1)?.candidate_id;
  const dataSignature=JSON.stringify([value.reports,value.reviews,value.responses,value.final_candidate_id,value.adjudications]);
  if(chosen){selectVersion.value=chosen;if(chosen!==collaborationCandidate||dataSignature!==candidateDataSignature){candidateDataSignature=dataSignature;await showCandidate(row.id,chosen,revision);}}
  else text($('candidateContent'),'候选尚未封存；不能展示其他运行的补丁。');
  text($('handoffs'),JSON.stringify({文档查阅:value.consultations,交接:value.handoffs,证据索引:value.evidences,响应:value.responses,截断:value.truncated_kinds||[],候选列表截断:value.candidates_truncated},null,2));
}
$('collaborationMode').onchange=collaborationChoices;
$('candidateVersion').onchange=()=>showCandidate(selected,$('candidateVersion').value,selectionRevision);
$('roleFilter').onchange=()=>refresh().catch(notice);
