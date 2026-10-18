import React, { useEffect, useState } from 'react';
import { detectNativeCoreEnvironment } from '../../lib/nativeCoreTransport';
import { buildVersionLabel, type BuildInfo } from '../../lib/buildInfo';

export const BuildInfoCard: React.FC = () => {
  const [info, setInfo] = useState<BuildInfo | null>(null);
  const [error, setError] = useState('');
  const native = detectNativeCoreEnvironment().native;
  useEffect(() => {
    if (!native) return;
    let active = true;
    void import('@tauri-apps/api/core').then(({ invoke }) => invoke<BuildInfo>('desktop_build_info'))
      .then((value) => { if (active) setInfo(value); })
      .catch(() => { if (active) setError('无法读取构建信息，请查看安装包内的来源摘要。'); });
    return () => { active = false; };
  }, [native]);
  return <section className="settings-hub-card"><h2>关于 Operant</h2>
    {!native ? <p>浏览器界面。桌面安装版本请在 Operant 桌面端查看。</p> : error ? <p role="alert">{error}</p> : !info ? <p role="status">正在读取构建信息…</p> : <>
      <p>{buildVersionLabel(info)}</p>
      <dl className="build-info-list">
        <div><dt>源码提交</dt><dd><code>{info.sourceCommit || '未记录'}</code>{info.sourceDirty && '（含未提交改动）'}</dd></div>
        <div><dt>构建身份</dt><dd>{info.candidateId ? `候选包：${info.candidateId}` : info.buildKind === 'development' ? '开发环境' : '产品安装包'}</dd></div>
        <div><dt>应用标识</dt><dd><code>{info.bundleIdentifier}</code></dd></div>
      </dl>
    </>}
  </section>;
};
