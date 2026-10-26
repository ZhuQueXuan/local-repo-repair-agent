import re,json,urllib.request
base='http://127.0.0.1:8765'
try:
    with urllib.request.urlopen(base+'/',timeout=5) as r:page=r.read().decode()
except OSError:
    print('Panel is already stopped.')
else:
    token=re.search(r"const token='([^']+)'",page).group(1)
    req=urllib.request.Request(base+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json','Origin':base,'X-Task-Token':token},method='POST')
    with urllib.request.urlopen(req,timeout=5) as r:print(r.read().decode())
