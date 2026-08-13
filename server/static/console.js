/* kt66 시나리오 콘솔 — 강사가 쓰는 한 화면.
 *
 * 상태를 만들지 않는다. 서버가 진실원천이고 화면은 비추기만 한다.
 * (kt66 관제 화면과 같은 규칙이다 — 화면이 자기 숫자를 만들기 시작하면
 *  강사가 보는 값과 학생 랩의 값이 갈라진다.)
 */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const KEY = () => $('#key').value.trim();

async function api(path, opt = {}) {
  const o = { headers: { 'content-type': 'application/json' }, ...opt };
  if (opt.body) o.body = JSON.stringify(opt.body);
  if (opt.auth) o.headers['x-api-key'] = KEY();
  const r = await fetch(path, o);
  const t = await r.text();
  let d; try { d = JSON.parse(t); } catch { d = t; }
  if (!r.ok) throw new Error(typeof d === 'object' ? (d.detail || JSON.stringify(d)) : d);
  return d;
}
const fmtT = ts => ts ? new Date(ts * 1000).toLocaleTimeString('ko-KR') : '—';
const mins = s => s == null ? '—' : (s >= 60 ? `${Math.round(s / 60)}분` : `${Math.round(s)}초`);

/* ── 탭 ─────────────────────────────────────────────────── */
$$('.tab').forEach(b => b.onclick = () => {
  $$('.tab').forEach(x => x.classList.toggle('on', x === b));
  $$('.panel').forEach(p => p.classList.toggle('on', p.id === b.dataset.tab));
  ({ students: loadStudents, runs: loadRuns, auto: loadSchedules, board: loadBoard }[b.dataset.tab] || (() => {}))();
});

/* ── 시나리오 ───────────────────────────────────────────── */
let CAT = [], SEL = null, PICKED = new Set();

async function loadCatalog() {
  const d = await api('/api/catalog');
  CAT = d.scenarios;
  $('#scen-err').textContent = d.errors.length
    ? `카탈로그 오류 ${d.errors.length}건:\n` + d.errors.join('\n') : '';
  renderCatalog();
}
function renderCatalog() {
  const q = $('#scen-q').value.trim().toLowerCase();
  const hit = CAT.filter(s => !q ||
    (s.id + s.title + s.category + (s.tags || []).join(' ')).toLowerCase().includes(q));
  $('#scen-list').innerHTML = hit.map(s => `
    <div class="item ${SEL === s.id ? 'on' : ''}" data-id="${s.id}">
      <div class="t">${s.title}</div>
      <div class="m">
        <span class="badge k-${s.kind}">${s.kind}</span>
        <span>${s.id}</span><span>난이도 ${s.difficulty}</span>
        <span>단계 ${s.steps}</span><span>${Math.round(s.duration_sec / 60)}분</span>
        <span>${s.max_points}점</span>
        ${s.verified ? '' : '<span class="badge unver">미검증</span>'}
      </div>
    </div>`).join('') || '<div class="item muted">결과 없음</div>';
  $$('#scen-list .item[data-id]').forEach(el =>
    el.onclick = () => selectScenario(el.dataset.id));
}
$('#scen-q').oninput = renderCatalog;

async function selectScenario(id) {
  SEL = id; renderCatalog();
  const d = await api('/api/scenario/' + encodeURIComponent(id));
  const gt = d.ground_truth || {};
  $('#scen-detail').classList.remove('muted');
  $('#scen-detail').innerHTML = `
    <h3 style="margin-top:0">${d.title}</h3>
    <pre>${d.brief || ''}</pre>
    <h3>단계 ${d.kind === 'timeline' ? '(시간 경과형 — 문제가 누적된다)' : ''}</h3>
    <ol>${d.steps.map(s => `<li><b>t+${Math.round(s.at_sec / 60)}분</b> —
      <code>${s.via}/${s.fault || s.action || '—'}</code>
      ${s.target ? `→ ${s.target}` : ''}<br><span class="muted">${s.note || ''}</span></li>`).join('')}</ol>
    <h3>근본 원인 (Ground Truth)</h3><pre>${gt.root_cause || ''}</pre>
    <h3>연쇄</h3><ul>${(gt.chain || []).map(c => `<li>${c}</li>`).join('')}</ul>
    ${gt.key_insight ? `<h3>핵심</h3><pre>${gt.key_insight}</pre>` : ''}
    <h3>채점 항목 (${d.max_points}점)</h3>
    <ul>${(gt.checks || []).map(c =>
      `<li><b>${c.points}점</b> ${c.note || c.id} <span class="muted">[${c.type}]</span></li>`).join('')}</ul>`;
  $('#fire-btn').disabled = false;
}

