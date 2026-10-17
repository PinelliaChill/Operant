export interface BuildInfo {
  buildKind: 'development' | 'packaged' | 'unmarked';
  productVersion: string;
  releaseLabel: string | null;
  sourceCommit: string | null;
  sourceDirty: boolean;
  candidateId: string | null;
  bundleIdentifier: string;
}

export function buildVersionLabel(info: BuildInfo): string {
  if (info.buildKind === 'development') return `开发构建（组件版本 ${info.productVersion}）`;
  return info.releaseLabel || `未标记发布版本（组件版本 ${info.productVersion}）`;
}
