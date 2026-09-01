import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './app/App';
import './styles/theme.css';
import './styles/layout.css';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Failed to find root mounting DOM node');
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
