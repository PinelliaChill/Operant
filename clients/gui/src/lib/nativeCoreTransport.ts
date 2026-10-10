import {
  fetchPhase1ETransport,
  type Phase1ERequest,
  type Phase1EResponse,
  type Phase1ETransport,
} from '../../../../sdk/typescript-client/phase1e-transport.ts';
import { nativeCoreRoutes } from './nativeCoreRoutes.generated.ts';

type NativeRequest = {
  operationId: string;
  pathParams: Record<string, string>;
  rawQuery: string;
  headers: Record<string, string>;
  body: string;
};
type NativeResponse = { status: number; headers: Record<string, string>; text: string };
type NativeInvoke = (request: NativeRequest) => Promise<NativeResponse>;

const protectedPaths = [
  '/v1/setup', '/v1/local-control', '/v1/remote-control', '/v1/extensions',
  '/v1/writer-workspaces', '/v1/writer-conflicts', '/v1/merge-runs',
];
const graphManagementPath = /^\/v1\/graph\/runs\/[^/]+\/(?:writer-workspaces|writer-artifacts|writer-conflicts|merge-runs)(?:\/|$)/;
const workbenchManagementPath = /^\/v1\/workbench\/(?:extensions\/commands|threads\/[^/]+\/(?:extension-commands|skill-commands))(?:\/|$)/;
const fixedCoreOrigin = 'http://127.0.0.1:8000';
const unreserved = /^[A-Za-z0-9._~-]+$/;

export type NativeCoreEnvironment = { native: boolean; expectedOrigin: string | null };

export function detectNativeCoreEnvironment(): NativeCoreEnvironment {
  if (typeof window === 'undefined') return { native: false, expectedOrigin: null };
  const shell = window as Window & { __TAURI_INTERNALS__?: unknown };
  if (!shell.__TAURI_INTERNALS__) return { native: false, expectedOrigin: null };
  if (window.location.protocol === 'tauri:' || window.location.hostname === 'tauri.localhost') {
    return { native: true, expectedOrigin: fixedCoreOrigin };
  }
  const { protocol, hostname, port, origin } = window.location;
  if (protocol === 'http:' && (hostname === '127.0.0.1' || hostname === '[::1]')
    && /^\d+$/.test(port) && Number(port) > 0 && Number(port) <= 65535) {
    return { native: true, expectedOrigin: origin };
  }
  // A Tauri shell on an unexpected origin must never downgrade protected calls.
  return { native: true, expectedOrigin: null };
}

function pathParamsFor(template: string, path: string): Record<string, string> | null {
  const expected = template.split('/');
  const actual = path.split('/');
  if (expected.length !== actual.length) return null;
  const params: Record<string, string> = {};
  for (let index = 0; index < expected.length; index += 1) {
    const slot = expected[index];
    const value = actual[index];
    const match = /^\{([a-z][a-z0-9_]*)\}$/.exec(slot);
    if (!match) {
      if (slot !== value) return null;
    } else {
      if (!unreserved.test(value) || value === '.' || value === '..') return null;
      params[match[1]] = value;
    }
  }
  return params;
}

function routeFor(request: Phase1ERequest, url: URL) {
  const matches = nativeCoreRoutes.flatMap((route) => {
    if (route.method !== request.method.toUpperCase()) return [];
    const params = pathParamsFor(route.pathTemplate, url.pathname);
    return params === null ? [] : [{ route, params }];
  });
  if (matches.length !== 1) throw new Error('本机管理请求未登记，已阻止发送。');
  return matches[0];
}

function matchesProtectedPath(path: string): boolean {
  return protectedPaths.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))
    || graphManagementPath.test(path) || workbenchManagementPath.test(path);
}

function isProtectedPath(path: string): boolean {
  let candidate = path;
  for (let depth = 0; depth < 3; depth += 1) {
    if (matchesProtectedPath(candidate)) return true;
    try {
      const decoded = decodeURIComponent(candidate);
      if (decoded === candidate) break;
      candidate = decoded;
    } catch {
      break;
    }
  }
  return false;
}

/** One transport for the generated Onboarding, Phase56, and Beta clients. */
export function createNativeCoreTransport(
  invokeNative: NativeInvoke,
  fallback: Phase1ETransport = fetchPhase1ETransport,
  environment: NativeCoreEnvironment = detectNativeCoreEnvironment(),
): Phase1ETransport {
  return async (request): Promise<Phase1EResponse> => {
    if (!environment.native) return fallback(request);
    const url = new URL(request.url);
    if (!isProtectedPath(url.pathname) && !(request.path && isProtectedPath(request.path))) {
      return fallback(request);
    }
    if (!environment.expectedOrigin || url.origin !== environment.expectedOrigin
      || url.hash || request.path !== url.pathname) {
      throw new Error('本机管理请求地址无效，已阻止发送。');
    }
    const { route, params } = routeFor(request, url);
    if (route.deviceAuth) return fallback(request);
    let response: NativeResponse;
    try {
      response = await invokeNative({
        operationId: route.operationId,
        pathParams: params,
        rawQuery: url.search.slice(1),
        headers: request.headers ?? {},
        body: request.body ?? '',
      });
    } catch {
      // The request may have committed before its response was lost. The SDK
      // turns this into transport_unavailable and keeps its unknown-write guard.
      throw new Error('本机管理请求结果未确认，请刷新并核对原请求。');
    }
    if (!response || typeof response !== 'object'
      || !Number.isInteger(response.status) || response.status < 100 || response.status > 599
      || !response.headers || typeof response.headers !== 'object' || Array.isArray(response.headers)
      || Object.values(response.headers).some((value) => typeof value !== 'string')
      || typeof response.text !== 'string') {
      throw new Error('本机管理响应格式无效，请刷新并核对结果。');
    }
    return { status: response.status, headers: response.headers, text: () => response.text };
  };
}

export const nativeCoreTransport = createNativeCoreTransport(async (request) => {
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke<NativeResponse>('local_core_request', request);
});
