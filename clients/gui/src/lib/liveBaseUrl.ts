const LIVE_ORIGIN_ERROR =
  'Live client requires the current browser origin; no Core-host or Mock fallback is configured.';

/**
 * Resolve the browser origin used by the generated live client.
 *
 * Vite proxies the same-origin /v1 path during development, while production
 * serves that path through the same reverse proxy as the GUI. A missing or
 * invalid origin is an explicit configuration error, never a reason to use a
 * direct Core host or silently switch to Mock data.
 */
export function resolveLiveBaseUrl(browserOrigin: string | undefined): string {
  if (typeof browserOrigin !== 'string' || browserOrigin.trim().length === 0) {
    throw new Error(LIVE_ORIGIN_ERROR);
  }

  let parsed: URL;
  try {
    parsed = new URL(browserOrigin);
  } catch {
    throw new Error(`${LIVE_ORIGIN_ERROR} Received an invalid browser origin.`);
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error(`${LIVE_ORIGIN_ERROR} Expected an HTTP(S) origin.`);
  }

  return parsed.origin;
}

/** Tauri uses a non-HTTP WebView origin; its audited bridge is the fixed local Core. */
export function resolveDesktopLiveBaseUrl(isTauri: boolean): string | null {
  return isTauri ? 'http://127.0.0.1:8000' : null;
}

/**
 * Resolve the URL for both the Vite development WebView and the packaged
 * Tauri shell.  A debug WebView may expose Tauri internals while still being
 * served from an HTTP origin; it must use that origin so Vite can proxy to
 * the isolated Core selected by OPERANT_CORE_URL.  Only the real tauri://
 * shell uses the fixed localhost Core endpoint.
 */
export function resolveBrowserOrDesktopLiveBaseUrl(
  browserOrigin: string | undefined,
  isTauri: boolean,
): string {
  if (typeof browserOrigin === 'string' && browserOrigin.trim().length > 0) {
    try {
      const parsed = new URL(browserOrigin);
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        if (isTauri && parsed.hostname === 'tauri.localhost') {
          return resolveDesktopLiveBaseUrl(true) as string;
        }
        return parsed.origin;
      }
    } catch {
      // Fall through to the explicit non-HTTP shell handling below.
    }
  }
  const desktopBaseUrl = resolveDesktopLiveBaseUrl(isTauri);
  if (desktopBaseUrl) return desktopBaseUrl;
  return resolveLiveBaseUrl(browserOrigin);
}

/** Resolve the origin of the page hosting the GUI. */
export function currentBrowserOrigin(): string {
  if (typeof window === 'undefined') {
    throw new Error(LIVE_ORIGIN_ERROR);
  }
  const tauriWindow = window as Window & { __TAURI_INTERNALS__?: unknown };
  return resolveBrowserOrDesktopLiveBaseUrl(
    window.location.origin,
    Boolean(tauriWindow.__TAURI_INTERNALS__),
  );
}
