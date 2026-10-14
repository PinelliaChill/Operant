interface Timer {
  set: (callback: () => void, delayMs: number) => number;
  clear: (timerId: number) => void;
}

interface OAuthStatusPollingOptions<T> {
  read: () => Promise<T>;
  onStatus: (status: T) => void;
  onError: (cause: unknown) => void;
  retryOnError: boolean;
  timer?: Timer;
}

interface OAuthAttemptIdentity {
  attempt_id: string;
  provider: string;
  connection_id: string;
}

export function sameOAuthAttempt(expected: OAuthAttemptIdentity, received: OAuthAttemptIdentity): boolean {
  return received.attempt_id === expected.attempt_id
    && received.provider === expected.provider
    && received.connection_id === expected.connection_id;
}

/** Read-only status polling. A transient Codex read failure keeps the same attempt alive. */
export function startOAuthStatusPolling<T>({ read, onStatus, onError, retryOnError, timer = {
  set: (callback, delayMs) => window.setTimeout(callback, delayMs),
  clear: (timerId) => window.clearTimeout(timerId),
} }: OAuthStatusPollingOptions<T>): () => void {
  let active = true;
  let timerId: number;
  const poll = () => {
    void read().then((status) => { if (active) onStatus(status); }).catch((cause: unknown) => {
      if (!active) return;
      onError(cause);
      if (retryOnError) timerId = timer.set(poll, 2500);
    });
  };
  timerId = timer.set(poll, 2500);
  return () => { active = false; timer.clear(timerId); };
}