/* ── 발사 ───────────────────────────────────────────────── */
function audience() {
  const v = $$('input[name=aud]').find(r => r.checked).value;
  if (v === 'cohort') return 'cohort:' + $('#aud-cohort').value.trim();
  if (v === 'students') return 'students:' + [...PICKED].join(',');
  return 'all';
}
$('#fire-btn').onclick = async () => {
  const m = $('#fire-msg'); m.className = 'msg'; m.textContent = '발사 중…';
  try {
    const r = await api('/api/launch', { method: 'POST', auth: true,
      body: { scenario_id: SEL, audience: audience(), mode: 'manual' } });
    m.className = 'msg ok';
    m.textContent = `발사 완료 — ${r.count}명 (batch ${r.batch})`;
  } catch (e) { m.className = 'msg bad'; m.textContent = '실패: ' + e.message; }
};

/* ── 학생 ───────────────────────────────────────────────── */
async function loadStudents() {
  const d = await api('/api/students');
  $('#stu-table tbody').innerHTML = d.students.map(s => `
    <tr><td><input type="checkbox" data-sid="${s.id}" ${PICKED.has(String(s.id)) ? 'checked' : ''}></td>
      <td>${s.name}</td><td>${s.cohort || '—'}</td><td class="muted">${s.host || '—'}</td>
      <td class="${s.online ? 'on-line' : 'off-line'}">${s.online ? '● 연결' : '○ 끊김'}</td>
      <td>${s.active || 0}</td><td class="muted">${fmtT(s.last_seen)}</td></tr>`).join('')
    || '<tr><td colspan="7" class="muted">등록된 학생이 없습니다. 학생 랩에서 에이전트를 실행하세요.</td></tr>';
  $$('#stu-table input[data-sid]').forEach(c => c.onchange = () => {
    c.checked ? PICKED.add(c.dataset.sid) : PICKED.delete(c.dataset.sid);
    $('#aud-pick').textContent = PICKED.size ? `${PICKED.size}명 선택됨` : '학생 탭에서 선택';
  });
}
$('#stu-refresh').onclick = loadStudents;

/* ── 진행 ───────────────────────────────────────────────── */
async function loadRuns() {
  const d = await api('/api/launches?limit=60');
  $('#run-table tbody').innerHTML = d.launches.map(L => {
    const ap = L.steps.filter(s => s.state === 'applied').length;
    const sc = L.score;
    return `<tr><td>${L.id}</td><td>${L.scenario_id}</td><td>${L.student_name}</td>
      <td class="st-${L.state}">${L.state}</td><td>${ap}/${L.steps.length}</td>
      <td>${sc ? `${sc.points}/${sc.max_points}` : '—'}</td>
      <td><button class="ghost" data-ev="${L.id}">증거</button>
          <button class="ghost" data-gr="${L.id}">채점</button>
          <button class="ghost danger" data-cx="${L.id}">취소</button></td></tr>`;
  }).join('') || '<tr><td colspan="7" class="muted">발사 이력이 없습니다.</td></tr>';

  $$('[data-ev]').forEach(b => b.onclick = () => showEvidence(b.dataset.ev));
  $$('[data-gr]').forEach(b => b.onclick = async () => {
    try { const r = await api('/api/grade/' + b.dataset.gr, { method: 'POST', auth: true });
      alert(`${r.points}/${r.max_points}점\n` +
        r.checks.map(c => `${c.passed ? '✔' : '✘'} ${c.id} (${c.points}/${c.of}) — ${c.why}`).join('\n'));
      loadRuns();
    } catch (e) { alert('채점 실패: ' + e.message); }
  });
  $$('[data-cx]').forEach(b => b.onclick = async () => {
    try { await api(`/api/cancel?launch_id=${b.dataset.cx}`, { method: 'POST', auth: true });
      loadRuns(); } catch (e) { alert(e.message); }
  });
}
async function showEvidence(lid) {
  const d = await api('/api/evidence/' + lid);
  const box = $('#ev-box'); box.hidden = false;
  box.innerHTML = `<h3>발사 #${lid} 증거 ${d.evidence.length}건</h3>` +
    (d.evidence.map(e => `<div style="border-bottom:1px solid var(--line);padding:6px 0">
      <b>${fmtT(e.ts)}</b> <span class="badge">${e.kind}</span>
      ${e.passed === 1 ? '<span class="chk-pass">통과</span>' : e.passed === 0 ? '<span class="chk-fail">실패</span>' : ''}
      <span class="muted">${e.source}</span><br>${e.summary}
      ${e.snippet ? `<pre>${e.snippet}</pre>` : ''}</div>`).join('')
      || '<p class="muted">아직 증거가 없습니다.</p>');
}
$('#run-refresh').onclick = loadRuns;

