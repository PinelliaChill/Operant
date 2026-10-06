import assert from 'node:assert/strict';
import test from 'node:test';
import { browseLocalPath, canBrowseLocalPaths, selectedPathValue } from '../src/lib/nativePaths.ts';

test('cancelled selection does not replace the current input', () => {
  assert.equal(selectedPathValue(null), null);
  assert.equal(selectedPathValue(null, '/tmp/project'), null);
});

test('native paths preserve spaces, Chinese names and Windows paths', () => {
  for (const path of ['/tmp/我的项目 folder', 'C:\\Projects\\My Project', '\\\\server\\share\\project']) {
    assert.equal(selectedPathValue(path), path);
  }
  for (const selection of [['/tmp/a', '/tmp/b'], 'folder/name', 'C:relative']) {
    assert.throws(() => selectedPathValue(selection), /未能读取所选路径/);
  }
});

test('relative fields keep the project boundary and reject sibling paths', () => {
  assert.equal(selectedPathValue('/tmp/project', '/tmp/project/'), '.');
  assert.equal(selectedPathValue('/tmp/project/src/中文 file.txt', '/tmp/project/'), 'src/中文 file.txt');
  assert.equal(selectedPathValue('/tmp/project/file.txt', '/'), 'tmp/project/file.txt');
  assert.equal(selectedPathValue('c:\\projects\\demo\\src', 'C:\\Projects\\Demo'), 'src');
  assert.throws(() => selectedPathValue('/tmp/project-other/file', '/tmp/project'), /项目文件夹内/);
  assert.throws(() => selectedPathValue('/tmp/project/../outside', '/tmp/project'), /请先选择项目文件夹/);
  assert.throws(() => selectedPathValue('/tmp/project/file', 'project'), /请先选择项目文件夹/);
});

test('web clients fail explicitly instead of returning a fake local path', async () => {
  assert.equal(canBrowseLocalPaths(), false);
  await assert.rejects(() => browseLocalPath('directory', ''), /桌面版/);
});
