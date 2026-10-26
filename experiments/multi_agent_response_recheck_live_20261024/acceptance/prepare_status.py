from pathlib import Path
p=Path('temp/multi_agent_feedback_recheck_20261024/status.py')
s=p.read_text(encoding='utf-8-sig').replace('temp/multi_agent_feedback_recheck_20261024','temp/multi_agent_response_recheck_20261024')
Path('temp/multi_agent_response_recheck_20261024/status.py').write_text(s,encoding='utf-8')
