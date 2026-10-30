"""Bounded source intake. Uploaded code is data and is never imported or run."""
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from collaboration_protocol import canonical_bytes, relative_path
from collaboration_panel import checked_bytes
from task_service_store import now

MAX_UPLOAD = 20 * 1024 * 1024
MAX_UNPACKED = 100 * 1024 * 1024
MAX_FILE = 256 * 1024
MAX_TEXT = 8 * 1024 * 1024
MAX_FILES = 1000
SKIP_DIRS = {'.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build',
             '.next', 'coverage', 'data', 'logs', 'uploads', 'media', 'temp', 'target'}
TEXT_SUFFIXES = {'.py', '.js', '.ts', '.jsx', '.tsx', '.vue', '.json', '.toml', '.yaml', '.yml', '.md',
                 '.rst', '.txt', '.ini', '.cfg', '.html', '.css', '.scss', '.sh', '.ps1', '.go', '.rs',
                 '.java', '.c', '.h', '.cpp', '.hpp', '.sql', '.rb', '.php', '.xml'}
SECRET = re.compile(r'(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9_]{20,}|\bAKIA[A-Z0-9]{16}\b|(?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*[\"\x27][^\"\x27\s]{8,}[\"\x27])')


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


def source_root(home, source_id):
    if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_id):
        raise ValueError('无效的代码来源编号。')
    return Path(home) / 'sources' / source_id


def load_source(home, source_id, expected=None):
    root = source_root(home, source_id)
    raw = checked_bytes(root, 'manifest.json', limit=2 * 1024 * 1024)
    if expected is not None and checksum(raw) != expected:
        raise ValueError('代码来源清单已改变，请重新导入。')
    value = json.loads(raw)
    if value['id'] != source_id:
        raise ValueError('代码来源身份不匹配。')
    return value | {'manifest_sha256': checksum(raw)}


def source_bytes(home, value, path):
    if path not in value['files']:
        raise ValueError('该文件不在允许的源码范围。')
    raw = checked_bytes(source_root(home, value['id']) / 'files', path, limit=MAX_FILE)
    if checksum(raw) != value['files'][path]['sha256']:
        raise ValueError('源码文件已改变，停止读取旧版本。')
    return raw


def import_source(home, name, raw, *, origin=None, secrets=()):
    if not isinstance(name, str) or not 1 <= len(name) <= 160 or Path(name).name != name or any(c in name for c in '/\\\0\r\n'):
        raise ValueError('请提供单个普通文件名。')
    if not raw or len(raw) > MAX_UPLOAD:
        raise ValueError('上传内容为空或超过20MiB。')
    entries = []
    if name.lower().endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > 5000 or sum(item.file_size for item in infos) > MAX_UNPACKED:
                    raise ValueError('压缩包超过5000条目或100MiB解压总量。')
                seen = set()
                for item in infos:
                    if item.orig_filename != item.filename:
                        raise ValueError('压缩包路径被系统改写，拒绝含反斜杠或空字符的路径。')
                    path = item.filename.rstrip('/')
                    relative_path(path)
                    if unicodedata.normalize('NFC', path) != path:
                        raise ValueError('压缩包文件名编码不唯一，请重新压缩。')
                    key = path.casefold()
                    if key in seen:
                        raise ValueError('压缩包存在大小写冲突或重复路径。')
                    seen.add(key)
                    mode = item.external_attr >> 16
                    if item.flag_bits & 1 or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ValueError('不接收加密、链接或特殊文件。')
                    if not item.is_dir():
                        entries.append((path, item, archive))
                roots = {path.split('/')[0] for path, _, _ in entries}
                prefix = next(iter(roots)) + '/' if len(roots) == 1 and all('/' in p for p, _, _ in entries) else ''
                return _save_source(home, name, raw, entries, prefix, origin, secrets)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise ValueError('无法读取该ZIP，请检查格式或加密设置。') from None
    relative_path(name)
    entries.append((name, raw, None))
    return _save_source(home, name, raw, entries, '', origin, secrets)


