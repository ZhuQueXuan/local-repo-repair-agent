from pathlib import Path
p=Path('temp/multi_agent_response_recheck_20261024/acceptance_details.py')
s=p.read_text(encoding='utf-8-sig').replace("reviews=json.loads((run/'reviews.json').read_text(encoding='utf-8'));", "reviews=sorted(json.loads((run/'reviews.json').read_text(encoding='utf-8')),key=lambda v:0 if v['review_id']=='reviewing-review' else 1);")
p.write_text(s,encoding='utf-8')
