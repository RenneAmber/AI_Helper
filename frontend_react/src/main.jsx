import React from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import './styles.css';

const rootElement = document.getElementById('root');

function renderCrashScreen(error) {
  if (!rootElement) {
    return;
  }

  rootElement.innerHTML = `
    <div style="padding:24px;font-family:Segoe UI,Tahoma,sans-serif;background:#fff7f5;color:#3b1d18;min-height:100vh;">
      <div style="max-width:920px;margin:0 auto;border:1px solid #efc4bb;border-radius:16px;background:#ffffff;padding:24px;box-shadow:0 16px 40px rgba(120,40,20,0.08);">
        <p style="margin:0 0 8px;font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#b04532;">Frontend bootstrap failed</p>
        <h1 style="margin:0 0 12px;font-size:32px;line-height:1.1;">ChatAI UI hit a runtime error.</h1>
        <p style="margin:0 0 16px;color:#6b4b45;line-height:1.6;">The page loaded, but the React app crashed during startup. The error is shown below so it can be fixed directly.</p>
        <pre style="margin:0;padding:16px;border-radius:12px;background:#1b1a19;color:#f8efe7;white-space:pre-wrap;overflow:auto;">${String(error?.stack || error?.message || error)}</pre>
      </div>
    </div>
  `;
}

try {
  createRoot(rootElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
} catch (error) {
  renderCrashScreen(error);
}

window.addEventListener('error', (event) => {
  renderCrashScreen(event.error || event.message || 'Unknown frontend error');
});
