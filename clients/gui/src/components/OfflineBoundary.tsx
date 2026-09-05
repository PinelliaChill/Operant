import React, { useEffect, useState } from 'react';

/**
 * The PWA shell can open without Core, but it must never turn an offline click
 * into a deferred write. API responses are not cached by the service worker.
 */
export const OfflineBoundary: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [online, setOnline] = useState(() => navigator.onLine);

  useEffect(() => {
    const markOnline = () => setOnline(true);
    const markOffline = () => setOnline(false);
    window.addEventListener('online', markOnline);
    window.addEventListener('offline', markOffline);
    return () => {
      window.removeEventListener('online', markOnline);
      window.removeEventListener('offline', markOffline);
    };
  }, []);

  return (
    <div
      className="offline-boundary"
      data-offline={online ? 'false' : 'true'}
    >
      {!online && (
        <div className="offline-banner" role="status" aria-live="polite">
          网络离线：Core 写操作仍由实时连接状态裁决；此 PWA 不缓存秘密、审批或写请求，也不会在恢复网络后自动发送。
        </div>
      )}
      {children}
    </div>
  );
};
