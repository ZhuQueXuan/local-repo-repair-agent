// Read-only version/evidence views. All model text is rendered via textContent.
let collaborationCandidate='',candidateRevision=0,collaborationAvailable=false,candidateDataSignature='';
const roleNames={repairing:'初修',reviewing:'独立首审',revising:'修订',final_review:'最终核查'};
const verdicts={approve:'审查通过',request_changes:'请求修订',insufficient_evidence:'证据不足',unavailable:'审查不可用',not_run:'尚未审查'};
function roleReason(role){const value=role.reason||role.result?.exit_reason||role.status;return ({Submitted:'正常提交',ReviewSubmitted:'已提交审查',pending:'未启动',running:'正在执行',succeeded:'已完成',skipped:'已跳过','Not required by this feedback branch':'本次分支无需启动','Cancelled before role start':'启动前已取消','Cancelled; no subsequent role or request is started.':'已取消，后续角色不再启动'})[value]||value;}
function collaborationChoices(){
  const offline=$('mode').value==='replay', selectMode=$('collaborationMode');
  selectMode.disabled=offline||!collaborationAvailable;
  if(offline)selectMode.value='single';
  const dual=selectMode.value==='dual_review';
  for(const option of $('profile').options)option.disabled=dual&&option.value!=='adaptive_history';
  if(dual)$('profile').value='adaptive_history';
  $('collaborationHint').textContent=dual?'修复 → 独立审查 → 至多一次修订 → 必要的最终核查。总时限30分钟；全部角色均计费，北邮费用可能未知。当前仅支持上下文与任务状态策略。':'单 Agent 沿用原策略；离线重放不产生新的模型审查。';
}
function resetCollaboration(){collaborationCandidate='';candidateDataSignature='';candidateRevision++;$('collaboration').hidden=true;$('candidateContent').textContent='请选择候选版本。';$('evidenceContent').textContent='点击证据查看原文；意见不是独立裁决。';$('candidateReviews').replaceChildren();}
async function showEvidence(runId,id,revision){
  let version=candidateRevision;
  try{let value=await api('/api/runs/'+runId+'/evidence/'+encodeURIComponent(id));
    if(selected!==runId||selectionRevision!==revision||candidateRevision!==version)return;
    text($('evidenceContent'),JSON.stringify(value,null,2));
  }catch(e){if(selected===runId&&selectionRevision===revision&&candidateRevision===version)text($('evidenceContent'),e.message);}
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
        card.append(node('p','独立裁决：未核实；'+(responses.length?responses.map(r=>r.disposition+' · '+r.reason+' · 目标版本 '+r.target_candidate_id).join('\n'):'尚无修复者响应'),'hint'));
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
  text($('exit'),value.roles.filter(r=>r.started).map(r=>roleNames[r.phase]+'：'+roleReason(r)).join('；')||'角色尚未启动');
  text($('collaborationStatus'),(value.synthetic?'离线人工夹具 · 无模型效果\n':'双角色真实运行\n')+'协作：'+value.collaboration_result+'；'+(verdicts[value.review_verdict]||value.review_verdict)+'\n最终候选：'+(value.final_candidate_id||'尚无最终验收版本')+(row.cancel_requested?'\n已请求取消；等待当前请求返回或命令收尾后停止。':'')+(row.status==='interrupted'?'\n执行中断，已保留证据，不自动续跑。':''));
  $('roles').replaceChildren();const usage=value.usage;
  for(const role of value.roles){const u=usage.by_role?.[role.role_run_id]||(usage.available?{attempts:0,known_price_estimate_cny:0,complete_cost_estimate_cny:0}:null);const card=node('div','','stat');
    card.append(node('b',roleNames[role.phase]+' · '+(flowNames[role.status]||({succeeded:'已完成',skipped:'已跳过',pending:'未启动'}[role.status])||role.status)),
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
  const dataSignature=JSON.stringify([value.reports,value.reviews,value.responses,value.final_candidate_id]);
  if(chosen){selectVersion.value=chosen;if(chosen!==collaborationCandidate||dataSignature!==candidateDataSignature){candidateDataSignature=dataSignature;await showCandidate(row.id,chosen,revision);}}
  else text($('candidateContent'),'候选尚未封存；不能展示其他运行的补丁。');
  text($('handoffs'),JSON.stringify({交接:value.handoffs,证据索引:value.evidences,响应:value.responses,截断:value.truncated_kinds||[],候选列表截断:value.candidates_truncated},null,2));
}
$('collaborationMode').onchange=collaborationChoices;
$('candidateVersion').onchange=()=>showCandidate(selected,$('candidateVersion').value,selectionRevision);
$('roleFilter').onchange=()=>refresh().catch(notice);
