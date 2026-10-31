"""Bounded public references. Search snippets/metadata are never full-text evidence."""
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit, urljoin
from urllib.request import getproxies

from analysis_sources import SECRET
from collaboration_protocol import sha256

OFFICIAL_HOSTS = frozenset({
    'docs.python.org', 'developer.mozilla.org', 'nodejs.org', 'react.dev', 'vuejs.org', 'www.typescriptlang.org',
    'docs.djangoproject.com', 'docs.pytest.org', 'flask.palletsprojects.com', 'itsdangerous.palletsprojects.com',
    'click.palletsprojects.com', 'fastapi.tiangolo.com', 'docs.sqlalchemy.org', 'requests.readthedocs.io',
    'urllib3.readthedocs.io', 'docs.docker.com', 'arxiv.org', 'export.arxiv.org', 'api.crossref.org',
    'www.crossref.org', 'docs.github.com', 'numpy.org', 'pandas.pydata.org', 'scikit-learn.org', 'pytorch.org',
})


def public_url(value):
    if not isinstance(value, str) or len(value) > 1000 or any(ord(char) < 33 for char in value):
        raise ValueError('资料链接无效。')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS or parsed.username or parsed.password
            or parsed.port not in {None, 443} or parsed.query or SECRET.search(value)):
        raise ValueError('仅支持登记的公开官方文档或论文站点HTTPS正文链接，不接受内网、凭据或查询参数。')
    return urlunsplit(('https', parsed.hostname, parsed.path or '/', '', ''))


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}: self.skip += 1
        if not self.skip and tag in {'p', 'div', 'h1', 'h2', 'h3', 'li', 'pre', 'br'}: self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'} and self.skip: self.skip -= 1

    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def read_public_page(url, *, deadline):
    """Pin a public DNS address, retain TLS hostname checks, validate every redirect."""
    for _ in range(3):
        url = public_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('资料读取达到期限。')
        host = urlsplit(url).hostname
        proxy = getproxies().get('https')
        if proxy:
            # A proxy is trusted USER/SYSTEM configuration, never supplied by a
            # model or page. CONNECT still verifies TLS for the allowed origin.
            # This supports Windows proxy DNS mappings without permitting private
            # URLs or weakening certificate/hostname validation.
            endpoint = urlsplit(proxy)
            if endpoint.scheme != 'http' or not endpoint.hostname or endpoint.username or endpoint.password:
                raise ValueError('当前资料读取仅支持无内嵌凭据的系统HTTP代理。')
            connection = http.client.HTTPSConnection(endpoint.hostname, endpoint.port or 80,
                timeout=min(8, remaining), context=ssl.create_default_context())
            connection.set_tunnel(host, 443)
            connection_mode = 'system_proxy_tls_origin_verified'
        else:
            addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
            if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
                raise ValueError('资料地址没有解析到可访问的公网。')
            connection = http.client.HTTPSConnection(host, timeout=min(8, remaining), context=ssl.create_default_context())
            connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection((addresses[0], 443), timeout, source_address)
            connection_mode = 'direct_public_ip_pinned'
        try:
            connection.request('GET', urlsplit(url).path or '/', headers={'User-Agent': 'LocalCodeReference/1.0', 'Accept': 'text/html,text/plain'})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.getheader('Location', ''))
                continue
            if response.status != 200: raise ValueError('资料站点未返回可读正文。')
            content_type = response.getheader('Content-Type', '').split(';')[0].strip().lower()
            if content_type not in {'text/html', 'text/plain', 'application/xhtml+xml'}:
                raise ValueError('当前只读取HTML/文字正文；PDF和付费全文不能标为已读。')
            raw = response.read(512001)
            if len(raw) > 512000: raise ValueError('资料正文过长，未截断冒充完整读取。')
            text = raw.decode('utf8')
            if content_type != 'text/plain':
                parser = TextParser(); parser.feed(text); text = ''.join(parser.parts)
            text = re.sub(r'[ \t]+', ' ', text)
            text = re.sub(r'\n\s*\n+', '\n', text).strip()
            if not text: raise ValueError('没有可读正文。')
            read_kind = ('paper_abstract' if host in {'arxiv.org', 'export.arxiv.org'} and urlsplit(url).path.startswith('/abs/') else
                         'paper_html' if host in {'arxiv.org', 'export.arxiv.org'} and urlsplit(url).path.startswith('/html/') else 'html_or_text')
            return {'url': url, 'text': text, 'body_sha256': sha256(raw), 'text_sha256': sha256(text.encode()),
                    'read_kind': read_kind, 'retrieved_at': time.time(), 'connection_mode': connection_mode}
        finally:
            connection.close()
    raise ValueError('资料跳转超过上限。')


def search_public(query, *, deadline):
    if (not isinstance(query, str) or not 2 <= len(query) <= 160 or SECRET.search(query)
            or any(char in query for char in '\n\r{};`') or '://' in query):
        raise ValueError('查资料只接受简短关键词，不接受源码、链接、凭据或命令。')
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise TimeoutError('资料查找达到期限。')
    try:
        from ddgs import DDGS
        values = list(DDGS(timeout=max(1, min(5, int(remaining)))).text(query, max_results=5, backend='duckduckgo'))
    except Exception:
        raise RuntimeError('公开检索组件或服务暂不可用；不补造搜索结果。') from None
    results = []
    for value in values[:5]:
        if not isinstance(value, dict): continue
        try: url = public_url(value.get('href', ''))
        except ValueError: continue
        results.append({'url': url, 'title': str(value.get('title', ''))[:240],
                        'preview': str(value.get('body', ''))[:400], 'read_kind': 'search_metadata_only'})
        if len(results) >= 3: break
    return results


def excerpt(text, topics):
    # An explicit small excerpt from captured text, not a generated quotation.
    matches = [text.casefold().find(topic.casefold()) for topic in topics if topic]
    starts = [position for position in matches if position >= 0]
    start = max(0, min(starts) - 150) if starts else 0
    value = text[start:start + 700]
    while len(value.encode()) > 1800: value = value[:-1]
    return value
