// Executed only inside the isolated Node container; input arrives through stdin.
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = '/work';
for (const [name, content] of Object.entries(data.files)) {
  const target = path.join(root, name);
  fs.mkdirSync(path.dirname(target), {recursive: true});
  fs.writeFileSync(target, Buffer.from(content, 'base64'));
}
fs.mkdirSync(root + '/_acceptance', {recursive: true});
for (const [name, content] of Object.entries(data.acceptance)) fs.writeFileSync(root + '/_acceptance/' + name, content);
const tests = Object.keys(data.files).filter(name => name.endsWith('.js') && (name.split('/').some(part => ['test','tests','__tests__'].includes(part)) || /(^|\/)(test[^/]*\.js|[^/]*\.(test|spec)\.js)$/.test(name)));
tests.push(...Object.keys(data.acceptance).filter(name => name.endsWith('.js')).map(name => '_acceptance/' + name));
if (!tests.length) { console.log('SOURCE_CHECK_RESULT_' + data.receipt + '=' + JSON.stringify({tests: 0, errors: 1, failures: 0, skipped: 0, reason: 'No Node test cases'})); process.exit(1); }
const result = cp.spawnSync(process.execPath, ['--test', '--test-reporter=tap', ...tests], {cwd: root, env: {PATH: process.env.PATH, HOME: '/tmp'}, encoding: 'utf8', timeout: data.timeout * 1000, maxBuffer: 1000000});
process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
const count = /^# tests (\d+)$/m.exec(result.stdout || '');
const failed = /^# fail (\d+)$/m.exec(result.stdout || '');
const skipped = /^# skipped (\d+)$/m.exec(result.stdout || '');
console.log('SOURCE_CHECK_RESULT_' + data.receipt + '=' + JSON.stringify({tests: Number(count?.[1] || 0), errors: result.error ? 1 : 0, failures: Number(failed?.[1] || (result.status ? 1 : 0)), skipped: Number(skipped?.[1] || 0)}));
process.exit(result.status === 0 && Number(count?.[1]) > Number(skipped?.[1] || 0) ? 0 : 1);
