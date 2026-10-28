"""Conservative literal source/archive reads shared by single and role agents.

No execution, shell expansion, repair answer or validation acceptance.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
from execution_progress import checkout_body

def canonical_bytes(value):return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
def sha256(raw):return hashlib.sha256(raw).hexdigest()

def read_plan(command, work, files, archives=None):
    archives=archives or {}
    body=checkout_body(command)
    if any(c in body for c in ('$', '`', '\n', '\r')):return None
    try:
        lexer=shlex.shlex(body,posix=True,punctuation_chars=';&|<>')
        lexer.whitespace_split=True;lexer.commenters=''
        parts=[];part=[];pipe=False
        for token in lexer:
            if token in {'&&',';','|'}:
                if not part:return None
                parts.append((part,pipe));part=[];pipe=token=='|'
            elif token in {'&','||','>','>>','<','<<','>&'}:return None
            else:part.append(token)
        if not part:return None
        parts.append((part,pipe))
    except ValueError:return None
    root=Path(work).resolve();spans=[];identity=[];dependencies={}

    def filename(name,*,directory=False):
        path=(root/name).resolve()
        if not path.is_relative_to(root):return None
        relative=path.relative_to(root).as_posix()
        if relative in files:
            dependencies[relative]=files[relative]
        elif relative in archives and path.is_file():
            if sha256(path.read_bytes())!=archives[relative]:return None
            dependencies[relative]=archives[relative]
        elif directory and path.is_dir() and any(n.startswith(relative+'/') for n in files):
            dependencies[relative]=sha256(canonical_bytes(files))
        else:return None
        return relative

    for tokens,piped in parts:
        if tokens[0]=='echo' and not piped:continue
        span=None
        if not piped and len(tokens)==4 and tokens[:2]==['sed','-n'] and re.fullmatch(r'\d+(,\d+)?p',tokens[2]):
            values=[int(x) for x in tokens[2][:-1].split(',')]
            name=filename(tokens[3]);start,end=values[0],values[-1]
            if name is None or start<1 or end<start:return None
            span=(name,start,end);tokens=['sed','-n',f'{start},{end}p',name]
        elif not piped and len(tokens)==2 and tokens[0]=='cat':
            name=filename(tokens[1])
            if name is None:return None
            span=(name,1,2**63-1);tokens=['cat',name]
        elif not piped and tokens[0]=='grep':
            offset=1
            while offset<len(tokens) and tokens[offset] in {'-n','-r','-rn','-nr','-i','-in','-E','-F','-nE'}:offset+=1
            if offset+1>=len(tokens) or tokens[offset].startswith('-'):return None
            names=[filename(n,directory=True) for n in tokens[offset+1:]]
            if not all(names):return None
            tokens=tokens[:offset+1]+names
        elif piped and tokens[0] in {'head','tail'} and (
                len(tokens)==2 and re.fullmatch(r'-\d+',tokens[1]) or
                len(tokens)==3 and tokens[1]=='-n' and tokens[2].isdigit()):pass
        elif not piped and len(tokens)==3 and tokens[:2]==['wc','-l']:
            name=filename(tokens[2])
            if name is None:return None
            tokens=tokens[:2]+[name]
        else:return None
        identity.append(tokens)
        if span:spans.append(span)
    if not identity:return None
    # A pipe filters its source; it cannot certify full source coverage.
    if any(piped for _,piped in parts):spans=[]
    key=sha256(canonical_bytes({'commands':identity,'inputs':dependencies}))
    return {'key':key,'spans':spans,'single_span':len(parts)==1 and len(identity)==1 and len(spans)==1}

