import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './app/App';
import './styles/theme.css';
import './styles/layout.css';
import './styles/b2-memory.css';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Failed to find root mounting DOM node');
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/service-worker.js');
  });
}
