import React, { useMemo, useState } from 'react';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:3000';

function formatConfidence(confidence, score) {
  const label = confidence ? String(confidence).toUpperCase() : 'UNKNOWN';
  const numeric = Number(score);
  if (Number.isFinite(numeric)) {
    return `${label} (${Math.round(numeric * 100)}%)`;
  }
  return label;
}

function toRiskText(item) {
  if (!item) {
    return '';
  }
  if (typeof item === 'string') {
    return item;
  }
  return item.risk || item.text || JSON.stringify(item);
}

function DecisionSection({ title, items, emptyText }) {
  const normalizedItems = Array.isArray(items) ? items.map(toRiskText).filter(Boolean) : [];

  return (
    <div className="decision-section-card">
      <h3>{title}</h3>
      {normalizedItems.length > 0 ? (
        <ul>
          {normalizedItems.map((item, index) => (
            <li key={`${title}-${index}`}>{item}</li>
          ))}
        </ul>
      ) : (
        <p>{emptyText}</p>
      )}
    </div>
  );
}

function OptionCard({ option, rankLabel }) {
  return (
    <article className="option-card">
      <h4>{option?.name || 'Unnamed option'}</h4>
      <p>{option?.description || '暂无描述。'}</p>
      <div className="chip-row">
        {rankLabel ? <span className="chip accent">{rankLabel}</span> : null}
        {option?.estimated_effort ? <span className="chip">Effort: {option.estimated_effort}</span> : null}
        {option?.timeline ? <span className="chip">Timeline: {option.timeline}</span> : null}
        <span className="chip score">Score: {Number(option?.score ?? 0).toFixed(2)}</span>
        {option?.score_confidence ? <span className="chip">Score confidence: {String(option.score_confidence).toUpperCase()}</span> : null}
      </div>
      {option?.rationale ? <p><strong>Why:</strong> {option.rationale}</p> : null}
      <DecisionSection title="Pros" items={option?.pros} emptyText="暂无优势总结。" />
      <DecisionSection title="Cons" items={option?.cons} emptyText="暂无代价说明。" />
      <DecisionSection title="Risks" items={option?.risks} emptyText="暂无风险说明。" />
      {Array.isArray(option?.assumption_ids) && option.assumption_ids.length > 0 ? (
        <p className="inline-note">Depends on assumptions: {option.assumption_ids.join(', ')}</p>
      ) : null}
    </article>
  );
}

