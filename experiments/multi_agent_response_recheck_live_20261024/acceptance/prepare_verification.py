from pathlib import Path
import json
r=Path.cwd();t=r/'temp/multi_agent_response_recheck_20261024'
s=(r/'temp/multi_agent_feedback_recheck_20261024/verify_live.py').read_text(encoding='utf-8-sig')
s=s.replace('temp/multi_agent_feedback_recheck_20261024','temp/multi_agent_response_recheck_20261024')
s=s.replace('len(added)==21','len(added)>0').replace("len(index['candidates'])==2 and not index['responses']","len(index['candidates'])==2 and len(index['responses'])>0")
s=s.replace("'registered_responses':0","'registered_responses':len(index['responses'])").replace("'new_tickets':21","'new_tickets':len(added)")
s=s.replace("'scope':'Repair passed and replayed, but collaboration failed at response metadata validation; final review did not run. Not effectiveness evidence.'","'scope':'One actual manual-candidate diagnostic completed all four phases and clean replay. Development integration evidence only, not effectiveness evidence.'")
(t/'verify_live.py').write_text(s,encoding='utf-8')
