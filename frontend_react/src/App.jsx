import React, { useEffect, useMemo, useState } from 'react';
import { DecisionMaking } from './DecisionMaking';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:3000';

const STATUS_TEXT = {
  checking: 'Checking',
  up: 'Online',
  down: 'Offline',
  error: 'Degraded',
};

export function App() {
  const [appMode, setAppMode] = useState('decision');
  const [gatewayStatus, setGatewayStatus] = useState('checking');
  const [backendStatus, setBackendStatus] = useState('checking');

  const [token, setToken] = useState('');
  const [message, setMessage] = useState('');
  const [sessionId, setSessionId] = useState(`s-${Math.random().toString(36).slice(2, 8)}`);
  const [forceFail, setForceFail] = useState(false);
  const [responseMode, setResponseMode] = useState('stream');
  const [output, setOutput] = useState('');
  const [traceId, setTraceId] = useState('');

  const canSend = useMemo(() => token.length > 10 && message.trim().length > 0, [token, message]);

  useEffect(() => {
    let cancelled = false;

    async function checkServices() {
      try {
        const loginProbe = await fetch(`${API_BASE}/auth/login`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: 'admin', password: 'admin123' }),
        });
        if (!cancelled) {
          setGatewayStatus(loginProbe.ok ? 'up' : 'error');
        }
      } catch {
        if (!cancelled) {
          setGatewayStatus('down');
        }
      }

      try {
        const healthProbe = await fetch('http://localhost:8000/health');
        if (!cancelled) {
          setBackendStatus(healthProbe.ok ? 'up' : 'error');
        }
      } catch {
        if (!cancelled) {
          setBackendStatus('down');
        }
      }
    }

    checkServices();
    return () => {
      cancelled = true;
    };
  }, []);

  async function login() {
    const response = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'admin', password: 'admin123' }),
    });
    const data = await response.json();
    setToken(data.access_token || '');
  }

  async function onSubmit(event) {
    event.preventDefault();
    setOutput('');

    const response = await fetch(`${API_BASE}/chat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        message,
        session_id: sessionId,
        stream: responseMode === 'stream',
        force_fail: forceFail,
      }),
    });

    setTraceId(response.headers.get('x-trace-id') || '');

    if (responseMode === 'json') {
      const data = await response.json();
      setOutput(data.answer || JSON.stringify(data, null, 2));
      return;
    }

    const reader = response.body?.getReader();
    const decoder = new TextDecoder('utf-8');
    if (!reader) {
      setOutput('Streaming reader unavailable');
      return;
    }

    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop() || '';

      for (const eventBlock of events) {
        if (eventBlock.includes('event: done')) {
          continue;
        }
        if (!eventBlock.startsWith('data: ')) {
          continue;
        }

        const jsonLine = eventBlock.replace(/^data:\s*/, '');
        try {
          const parsed = JSON.parse(jsonLine);
          setOutput((prev) => prev + (parsed.chunk || parsed.answer || ''));
        } catch {
          setOutput((prev) => prev + jsonLine);
        }
      }
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <p className="brand-kicker">ChatAI Studio</p>
          <h1>Decision-first control room</h1>
          <p className="brand-sub">先决策，再复盘；必要时切回 Medical Chat 做对话验证。</p>
        </div>

        <div className="status-wrap">
          <StatusPill label="Gateway" value={STATUS_TEXT[gatewayStatus]} state={gatewayStatus} />
          <StatusPill label="Backend" value={STATUS_TEXT[backendStatus]} state={backendStatus} />
        </div>
      </header>

      <nav className="mode-nav">
        <button className={appMode === 'decision' ? 'tab active' : 'tab'} onClick={() => setAppMode('decision')}>
          Decision Studio
        </button>
        <button className={appMode === 'chat' ? 'tab active' : 'tab'} onClick={() => setAppMode('chat')}>
          Medical Chat
        </button>
      </nav>

      {appMode === 'decision' ? (
        <DecisionMaking />
      ) : (
        <main className="workspace-grid">
          <section className="card input-card">
            <div className="card-head">
              <div>
                <p className="kicker">Medical Chat</p>
                <h2>对话与流式响应</h2>
                <p className="subtext">用于测试普通医疗咨询回答，支持 stream/json 两种模式。</p>
              </div>
              <button onClick={login} type="button" className="btn ghost">
                Quick Login
              </button>
            </div>

            <form onSubmit={onSubmit} className="form-stack">
              <label className="field">
                <span>Access token</span>
                <textarea rows={3} value={token} onChange={(event) => setToken(event.target.value)} />
              </label>

              <div className="two-col">
                <label className="field">
                  <span>Session</span>
                  <input value={sessionId} onChange={(event) => setSessionId(event.target.value)} />
                </label>

                <label className="field">
                  <span>Mode</span>
                  <select value={responseMode} onChange={(event) => setResponseMode(event.target.value)}>
                    <option value="stream">Streaming</option>
                    <option value="json">JSON</option>
                  </select>
                </label>
              </div>

              <label className="check-row">
                <input type="checkbox" checked={forceFail} onChange={(event) => setForceFail(event.target.checked)} />
                <span>Force tool failure</span>
              </label>

              <label className="field">
                <span>Prompt</span>
                <textarea rows={6} value={message} onChange={(event) => setMessage(event.target.value)} />
              </label>

              <button type="submit" disabled={!canSend} className="btn primary">
                Send to agent
              </button>
            </form>
          </section>

          <section className="card output-card-v2">
            <div className="card-head compact">
              <div>
                <p className="kicker">Live output</p>
                <h2>Assistant response</h2>
              </div>
              <div className="id-chip">Trace: {traceId || 'pending'}</div>
            </div>

            <article className="answer-panel">
              <h3>Answer</h3>
              <pre>{output || '发送请求后，这里显示回答内容。'}</pre>
            </article>
          </section>
        </main>
      )}
    </div>
  );
}

function StatusPill({ label, value, state }) {
  return (
    <div className="status-pill">
      <span className={`dot ${state}`} />
      <span className="label">{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
