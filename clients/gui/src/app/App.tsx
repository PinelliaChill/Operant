import React from 'react';
import { RouterProvider } from 'react-router-dom';
import { ClientProvider } from '../context/ClientContext';
import { DemoProvider } from '../demo/DemoContext';
import { ErrorBoundary } from '../components/ErrorBoundary';
import { LiveProvider } from '../live/LiveContext';
import { Phase45Provider } from '../live45/Phase45Context';
import { router } from './routes';

export const App: React.FC = () => {
  return (
    <ErrorBoundary fallbackTitle="Operant 2.0 严重系统错误">
      <ClientProvider>
        {/* DemoProvider 必须在 ClientProvider 之内：审批卡回写依赖 client */}
        <DemoProvider>
          <LiveProvider>
            <Phase45Provider>
              <RouterProvider router={router} />
            </Phase45Provider>
          </LiveProvider>
        </DemoProvider>
      </ClientProvider>
    </ErrorBoundary>
  );
};
