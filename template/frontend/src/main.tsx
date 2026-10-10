import React from 'react';
import ReactDOM from 'react-dom/client';
import { flushSync } from 'react-dom';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './index.css';

// The server snapshot is already authorized and available before this module.
// Commit its first view before document-load consumers inspect the page.
flushSync(() => ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
));