/* ── 자동 발사 ──────────────────────────────────────────── */
async function loadSchedules() {
  const d = await api('/api/schedules');
  $('#sch-table tbody').innerHTML = d.schedules.map(s => `
    <tr><td>${s.id}</td><td>${s.name}</td><td>${Math.round(s.every_sec / 60)}분</td>
      <td>${s.audience}</td><td class="muted">${s.scenarios.join(', ')}</td>
      <td class="muted">${fmtT(s.next_fire)}</td>
      <td><button class="ghost" data-tg="${s.id}" data-en="${s.enabled ? 0 : 1}">
          ${s.enabled ? '중지' : '시작'}</button>
        <button class="ghost danger" data-del="${s.id}">삭제</button></td></tr>`).join('')
    || '<tr><td colspan="7" class="muted">일정이 없습니다.</td></tr>';
  $$('[data-tg]').forEach(b => b.onclick = async () => {
    await api(`/api/schedule/${b.dataset.tg}/toggle?enabled=${b.dataset.en === '1'}`,
      { method: 'POST', auth: true }); loadSchedules();
  });
  $$('[data-del]').forEach(b => b.onclick = async () => {
    await api('/api/schedule/' + b.dataset.del, { method: 'DELETE', auth: true }); loadSchedules();
  });
}
$('#sch-add').onclick = async () => {
  try {
    await api('/api/schedule', { method: 'POST', auth: true, body: {
      name: $('#sch-name').value.trim() || '무제',
      every_sec: Math.max(30, (+$('#sch-every').value || 15) * 60),
      pick: $('#sch-pick').value, audience: $('#sch-aud').value.trim() || 'all',
      scenarios: $('#sch-list').value.split(',').map(s => s.trim()).filter(Boolean) } });
    loadSchedules();
  } catch (e) { alert('일정 추가 실패: ' + e.message); }
};

/* ── 채점 ───────────────────────────────────────────────── */
async function loadBoard() {
  const d = await api('/api/scoreboard');
  $('#bd-table tbody').innerHTML = d.rows.map(r => `
    <tr><td>${r.name}</td><td>${r.cohort || '—'}</td><td>${r.attempts || 0}</td>
      <td>${r.points || 0} / ${r.max_points || 0}</td>
      <td>${r.detect_ok || 0}</td><td>${r.mitigate_ok || 0}</td>
      <td class="${r.forbidden ? 'chk-fail' : ''}">${r.forbidden || 0}</td></tr>`).join('')
    || '<tr><td colspan="7" class="muted">채점 결과가 없습니다.</td></tr>';
}
$('#bd-refresh').onclick = loadBoard;

/* ── 부팅 ───────────────────────────────────────────────── */
$('#key').value = localStorage.getItem('kt66s_key') || '';
$('#key').oninput = () => localStorage.setItem('kt66s_key', KEY());
async function health() {
  try {
    const h = await api('/health');
    $('#health').className = 'pill ok';
    $('#health').textContent = `시나리오 ${h.scenarios} · 학생 ${h.students}` +
      (h.errors ? ` · 오류 ${h.errors}` : '');
  } catch { $('#health').className = 'pill bad'; $('#health').textContent = '서버 응답 없음'; }
}
loadCatalog(); health(); loadStudents();
setInterval(() => { health(); if ($('#runs').classList.contains('on')) loadRuns();
  if ($('#students').classList.contains('on')) loadStudents(); }, 5000);
