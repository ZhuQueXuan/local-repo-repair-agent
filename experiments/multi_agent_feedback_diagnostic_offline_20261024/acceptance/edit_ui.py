from pathlib import Path
root=Path.cwd()
p=root/'scripts/task_panel.html'
s=p.read_text(encoding='utf-8')
s=s.replace('<option value="dual_review">双 Agent 审查（实验）</option>', '<option value="dual_review">双 Agent 审查（实验）</option><option value="dual_review_diagnostic">反馈流程验收（人工缺陷）</option>')
p.write_text(s,encoding='utf-8',newline='\n')
p=root/'scripts/collaboration_panel.js'
s=p.read_text(encoding='utf-8').replace("const dual=selectMode.value==='dual_review';", "const diagnostic=selectMode.value==='dual_review_diagnostic', dual=['dual_review','dual_review_diagnostic'].includes(selectMode.value);\n  for(const option of $('task').options)option.disabled=diagnostic&&option.value!=='split-after-boundary';\n  if(diagnostic)$('task').value='split-after-boundary';")
s=s.replace("$('collaborationHint').textContent=dual?", "$('collaborationHint').textContent=diagnostic?'人工提供已知缺陷候选 → 模型原会话确认 → 独立审查 → 至多一次返工 → 新版核查。额外检查公开；这是接线诊断，不代表模型发现新问题或效果提升。总时限30分钟，全部角色计费。':dual?")
s=s.replace("(value.synthetic?'离线人工夹具 · 无模型效果\\n':'双角色真实运行\\n')", "(value.synthetic?'离线人工夹具 · 无模型效果\\n':value.mode==='dual_review_diagnostic'?'人工缺陷候选 · 真实模型反馈诊断（非效果比较）\\n':'双角色真实运行\\n')")
s=s.replace("roleNames[r.phase]+'：'", "(value.mode==='dual_review_diagnostic'&&r.phase==='repairing'?'人工候选确认':roleNames[r.phase])+'：'")
s=s.replace("roleNames[role.phase]+' · '", "(value.mode==='dual_review_diagnostic'&&role.phase==='repairing'?'人工候选确认':roleNames[role.phase])+' · '")
p.write_text(s,encoding='utf-8',newline='\n')
