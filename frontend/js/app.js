/* ══════════════════════════════════════════════════════════════════════════════
   PetroKenya — station dashboard (browser side)
   compute() mirrors the server's engine in backend/server.py; the server is the
   source of truth and recomputes every figure on save.
   ══════════════════════════════════════════════════════════════════════════════ */

// ── Constants (same as backend/server.py) ────────────────────────────────────
const VAT       = 0.16;
const EX_P      = 21.95;   // Excise petrol
const EX_D      = 7.21;    // Excise diesel
const EX_K      = 7.19;    // Excise kerosene
const RL_P      = 18.00;   // Road levy petrol
const RL_D      = 13.19;   // Road levy diesel

// ── Helpers ──────────────────────────────────────────────────────────────────
const $  = id => document.getElementById(id);
const fv = id => parseFloat($(id)?.value) || 0;
const sv = id => $(id)?.value?.trim() || '';
const r2 = v  => Math.round(v * 100) / 100;
const fmt = n => Number(n||0).toLocaleString('en-KE',{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtL = n => Number(n||0).toFixed(2);
// Escape text before putting it into innerHTML — records are shared between users
const esc = v => String(v ?? '').replace(/[&<>"']/g, ch =>
  ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[ch]));

// ── Backend API + session ─────────────────────────────────────────────────────
const CACHE_KEY = 'pk_cache_v1';   // last records seen, for read-only offline use
let ONLINE = false;
let USER = null;                   // { username, role, station }

const storeGet = key => { try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; } };
const storeSet = (key, val) => { try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) { /* storage unavailable */ } };

async function api(method, path, body) {
  let res;
  try {
    res = await fetch('/api' + path, {
      method,
      credentials: 'same-origin',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined
    });
  } catch (e) {
    setOnline(false);
    throw new Error('No connection to the server');
  }
  setOnline(true);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== '/login') {
    USER = null;
    showLogin();
  }
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function setOnline(v) {
  ONLINE = v;
  const el = $('dbPill');
  if (!el) return;
  el.classList.toggle('offline', !v);
  el.lastChild.textContent = v ? ' Online · Synced' : ' Offline · Read-only';
}

// Pull records from the server into RECS (falls back to the offline cache)
async function syncFromServer() {
  try {
    const data = await api('GET', '/records');
    RECS = Array.isArray(data.records) ? data.records : [];
    storeSet(CACHE_KEY, { user: USER && USER.username, records: RECS });
    return true;
  } catch (e) {
    const cache = storeGet(CACHE_KEY);
    if (!ONLINE && cache && USER && cache.user === USER.username) {
      RECS = cache.records || [];
      toast('📴 Offline — showing records from your last sync', true);
    } else if (ONLINE) {
      toast('⚠️ ' + e.message, true);
    }
    return false;
  }
}

async function refreshRecs() {
  await syncFromServer();
  loadRecs();
  renderAnalytics();
}

function requireOnline(action) {
  if (ONLINE) return true;
  toast('📴 You are offline — connect to the internet to ' + action, true);
  return false;
}

// ── Sign in / account ─────────────────────────────────────────────────────────
function showLogin(msg) {
  $('loginError').textContent = msg || '';
  $('loginOverlay').classList.add('open');
  setTimeout(() => $('loginUser').focus(), 50);
}

