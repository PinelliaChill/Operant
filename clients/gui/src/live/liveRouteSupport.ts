/**
 * Operant GUI Live Route Support Resolver
 * 判定给定路径在 Live 模式下是否受支持，或是否应重定向/展示 LiveUnavailableView
 */

export type LiveUnavailableSection =
  | 'tasks'
  | 'agents'
  | 'runs'
  | 'workflow'
  | 'collab_canvas'
  | 'session';

export interface LiveRouteSupportResult {
  isSupported: boolean;
  unavailableSection: string;
}

/** 检查当前路径在 Live 模式下是否受支持 */
export function resolveLiveRouteSupport(pathname: string): LiveRouteSupportResult {
  const normalized = pathname.replace(/^\/+/, '').split('?')[0];
  const segments = normalized.split('/').filter(Boolean);
  const primary = segments[0] || 'chat';

  // 嵌套路由检查：/collab/:wfId/canvas 是 Demo 独有的画布预览，Live 协作图使用统一的 LiveGraphTeamView
  if (primary === 'collab' && segments.length > 1 && segments[segments.length - 1] === 'canvas') {
    return { isSupported: false, unavailableSection: 'collab_canvas' };
  }

  // 一级路由支持判定
  const supportedPrimarySections = new Set([
    'chat',
    'projects',
    'approvals',
    'schedules',
    'collab',
    'skills',
    'extensions',
    'settings',
  ]);

  if (supportedPrimarySections.has(primary)) {
    return { isSupported: true, unavailableSection: primary };
  }

  return { isSupported: false, unavailableSection: primary };
}
