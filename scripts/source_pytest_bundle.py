"""Bundle only named, already installed pure-Python test tools, never host configs."""
import importlib.metadata
import importlib.util
from pathlib import Path
import sysconfig

from collaboration_protocol import canonical_bytes, sha256

PACKAGES = ('pytest', '_pytest', 'py', 'pluggy', 'iniconfig', 'packaging', 'pygments', 'tomli', 'colorama', 'exceptiongroup', 'typing_extensions')
DISTRIBUTIONS = ('pytest', 'pluggy', 'iniconfig', 'packaging', 'pygments', 'tomli', 'colorama', 'exceptiongroup', 'typing_extensions')


def pytest_bundle():
    purelib = Path(sysconfig.get_path('purelib')).resolve()
    files = {}
    for name in PACKAGES:
        spec = importlib.util.find_spec(name)
        if not spec or not spec.origin: raise ValueError('本机缺少独立Python检查组件，未创建修复任务。')
        origin = Path(spec.origin).resolve()
        if not origin.is_relative_to(purelib): raise ValueError('检查组件不在登记的项目环境内。')
        paths = list(origin.parent.rglob('*.py')) if spec.submodule_search_locations else [origin]
        for path in paths:
            if '__pycache__' in path.parts or not path.resolve().is_relative_to(purelib): continue
            raw = path.read_bytes()
            files[path.relative_to(purelib).as_posix()] = raw
    versions = {}
    for name in DISTRIBUTIONS:
        distribution = importlib.metadata.distribution(name)
        versions[name] = distribution.version
        for file in distribution.files or ():
            if Path(file).name not in {'METADATA', 'entry_points.txt'} and not Path(file).name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING')): continue
            path = Path(distribution.locate_file(file)).resolve()
            if path.is_relative_to(purelib) and '.dist-info' in str(file):
                files[str(file).replace('\\', '/')] = path.read_bytes()
    if len(files) > 1800 or sum(len(raw) for raw in files.values()) > 12 * 1024 * 1024:
        raise ValueError('检查组件超过预设限额，未发送源码。')
    binding = {'versions': versions, 'sha256': sha256(canonical_bytes({name: sha256(raw) for name, raw in sorted(files.items())})),
               'files': len(files), 'bytes': sum(map(len, files.values()))}
    return files, binding