async function doLogin(evt) {
  if (evt) evt.preventDefault();
  const btn = $('loginBtn');
  btn.disabled = true;
  $('loginError').textContent = '';
  try {
    const res = await api('POST', '/login', { username: sv('loginUser'), password: $('loginPass').value });
    USER = res.user;
    storeSet('pk_user', USER);
    $('loginPass').value = '';
    $('loginOverlay').classList.remove('open');
    await startSession();
    toast('👋 Welcome, ' + displayName(USER));
  } catch (e) {
    $('loginError').textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function logout() {
  try { await api('POST', '/logout'); } catch (e) { /* already signed out */ }
  USER = null;
  RECS = [];
  $('fOperator').value = '';
  try { localStorage.removeItem(CACHE_KEY); localStorage.removeItem('pk_user'); } catch (e) { /* ignore */ }
  closeAccount();
  clearFormForNewEntry();
  loadRecs();
  renderAnalytics();
  showLogin();
}

const displayName = u => (u && (u.display_name || u.full_name || u.username)) || '';

// Lock the UI to what this user is allowed to do
function applyUserToUI() {
  const isAdmin = USER && USER.role === 'admin';
  const fixed = USER && USER.role === 'staff' && USER.station;
  $('userBtn').textContent = '👤 ' + (USER ? displayName(USER) : 'Sign in');
  // The operator is whoever is signed in; the server records the same name on save
  $('fOperator').value = displayName(USER);
  const sel = $('fStation');
  if (fixed) sel.value = USER.station;
  sel.disabled = !!fixed;
  $('hStation').textContent = sel.value;
  $('filterStationBtn').style.display = fixed ? 'none' : '';
  if (fixed) $('filterStation').style.display = 'none';
  $('eraseAllBtn').style.display = isAdmin ? '' : 'none';
}

async function startSession() {
  applyUserToUI();
  await syncFromServer();
  loadRecs();
  renderAnalytics();
  loadRecordForDateAndStation();
}

function openAccount() {
  if (!USER) { showLogin(); return; }
  $('acctName').textContent = displayName(USER);
  $('meFullName').value = USER.full_name || '';
  $('acctRole').textContent = USER.role === 'admin' ? 'Administrator — all branches'
    : 'Staff — ' + (USER.station || 'all branches');
  $('usersSec').style.display = USER.role === 'admin' ? 'block' : 'none';
  ['pwCurrent','pwNew'].forEach(id => $(id).value = '');
  if (USER.role === 'admin') loadUsers();
  $('accountOverlay').classList.add('open');
}
function closeAccount() { $('accountOverlay').classList.remove('open'); }

async function saveMyName() {
  if (!requireOnline('change your name')) return;
  try {
    USER = (await api('POST', '/me/name', { full_name: sv('meFullName') })).user;
    storeSet('pk_user', USER);
    applyUserToUI();
    $('acctName').textContent = displayName(USER);
    toast('✅ Name saved — ' + displayName(USER));
  } catch (e) { toast('⚠️ ' + e.message, true); }
}

async function changePassword() {
  if (!requireOnline('change your password')) return;
  try {
    await api('POST', '/me/password', { current_password: $('pwCurrent').value, new_password: $('pwNew').value });
    ['pwCurrent','pwNew'].forEach(id => $(id).value = '');
    toast('🔑 Password changed');
  } catch (e) { toast('⚠️ ' + e.message, true); }
}

async function loadUsers() {
  const list = $('usersList');
  try {
    const { users } = await api('GET', '/users');
    list.innerHTML = users.map(u => `
      <div class="m-row" style="align-items:center;gap:8px">
        <span>${esc(displayName(u))} <small style="color:var(--mut)">· ${u.full_name ? esc(u.username) + ' · ' : ''}${esc(u.role)}${u.station ? ' · ' + esc(u.station) : ''}</small></span>
        <span style="display:flex;gap:6px">
          <button class="btn btn-o btn-xs" onclick="resetUserPassword(${u.id}, '${esc(u.username)}')">🔑</button>
          ${u.id === USER.id ? '' : `<button class="btn btn-r btn-xs" onclick="deleteUser(${u.id}, '${esc(u.username)}')">✕</button>`}
        </span>
      </div>`).join('');
  } catch (e) {
    list.innerHTML = `<div class="m-row"><span style="color:var(--red)">${esc(e.message)}</span></div>`;
  }
}

async function addUser() {
  if (!requireOnline('add users')) return;
  try {
    await api('POST', '/users', {
      username: sv('nuName'), password: $('nuPass').value, full_name: sv('nuFullName'),
      role: sv('nuRole'), station: sv('nuStation')
    });
    toast('✅ User ' + sv('nuName') + ' added');
    ['nuName','nuPass','nuFullName'].forEach(id => $(id).value = '');
    loadUsers();
  } catch (e) { toast('⚠️ ' + e.message, true); }
}

async function resetUserPassword(id, name) {
  const password = prompt('New password for ' + name + ' (at least 8 characters):');
  if (!password) return;
  try {
    await api('POST', '/users/' + id + '/password', { password });
    toast('🔑 Password reset for ' + name);
  } catch (e) { toast('⚠️ ' + e.message, true); }
}

async function deleteUser(id, name) {
  const ok = await showConfirmModal('Delete user?', name + ' will no longer be able to sign in.');
  if (!ok) return;
  try {
    await api('DELETE', '/users/' + id);
    toast('🗑️ User ' + name + ' deleted');
    loadUsers();
  } catch (e) { toast('⚠️ ' + e.message, true); }
}

let COMP = {};   // last computed values
let RECS = [];   // saved records
let INVOICES = []; // today's credit invoices (customer, car reg, phone, company)
let MISC_ITEMS = []; // today's itemised miscellaneous expenses (name, amount)
let GAS_ITEMS = [];  // today's itemised gases & lubricants sales (name, category, qty, price)

// ── Main computation — mirrors C++ engine exactly ────────────────────────────
function compute() {
  // Gases & Lubricants (itemised revenue)
  const gasLubeRev = r2(GAS_ITEMS.reduce((sum, i) => sum + (i.qty * i.price), 0));
  const gasRevOnly  = r2(GAS_ITEMS.filter(i => i.category === 'Gas').reduce((s,i)=>s+(i.qty*i.price),0));
  const lubeRevOnly = r2(GAS_ITEMS.filter(i => i.category === 'Lubricant').reduce((s,i)=>s+(i.qty*i.price),0));

  // Fuel
  const pL = Math.max(0, fv('pClose') - fv('pOpen'));
  const dL = Math.max(0, fv('dClose') - fv('dOpen'));
  const kL = Math.max(0, fv('kClose') - fv('kOpen'));
  const pRev = r2(pL * fv('pPrice')), dRev = r2(dL * fv('dPrice')), kRev = r2(kL * fv('kPrice'));
  const pFC  = r2(pL * fv('pCost')),  dFC  = r2(dL * fv('dCost')),  kFC  = r2(kL * fv('kCost'));
  const pMgn = r2(pRev - pFC), dMgn = r2(dRev - dFC), kMgn = r2(kRev - kFC);
  const totalL   = r2(pL + dL + kL);
  const grossRev = r2(pRev + dRev + kRev + gasLubeRev);
  const totalFC  = r2(pFC + dFC + kFC);

  // Pump sales meter (the pump's own KES counter) — cross-check against calculated revenue
  const pPumpSales = Math.max(0, r2(fv('pSalesClose') - fv('pSalesOpen')));
  const dPumpSales = Math.max(0, r2(fv('dSalesClose') - fv('dSalesOpen')));
  const kPumpSales = Math.max(0, r2(fv('kSalesClose') - fv('kSalesOpen')));
  const totalPumpSales = r2(pPumpSales + dPumpSales + kPumpSales);
  const pPumpVariance = r2(pRev - pPumpSales);
  const dPumpVariance = r2(dRev - dPumpSales);
  const kPumpVariance = r2(kRev - kPumpSales);
  const pumpHasData = (fv('pSalesOpen') || fv('pSalesClose') || fv('dSalesOpen') || fv('dSalesClose') || fv('kSalesOpen') || fv('kSalesClose')) > 0;

  // Tank dip reconciliation: Expected Stock = Opening Dip + Deliveries − Litres Sold (pump meters)
  const pDipOpen = fv('pDipOpen'), pDelivered = fv('pDelivered'), pDipClose = fv('pDipClose');
  const dDipOpen = fv('dDipOpen'), dDelivered = fv('dDelivered'), dDipClose = fv('dDipClose');
  const kDipOpen = fv('kDipOpen'), kDelivered = fv('kDelivered'), kDipClose = fv('kDipClose');

  const pExpectedStock = r2(pDipOpen + pDelivered - pL);
  const dExpectedStock = r2(dDipOpen + dDelivered - dL);
  const kExpectedStock = r2(kDipOpen + kDelivered - kL);

  const pDipVariance = r2(pDipClose - pExpectedStock);
  const dDipVariance = r2(dDipClose - dExpectedStock);
  const kDipVariance = r2(kDipClose - kExpectedStock);

  const pDipHasData = (pDipOpen || pDipClose) > 0;
  const dDipHasData = (dDipOpen || dDipClose) > 0;
  const kDipHasData = (kDipOpen || kDipClose) > 0;

  // Payments
  const mpesa = fv('fMpesa'), cash = fv('fCash');
  const credit = fv('fCredit'), card = fv('fCard');
  const collected = r2(mpesa + cash + credit + card);
  const variance  = r2(grossRev - collected);
  const mpesaPct  = grossRev > 0 ? r2(mpesa / grossRev * 100) : 0;
  const cashPct   = grossRev > 0 ? r2(cash  / grossRev * 100) : 0;
  const otherPct  = grossRev > 0 ? r2((credit+card) / grossRev * 100) : 0;
  const float_ = fv('fFloat'), banked = fv('fBanked');
  const tillBal = r2(cash + float_ - banked);

  // Expenses
  const totalExp = r2(fv('eWages')+fv('eElec')+fv('eWater')+fv('eMaint')+fv('eLubes')+fv('eMisc'));

  // Profit
  const totalCost  = r2(totalFC + totalExp);
  const netProfit  = r2(grossRev - totalCost);
  const profitMgn  = grossRev > 0 ? r2(netProfit / grossRev * 100) : 0;
  const revPerL    = totalL > 0 ? r2(grossRev / totalL) : 0;
  const profitPerL = totalL > 0 ? r2(netProfit / totalL) : 0;

  // KRA Tax
  const exVat      = r2(grossRev / (1 + VAT));
  const outputVAT  = r2(grossRev - exVat);
  const excP = r2(pL * EX_P), excD = r2(dL * EX_D), excK = r2(kL * EX_K);
  const totalExcise = r2(excP + excD + excK);
  const rlP = r2(pL * RL_P), rlD = r2(dL * RL_D);
  const totalTax  = r2(outputVAT + totalExcise);

  return {
    pL,dL,kL,totalL,pRev,dRev,kRev,pFC,dFC,kFC,pMgn,dMgn,kMgn,
    gasLubeRev,gasRevOnly,lubeRevOnly,
    pPumpSales,dPumpSales,kPumpSales,totalPumpSales,
    pPumpVariance,dPumpVariance,kPumpVariance,pumpHasData,
    pExpectedStock,dExpectedStock,kExpectedStock,
    pDipVariance,dDipVariance,kDipVariance,
    pDipHasData,dDipHasData,kDipHasData,
    grossRev,totalFC,totalExp,totalCost,netProfit,profitMgn,revPerL,profitPerL,
    mpesa,cash,credit,card,collected,variance,mpesaPct,cashPct,otherPct,tillBal,
    exVat,outputVAT,excP,excD,excK,totalExcise,rlP,rlD,totalTax
  };
}

let debounceTimer = null;
function calc() {
  clearTimeout(debounceTimer);
  setStatus('calc');
  debounceTimer = setTimeout(() => {
    COMP = compute();
    updateUI();
    setStatus('ok');
  }, 120);
}

// ── Update every UI element ───────────────────────────────────────────────────
// ── Show/hide and colour the pump-sales-meter cross-check row for one fuel ────
function updatePumpCheckRow(prefix, pumpSales, variance, hasData) {
  const row = $(prefix + 'PumpCheckRow');
  if (!row) return;

  if (!hasData) {
    row.style.display = 'none';
    return;
  }
  row.style.display = 'flex';

  $(prefix + 'PumpSales').textContent = 'KES ' + fmt(pumpSales);

  const varEl = $(prefix + 'PumpVariance');
  const labelEl = $(prefix + 'PumpVarianceLabel');
  varEl.textContent = 'KES ' + fmt(variance);

  if (Math.abs(variance) < 1) {
    labelEl.textContent = '✅ Matches Pump: ';
    varEl.style.color = 'var(--acc)';
  } else if (Math.abs(variance) < 50) {
    labelEl.textContent = '⚠️ Variance: ';
    varEl.style.color = 'var(--acc2)';
  } else {
    labelEl.textContent = '🚨 Variance: ';
    varEl.style.color = 'var(--red)';
  }
}

// ── Show/hide and colour the tank dip reconciliation row for one fuel ────────
function updateDipRow(prefix, sold, expectedStock, variance, hasData) {
  const soldEl = $(prefix + 'DipSold');
  const expEl  = $(prefix + 'DipExpected');
  if (soldEl) soldEl.textContent = fmtL(sold) + ' L';
  if (expEl)  expEl.textContent  = fmtL(expectedStock) + ' L';

  const row = $(prefix + 'DipVarianceRow');
  if (!row) return;

  if (!hasData) {
    row.style.display = 'none';
    return;
  }
  row.style.display = 'flex';

  const varEl = $(prefix + 'DipVariance');
  const labelEl = $(prefix + 'DipVarianceLabel');
  varEl.textContent = fmtL(variance) + ' L';

  if (Math.abs(variance) < 10) {
    labelEl.textContent = '✅ Within Tolerance: ';
    varEl.style.color = 'var(--acc)';
  } else if (variance < 0) {
    labelEl.textContent = '🚨 Stock Short (possible leak/theft): ';
    varEl.style.color = 'var(--red)';
  } else {
    labelEl.textContent = '⚠️ Stock Surplus: ';
    varEl.style.color = 'var(--acc2)';
  }
}

function updateUI() {
  const c = COMP;

  // Fuel tab
  $('pLit').textContent = fmtL(c.pL)+' L'; $('pRev').textContent = 'KES '+fmt(c.pRev); $('pMgn').textContent = 'KES '+fmt(c.pMgn);
  $('dLit').textContent = fmtL(c.dL)+' L'; $('dRev').textContent = 'KES '+fmt(c.dRev); $('dMgn').textContent = 'KES '+fmt(c.dMgn);
  $('kLit').textContent = fmtL(c.kL)+' L'; $('kRev').textContent = 'KES '+fmt(c.kRev); $('kMgn').textContent = 'KES '+fmt(c.kMgn);
  $('pPriceTag').textContent = fv('pPrice').toFixed(2);
  $('dPriceTag').textContent = fv('dPrice').toFixed(2);
  $('kPriceTag').textContent = fv('kPrice').toFixed(2);

  // Pump sales meter cross-check rows (only shown once sales-meter values are entered)
  updatePumpCheckRow('p', c.pPumpSales, c.pPumpVariance, fv('pSalesOpen') || fv('pSalesClose'));
  updatePumpCheckRow('d', c.dPumpSales, c.dPumpVariance, fv('dSalesOpen') || fv('dSalesClose'));
  updatePumpCheckRow('k', c.kPumpSales, c.kPumpVariance, fv('kSalesOpen') || fv('kSalesClose'));

  // Tank dip reconciliation rows
  updateDipRow('p', c.pL, c.pExpectedStock, c.pDipVariance, c.pDipHasData);
  updateDipRow('d', c.dL, c.dExpectedStock, c.dDipVariance, c.dDipHasData);
  updateDipRow('k', c.kL, c.kExpectedStock, c.kDipVariance, c.kDipHasData);

  // Summary cards
  $('cRev').textContent   = 'KES '+fmt(c.grossRev);
  $('cRevSub').textContent= c.gasLubeRev > 0
    ? `Fuel: KES ${fmt(c.pRev+c.dRev+c.kRev)} + Gas/Lube: KES ${fmt(c.gasLubeRev)}`
    : `P: ${fmtL(c.pL)}L | D: ${fmtL(c.dL)}L | K: ${fmtL(c.kL)}L`;
  $('cLit').textContent   = fmt(c.totalL);
  $('cLitSub').textContent= `Petrol ${fmtL(c.pL)}L · Diesel ${fmtL(c.dL)}L · Kero ${fmtL(c.kL)}L`;
  $('cMpesa').textContent = 'KES '+fmt(c.mpesa);
  $('cMpesaSub').textContent = `${c.mpesaPct.toFixed(1)}% of sales · ${fv('fMpesaTxn')|0} txns`;
  $('cCash').textContent  = 'KES '+fmt(c.cash);
  $('cExp').textContent   = 'KES '+fmt(c.totalExp);
  $('cExpSub').textContent= `Wages+Util+Misc: KES ${fmt(c.totalExp)}`;
  $('cProfit').textContent= 'KES '+fmt(c.netProfit);
  $('cMarginSub').textContent = `Margin: ${c.profitMgn.toFixed(1)}%`;
  $('cTax').textContent   = 'KES '+fmt(c.totalTax);
  $('cTaxSub').textContent= `VAT: KES ${fmt(c.outputVAT)} | Excise: KES ${fmt(c.totalExcise)}`;
  $('cRevPerL').textContent = 'KES '+fmt(c.revPerL);
  $('cProfPerL').textContent = `Profit/L: KES ${fmt(c.profitPerL)}`;

  // Profit card colour
  $('profCard').style.setProperty('--c', c.netProfit >= 0 ? '#b57bee' : '#e05252');

  // Profit box
  $('pvRev').textContent  = 'KES '+fmt(c.grossRev);
  $('pvFuelCost').textContent = '− KES '+fmt(c.totalFC);
  $('pvExpenses').textContent = '− KES '+fmt(c.totalExp);
  $('pvProfit').textContent = 'KES '+fmt(c.netProfit);
  $('pvProfit').style.color = c.netProfit >= 0 ? 'var(--acc)' : 'var(--red)';
  $('pvMargin').textContent = `Margin: ${c.profitMgn.toFixed(1)}%`;

  // Variance
  const vb = $('varBar');
  if (Math.abs(c.variance) < 0.5) {
    vb.className = 'var-bar ok';
    vb.textContent = `✅ Sales vs Collected: Balanced (KES ${fmt(c.variance)})`;
  } else if (Math.abs(c.variance) < 10000) {
    vb.className = 'var-bar warn';
    vb.textContent = `⚠️ Variance: KES ${fmt(c.variance)} — Check credit/card entries`;
  } else {
    vb.className = 'var-bar bad';
    vb.textContent = `🚨 Large Variance: KES ${fmt(c.variance)} — Investigate discrepancy`;
  }

  // Payments tab
  $('pMpesaPct').textContent = c.mpesaPct.toFixed(1)+'%';
  $('pCashPct').textContent  = c.cashPct.toFixed(1)+'%';
  $('pOtherPct').textContent = c.otherPct.toFixed(1)+'%';
  $('pCollected').textContent = 'KES '+fmt(c.collected);
  $('fTillBal').value = c.tillBal.toFixed(2);

  // Expenses tab
  $('eTotalDisp').textContent = 'KES '+fmt(c.totalExp);
  $('eRevPerL').textContent   = 'KES '+fmt(c.revPerL);
  $('eProfPerL').textContent  = 'KES '+fmt(c.profitPerL);
  $('eProfPerL').style.color  = c.profitPerL >= 0 ? 'var(--acc)' : 'var(--red)';
  $('eSalesRef').textContent  = 'KES '+fmt(c.grossRev);
  $('eExpRef').textContent    = 'KES '+fmt(c.totalExp);
  $('eSalesAfterExp').textContent = 'KES '+fmt(r2(c.grossRev - c.totalExp));
  $('eSalesAfterExp').style.color = (c.grossRev - c.totalExp) >= 0 ? 'var(--acc)' : 'var(--red)';

  // KRA tab
  $('kExVat').textContent  = 'KES '+fmt(c.exVat);
  $('kVat').textContent    = 'KES '+fmt(c.outputVAT);
  $('kInclVat').textContent= 'KES '+fmt(c.grossRev);
  $('kExcP').textContent   = 'KES '+fmt(c.excP);
  $('kExcD').textContent   = 'KES '+fmt(c.excD);
  $('kExcK').textContent   = 'KES '+fmt(c.excK);
  $('kRLP').textContent    = 'KES '+fmt(c.rlP);
  $('kRLD').textContent    = 'KES '+fmt(c.rlD);
  $('kTotalTax').textContent = 'KES '+fmt(c.totalTax);
}

// ── Status pill ───────────────────────────────────────────────────────────────
function setStatus(s) {
  const el = $('enginePill');
  if (s === 'ok') {
    el.className = 'status-pill ok';
    el.innerHTML = '<span class="dot pulse"></span> Engine Active';
  } else if (s === 'calc') {
    el.className = 'status-pill calc';
    el.innerHTML = '<span class="spin"></span> Calculating…';
  }
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function tab(name, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('on'));
  $('p-'+name).classList.add('on');
  btn.classList.add('on');
  // On phones the menu sits above the section, so jump down to what was opened
  if (window.matchMedia('(max-width:760px)').matches) $('p-'+name).scrollIntoView({ block: 'start' });
  if (name === 'ana') renderAnalytics();
  if (name === 'rec') loadRecs();
}

// ── Load a record into the form (used when switching to a date that has data) ─
function populateFormFromRecord(r) {
  const setVal = (id, val) => { if ($(id)) $(id).value = (val === undefined || val === null) ? '' : val; };

  // Fuel
  setVal('pOpen', r.petrol_open);   setVal('pClose', r.petrol_close);
  setVal('pPrice', r.petrol_price); setVal('pCost', r.petrol_cost);
  setVal('pSalesOpen', r.petrol_sales_open); setVal('pSalesClose', r.petrol_sales_close);
  setVal('dOpen', r.diesel_open);   setVal('dClose', r.diesel_close);
  setVal('dPrice', r.diesel_price); setVal('dCost', r.diesel_cost);
  setVal('dSalesOpen', r.diesel_sales_open); setVal('dSalesClose', r.diesel_sales_close);
  setVal('kOpen', r.kero_open);     setVal('kClose', r.kero_close);
  setVal('kPrice', r.kero_price);   setVal('kCost', r.kero_cost);
  setVal('kSalesOpen', r.kero_sales_open);   setVal('kSalesClose', r.kero_sales_close);
  setVal('pDipOpen', r.petrol_dip_open); setVal('pDelivered', r.petrol_delivered); setVal('pDipClose', r.petrol_dip_close);
  setVal('dDipOpen', r.diesel_dip_open); setVal('dDelivered', r.diesel_delivered); setVal('dDipClose', r.diesel_dip_close);
  setVal('kDipOpen', r.kero_dip_open);   setVal('kDelivered', r.kero_delivered);   setVal('kDipClose', r.kero_dip_close);

  // Payments
  setVal('fMpesa', r.mpesa_amount); setVal('fMpesaTxn', r.mpesa_txns);
  setVal('fCash', r.cash_amount);
  setVal('fCredit', r.credit_amount); setVal('fCard', r.card_amount);

  // Expenses
  setVal('eWages', r.exp_wages); setVal('eElec', r.exp_elec); setVal('eWater', r.exp_water);
  setVal('eMaint', r.exp_maint); setVal('eLubes', r.exp_lubes); setVal('eMisc', r.exp_misc);

  // Itemised lists
  MISC_ITEMS = Array.isArray(r.misc_items) ? JSON.parse(JSON.stringify(r.misc_items)) : [];
  GAS_ITEMS  = Array.isArray(r.gas_items)  ? JSON.parse(JSON.stringify(r.gas_items))  : [];
  INVOICES   = Array.isArray(r.invoices)   ? JSON.parse(JSON.stringify(r.invoices))   : [];

  // KRA fields
  setVal('kPin', r.kra_pin); setVal('kBiz', r.kra_business); setVal('kPeriod', r.kra_period);
  setVal('kEtr', r.kra_etr); setVal('kRemarks', r.kra_remarks);
  if (r.kra_return_type && $('kReturnType')) $('kReturnType').value = r.kra_return_type;
  setVal('kBranch', r.kra_branch);
  setVal('fFloat', r.float_amount); setVal('fBanked', r.banked_amount);
  setVal('fMpesaTill', r.mpesa_till); setVal('fMpesaRef', r.mpesa_ref);
  setVal('fCreditCust', r.credit_customer);
  if (r.shift) setVal('fShift', r.shift);
  // A saved day keeps the operator who recorded its sales
  setVal('fOperator', r.operator || displayName(USER));

  // Credit field lock state follows invoice presence, same rule used elsewhere
  if ($('fCredit')) $('fCredit').readOnly = INVOICES.length > 0;

  renderMiscItems();
  renderGasItems();
  renderInvoices();
  calc();
}

// ── Clear the form for a fresh entry (used when switching to a date with no data) ─
function clearFormForNewEntry() {
  document.querySelectorAll('input[type="number"]').forEach(i => { if (!i.readOnly) i.value = ''; });
  ['kPin','kBiz','kPeriod','kEtr','fMpesaTill','fMpesaRef','fCreditCust','kRemarks']
    .forEach(id => $(id) && ($(id).value = ''));
  if ($('kReturnType')) $('kReturnType').value = 'vat';

  INVOICES = [];
  MISC_ITEMS = [];
  GAS_ITEMS = [];
  if ($('fCredit')) { $('fCredit').readOnly = false; $('fCredit').value = ''; }
  // eMisc is readonly (driven by MISC_ITEMS), so the loop above skips it
  if ($('eMisc')) $('eMisc').value = '';
  // A new day is recorded under whoever is signed in
  if ($('fOperator')) $('fOperator').value = displayName(USER);

  renderInvoices();
  renderMiscItems();
  renderGasItems();
  calc();
}

// ── Fires whenever the date or station changes: loads matching record, or resets ─
function loadRecordForDateAndStation() {
  const date = sv('fDate');
  const station = sv('fStation');
  if (!date || !station) return;

  const existing = RECS.find(r => r.report_date === date && r.station === station);

  if (existing) {
    populateFormFromRecord(existing);
    toast('📂 Loaded existing record for ' + date + ' · ' + station);
  } else {
    clearFormForNewEntry();
    toast('🆕 No record for ' + date + ' · ' + station + ' — ready for new entry');
  }
}

// ── Save record ───────────────────────────────────────────────────────────────
async function saveRec() {
  const c = COMP;
  if (!c || c.grossRev === undefined) {
    toast('⚠️ No data to save — fill in fuel readings first', true);
    return;
  }
  const now = Date.now();
  const rec = {
    id:           now,
    uid:          'rec_' + now,
    report_date:  sv('fDate') || new Date().toISOString().split('T')[0],
    station:      sv('fStation') || 'Unknown Station',
    // raw inputs
    petrol_open:  fv('pOpen'),   petrol_close: fv('pClose'),
    petrol_price: fv('pPrice'),  petrol_cost:  fv('pCost'),
    petrol_sales_open: fv('pSalesOpen'), petrol_sales_close: fv('pSalesClose'),
    diesel_open:  fv('dOpen'),   diesel_close: fv('dClose'),
    diesel_price: fv('dPrice'),  diesel_cost:  fv('dCost'),
    diesel_sales_open: fv('dSalesOpen'), diesel_sales_close: fv('dSalesClose'),
    kero_open:    fv('kOpen'),   kero_close:   fv('kClose'),
    kero_price:   fv('kPrice'),  kero_cost:    fv('kCost'),
    kero_sales_open: fv('kSalesOpen'), kero_sales_close: fv('kSalesClose'),
    petrol_dip_open: fv('pDipOpen'), petrol_delivered: fv('pDelivered'), petrol_dip_close: fv('pDipClose'),
    diesel_dip_open: fv('dDipOpen'), diesel_delivered: fv('dDelivered'), diesel_dip_close: fv('dDipClose'),
    kero_dip_open: fv('kDipOpen'), kero_delivered: fv('kDelivered'), kero_dip_close: fv('kDipClose'),
    mpesa_amount: fv('fMpesa'),  mpesa_txns:   fv('fMpesaTxn'),
    cash_amount:  fv('fCash'),   credit_amount:fv('fCredit'), card_amount:fv('fCard'),
    exp_wages:fv('eWages'), exp_elec:fv('eElec'), exp_water:fv('eWater'),
    exp_maint:fv('eMaint'), exp_lubes:fv('eLubes'), exp_misc:fv('eMisc'),
    misc_items: JSON.parse(JSON.stringify(MISC_ITEMS)),
    gas_items: JSON.parse(JSON.stringify(GAS_ITEMS)),
    gas_lube_revenue: COMP.gasLubeRev || 0,
    // computed (from C++ engine mirror)
    petrol_litres:  c.pL    || 0,
    diesel_litres:  c.dL    || 0,
    kero_litres:    c.kL    || 0,
    total_litres:   c.totalL|| 0,
    gross_revenue:  c.grossRev   || 0,
    total_expenses: c.totalExp   || 0,
    total_cost:     c.totalCost  || 0,
    net_profit:     c.netProfit  || 0,
    profit_margin:  c.profitMgn  || 0,
    output_vat:     c.outputVAT  || 0,
    total_excise:   c.totalExcise|| 0,
    total_tax:      c.totalTax   || 0,
    // KRA filing fields
    kra_pin:         sv('kPin'),
    kra_business:    sv('kBiz'),
    kra_period:      sv('kPeriod'),
    kra_etr:         sv('kEtr'),
    kra_return_type: sv('kReturnType'),
    kra_remarks:     sv('kRemarks'),
    invoices:        JSON.parse(JSON.stringify(INVOICES)), // snapshot of today's credit invoices
    kra_branch:      sv('kBranch'),
    float_amount:    fv('fFloat'),  banked_amount: fv('fBanked'),
    mpesa_till:      sv('fMpesaTill'), mpesa_ref: sv('fMpesaRef'),
    credit_customer: sv('fCreditCust'),
    operator:        sv('fOperator'), shift: sv('fShift'),
    created_at: new Date().toISOString()
  };

  if (!requireOnline('save records')) return;
  // Server recomputes all figures and upserts on date + station
  try {
    const res = await api('POST', '/records', rec);
    toast((res.created ? '✅ Record saved — ' : '✅ Record updated — ') + rec.report_date + ' · ' + rec.station);
    await refreshRecs();
  } catch (e) {
    toast('⚠️ Save failed: ' + e.message, true);
  }
}

// ── Station filter toggle ─────────────────────────────────────────────────────
function toggleStationFilter() {
  const sel = $('filterStation');
  if (!sel) return;
  const isHidden = sel.style.display === 'none';
  sel.style.display = isHidden ? 'block' : 'none';
  const btn = $('filterStationBtn');
  if (btn) btn.textContent = isHidden ? '✖ Hide Filter' : '🔽 Filter Branch';
  if (!isHidden) { sel.value = ''; loadRecs(); }
}

// ── Erase all records ─────────────────────────────────────────────────────────
async function eraseAllRecs() {
  if (!RECS.length) { toast('⚠️ No records to erase', true); return; }

  const filterBranch = $('filterStation') ? $('filterStation').value : '';
  const count = filterBranch
    ? RECS.filter(r => r.station === filterBranch).length
    : RECS.length;
  const target = filterBranch
    ? `all ${count} record${count !== 1 ? 's' : ''} for ${filterBranch}`
    : `ALL ${count} record${count !== 1 ? 's' : ''} from every branch`;

  const confirmed = await showConfirmModal(
    'Permanently Erase Records?',
    `This will delete ${target}.\n\nThis action CANNOT be undone.`
  );

  if (!confirmed) {
    toast('Erase cancelled — no records were deleted');
    return;
  }

  if (!requireOnline('erase records')) return;
  try {
    await api('DELETE', '/records' + (filterBranch ? '?station=' + encodeURIComponent(filterBranch) : ''));
    toast(filterBranch ? `🗑️ All records for ${filterBranch} erased` : '🗑️ All records erased from every branch');
    await refreshRecs();
  } catch (e) {
    toast('⚠️ Erase failed: ' + e.message, true);
  }
}

// ── Invoice / Credit Customer management ──────────────────────────────────────
function addInvoice() {
  const customer = sv('invCustomer');
  const company  = sv('invCompany');
  const carReg   = sv('invCarReg').toUpperCase();
  const phone    = sv('invPhone');
  const fuelType = sv('invFuelType');
  const amount   = fv('invAmount');

  if (!customer) { toast('⚠️ Enter customer name', true); return; }
  if (!amount || amount <= 0) { toast('⚠️ Enter a valid invoice amount', true); return; }

  const inv = {
    id: Date.now(),
    invoiceNo: 'INV-' + new Date().toISOString().slice(0,10).replace(/-/g,'') + '-' + String(INVOICES.length + 1).padStart(3,'0'),
    date: sv('fDate') || todayLocal(),
    station: sv('fStation'),
    customer, company, carReg, phone, fuelType, amount,
    created_at: new Date().toISOString()
  };

  INVOICES.push(inv);
  renderInvoices();
  syncInvoicesToCredit();

  // Clear the form for next entry
  ['invCustomer','invCompany','invCarReg','invPhone','invAmount'].forEach(id => { if($(id)) $(id).value=''; });
  toast('🧾 Invoice added for ' + customer);
}

async function deleteInvoice(id) {
  const ok = await showConfirmModal('Delete Invoice?', 'This invoice will be permanently removed.');
  if (!ok) return;
  INVOICES = INVOICES.filter(i => i.id !== id);
  renderInvoices();
  syncInvoicesToCredit();
  toast('🗑️ Invoice deleted');
}

function renderInvoices() {
  const tbody = $('invoiceBody');
  if (!tbody) return;

  if (!INVOICES.length) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:24px;color:var(--mut);font-family:var(--mono);font-size:.74rem">
      No invoices added yet — fill the form above.
    </td></tr>`;
  } else {
    tbody.innerHTML = INVOICES.map((inv, i) => `
      <tr>
        <td style="color:var(--mut)">${i+1}</td>
        <td>${esc(inv.customer)}</td>
        <td style="color:var(--acc2)">${esc(inv.company || '—')}</td>
        <td style="color:var(--blue)">${esc(inv.carReg || '—')}</td>
        <td>${esc(inv.phone || '—')}</td>
        <td>${esc(inv.fuelType)}</td>
        <td style="color:var(--purple)">KES ${fmt(inv.amount)}</td>
        <td><button class="btn btn-o" style="padding:3px 9px;font-size:.65rem" onclick="printInvoice(${Number(inv.id)})">🖨️</button></td>
        <td><button class="btn btn-r" style="padding:3px 9px;font-size:.65rem" onclick="deleteInvoice(${Number(inv.id)})">✕</button></td>
      </tr>
    `).join('');
  }

  const totalInvoiced = INVOICES.reduce((sum, inv) => sum + inv.amount, 0);
  if ($('invCount'))      $('invCount').textContent = INVOICES.length;
  if ($('invTotalDisp'))  $('invTotalDisp').textContent = 'KES ' + fmt(totalInvoiced);
}

// ── Push invoice total into the Credit Sales field automatically ─────────────
// ── Miscellaneous expenses: itemised list ─────────────────────────────────────
function toggleMiscPanel() {
  const panel = $('miscPanel');
  const icon  = $('miscToggleIcon');
  if (!panel) return;
  const isHidden = panel.style.display === 'none';
  panel.style.display = isHidden ? 'block' : 'none';
  if (icon) icon.textContent = isHidden ? '▾' : '▸';
  if (isHidden) {
    const nameField = $('miscItemName');
    if (nameField) nameField.focus();
  }
}

function addMiscItem() {
  const name   = sv('miscItemName');
  const amount = fv('miscItemAmount');

  if (!name) {
    toast('⚠️ Enter what the expense was for', true);
    $('miscItemName').focus();
    return;
  }
  if (amount <= 0) {
    toast('⚠️ Enter a valid amount', true);
    $('miscItemAmount').focus();
    return;
  }

  MISC_ITEMS.push({
    id: Date.now(),
    name: name,
    amount: r2(amount)
  });

  $('miscItemName').value = '';
  $('miscItemAmount').value = '';
  $('miscItemName').focus();

  renderMiscItems();
  syncMiscToExpenses();
  toast('✅ Added: ' + name + ' — KES ' + fmt(amount));
}

function deleteMiscItem(id) {
  MISC_ITEMS = MISC_ITEMS.filter(i => i.id !== id);
  renderMiscItems();
  syncMiscToExpenses();
}

function renderMiscItems() {
  const list = $('miscItemsList');
  if (!list) return;

  if (!MISC_ITEMS.length) {
    list.innerHTML = `<div style="font-family:var(--mono);font-size:.74rem;color:var(--mut);padding:6px 0">
      No items yet — add each miscellaneous expense below.</div>`;
  } else {
    list.innerHTML = MISC_ITEMS.map(item => `
      <div style="display:flex;align-items:center;gap:10px;background:var(--s2);
        border:1px solid var(--brd);border-radius:7px;padding:8px 12px;font-family:var(--mono);font-size:.78rem">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--txt)" title="${esc(item.name)}">${esc(item.name)}</span>
        <span style="flex-shrink:0;color:var(--acc2);font-weight:600">KES ${fmt(item.amount)}</span>
        <button class="btn btn-r" style="flex-shrink:0;padding:2px 8px;font-size:.62rem"
          onclick="deleteMiscItem(${Number(item.id)})">✕</button>
      </div>
    `).join('');
  }

  const total = MISC_ITEMS.reduce((sum, i) => sum + i.amount, 0);
  $('miscItemCount').textContent = MISC_ITEMS.length;
  $('miscItemsTotal').textContent = 'KES ' + fmt(total);
}

function syncMiscToExpenses() {
  const total = r2(MISC_ITEMS.reduce((sum, i) => sum + i.amount, 0));
  const miscField = $('eMisc');
  if (miscField) miscField.value = total > 0 ? total.toFixed(2) : '';
  calc();
}

// ── Gases & Lubricants: itemised sales list ───────────────────────────────────
function addGasItem() {
  const name  = sv('gasItemName');
  const cat   = sv('gasItemCategory') || 'Gas';
  const qty   = fv('gasItemQty') || 1;
  const price = fv('gasItemPrice');

  if (!name) {
    toast('⚠️ Enter the product name', true);
    $('gasItemName').focus();
    return;
  }
  if (qty <= 0) {
    toast('⚠️ Quantity must be at least 1', true);
    $('gasItemQty').focus();
    return;
  }
  if (price <= 0) {
    toast('⚠️ Enter a valid unit price', true);
    $('gasItemPrice').focus();
    return;
  }

  GAS_ITEMS.push({
    id: Date.now(),
    name: name,
    category: cat,
    qty: qty,
    price: r2(price)
  });

  $('gasItemName').value = '';
  $('gasItemQty').value = '1';
  $('gasItemPrice').value = '';
  $('gasItemName').focus();

  renderGasItems();
  calc();
  toast('✅ Added: ' + qty + ' × ' + name + ' — KES ' + fmt(qty * price));
}

function deleteGasItem(id) {
  GAS_ITEMS = GAS_ITEMS.filter(i => i.id !== id);
  renderGasItems();
  calc();
}

function renderGasItems() {
  const list = $('gasItemsList');
  if (!list) return;

  if (!GAS_ITEMS.length) {
    list.innerHTML = `<div style="font-family:var(--mono);font-size:.74rem;color:var(--mut);padding:6px 0">
      No items yet — add each gas cylinder or lubricant product sold today.</div>`;
  } else {
    list.innerHTML = GAS_ITEMS.map(item => {
      const lineTotal = item.qty * item.price;
      const catColor = item.category === 'Gas' ? 'var(--acc2)'
                      : item.category === 'Lubricant' ? 'var(--blue)' : 'var(--purple)';
      return `
      <div style="display:flex;align-items:center;gap:10px;background:var(--s2);
        border:1px solid var(--brd);border-radius:7px;padding:8px 12px;font-family:var(--mono);font-size:.78rem">
        <span style="flex:2;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--txt)" title="${esc(item.name)}">${esc(item.name)}</span>
        <span style="flex-shrink:0;color:${catColor};font-size:.68rem;border:1px solid ${catColor};
          border-radius:4px;padding:1px 7px;text-align:center">${esc(item.category)}</span>
        <span style="flex-shrink:0;color:var(--mut);white-space:nowrap">${esc(item.qty)} × KES ${fmt(item.price)}</span>
        <span style="flex-shrink:0;color:var(--acc2);font-weight:600;text-align:right;white-space:nowrap">KES ${fmt(lineTotal)}</span>
        <button class="btn btn-r" style="flex-shrink:0;padding:2px 8px;font-size:.62rem"
          onclick="deleteGasItem(${Number(item.id)})">✕</button>
      </div>`;
    }).join('');
  }

  const gasTotal  = GAS_ITEMS.filter(i => i.category === 'Gas').reduce((s,i)=>s+(i.qty*i.price),0);
  const lubeTotal = GAS_ITEMS.filter(i => i.category === 'Lubricant').reduce((s,i)=>s+(i.qty*i.price),0);
  const grandTotal = GAS_ITEMS.reduce((s,i)=>s+(i.qty*i.price),0);

  $('gasItemCount').textContent = GAS_ITEMS.length;
  $('gasCategoryTotal').textContent = 'KES ' + fmt(gasTotal);
  $('lubeCategoryTotal').textContent = 'KES ' + fmt(lubeTotal);
  $('gasItemsTotal').textContent = 'KES ' + fmt(grandTotal);
}

function syncInvoicesToCredit() {
  const totalInvoiced = INVOICES.reduce((sum, inv) => sum + inv.amount, 0);
  const creditField = $('fCredit');
  if (creditField) {
    creditField.value = totalInvoiced > 0 ? totalInvoiced.toFixed(2) : '';
    creditField.readOnly = INVOICES.length > 0;
  }
  // Reflect customer names in the credit customer field
  const custField = $('fCreditCust');
  if (custField && INVOICES.length) {
    custField.value = INVOICES.map(i => i.customer).join(', ');
  }
  calc();
}

// ── Print a single invoice as a clean receipt ─────────────────────────────────
function printInvoice(id) {
  const inv = INVOICES.find(i => i.id === id);
  if (!inv) return;

  const win = window.open('', '_blank', 'width=420,height=600');
  win.document.write(`
    <html><head><title>${esc(inv.invoiceNo)}</title>
    <style>
      body{font-family:'IBM Plex Mono',monospace;padding:24px;color:#111;max-width:380px;margin:0 auto}
      h1{font-size:16px;margin:0 0 2px}
      .sub{font-size:11px;color:#666;margin-bottom:16px}
      .row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #ddd;font-size:12.5px}
      .row span:last-child{font-weight:600}
      .total{margin-top:14px;padding-top:10px;border-top:2px solid #111;font-size:15px;display:flex;justify-content:space-between;font-weight:700}
      .foot{margin-top:24px;font-size:10px;color:#888;text-align:center}
    </style></head>
    <body>
      <h1>⛽ ${esc(inv.station || 'PetroKenya')}</h1>
      <div class="sub">Fuel Credit Invoice &nbsp;·&nbsp; ${esc(inv.invoiceNo)}</div>
      <div class="row"><span>Date</span><span>${esc(inv.date)}</span></div>
      <div class="row"><span>Customer Name</span><span>${esc(inv.customer)}</span></div>
      <div class="row"><span>Company</span><span>${esc(inv.company || '—')}</span></div>
      <div class="row"><span>Car Reg. No.</span><span>${esc(inv.carReg || '—')}</span></div>
      <div class="row"><span>Phone Number</span><span>${esc(inv.phone || '—')}</span></div>
      <div class="row"><span>Fuel Type</span><span>${esc(inv.fuelType)}</span></div>
      <div class="total"><span>AMOUNT DUE</span><span>KES ${fmt(inv.amount)}</span></div>
      <div class="foot">Thank you for fuelling with us.<br>This is a credit invoice — payment terms apply.</div>
    </body></html>
  `);
  win.document.close();
  win.focus();
  setTimeout(() => win.print(), 300);
}

// ── Load & render records table ───────────────────────────────────────────────
function loadRecs() {

  // Apply branch filter if active
  const filterBranch = $('filterStation') ? $('filterStation').value : '';
  const display = filterBranch ? RECS.filter(r => r.station === filterBranch) : RECS;

  const countEl = $('recCount');
  if (countEl) {
    countEl.textContent = filterBranch
      ? `${display.length} of ${RECS.length} record${RECS.length !== 1 ? 's' : ''} (${filterBranch})`
      : `${RECS.length} record${RECS.length !== 1 ? 's' : ''} — all branches`;
  }

  const tbody = $('recBody');
  if (!tbody) return;

  if (!display.length) {
    tbody.innerHTML = `<tr><td colspan="15" style="text-align:center;padding:36px;
      color:var(--mut);font-family:var(--mono);font-size:.76rem">
      ${filterBranch ? `No records for ${esc(filterBranch)} yet.` : 'No records saved yet.'}<br>
      Fill in today\'s data and click 💾 Save Daily Record.
    </td></tr>`;
    return;
  }

  try {
    tbody.innerHTML = display.map(r => {
      const idLabel   = r.uid ? r.uid.slice(-6) : String(r.id).slice(-6);
      const profit    = Number(r.net_profit  || 0);
      const vat       = Number(r.output_vat  || 0);
      const tax       = Number(r.total_tax   || 0);
      const mpesa     = Number(r.mpesa_amount|| 0);
      const cash      = Number(r.cash_amount || 0);
      const expenses  = Number(r.total_expenses || 0);
      const gross     = Number(r.gross_revenue  || 0);
      const pLit      = Number(r.petrol_litres  || 0);
      const dLit      = Number(r.diesel_litres  || 0);
      const totLit    = Number(r.total_litres   || 0);
      const safeId    = r.id;

      return `<tr data-id="${safeId}" onclick="openDetail(event, ${safeId})" style="cursor:pointer">
        <td style="color:var(--mut)">${idLabel}</td>
        <td>${esc(r.report_date || '—')}</td>
        <td style="color:var(--acc2);max-width:120px;overflow:hidden;text-overflow:ellipsis">${esc(r.station || '—')}</td>
        <td>${esc(r.operator || '—')}</td>
        <td>${fmtL(pLit)}</td>
        <td>${fmtL(dLit)}</td>
        <td style="color:var(--blue)">${fmtL(totLit)}</td>
        <td style="color:var(--acc2)">KES ${fmt(gross)}</td>
        <td style="color:#00a651">KES ${fmt(mpesa)}</td>
        <td>KES ${fmt(cash)}</td>
        <td style="color:var(--red)">KES ${fmt(expenses)}</td>
        <td><span class="badge ${profit >= 0 ? 'bg' : 'br'}">KES ${fmt(profit)}</span></td>
        <td>KES ${fmt(vat)}</td>
        <td style="color:var(--acc2)">KES ${fmt(tax)}</td>
        <td>
          <button class="btn btn-r btn-xs" data-id="${safeId}"
            onclick="event.stopPropagation();delRec(this)">✕</button>
        </td>
      </tr>`;
    }).join('');
  } catch(e) {
    console.error('loadRecs render error:', e);
    tbody.innerHTML = `<tr><td colspan="15" style="color:var(--red);padding:20px;font-family:var(--mono);font-size:.74rem">
      Render error: ${esc(e.message)}
    </td></tr>`;
  }
}

// ── Delete a record ───────────────────────────────────────────────────────────
async function delRec(btn) {
  const ok = await showConfirmModal('Delete Record?', 'This record will be permanently deleted.');
  if (!ok) return;

  const id = Number(btn.dataset.id);
  if (isNaN(id)) {
    toast('⚠️ Could not identify this record', true);
    return;
  }

  if (!requireOnline('delete records')) return;
  try {
    await api('DELETE', '/records/' + id);
    toast('🗑️ Record deleted');
  } catch (e) {
    toast('⚠️ Delete failed: ' + e.message, true);
  }
  await refreshRecs();
}

// ── Open the full detail view for one record ──────────────────────────────────
let DETAIL_ID = null; // tracks which record is currently shown, for delete-from-modal

function openDetail(evt, id) {
  // Ignore the click if it originated from the delete button (handled separately)
  if (evt && evt.target && evt.target.closest && evt.target.closest('button')) return;

  const r = RECS.find(rec => Number(rec.id) === Number(id));
  if (!r) { toast('⚠️ Record not found', true); return; }

  DETAIL_ID = Number(id);

  const num = v => Number(v || 0);

  // Header
  $('rdStation').textContent = r.station || 'Unknown Station';
  $('rdDate').textContent = r.report_date
    ? new Date(r.report_date + 'T00:00:00').toLocaleDateString('en-KE',
        { weekday:'long', day:'numeric', month:'long', year:'numeric' })
    : (r.created_at ? new Date(r.created_at).toLocaleDateString('en-KE') : '—');
  if (r.operator) $('rdDate').textContent += '  ·  Operator: ' + r.operator;
  if (r.last_edited_by && r.last_edited_by !== r.operator) $('rdDate').textContent += '  ·  Last changed by: ' + r.last_edited_by;

  // Fuel
  $('rdPetrol').textContent = fmtL(num(r.petrol_litres)) + ' L  ·  KES ' + fmt(num(r.petrol_litres) * num(r.petrol_price));
  $('rdDiesel').textContent = fmtL(num(r.diesel_litres)) + ' L  ·  KES ' + fmt(num(r.diesel_litres) * num(r.diesel_price));
  $('rdKero').textContent   = fmtL(num(r.kero_litres))   + ' L  ·  KES ' + fmt(num(r.kero_litres) * num(r.kero_price));
  $('rdTotalL').textContent = fmtL(num(r.total_litres)) + ' L';

  // Pump sales meter cross-check (only shown if that data was recorded)
  const pumpSec = $('rdPumpSec'), pumpList = $('rdPumpItems');
  const fuelPumpData = [
    { label: 'Petrol', open: r.petrol_sales_open, close: r.petrol_sales_close, calcRev: num(r.petrol_litres) * num(r.petrol_price) },
    { label: 'Diesel', open: r.diesel_sales_open, close: r.diesel_sales_close, calcRev: num(r.diesel_litres) * num(r.diesel_price) },
    { label: 'Kerosene', open: r.kero_sales_open, close: r.kero_sales_close, calcRev: num(r.kero_litres) * num(r.kero_price) },
  ].filter(f => num(f.open) || num(f.close));

  if (fuelPumpData.length) {
    pumpSec.style.display = 'block';
    pumpList.innerHTML = fuelPumpData.map(f => {
      const pumpTotal = Math.max(0, num(f.close) - num(f.open));
      const variance = r2(f.calcRev - pumpTotal);
      const color = Math.abs(variance) < 1 ? 'var(--acc)' : Math.abs(variance) < 50 ? 'var(--acc2)' : 'var(--red)';
      return `<div class="m-row"><span>${f.label} — Pump Sales Total</span><span>KES ${fmt(pumpTotal)}</span></div>
              <div class="m-row" style="padding-left:12px;font-size:.72rem"><span>↳ vs Calculated Revenue</span><span style="color:${color}">Variance: KES ${fmt(variance)}</span></div>`;
    }).join('');
  } else {
    pumpSec.style.display = 'none';
  }

  // Tank dip reconciliation (only shown if dip data was recorded)
  const dipSec = $('rdDipSec'), dipList = $('rdDipItems');
  const fuelDipData = [
    { label: 'Petrol', open: r.petrol_dip_open, delivered: r.petrol_delivered, close: r.petrol_dip_close, sold: num(r.petrol_litres) },
    { label: 'Diesel', open: r.diesel_dip_open, delivered: r.diesel_delivered, close: r.diesel_dip_close, sold: num(r.diesel_litres) },
    { label: 'Kerosene', open: r.kero_dip_open, delivered: r.kero_delivered, close: r.kero_dip_close, sold: num(r.kero_litres) },
  ].filter(f => num(f.open) || num(f.close));

  if (fuelDipData.length) {
    dipSec.style.display = 'block';
    dipList.innerHTML = fuelDipData.map(f => {
      const expected = r2(num(f.open) + num(f.delivered) - f.sold);
      const variance = r2(num(f.close) - expected);
      const color = Math.abs(variance) < 10 ? 'var(--acc)' : variance < 0 ? 'var(--red)' : 'var(--acc2)';
      const varLabel = Math.abs(variance) < 10 ? 'Within tolerance' : variance < 0 ? 'Stock short' : 'Stock surplus';
      return `<div class="m-row"><span>${f.label} — Expected Stock</span><span>${fmtL(expected)} L</span></div>
              <div class="m-row"><span>${f.label} — Closing Dip</span><span>${fmtL(num(f.close))} L</span></div>
              <div class="m-row" style="padding-left:12px;font-size:.72rem"><span>↳ ${varLabel}</span><span style="color:${color}">Variance: ${fmtL(variance)} L</span></div>`;
    }).join('');
  } else {
    dipSec.style.display = 'none';
  }

  // Gases & Lubricants (only show section if items exist)
  const gasSec = $('rdGasSec'), gasList = $('rdGasItems');
  if (r.gas_items && r.gas_items.length) {
    gasSec.style.display = 'block';
    gasList.innerHTML = r.gas_items.map(i =>
      `<div class="m-row"><span>${esc(i.name)} (${esc(i.category)}) — ${esc(i.qty)} × KES ${fmt(i.price)}</span><span>KES ${fmt(i.qty * i.price)}</span></div>`
    ).join('') + `<div class="m-row" style="border-top:1px solid var(--brd);margin-top:4px;padding-top:6px">
      <span><b>Gas/Lube Revenue</b></span><span>KES ${fmt(r.gas_lube_revenue || 0)}</span></div>`;
  } else {
    gasSec.style.display = 'none';
  }

  // Payments
  const collected = num(r.mpesa_amount) + num(r.cash_amount) + num(r.credit_amount) + num(r.card_amount);
  $('rdMpesa').textContent  = 'KES ' + fmt(num(r.mpesa_amount));
  $('rdCash').textContent   = 'KES ' + fmt(num(r.cash_amount));
  $('rdCredit').textContent = 'KES ' + fmt(num(r.credit_amount));
  $('rdCard').textContent   = 'KES ' + fmt(num(r.card_amount));
  $('rdTotalCollected').textContent = 'KES ' + fmt(collected);

  // Credit invoices (only show section if any exist)
  const invSec = $('rdInvoiceSec'), invList = $('rdInvoiceItems');
  if (r.invoices && r.invoices.length) {
    invSec.style.display = 'block';
    invList.innerHTML = r.invoices.map(inv =>
      `<div class="m-row"><span>${esc(inv.customer || '—')}${esc(inv.company ? ' · ' + inv.company : '')}${esc(inv.carReg ? ' · ' + inv.carReg : '')}</span><span>KES ${fmt(inv.amount || 0)}</span></div>`
    ).join('');
  } else {
    invSec.style.display = 'none';
  }

  // Expenses
  $('rdWages').textContent = 'KES ' + fmt(num(r.exp_wages));
  $('rdElec').textContent  = 'KES ' + fmt(num(r.exp_elec));
  $('rdWater').textContent = 'KES ' + fmt(num(r.exp_water));
  $('rdMaint').textContent = 'KES ' + fmt(num(r.exp_maint));
  $('rdLubes').textContent = 'KES ' + fmt(num(r.exp_lubes));
  $('rdMisc').textContent  = 'KES ' + fmt(num(r.exp_misc));

  const miscList = $('rdMiscItems');
  if (r.misc_items && r.misc_items.length) {
    miscList.innerHTML = r.misc_items.map(i =>
      `<div class="m-row" style="padding-left:12px;font-size:.72rem;color:var(--mut)"><span>↳ ${esc(i.name)}</span><span>KES ${fmt(i.amount)}</span></div>`
    ).join('');
  } else {
    miscList.innerHTML = '';
  }

  $('rdTotalExp').textContent = 'KES ' + fmt(num(r.total_expenses));

  // P&L
  $('rdGross').textContent     = 'KES ' + fmt(num(r.gross_revenue));
  $('rdTotalCost').textContent = 'KES ' + fmt(num(r.total_cost));
  $('rdProfit').textContent    = 'KES ' + fmt(num(r.net_profit));
  $('rdProfit').style.color    = num(r.net_profit) >= 0 ? 'var(--acc)' : 'var(--red)';
  $('rdMargin').textContent    = (num(r.profit_margin)).toFixed(1) + '%';

  // KRA
  $('rdVat').textContent    = 'KES ' + fmt(num(r.output_vat));
  $('rdExcise').textContent = 'KES ' + fmt(num(r.total_excise));
  $('rdTax').textContent    = 'KES ' + fmt(num(r.total_tax));
  $('rdKraPin').textContent  = r.kra_pin || '—';
  $('rdKraType').textContent = r.kra_return_type || '—';

  // Remarks
  const remarksSec = $('rdRemarksSec');
  if (r.kra_remarks) {
    remarksSec.style.display = 'block';
    $('rdRemarks').textContent = r.kra_remarks;
  } else {
    remarksSec.style.display = 'none';
  }

  $('detailOverlay').classList.add('open');
}

function closeDetail() {
  $('detailOverlay').classList.remove('open');
  DETAIL_ID = null;
}

// ── In-page confirm modal: replaces native confirm() which some browsers ──────
// silently suppress or auto-dismiss with no visible feedback to the user.
let _confirmResolve = null;

function showConfirmModal(title, message) {
  return new Promise(resolve => {
    $('confirmTitle').textContent = title;
    $('confirmMessage').textContent = message;
    _confirmResolve = resolve;
    $('confirmOverlay').classList.add('open');
  });
}

function closeConfirmModal(result) {
  $('confirmOverlay').classList.remove('open');
  if (_confirmResolve) {
    _confirmResolve(result);
    _confirmResolve = null;
  }
}

async function deleteFromDetail() {
  if (DETAIL_ID === null) return;
  const ok = await showConfirmModal('Delete Record?', 'This record will be permanently deleted.');
  if (!ok) return;

  if (!requireOnline('delete records')) return;
  try {
    await api('DELETE', '/records/' + DETAIL_ID);
    toast('🗑️ Record deleted');
  } catch (e) {
    toast('⚠️ Delete failed: ' + e.message, true);
  }
  closeDetail();
  await refreshRecs();
}

// ── Analytics ─────────────────────────────────────────────────────────────────
function renderAnalytics() {

  // Last 7 days — sort newest first, take up to 7, reverse for chart order
  const sorted = [...RECS].sort((a, b) => {
    const da = a.report_date || a.created_at || '';
    const db = b.report_date || b.created_at || '';
    return db.localeCompare(da);
  });
  const week = sorted.slice(0, 7).reverse();

  renderBars('chartRev',    week, 'gross_revenue',  'var(--acc)');
  renderBars('chartProfit', week, 'net_profit',     'var(--purple)');
  renderBars('chartLit',    week, 'total_litres',   'var(--blue)');

  // All-time totals
  const totals = RECS.reduce((a, r) => {
    a.rev    += Number(r.gross_revenue  || 0);
    a.lit    += Number(r.total_litres   || 0);
    a.profit += Number(r.net_profit     || 0);
    a.tax    += Number(r.total_tax      || 0);
    a.mpesa  += Number(r.mpesa_amount   || 0);
    a.cash   += Number(r.cash_amount    || 0);
    a.days   += 1;
    return a;
  }, { rev:0, lit:0, profit:0, tax:0, mpesa:0, cash:0, days:0 });

  const allTimeEl = $('allTime');
  if (!allTimeEl) return;

  if (!RECS.length) {
    allTimeEl.innerHTML = `<div style="color:var(--mut);font-family:var(--mono);font-size:.74rem;padding:8px">
      No records yet — save a daily entry first.</div>`;
    return;
  }

  allTimeEl.innerHTML = [
    ['Days Recorded',      totals.days],
    ['Total Revenue',      'KES ' + fmt(totals.rev)],
    ['Total Litres Sold',  fmt(totals.lit) + ' L'],
    ['Total Net Profit',   'KES ' + fmt(totals.profit)],
    ['Avg Daily Profit',   'KES ' + fmt(totals.days > 0 ? totals.profit / totals.days : 0)],
    ['Total M-Pesa',       'KES ' + fmt(totals.mpesa)],
    ['Total Cash',         'KES ' + fmt(totals.cash)],
    ['Total KRA Tax',      'KES ' + fmt(totals.tax)],
  ].map(([k, v]) =>
    `<div class="stat-row"><span>${k}</span><span>${v}</span></div>`
  ).join('');
}

// ── Bar chart renderer ────────────────────────────────────────────────────────
function renderBars(id, data, field, color) {
  const el = $(id);
  if (!el) return;

  if (!data || !data.length) {
    el.innerHTML = `<div style="color:var(--mut);font-family:var(--mono);
      font-size:.74rem;padding:8px 0">No data yet — save records to see charts.</div>`;
    return;
  }

  const max = Math.max(...data.map(r => Math.abs(Number(r[field]) || 0)), 1);

  el.innerHTML = data.map(r => {
    const v   = Number(r[field]) || 0;
    const pct = Math.min((Math.abs(v) / max * 100), 100).toFixed(1);
    const lbl = (r.report_date || '').substring(5) || '—';
    const barColor = v < 0 ? 'var(--red)' : color;
    return `<div class="cb-row">
      <div class="cb-lbl" title="${esc(r.report_date || '')}">${esc(lbl)}</div>
      <div class="cb-track">
        <div class="cb-fill" style="width:${pct}%;background:${barColor}"></div>
      </div>
      <div class="cb-val">${v < 0 ? '-' : ''}${fmt(Math.abs(v))}</div>
    </div>`;
  }).join('');
}

// ── KRA Modal ─────────────────────────────────────────────────────────────────
function openKra() {
  const c = COMP;
  $('mPin').textContent    = sv('kPin')||'—';
  $('mBiz').textContent    = sv('kBiz')||'—';
  $('mPeriod').textContent = sv('kPeriod')||'—';
  $('mRType').textContent  = sv('kReturnType')||'—';
  $('mEtr').textContent    = sv('kEtr')||'—';
  $('mDate').textContent   = sv('fDate')||new Date().toLocaleDateString('en-KE');
  $('mPL').textContent     = fmtL(c.pL)+' L';
  $('mDL').textContent     = fmtL(c.dL)+' L';
  $('mKL').textContent     = fmtL(c.kL)+' L';
  $('mGross').textContent  = 'KES '+fmt(c.grossRev);
  $('mExVat').textContent  = 'KES '+fmt(c.exVat);
  $('mVat').textContent    = 'KES '+fmt(c.outputVAT);
  $('mExcP').textContent   = 'KES '+fmt(c.excP);
  $('mExcD').textContent   = 'KES '+fmt(c.excD);
  $('mExcK').textContent   = 'KES '+fmt(c.excK);
  $('mRLP').textContent    = 'KES '+fmt(c.rlP);
  $('mRLD').textContent    = 'KES '+fmt(c.rlD);
  $('mTotal').textContent  = 'KES '+fmt(c.totalTax);
  $('mRemarks').textContent = sv('kRemarks')||'None';
  $('kraOverlay').classList.add('open');
}
function closeKra() { $('kraOverlay').classList.remove('open'); }
$('kraOverlay').addEventListener('click', e => { if(e.target===$('kraOverlay')) closeKra(); });
$('detailOverlay').addEventListener('click', e => { if(e.target===$('detailOverlay')) closeDetail(); });
$('confirmOverlay').addEventListener('click', e => { if(e.target===$('confirmOverlay')) closeConfirmModal(false); });
$('accountOverlay').addEventListener('click', e => { if(e.target===$('accountOverlay')) closeAccount(); });

// ── Export CSV ────────────────────────────────────────────────────────────────
function exportCSV() {
  if (!RECS.length) { toast('⚠️ No records to export yet', true); return; }
  const cols = [
    'report_date','station','operator',
    'petrol_litres','diesel_litres','kero_litres','total_litres',
    'gross_revenue','mpesa_amount','cash_amount','credit_amount','card_amount',
    'total_expenses','net_profit','profit_margin',
    'output_vat','total_excise','total_tax',
    'kra_pin','kra_business','kra_period','kra_return_type',
    'created_at'
  ];
  const escape = v => {
    if (v === null || v === undefined) return '';
    let s = String(v);
    if (typeof v === 'string' && /^[=+\-@\t\r]/.test(s)) s = "'" + s;
    return s.includes(',') || s.includes('"') || s.includes('\n')
      ? '"' + s.replace(/"/g, '""') + '"'
      : s;
  };
  const hdr  = cols.join(',');
  const rows  = RECS.map(r => cols.map(c => escape(r[c])).join(','));
  const blob  = new Blob([hdr + '\n' + rows.join('\n')], { type: 'text/csv' });
  const a     = document.createElement('a');
  a.href      = URL.createObjectURL(blob);
  a.download  = `PetroKenya_${sv('fStation').replace(/\s+/g,'-') || 'Station'}_${new Date().toISOString().split('T')[0]}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
  toast('📥 CSV exported — ' + RECS.length + ' records');
}

// ── Clear ─────────────────────────────────────────────────────────────────────
async function clearAll() {
  const ok = await showConfirmModal(
    'Clear All Form Data?',
    "This also clears today's invoice, misc, and gas/lube item lists."
  );
  if (!ok) return;
  document.querySelectorAll('input[type="number"]').forEach(i => { if(!i.readOnly) i.value=''; });
  ['kPin','kBiz','kPeriod','kBranch','kEtr','fMpesaTill','fMpesaRef','fCreditCust','kRemarks']
    .forEach(id => $(id) && ($(id).value=''));
  INVOICES = [];
  MISC_ITEMS = [];
  GAS_ITEMS = [];
  if ($('fCredit')) { $('fCredit').readOnly = false; $('fCredit').value = ''; }
  if ($('eMisc')) { $('eMisc').value = ''; }
  renderInvoices();
  renderMiscItems();
  renderGasItems();
  calc();
  toast('🗑️ Form, invoices, misc items, and gas/lube sales cleared');
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, isErr=false) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show' + (isErr?' e':'');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.className = '', 3200);
}

// ── Init ──────────────────────────────────────────────────────────────────────
// ── Get today's date in local time as YYYY-MM-DD (not UTC — avoids off-by-one) ──
function todayLocal() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

// ── Apply today's date to the date field + header ────────────────────────────
function applyTodayDate() {
  const dateEl = $('fDate');
  if (dateEl) dateEl.value = todayLocal();
  const now = new Date();
  $('hDate').textContent = now.toLocaleDateString('en-KE',
    { weekday:'short', day:'numeric', month:'short', year:'numeric' });
}

// ── Watch for midnight rollover while the app stays open ─────────────────────
function startMidnightWatcher() {
  let lastKnownDate = todayLocal();
  setInterval(() => {
    const nowDate = todayLocal();
    if (nowDate !== lastKnownDate) {
      lastKnownDate = nowDate;
      applyTodayDate();
      loadRecordForDateAndStation();
      toast('📅 New day started — date auto-updated to ' + nowDate);
    }
  }, 60000); // check every minute
}

// ── Installable app: service worker caches the app shell for offline start ────
function registerServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(e => console.warn('SW registration failed:', e));
  }
}

// Android/Chrome "Install app" prompt → show our own install button
let installPrompt = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  installPrompt = e;
  $('installBtn').style.display = '';
});
async function installApp() {
  if (!installPrompt) return;
  installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  $('installBtn').style.display = 'none';
}

async function init() {
  // Auto-set today's date (local time, Kenya-correct)
  applyTodayDate();
  $('hStation').textContent = sv('fStation') || 'PetroKenya';
  registerServiceWorker();
  renderInvoices();
  renderMiscItems();
  renderGasItems();
  calc();

  // Who is signed in? Offline, fall back to the last signed-in user (read-only)
  try {
    USER = (await api('GET', '/me')).user;
    storeSet('pk_user', USER);
  } catch (e) {
    if (ONLINE) return;            // 401 → api() already opened the sign-in screen
    USER = storeGet('pk_user');
    if (!USER) { showLogin('You are offline. Connect to the internet to sign in.'); return; }
  }
  await startSession();
}

// Keep the date correct automatically if the app is left open overnight
startMidnightWatcher();

// Re-sync date and records when the app comes back to the foreground (phone unlock, tab switch)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible' || !USER) return;
  const nowDate = todayLocal();
  if ($('fDate') && $('fDate').value !== nowDate && !$('fDate').dataset.userEdited) {
    applyTodayDate();
    loadRecordForDateAndStation();
  }
  refreshRecs();
});
window.addEventListener('online', () => { if (USER) refreshRecs(); });
window.addEventListener('offline', () => setOnline(false));

init();
