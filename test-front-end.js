fetch('http://localhost:5000/chat', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({ message: '用Markdown解释一下Transformer的核心' })
}).then(async r => {
  const ct = r.headers.get('content-type') || '';
  const raw = await r.text();
  if (!r.ok) throw new Error(raw);
  if (!ct.includes('application/json')) throw new Error('Not JSON:\n' + raw);
  const data = JSON.parse(raw);
  console.log(data);
});