export function DecisionMaking() {
  const [token, setToken] = useState('');
  const [question, setQuestion] = useState('Should we enable feature flag X for 10% users this week?');
  const [domain, setDomain] = useState('engineering');
  const [riskWeight, setRiskWeight] = useState('0.4');
  const [impactWeight, setImpactWeight] = useState('0.3');
  const [feasibilityWeight, setFeasibilityWeight] = useState('0.2');
  const [costWeight, setCostWeight] = useState('0.1');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [rawPayload, setRawPayload] = useState('');

  const data = result?.data;
  const recommendation = data?.primary_recommendation;
  const options = Array.isArray(data?.generated_options) ? data.generated_options : [];
  const assumptions = Array.isArray(data?.explicit_assumptions) ? data.explicit_assumptions : [];
  const rankedRecommendations = Array.isArray(data?.ranked_recommendations) ? data.ranked_recommendations : [];
  const criteriaPreview = useMemo(
    () => ({
      risk: Number(riskWeight) || 0,
      impact: Number(impactWeight) || 0,
      feasibility: Number(feasibilityWeight) || 0,
      cost: Number(costWeight) || 0,
    }),
    [costWeight, feasibilityWeight, impactWeight, riskWeight],
  );

  async function quickLogin() {
    try {
      const res = await fetch(`${API_BASE}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'admin', password: 'admin123' }),
      });
      const payload = await res.json();
      setToken(payload.access_token || '');
      setError('');
    } catch (event) {
      setError(`Login failed: ${event.message}`);
    }
  }

  async function onSubmit() {
    if (!token) {
      setError('请先登录（Quick Login）');
      return;
    }
    if (!question.trim()) {
      setError('Decision question 不能为空');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const response = await fetch(`${API_BASE}/decisions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          problem_statement: question,
          domain,
          evaluation_criteria: criteriaPreview,
          user_id: 'u_admin',
        }),
      });

      if (!response.ok) {
        throw new Error(await response.text());
      }

      const payload = await response.json();
      setResult(payload);
      setRawPayload(JSON.stringify(payload, null, 2));
    } catch (event) {
      setError(`Error: ${event.message}`);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="workspace-grid">
      <section className="card input-card">
        <div className="card-head">
          <div>
            <p className="kicker">Decision Copilot</p>
            <h2>假设显性化决策台</h2>
            <p className="subtext">旧的 Create / Run / Replay 已移除，现在一次提交直接返回澄清、假设、方案、推荐与增强路径。</p>
          </div>
          <button onClick={quickLogin} type="button" className="btn ghost">
            Quick Login
          </button>
        </div>

        <div className="mode-pills">
          <span className="pill active">Clarify</span>
          <span className="pill active">Assumptions</span>
          <span className="pill active">Options</span>
          <span className="pill active">Recommend</span>
        </div>

        <div className="form-stack">
          <label className="field">
            <span>Access token</span>
            <textarea rows={3} value={token} onChange={(event) => setToken(event.target.value)} placeholder="JWT token" />
          </label>

          <label className="field">
            <span>Decision question</span>
            <textarea rows={4} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="例如：Should we enable feature flag X for 10% users this week?" />
          </label>

          <div className="two-col">
            <label className="field">
              <span>Domain</span>
              <select value={domain} onChange={(event) => setDomain(event.target.value)}>
                <option value="engineering">Engineering</option>
                <option value="product">Product</option>
                <option value="business">Business</option>
                <option value="personal">Personal</option>
                <option value="medical">Medical</option>
              </select>
            </label>

            <label className="field">
              <span>Risk weight</span>
              <input value={riskWeight} onChange={(event) => setRiskWeight(event.target.value)} />
            </label>
          </div>

          <div className="two-col">
            <label className="field">
              <span>Impact weight</span>
              <input value={impactWeight} onChange={(event) => setImpactWeight(event.target.value)} />
            </label>

            <label className="field">
              <span>Feasibility weight</span>
              <input value={feasibilityWeight} onChange={(event) => setFeasibilityWeight(event.target.value)} />
            </label>
          </div>

          <label className="field">
            <span>Cost weight</span>
            <input value={costWeight} onChange={(event) => setCostWeight(event.target.value)} />
          </label>

          <button type="button" className="btn primary" onClick={onSubmit} disabled={loading}>
            {loading ? 'Thinking...' : 'Run Decision Copilot'}
          </button>
        </div>

        {error ? <div className="error-banner">{error}</div> : null}
      </section>

      <section className="card output-card-v2">
        <div className="card-head compact">
          <div>
            <p className="kicker">Decision answer</p>
            <h2>新的决策输出</h2>
          </div>
          <div className="id-chip">Decision ID: {result?.decision_id || result?.decisionId || 'pending'}</div>
        </div>

        <article className="answer-panel">
          <h3>Recommendation</h3>
          {data ? (
            <div className="decision-answer-grid">
              <div className="decision-highlight">
                <span>Status</span>
                <strong>{result?.status || 'completed'}</strong>
              </div>
              <div className="decision-highlight">
                <span>Confidence</span>
                <strong>{formatConfidence(data.recommendation_confidence, data.recommendation_confidence_score)}</strong>
              </div>
              <div className="decision-question-card">
                <span>Primary recommendation</span>
                <strong>{recommendation?.name || 'N/A'}</strong>
                {recommendation?.description ? <p>{recommendation.description}</p> : null}
              </div>
              <DecisionSection title="Rationale" items={data.recommendation_rationale ? [data.recommendation_rationale] : []} emptyText="暂无推荐说明。" />
              <DecisionSection title="Key risks" items={data.key_risks} emptyText="暂无关键风险。" />
              <DecisionSection title="Mitigations" items={data.mitigation_strategies} emptyText="暂无缓解策略。" />
              <DecisionSection title="Next steps to strengthen" items={data.next_steps_to_strengthen} emptyText="暂无增强路径。" />
            </div>
          ) : (
            <pre>提交一次问题后，这里会展示新 Decision Copilot 的完整输出。</pre>
          )}
        </article>

        <article className="payload-panel">
          <h3>Ranked answers with confidence scores</h3>
          {rankedRecommendations.length > 0 ? (
            <div className="option-grid">
              {rankedRecommendations.map((item) => (
                <article key={`${item.option_id}-${item.rank}`} className="option-card">
                  <h4>#{item.rank} {item.option_name}</h4>
                  <div className="chip-row">
                    <span className="chip score">Score: {Number(item.score ?? 0).toFixed(2)}</span>
                    <span className="chip">Score confidence: {String(item.score_confidence || 'unknown').toUpperCase()}</span>
                    <span className="chip accent">Confidence score: {Math.round(Number(item.confidence_score ?? 0) * 100)}%</span>
                  </div>
                  {item.rationale ? <p><strong>Why ranked here:</strong> {item.rationale}</p> : null}
                </article>
              ))}
            </div>
          ) : (
            <pre>排序榜单会在这里显示（多个答案 + 置信度评分）。</pre>
          )}
        </article>

        <div className="stats-row">
          <div className="stat-box">
            <span>Assumptions</span>
            <strong>{assumptions.length}</strong>
          </div>
          <div className="stat-box">
            <span>Options</span>
            <strong>{options.length}</strong>
          </div>
          <div className="stat-box">
            <span>Audit events</span>
            <strong>{Array.isArray(data?.audit_trail) ? data.audit_trail.length : 0}</strong>
          </div>
        </div>

        <article className="payload-panel">
          <h3>Clarified context</h3>
          {data?.clarified_context ? (
            <div className="option-grid">
              <DecisionSection title="Objectives" items={data.clarified_context.objectives} emptyText="暂无目标。" />
              <DecisionSection title="Constraints" items={data.clarified_context.constraints} emptyText="暂无约束。" />
              <DecisionSection title="Stakeholders" items={data.clarified_context.stakeholders} emptyText="暂无涉众。" />
              <DecisionSection title="Success criteria" items={data.clarified_context.success_criteria} emptyText="暂无成功标准。" />
            </div>
          ) : (
            <pre>澄清后的上下文会显示在这里。</pre>
          )}
        </article>

        <article className="payload-panel">
          <h3>Explicit assumptions</h3>
          {assumptions.length > 0 ? (
            <div className="option-grid">
              {assumptions.map((assumption) => (
                <article key={assumption.id} className="option-card">
                  <h4>{assumption.statement}</h4>
                  <div className="chip-row">
                    <span className="chip">Confidence: {String(assumption.confidence).toUpperCase()}</span>
                    <span className="chip">Impact if wrong: {String(assumption.impact_if_wrong).toUpperCase()}</span>
                    <span className="chip">Verifiable: {assumption.can_be_verified ? 'YES' : 'NO'}</span>
                  </div>
                  <p><strong>Why assumed:</strong> {assumption.justification}</p>
                  {assumption.how_to_verify ? <p><strong>How to verify:</strong> {assumption.how_to_verify}</p> : null}
                </article>
              ))}
            </div>
          ) : (
            <pre>显性化假设会显示在这里。</pre>
          )}
        </article>

        <article className="payload-panel">
          <h3>Options</h3>
          {options.length > 0 ? (
            <div className="option-grid">
              {options.map((option, index) => {
                const rankLabel = option?.name === recommendation?.name ? 'Primary recommendation' : `Option ${index + 1}`;
                return <OptionCard key={option.id || option.name || index} option={option} rankLabel={rankLabel} />;
              })}
            </div>
          ) : (
            <pre>候选方案会显示在这里。</pre>
          )}
        </article>

        <article className="payload-panel">
          <h3>Raw payload</h3>
          <pre>{rawPayload || '原始响应会展示在这里，便于调试和审计。'}</pre>
        </article>
      </section>
    </main>
  );
}