def _save_source(home, name, original, entries, prefix, origin, secrets):
    source_id = uuid.uuid4().hex
    root = source_root(home, source_id)
    selected, excluded, total = {}, [], 0
    for archive_path, item, archive in entries:
        path = archive_path[len(prefix):]
        relative_path(path)
        if len(path) > 180 or any(len(part) > 100 for part in path.split('/')):
            excluded.append({'path': path[:240], 'reason': '路径过长，首版不纳入'})
            continue
        parts, lower = path.split('/'), Path(path).name.lower()
        size = item.file_size if archive else len(item)
        reason = None
        if any(part.lower() in SKIP_DIRS for part in parts[:-1]) or lower.startswith('.env'):
            reason = '默认排除的配置、运行数据或依赖目录'
        elif Path(path).suffix.lower() not in TEXT_SUFFIXES and lower not in {'dockerfile', 'makefile', 'license', 'readme'}:
            reason = '非支持的源码/文字格式'
        elif size > MAX_FILE:
            reason = '单文件超过256KiB'
        elif len(selected) >= MAX_FILES or total + size > MAX_TEXT:
            reason = '超过1000文件或8MiB可分析文字上限'
        if reason:
            excluded.append({'path': path, 'reason': reason})
            continue
        raw = archive.read(item) if archive else item
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            excluded.append({'path': path, 'reason': '不是UTF-8文字'})
            continue
        if '\0' in text or SECRET.search(text) or any(secret and secret in text for secret in secrets):
            excluded.append({'path': path, 'reason': '二进制或疑似敏感内容，未纳入'})
            continue
        destination = root / 'files' / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        selected[path] = {'sha256': checksum(raw), 'bytes': len(raw), 'lines': len(text.splitlines())}
        total += len(raw)
    if not selected:
        raise ValueError('没有可分析的UTF-8源码文件，请检查格式、体积或敏感内容。')
    value = {'schema_version': 1, 'id': source_id, 'name': name, 'created': now(),
             'origin': origin or {'kind': 'upload'}, 'input_sha256': checksum(original),
             'archive_prefix_removed': prefix, 'files': selected, 'excluded': excluded, 'total_bytes': total,
             'scope': '只读源码快照；未运行代码、安装依赖或发送模型请求。'}
    manifest = canonical_bytes(value)
    if len(manifest) > 2 * 1024 * 1024:
        raise ValueError('来源清单过长，请上传精简的项目ZIP。')
    (root / 'manifest.json').write_bytes(manifest)
    return load_source(home, source_id)


def validate_github(url, ref=''):
    if not isinstance(url, str) or len(url) > 500 or not isinstance(ref, str) or len(ref) > 200 or any(ord(c) < 32 for c in ref):
        raise ValueError('请填写普通GitHub仓库链接和可选版本。')
    parsed = urllib.parse.urlparse(url.strip())
    if (parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('首版只接收https://github.com/作者/仓库，不接收凭据或其他下载地址。')
    match = re.fullmatch(r'/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?', parsed.path)
    if not match:
        raise ValueError('请填写仓库首页链接；分支/提交请填入单独的版本栏。')
    owner, repo = match.groups()
    repo = repo.removesuffix('.git')
    if owner in {'.', '..'} or repo in {'', '.', '..'}:
        raise ValueError('仓库名称不正确。')
    return owner, repo, ref.strip()


def fetch_github(home, url, ref='', *, secrets=()):
    owner, repo, ref = validate_github(url, ref)
    deadline = time.monotonic() + 60
    hosts = {'api.github.com', 'codeload.github.com'}

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            target = urllib.parse.urlparse(newurl)
            if target.scheme != 'https' or target.hostname not in hosts or target.port not in (None, 443) or target.username or target.password:
                raise ValueError('仓库下载重定向超出允许来源。')
            return super().redirect_request(request, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(Redirect())
    def download(address, limit):
        request = urllib.request.Request(address, headers={'User-Agent': 'Local-Repository-Analysis/1.1', 'Accept': 'application/vnd.github+json'})
        with opener.open(request, timeout=20) as response:
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError('公开仓库下载超过60秒，请上传ZIP或稍后重试。')
                chunk = response.read(min(65536, limit - size + 1))
                if not chunk:
                    return b''.join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise ValueError('仓库下载超过首版大小限制，请上传精简ZIP。')
    try:
        commit = json.loads(download(f'https://api.github.com/repos/{owner}/{repo}/commits/' + urllib.parse.quote(ref or 'HEAD', safe=''), 1024 * 1024))
        sha = commit['sha']
        if not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{40}', sha):
            raise ValueError('仓库没有返回可固定的版本。')
        raw = download(f'https://api.github.com/repos/{owner}/{repo}/zipball/{sha}', MAX_UPLOAD)
    except urllib.error.HTTPError as error:
        raise ValueError(f'公开仓库获取失败（HTTP {error.code}）；可能是私有仓库、版本不存在或接口限流。可改为上传ZIP。') from None
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError):
        raise ValueError('无法获取公开仓库，请检查网络/版本，或上传ZIP。') from None
    return import_source(home, repo + '.zip', raw, origin={'kind': 'github', 'url': f'https://github.com/{owner}/{repo}', 'requested_ref': ref, 'commit': sha}, secrets=secrets)
