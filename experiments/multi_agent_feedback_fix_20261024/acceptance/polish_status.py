from pathlib import Path
r=Path.cwd()
p=r/'README.md';s=p.read_text(encoding='utf-8').replace('2026-10-24最新：[真实反馈诊断]','2026-10-24真实诊断记录（修复前）：[真实反馈诊断]',1).replace('当前上游301、本项目280通过/1权限跳过。','当时上游301、本项目280通过/1权限跳过。',1);p.write_text(s,encoding='utf-8')
p=r.parent/'job/AGENTS.md';s=p.read_text(encoding='utf-8').replace('## 下一步\n','## 下一步\n\n2026-10-24后续：28中的已知程序缺口已修复并通过离线复现，新代码尚未接受真实全链路复验。后续真实验收使用新冻结身份，不恢复旧诊断或自动追加付费批次；之后再做意见裁决/S+和新题评价。当前Python边界不自动扩展。\n',1);p.write_text(s,encoding='utf-8')
