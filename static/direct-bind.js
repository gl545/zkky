(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const PROXY_DRAFT_KEY = 'direct-bind:proxy-draft:v5';
  const ACCOUNT_LIST_KEY = 'direct-bind:account-list:v2';
  const CARD_DECLINE_MESSAGE = '绑卡未完成：发卡行拒绝了卡片，账号没有新增支付方式。';
  const TASK_POLL_MS = 400;
  const MAX_BATCH_CONCURRENCY = 50;
  const MAX_PROXY_FALLBACKS = 4;
  const ACCOUNT_SAVE_DEBOUNCE_MS = 120;
  let stripeJsPromise = null;
  let accountSaveTimer = 0;
  let accountRenderFrame = 0;
  const billingFetchPromises = new Map();
  const state = {
    stripe: null,
    publishableKey: '',
    elements: {},
    complete: { number: false, expiry: false, cvc: false },
    mounted: false,
    running: false,
    stop: false,
    accounts: [],
    nextId: 1,
    bindProxyCursor: 0,
    promoProxyCursor: 0,
  };

  // Open-source cleanup migration: discard drafts created by earlier private
  // builds, which may contain access tokens, proxy credentials and task state.
  try {
    for (let index = localStorage.length - 1; index >= 0; index -= 1) {
      const key = localStorage.key(index) || '';
      if (key.startsWith('direct-bind:') && ![PROXY_DRAFT_KEY, ACCOUNT_LIST_KEY].includes(key)) {
        localStorage.removeItem(key);
      }
    }
  } catch (_) {}

  function splitValues(value) {
    return [...new Set(String(value || '').split(/[\r\n,;]+/).map((item) => item.trim()).filter(Boolean))];
  }

  function parseProxyPool(value) {
    const source = String(value || '').trim();
    if (!source) return [];
    let values = [];
    if (source.startsWith('[')) {
      try {
        const parsed = JSON.parse(source);
        if (Array.isArray(parsed)) values = parsed;
      } catch (_) {}
    }
    if (!values.length) values = source.split(/[\s,;，；]+/);
    return [...new Set(values
      .map((item) => String(item || '').trim().replace(/^["']|["']$/g, ''))
      .filter(Boolean))].slice(0, 500);
  }

  function extractTokens(text) {
    const source = String(text || '');
    const isImportable = (token) => {
      const decoded = decodeAccount(token);
      return Boolean(decoded.accountId || decoded.userId || decoded.email);
    };
    const namedAccessTokens = [...source.matchAll(
      /["']access_token["']\s*:\s*["'](eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)["']/gi,
    )].map((match) => match[1]);
    if (namedAccessTokens.length) return [...new Set(namedAccessTokens.filter(isImportable))];
    const jwtMatches = source.match(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/g);
    const candidates = jwtMatches?.length
      ? [...new Set(jwtMatches)]
      : splitValues(source).map((item) => item.replace(/^["']|["']$/g, '')).filter(Boolean);
    return candidates.filter(isImportable);
  }

  function decodeAccount(token) {
    try {
      const part = String(token).split('.')[1];
      if (!part) return {};
      const raw = part.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(part.length / 4) * 4, '=');
      const claims = JSON.parse(decodeURIComponent(Array.from(atob(raw), (ch) => `%${ch.charCodeAt(0).toString(16).padStart(2, '0')}`).join('')));
      const profile = claims['https://api.openai.com/profile'] || {};
      const auth = claims['https://api.openai.com/auth'] || {};
      return {
        email: String(profile.email || claims.email || ''),
        accountId: String(auth.chatgpt_account_id || auth.account_id || claims.chatgpt_account_id || claims.account_id || ''),
        userId: String(auth.user_id || claims.user_id || claims.sub || ''),
      };
    } catch (_) {
      return {};
    }
  }

  function maskToken(value) {
    const text = String(value || '');
    return text.length > 24 ? `${text.slice(0, 12)}…${text.slice(-8)}` : text;
  }

  async function copyText(value) {
    const text = String(value || '').trim();
    if (!text) return false;
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const field = document.createElement('textarea');
      field.value = text;
      field.setAttribute('readonly', '');
      field.style.position = 'fixed';
      field.style.opacity = '0';
      document.body.appendChild(field);
      field.select();
      const copied = document.execCommand('copy');
      field.remove();
      return copied;
    }
  }

  function canonicalCheckoutLink(value = {}, fallback = '') {
    const direct = String(value.checkout_link || value.checkoutLink || value.link || fallback || '').trim();
    if (/^https:\/\/chatgpt\.com\/checkout\/[A-Za-z0-9_-]+\/oaics_[A-Za-z0-9_-]+$/.test(direct)) {
      return direct;
    }
    const checkoutId = String(value.checkout_id || value.checkoutId || '').trim();
    const processor = String(value.processor_entity || value.processorEntity || '').trim();
    if (/^oaics_[A-Za-z0-9_-]+$/.test(checkoutId) && /^[A-Za-z0-9_-]+$/.test(processor)) {
      return `https://chatgpt.com/checkout/${processor}/${checkoutId}`;
    }
    return '';
  }

  function restoredAccountStage(item = {}) {
    const stage = String(item.stage || '待执行')
      .replace(/^注册指纹已就绪$/, '环境已就绪');
    if (/^(待会话确认：复制 Checkout 链接后在该账号当前会话继续|订阅尚未提交：当前任务缺少该 Checkout 的会话数据)$/.test(stage)) {
      return canonicalCheckoutLink(item, item.link)
        ? '订阅待确认：使用当前行的 Checkout 链接继续'
        : '订阅待确认：旧任务未保存 Checkout 链接，请重新执行“提链 + 支付”';
    }
    return stage;
  }

  function csvCell(value) {
    return `"${String(value ?? '').replace(/"/g, '""')}"`;
  }

  function exportAccountsCsv() {
    if (!state.accounts.length) return false;
    const rows = [
      ['账号', '账号ID', '状态', '当前步骤', 'Checkout链接', '错误'],
      ...state.accounts.map((account) => [
        account.email || '',
        account.accountId || '',
        accountStatusLabel(account),
        account.stage || '',
        canonicalCheckoutLink(account, account.link),
        account.error || '',
      ]),
    ];
    const csv = `\uFEFF${rows.map((row) => row.map(csvCell).join(',')).join('\r\n')}`;
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const download = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    download.href = url;
    download.download = `账号任务-${stamp}.csv`;
    document.body.appendChild(download);
    download.click();
    download.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    return true;
  }

  function translateError(value) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    const lower = text.toLowerCase();
    if (!text) return '任务执行失败。';
    if (lower.includes('curl: (35)') || lower.includes('tls connect error') || lower.includes('openssl_internal:invalid library')) {
      return '代理 TLS 握手失败，请检查代理协议、地址、端口和有效期。';
    }
    if (lower.includes('impersonating') && lower.includes('is not supported')) {
      return '当前浏览器 TLS 指纹版本不受运行库支持，系统已自动切换兼容版本，请重试。';
    }
    if (lower.includes('card_declined') || lower.includes('your card was declined')) {
      return CARD_DECLINE_MESSAGE;
    }
    if (lower.includes('secret_key_required')) {
      return '支付接口权限不匹配，当前步骤没有获得所需的服务端权限。';
    }
    if (lower.includes('invalidated') || lower.includes('token expired') || lower.includes('at invalid') || lower.includes('http 401')) {
      return '账号凭证已失效，请删除后重新导入。';
    }
    if (lower.includes('at does not contain an account id') || lower.includes('at 缺少账号 id')) {
      return '该条不是可用的 AT（缺少账号 ID），请导入 access_token。';
    }
    if (lower.includes('missing or invalid paymentmethod') || lower.includes('paymentmethod 创建失败')) {
      return '卡片支付方式创建失败，请重新填写卡片。';
    }
    if (lower.includes('preflight bind proxy pool exhausted')) {
      return '预检失败：绑卡/支付代理池候选均连接失败，请检查节点状态、协议和有效期。';
    }
    if (lower.includes('提链代理池已耗尽')) {
      return '预检失败：提链代理池候选均连接失败，请检查节点状态、协议和有效期。';
    }
    if (lower.includes('proxy') && (lower.includes('connect') || lower.includes('failed') || lower.includes('error'))) {
      return '代理连接失败，请检查代理格式、节点状态和网络。';
    }
    if (lower.includes('timeout') || lower.includes('timed out') || lower.includes('任务超时')) {
      return '请求超时，请检查代理速度后重试。';
    }
    if (lower.includes('checkout confirm precondition')) {
      return '订阅尚未提交：缺少当前 Checkout 会话生成的动态校验数据。';
    }
    if (lower.includes('checkout confirm') && lower.includes('server returned blocked')) {
      return '订阅请求已提交，账号风控返回 status=blocked；订阅未生效，未进入 Stripe Intent，自动重试已关闭。';
    }
    if (lower.includes('checkout confirm') && lower.includes('http 403')) {
      return '提交支付确认被拒绝（HTTP 403）：当前 Checkout 的会话校验数据无效或缺失。';
    }
    if (lower.includes('confirmation token') && lower.includes('http 403')) {
      return 'Stripe Confirmation Token 请求被拒绝（HTTP 403），请检查支付方式与商户公钥是否匹配。';
    }
    if (lower.includes('http 403')) return '请求被拒绝（HTTP 403），请检查账号状态和接口权限。';
    if (lower.includes('http 402')) return '支付请求被拒绝（HTTP 402），请检查卡片状态。';
    if (lower.includes('failed to perform, curl')) return '网络请求失败，请检查代理和本机网络。';
    if (/[\u4e00-\u9fff]/.test(text)) return text;
    return `任务失败：${text.slice(0, 220)}`;
  }

  function normalizedBilling(value = {}, email = '') {
    return {
      name: String(value.name || '').trim(),
      line1: String(value.line1 || '').trim(),
      line2: String(value.line2 || '').trim(),
      city: String(value.city || '').trim(),
      state: String(value.state || '').trim().toUpperCase(),
      postal_code: String(value.postal_code || '').trim().toUpperCase(),
      country: String(value.country || 'US').trim().toUpperCase(),
      email: String(email || value.email || '').trim(),
    };
  }

  function billingIssues(billing) {
    const issues = [];
    if (!billing.name) issues.push('持卡人姓名');
    if (!billing.line1) issues.push('账单地址');
    if (!billing.city) issues.push('城市');
    if (!/^[A-Z]{2}$/.test(billing.country)) issues.push('两位国家代码');
    if (!billing.postal_code) issues.push('邮编');
    if (billing.country === 'US') {
      if (!billing.state) issues.push('州代码');
      if (!/^\d{5}(?:-\d{4})?$/.test(billing.postal_code)) issues.push('美国 ZIP 格式');
    }
    return issues;
  }

  function ensureStripeJs() {
    if (typeof window.Stripe === 'function') return Promise.resolve(window.Stripe);
    if (stripeJsPromise) return stripeJsPromise;
    stripeJsPromise = new Promise((resolve, reject) => {
      document.querySelectorAll('script[data-direct-stripe-loader]').forEach((node) => node.remove());
      const script = document.createElement('script');
      script.src = 'https://js.stripe.com/v3/';
      script.async = true;
      script.dataset.directStripeLoader = '1';
      const timer = window.setTimeout(() => {
        script.remove();
        reject(new Error('Stripe 安全组件加载超时，请检查浏览器网络或拦截扩展后重试'));
      }, 20000);
      script.addEventListener('load', () => {
        window.clearTimeout(timer);
        if (typeof window.Stripe === 'function') resolve(window.Stripe);
        else reject(new Error('Stripe 安全组件脚本已返回，但组件没有初始化'));
      }, { once: true });
      script.addEventListener('error', () => {
        window.clearTimeout(timer);
        reject(new Error('Stripe 安全组件加载失败，请检查浏览器网络或拦截扩展后重试'));
      }, { once: true });
      document.head.appendChild(script);
    }).catch((error) => {
      stripeJsPromise = null;
      throw error;
    });
    return stripeJsPromise;
  }

  function isStoredCardDecline(value) {
    const text = String(value || '').toLowerCase();
    return text.includes('card_declined')
      || text.includes('your card was declined')
      || text.includes('银行卡被拒绝')
      || text.includes('发卡行拒绝');
  }

  async function fetchBillingForAccount(account) {
    const key = account.id || account.token;
    if (billingFetchPromises.has(key)) return billingFetchPromises.get(key);
    const request = (async () => {
      const response = await fetch('/api/address', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false || !payload.billing) {
        throw new Error(payload.error || `地址获取失败：HTTP ${response.status}`);
      }
      account.billing = normalizedBilling(payload.billing, account.email);
      account.billingMode = 'auto';
      return true;
    })().finally(() => billingFetchPromises.delete(key));
    billingFetchPromises.set(key, request);
    return request;
  }

  async function assignBillingAddresses(accounts) {
    if (!accounts.length) return;
    $('billingAutoStatus').className = 'billing-auto-status is-test';
    $('billingAutoStatus').textContent = `正在为 ${accounts.length} 个账号分别获取 DE 账单地址…`;
    let cursor = 0;
    async function worker() {
      while (cursor < accounts.length) {
        const account = accounts[cursor++];
        await fetchBillingForAccount(account);
      }
    }
    await Promise.all(Array.from({ length: Math.min(8, accounts.length) }, worker));
    renderAccounts();
    refresh();
    $('billingAutoStatus').textContent = `已为 ${accounts.length} 个账号分别分配接口账单地址。`;
    appendLog(`${accounts.length} 个账号的独立 DE 接口地址已就绪。`);
  }

  function setStep(id, status) {
    const node = $(id);
    if (node) node.className = status || '';
  }

  function log(message) {
    void message;
  }

  function appendLog(message) {
    void message;
  }

  function hideBatchSummary() {
    const node = $('batchSummary');
    node.hidden = true;
    node.className = 'batch-summary';
    node.textContent = '';
  }

  function showBatchSummary(label, success, failed, stopped = false, pending = 0) {
    const node = $('batchSummary');
    node.hidden = false;
    node.className = `batch-summary ${stopped || pending ? 'is-stopped' : failed ? '' : 'is-success'}`;
    node.textContent = stopped
      ? `${label}已停止：${success} 个成功，${failed} 个失败或未执行。`
      : pending
        ? `${label}已完成 HTTP 阶段：${success} 个成功，${pending} 个尚未提交，${failed} 个失败。`
        : `${label}完成：${success} 个成功，${failed} 个失败。`;
  }

  function saveProxyDraft() {
    try {
      localStorage.setItem(PROXY_DRAFT_KEY, JSON.stringify({
        bind: $('bindProxyPool').value,
        promo: $('promoProxyPool').value,
        concurrency: $('batchConcurrency').value,
      }));
    } catch (_) {}
  }

  function loadProxyDraft() {
    try {
      const draft = JSON.parse(localStorage.getItem(PROXY_DRAFT_KEY) || '{}');
      if (typeof draft.bind === 'string') $('bindProxyPool').value = draft.bind;
      if (typeof draft.promo === 'string') $('promoProxyPool').value = draft.promo;
      if (draft.concurrency != null) $('batchConcurrency').value = draft.concurrency;
    } catch (_) {}
  }

  function saveAccountsNow() {
    if (accountSaveTimer) {
      window.clearTimeout(accountSaveTimer);
      accountSaveTimer = 0;
    }
    try {
      localStorage.setItem(ACCOUNT_LIST_KEY, JSON.stringify({
        nextId: state.nextId,
        items: state.accounts.map((account) => ({
          id: account.id,
          token: account.token,
          email: account.email || '',
          accountId: account.accountId || '',
          userId: account.userId || '',
          selected: Boolean(account.selected),
          status: account.status || 'idle',
          stage: account.stage || '待执行',
          error: account.error || '',
          link: canonicalCheckoutLink(account, account.link),
          checkoutId: account.checkoutId || '',
          processorEntity: account.processorEntity || '',
          lastTaskId: account.lastTaskId || '',
          taskId: account.taskId || '',
          bindProxyLabel: account.bindProxyLabel || '',
          promoProxyLabel: account.promoProxyLabel || '',
          fingerprint: account.fingerprint || '',
          fingerprintId: account.fingerprintId || '',
          fingerprintTls: account.fingerprintTls || '',
          fingerprintRegion: account.fingerprintRegion || '',
          fingerprintTimezone: account.fingerprintTimezone || '',
          steps: Array.isArray(account.steps) ? account.steps : [],
          billingMode: account.billingMode || '',
          billing: account.billing ? normalizedBilling(account.billing, account.email) : null,
        })),
      }));
    } catch (_) {}
  }

  function saveAccounts() {
    if (accountSaveTimer) window.clearTimeout(accountSaveTimer);
    accountSaveTimer = window.setTimeout(saveAccountsNow, ACCOUNT_SAVE_DEBOUNCE_MS);
  }

  function scheduleRenderAccounts() {
    if (accountRenderFrame) return;
    accountRenderFrame = window.requestAnimationFrame(() => {
      accountRenderFrame = 0;
      renderAccounts();
    });
  }

  function loadAccounts() {
    try {
      const saved = JSON.parse(localStorage.getItem(ACCOUNT_LIST_KEY) || '{}');
      const items = Array.isArray(saved.items) ? saved.items : [];
      state.accounts = items
        .filter((item) => item && typeof item.token === 'string' && item.token.trim())
        .map((item) => ({ item, decoded: decodeAccount(item.token.trim()) }))
        .filter(({ decoded }) => Boolean(decoded.accountId || decoded.userId || decoded.email))
        .map(({ item, decoded }, index) => ({
          id: String(item.id || `account-${index + 1}`),
          token: item.token.trim(),
          email: String(decoded.email || item.email || ''),
          accountId: String(decoded.accountId || item.accountId || ''),
          userId: String(decoded.userId || item.userId || ''),
          selected: item.selected !== false,
          status: ['idle', 'running', 'success', 'error', 'action_required'].includes(item.status) ? item.status : 'idle',
          stage: isStoredCardDecline(item.error) ? '绑卡失败' : restoredAccountStage(item),
          error: isStoredCardDecline(item.error)
            ? CARD_DECLINE_MESSAGE
            : item.error ? translateError(item.error) : '',
          checkoutId: String(item.checkoutId || ''),
          processorEntity: String(item.processorEntity || ''),
          link: canonicalCheckoutLink(item, item.link),
          lastTaskId: String(item.lastTaskId || ''),
          taskId: String(item.taskId || ''),
          bindProxyLabel: String(item.bindProxyLabel || ''),
          promoProxyLabel: String(item.promoProxyLabel || ''),
          fingerprint: String(item.fingerprint || ''),
          fingerprintId: String(item.fingerprintId || ''),
          fingerprintTls: String(item.fingerprintTls || ''),
          fingerprintRegion: String(item.fingerprintRegion || ''),
          fingerprintTimezone: String(item.fingerprintTimezone || ''),
          steps: Array.isArray(item.steps) ? item.steps.map((step) => ({
            key: String(step?.key || ''),
            label: String(step?.label || ''),
            status: ['pending', 'running', 'done', 'error'].includes(step?.status) ? step.status : 'pending',
            detail: step?.status === 'error' && step?.detail
              ? translateError(step.detail)
              : String(step?.detail || ''),
          })) : [],
          billingMode: String(item.billingMode || ''),
          billing: item.billing && typeof item.billing === 'object'
            ? normalizedBilling(item.billing, item.email)
            : null,
        }));
      state.nextId = Math.max(Number(saved.nextId) || 1, state.accounts.length + 1);
      if (state.accounts.length !== items.length) saveAccountsNow();
    } catch (_) {
      state.accounts = [];
      state.nextId = 1;
    }
  }

  function selectedAccounts() {
    return state.accounts.filter((account) => account.selected);
  }

  function maskProxy(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    try {
      const normalized = text.includes('://') ? text : `http://${text}`;
      const url = new URL(normalized);
      return `${url.protocol}//${url.username ? '***@' : ''}${url.hostname}${url.port ? `:${url.port}` : ''}`;
    } catch (_) {
      const parts = text.split(':');
      return parts.length >= 2 ? `${parts[0]}:${parts[1]}` : '代理节点';
    }
  }

  function initialAccountSteps(mode) {
    const definitions = [
      ['proxy', '分配代理'],
    ];
    if (mode !== 'link_only') definitions.push(['card_method', '生成支付方式']);
    definitions.push(['fingerprint', '加载指纹'], ['first_link', '首次提链']);
    if (mode === 'bind_only') definitions.push(['bind', '绑卡']);
    else if (mode === 'link_pay') definitions.push(
      ['payment_prepare', '验证支付方式'],
      ['second_link', '刷新支付链接'],
      ['payment', '提交支付'],
      ['verify', '结果校验'],
    );
    else if (mode !== 'link_only') definitions.push(
      ['bind', '绑卡'],
      ['second_link', '二次提链'],
      ['payment', '提交支付'],
      ['verify', '结果校验'],
    );
    return definitions.map(([key, label]) => ({
      key,
      label,
      status: key === 'proxy' ? 'done' : 'pending',
      detail: key === 'proxy' ? (mode === 'link_only' ? '提链代理已分配' : '双代理已分配') : '等待执行',
    }));
  }

  function setAccountStep(account, key, status, detail) {
    const steps = Array.isArray(account.steps) ? account.steps : [];
    let step = steps.find((item) => item.key === key);
    if (!step) {
      step = { key, label: key, status: 'pending', detail: '等待执行' };
      steps.push(step);
    }
    step.status = status;
    step.detail = detail || step.detail;
    account.steps = steps;
  }

  function failRunningAccountStep(account, detail) {
    const steps = Array.isArray(account.steps) ? account.steps : [];
    const running = [...steps].reverse().find((step) => step.status === 'running');
    if (running) {
      running.status = 'error';
      running.detail = detail;
    }
  }

  function assignProxies(accounts, bindPool, promoPool, mode) {
    const bindStart = bindPool.length ? state.bindProxyCursor % bindPool.length : 0;
    const promoStart = state.promoProxyCursor % promoPool.length;
    accounts.forEach((account, index) => {
      account.bindProxy = bindPool.length ? bindPool[(bindStart + index) % bindPool.length] : '';
      account.promoProxy = promoPool[(promoStart + index) % promoPool.length];
      account.bindProxyPool = bindPool.length
        ? bindPool.map((_, offset) => bindPool[(bindStart + index + offset) % bindPool.length]).slice(0, MAX_PROXY_FALLBACKS)
        : [];
      account.promoProxyPool = promoPool
        .map((_, offset) => promoPool[(promoStart + index + offset) % promoPool.length])
        .slice(0, MAX_PROXY_FALLBACKS);
      account.bindProxyLabel = maskProxy(account.bindProxy);
      account.promoProxyLabel = maskProxy(account.promoProxy);
      account.status = 'running';
      account.stage = mode === 'link_only' ? '提链代理分配完成，等待并发启动' : '代理分配完成，等待并发启动';
      account.error = '';
      account.steps = initialAccountSteps(mode);
    });
    if (bindPool.length) state.bindProxyCursor = (bindStart + accounts.length) % bindPool.length;
    state.promoProxyCursor = (promoStart + accounts.length) % promoPool.length;
    renderAccounts();
    saveAccounts();
  }

  function accountStatusLabel(account) {
    if (account.status === 'success') return '完成';
    if (account.status === 'error') return '失败';
    if (account.status === 'action_required') return '待提交';
    if (account.status === 'running') return '运行中';
    return '待执行';
  }

  function renderAccounts() {
    if (accountRenderFrame) {
      window.cancelAnimationFrame(accountRenderFrame);
      accountRenderFrame = 0;
    }
    const root = $('taskRows');
    root.innerHTML = '';
    $('accountEmpty').hidden = state.accounts.length > 0;

    state.accounts.forEach((account, index) => {
      const row = document.createElement('div');
      row.className = `task-row account-row ${account.status === 'error' ? 'is-error' : ''}`;
      row.dataset.accountId = account.id;

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'account-select';
      checkbox.checked = account.selected;
      checkbox.disabled = state.running;
      checkbox.addEventListener('change', () => {
        account.selected = checkbox.checked;
        refresh();
        renderAccounts();
      });

      const meta = document.createElement('div');
      meta.className = 'task-row__meta account-row__summary';
      const accountName = document.createElement('div');
      accountName.className = 'task-row__account';
      accountName.textContent = account.email || `账号 ${String(index + 1).padStart(2, '0')}`;
      accountName.title = maskToken(account.token);
      const stage = document.createElement('div');
      stage.className = `task-row__stage account-row__current-step ${account.error ? 'is-error' : ''}`;
      const stageLabel = account.stage || '任务失败';
      const errorText = String(account.error || '');
      stage.textContent = errorText
        ? errorText.startsWith(stageLabel) ? errorText : `${stageLabel}：${errorText}`
        : account.stage || '待执行';
      const diagnostics = [
        `AT：${maskToken(account.token)}`,
        account.bindProxyLabel ? `绑卡/支付代理：${account.bindProxyLabel}` : '',
        account.promoProxyLabel ? `提链代理：${account.promoProxyLabel}` : '',
        account.fingerprint ? `指纹：${account.fingerprint}` : '',
        account.billing ? `账单：${account.billing.city || ''}, ${account.billing.state || ''} ${account.billing.postal_code || ''}` : '',
      ].filter(Boolean);
      stage.title = diagnostics.join('\n');
      meta.append(accountName, stage);
      const checkoutLink = canonicalCheckoutLink(account, account.link);
      if (checkoutLink) {
        account.link = checkoutLink;
        const link = document.createElement('button');
        link.type = 'button';
        link.className = 'task-row__link';
        link.textContent = '复制链接';
        link.title = checkoutLink;
        link.addEventListener('click', async () => {
          const copied = await copyText(checkoutLink);
          link.textContent = copied ? '已复制' : '复制失败';
          window.setTimeout(() => {
            if (link.isConnected) link.textContent = '复制链接';
          }, 1200);
        });
        meta.appendChild(link);
      }

      const actions = document.createElement('div');
      actions.className = 'account-row-actions';
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = accountStatusLabel(account);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'at-row__remove';
      remove.textContent = '删除';
      remove.disabled = state.running;
      remove.addEventListener('click', () => {
        state.accounts = state.accounts.filter((item) => item.id !== account.id);
        renderAccounts();
        refresh();
        appendLog('账号已从列表删除。');
      });
      actions.append(tag, remove);
      row.append(checkbox, meta, actions);
      root.appendChild(row);
    });

    const selected = selectedAccounts().length;
    $('selectAllAccounts').checked = Boolean(state.accounts.length) && selected === state.accounts.length;
    $('selectAllAccounts').indeterminate = selected > 0 && selected < state.accounts.length;
    saveAccounts();
  }

  async function allocateAccountFingerprint(account, { quiet = false, deferRender = false } = {}) {
    const previousStage = account.stage;
    if (account.status === 'idle') account.stage = '正在分配注册指纹';
    if (!deferRender) scheduleRenderAccounts();
    try {
      const bind = parseProxyPool($('bindProxyPool').value);
      const accountIndex = Math.max(0, state.accounts.indexOf(account));
      const response = await fetch('/api/fingerprint/allocate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          access_token: account.token,
          bind_proxy_pool: bind.length ? [bind[accountIndex % bind.length]] : [],
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || `指纹分配失败：HTTP ${response.status}`);
      }
      account.email = payload.email || account.email;
      account.accountId = payload.account_id || account.accountId;
      account.fingerprint = String(payload.fingerprint || '');
      account.fingerprintId = String(payload.fingerprint_id || '');
      account.fingerprintTls = String(payload.tls || '');
      account.fingerprintRegion = String(payload.region || '');
      account.fingerprintTimezone = String(payload.timezone || '');
      if (account.status === 'idle') account.stage = '环境已就绪';
      account.error = '';
      if (!quiet) appendLog(`${account.email || account.id}：注册指纹 ${account.fingerprintId} 已分配。`);
    } catch (error) {
      if (account.status === 'idle') account.stage = previousStage || '待执行';
      account.error = translateError(error?.message || error);
    }
    saveAccounts();
    if (!deferRender) scheduleRenderAccounts();
  }

  async function allocateFingerprints(accounts, { quiet = false } = {}) {
    if (!accounts.length) return;
    const previousStages = new Map();
    accounts.forEach((account) => {
      previousStages.set(account.id, account.stage);
      if (account.status === 'idle') account.stage = '正在批量分配注册指纹';
    });
    scheduleRenderAccounts();
    const bind = parseProxyPool($('bindProxyPool').value);
    for (let offset = 0; offset < accounts.length; offset += 500) {
      const chunk = accounts.slice(offset, offset + 500);
      try {
        const response = await fetch('/api/fingerprint/allocate-batch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            accounts: chunk.map((account) => {
              const accountIndex = Math.max(0, state.accounts.indexOf(account));
              return {
                client_id: account.id,
                access_token: account.token,
                bind_proxy_pool: bind.length ? [bind[accountIndex % bind.length]] : [],
              };
            }),
          }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.ok === false || !Array.isArray(payload.items)) {
          throw new Error(payload.error || `批量指纹分配失败：HTTP ${response.status}`);
        }
        const byId = new Map(payload.items.map((item) => [String(item?.client_id || ''), item]));
        chunk.forEach((account) => {
          const item = byId.get(account.id) || {};
          if (item.ok === false || !item.fingerprint_id) {
            account.stage = previousStages.get(account.id) || '待执行';
            account.error = translateError(item.error || '批量指纹分配结果缺失');
            return;
          }
          account.email = item.email || account.email;
          account.accountId = item.account_id || account.accountId;
          account.fingerprint = String(item.fingerprint || '');
          account.fingerprintId = String(item.fingerprint_id || '');
          account.fingerprintTls = String(item.tls || '');
          account.fingerprintRegion = String(item.region || '');
          account.fingerprintTimezone = String(item.timezone || '');
          if (account.status === 'idle') account.stage = '环境已就绪';
          account.error = '';
        });
      } catch (error) {
        const message = translateError(error?.message || error);
        chunk.forEach((account) => {
          account.stage = previousStages.get(account.id) || '待执行';
          account.error = message;
        });
      }
    }
    saveAccounts();
    renderAccounts();
    refresh();
    if (!quiet) appendLog(`${accounts.length} 个账号的固定指纹已批量分配。`);
  }

  async function hydrateMissingFingerprints() {
    // 已落盘且为 US 的兼容指纹直接复用；只修复缺失或旧 TR/chrome144 数据。
    const pending = state.accounts.filter((account) => (
      account.accountId
      && (!account.fingerprintId
      || !account.fingerprintTls
      || String(account.fingerprintRegion || '').toUpperCase() !== 'US'
      || String(account.fingerprintTls || '').toLowerCase() === 'chrome144'
      || /chrome\/144/i.test(String(account.fingerprint || '')))
    ));
    if (!pending.length) return;
    await allocateFingerprints(pending, { quiet: true });
    if (pending.length) appendLog(`已同步 ${pending.length} 个账号的注册指纹与 TLS 兼容版本。`);
  }

  async function resolveAccountIds(accounts, { quiet = false } = {}) {
    const pending = accounts.filter((account) => !account.accountId);
    if (!pending.length) return { resolved: 0, failed: 0 };
    const bind = parseProxyPool($('bindProxyPool').value);
    let cursor = 0;
    let resolved = 0;
    let failed = 0;
    pending.forEach((account) => {
      account.status = 'idle';
      account.stage = '正在通过 AT 获取账号 ID';
      account.error = '';
    });
    scheduleRenderAccounts();
    async function worker() {
      while (cursor < pending.length) {
        const index = cursor++;
        const account = pending[index];
        const accountIndex = Math.max(0, state.accounts.indexOf(account));
        try {
          const response = await fetch('/api/account/resolve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              access_token: account.token,
              bind_proxy_pool: bind.length ? [bind[accountIndex % bind.length]] : [],
            }),
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || payload.ok === false || !payload.account_id) {
            throw new Error(payload.error || `账号 ID 获取失败：HTTP ${response.status}`);
          }
          account.accountId = String(payload.account_id);
          account.userId = String(payload.user_id || account.userId || '');
          account.email = String(payload.email || account.email || '');
          account.status = 'idle';
          account.stage = '账号 ID 已获取，正在准备环境';
          account.error = '';
          resolved += 1;
        } catch (error) {
          account.status = 'error';
          account.stage = '账号 ID 获取失败';
          account.error = translateError(error?.message || error);
          failed += 1;
        }
        scheduleRenderAccounts();
      }
    }
    await Promise.all(Array.from({ length: Math.min(8, pending.length) }, worker));
    saveAccounts();
    renderAccounts();
    refresh();
    if (!quiet) appendLog(`AT 获取账号 ID：${resolved} 个成功，${failed} 个失败。`);
    return { resolved, failed };
  }

  async function hydrateMissingAccountIds() {
    const pending = state.accounts.filter((account) => !account.accountId);
    if (pending.length) await resolveAccountIds(pending, { quiet: true });
  }

  async function hydrateMissingBillingAddresses() {
    const pending = state.accounts.filter((account) => (
      account.billingMode !== 'auto'
      || billingIssues(normalizedBilling(account.billing || {}, account.email)).length > 0
    ));
    if (pending.length) await assignBillingAddresses(pending);
  }

  async function importAccounts(text) {
    const values = extractTokens(text);
    const existing = new Set(state.accounts.map((account) => account.token));
    const imported = [];
    let added = 0;
    values.forEach((token) => {
      if (!token || existing.has(token)) return;
      const decoded = decodeAccount(token);
      const identityMissing = !decoded.accountId;
      state.accounts.push({
        id: `account-${state.nextId++}`,
        token,
        email: decoded.email || '',
        accountId: decoded.accountId || '',
        userId: decoded.userId || '',
        selected: true,
        status: 'idle',
        stage: identityMissing ? '账号已导入，缺少账号 ID' : '待执行',
        error: '',
        link: '',
        checkoutId: '',
        processorEntity: '',
        lastTaskId: '',
        taskId: '',
        bindProxyLabel: '',
        promoProxyLabel: '',
        fingerprint: '',
        fingerprintId: '',
        fingerprintTls: '',
        fingerprintRegion: '',
        fingerprintTimezone: '',
        steps: [],
        billingMode: '',
        billing: null,
      });
      imported.push(state.accounts[state.accounts.length - 1]);
      existing.add(token);
      added += 1;
    });
    $('accountImportInput').value = '';
    updateImportCount();
    renderAccounts();
    refresh();
    const importStatus = $('accountImportStatus');
    if (importStatus) {
      importStatus.className = `proxy-pool-note ${added ? '' : 'is-error'}`.trim();
      importStatus.textContent = added
        ? `已导入 ${added} 个账号，正在通过 AT 获取账号 ID。`
        : '没有发现新的账号，或文件中的账号已经导入。';
    }
    log(added ? `已导入 ${added} 个账号，导入框已清空。` : '没有发现新的账号。');
    if (imported.length) {
      const billingPromise = assignBillingAddresses(imported);
      const identityResult = await resolveAccountIds(imported, { quiet: true });
      const resolvedAccounts = imported.filter((account) => account.accountId);
      await Promise.all([allocateFingerprints(resolvedAccounts), billingPromise]);
      if (importStatus) {
        importStatus.className = `proxy-pool-note ${identityResult.failed ? 'is-error' : ''}`.trim();
        importStatus.textContent = `已导入 ${added} 个账号；AT 获取账号 ID 成功 ${identityResult.resolved + (added - identityResult.resolved - identityResult.failed)} 个，失败 ${identityResult.failed} 个。`;
      }
    }
  }

  function updateImportCount() {
    $('atCount').textContent = `${extractTokens($('accountImportInput').value).length} 条待导入`;
  }

  function updateAccount(account, values) {
    Object.assign(account, values);
    saveAccounts();
    scheduleRenderAccounts();
  }

  function refresh() {
    const bind = parseProxyPool($('bindProxyPool').value);
    const promo = parseProxyPool($('promoProxyPool').value);
    const selected = selectedAccounts();
    const missingAccountIds = selected.filter((account) => !account.accountId);
    const billingMissingAccounts = state.publishableKey
      ? selected.filter((account) => billingIssues(normalizedBilling(account.billing || {}, account.email)).length > 0)
      : [];
    $('bindProxyCount').textContent = `${bind.length} PROXY`;
    $('promoProxyCount').textContent = `${promo.length} PROXY`;
    const identityReady = Boolean(selected.length && missingAccountIds.length === 0);
    const basicReady = Boolean(identityReady && bind.length && promo.length);
    const linkOnlyReady = Boolean(identityReady && promo.length);
    const cardInputReady = state.mounted && Object.values(state.complete).every(Boolean);
    const billingReady = !state.publishableKey || billingMissingAccounts.length === 0;
    const ready = basicReady && cardInputReady && billingReady;
    $('loadCardButton').disabled = !basicReady || state.mounted || state.running;
    ['batchBindLinkButton', 'fullFlowButton', 'linkPayButton'].forEach((id) => {
      $(id).disabled = !ready || state.running;
    });
    $('linkOnlyButton').disabled = !linkOnlyReady || state.running;
    $('deleteSelectedButton').disabled = state.running || !selected.length;
    $('exportCsvButton').disabled = !state.accounts.length;
    $('clearButton').disabled = state.running || !state.accounts.length;
    $('selectAllAccounts').disabled = state.running || !state.accounts.length;
    $('cardReady').className = `inline-state ${ready ? 'ok' : ''}`;
    if (ready) $('cardReady').textContent = `卡片输入已就绪；已选择 ${selected.length} 个账号。`;
    else if (missingAccountIds.length) $('cardReady').textContent = `${missingAccountIds.length} 个 AT 缺少账号 ID，请导入 ChatGPT access_token。`;
    else if (!basicReady) $('cardReady').textContent = '请先导入并选择账号，填写双代理池后再加载卡片。';
    else if (!billingReady) $('cardReady').textContent = `还有 ${billingMissingAccounts.length} 个账号的账单地址未就绪。`;
    return { accounts: selected, bind, promo, basicReady, linkOnlyReady, cardInputReady, billingReady, billingMissingAccounts, missingAccountIds, ready };
  }

  async function mountCard() {
    const data = refresh();
    if (!data.basicReady || state.mounted) return;
    $('loadCardButton').disabled = true;
    $('cardReady').textContent = '正在并行加载 Stripe、安全地址与 Checkout…';
    try {
      const missingBilling = state.accounts.filter((account) => (
        billingIssues(normalizedBilling(account.billing || {}, account.email)).length > 0
      ));
      const preflightPayloads = new Array(data.accounts.length);
      let preflightCursor = 0;
      const preflightLimit = Math.max(
        1,
        Math.min(
          MAX_BATCH_CONCURRENCY,
          Number($('batchConcurrency').value) || 1,
          data.accounts.length,
        ),
      );
      async function preflightWorker() {
        while (preflightCursor < data.accounts.length) {
          const index = preflightCursor++;
          const account = data.accounts[index];
          const previewBindPool = data.bind
            .map((_, offset) => data.bind[(state.bindProxyCursor + index + offset) % data.bind.length])
            .slice(0, MAX_PROXY_FALLBACKS);
          const previewPromoPool = data.promo
            .map((_, offset) => data.promo[(state.promoProxyCursor + index + offset) % data.promo.length])
            .slice(0, MAX_PROXY_FALLBACKS);
          const response = await fetch('/api/card-bind/session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              access_token: account.token,
              bind_proxy_pool: previewBindPool,
              promo_proxy_pool: previewPromoPool,
            }),
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || payload.ok === false) {
            throw new Error(`${account.email || account.id} 预检失败：${payload.error || `HTTP ${response.status}`}`);
          }
          preflightPayloads[index] = payload;
        }
      }
      await Promise.all([
        ensureStripeJs(),
        assignBillingAddresses(missingBilling),
        Promise.all(Array.from({ length: preflightLimit }, preflightWorker)),
      ]);
      const keys = new Set(preflightPayloads.map((item) => String(item?.publishable_key || '')));
      if (keys.has('') || keys.size !== 1) {
        throw new Error(`所选账号分属 ${keys.size} 个 Stripe 商户，当前卡片输入框只能处理同一商户账号`);
      }
      preflightPayloads.forEach((payload, index) => {
        const account = data.accounts[index];
        account.fingerprint = String(payload.fingerprint || account.fingerprint || '');
        account.fingerprintId = String(payload.fingerprint_id || account.fingerprintId || '');
        account.fingerprintTls = String(payload.fingerprint_tls || account.fingerprintTls || '');
        account.fingerprintRegion = String(payload.fingerprint_region || account.fingerprintRegion || '');
        account.fingerprintTimezone = String(payload.fingerprint_timezone || account.fingerprintTimezone || '');
      });
      saveAccounts();
      renderAccounts();
      const payload = preflightPayloads[0];
      const key = String(payload.publishable_key || '');
      if (!key.startsWith('pk_')) throw new Error('账号 Stripe 初始化没有返回有效公钥');
      state.publishableKey = key;
      state.stripe = window.Stripe(key);
      const elements = state.stripe.elements();
      const style = {
        base: { fontSize: '15px', color: '#e7f0fb', '::placeholder': { color: '#70869e' } },
        invalid: { color: '#ff7186' },
      };
      state.elements.number = elements.create('cardNumber', { showIcon: true, style });
      state.elements.expiry = elements.create('cardExpiry', { style });
      state.elements.cvc = elements.create('cardCvc', { style });
      [['number', 'cardNumberElement'], ['expiry', 'cardExpiryElement'], ['cvc', 'cardCvcElement']].forEach(([name, id]) => {
        state.elements[name].mount(`#${id}`);
        state.elements[name].on('change', (event) => {
          state.complete[name] = Boolean(event.complete);
          if (event.error) $('cardReady').textContent = translateError(event.error.message);
          refresh();
        });
      });
      state.mounted = true;
      setStep('stepCard', 'done');
      setStep('stepAt', 'active');
      $('cardReady').textContent = `安全卡片输入框已加载：${payload.country || 'PH'}/${payload.currency || 'PHP'}`;
      appendLog('卡片输入框已加载，后续任务由独立 HTTP 服务处理。');
      refresh();
    } catch (error) {
      const message = translateError(error.message);
      $('loadCardButton').disabled = false;
      $('cardReady').className = 'inline-state error';
      $('cardReady').textContent = message;
      appendLog(message);
    }
  }

  async function createPaymentMethod(account) {
    if (!state.stripe || !state.elements.number) throw new Error('卡片输入框尚未加载');
    const billing = normalizedBilling(account?.billing || {}, account?.email || '');
    const issues = billingIssues(billing);
    if (issues.length) throw new Error(`${account?.email || account?.id || '当前账号'} 的账单地址缺少：${issues.join('、')}`);
    const result = await state.stripe.createPaymentMethod({
      type: 'card',
      card: state.elements.number,
      allow_redisplay: 'always',
      billing_details: {
        name: billing.name,
        email: billing.email || undefined,
        address: {
          line1: billing.line1,
          line2: billing.line2 || undefined,
          city: billing.city,
          state: billing.state || undefined,
          postal_code: billing.postal_code,
          country: billing.country,
        },
      },
    });
    if (result.error) throw new Error(result.error.message || 'PaymentMethod 创建失败');
    const paymentMethod = result.paymentMethod || {};
    if (!paymentMethod.id) throw new Error('PaymentMethod 创建失败');
    return {
      id: paymentMethod.id,
      last4: paymentMethod.card?.last4 || '',
      billing,
      publishableKey: state.publishableKey,
    };
  }

  async function prepareAccountCard(account) {
    updateAccount(account, { status: 'running', stage: '正在准备同批同步起跑', error: '' });
    setAccountStep(account, 'card_method', 'running', '正在通过安全卡片输入框生成支付方式');
    renderAccounts();
    const browserCard = await createPaymentMethod(account);
    setAccountStep(account, 'card_method', 'done', `支付方式已生成 ····${browserCard.last4 || ''}`);
    updateAccount(account, { stage: '支付方式已生成，等待同批账号同步起跑' });
    return browserCard;
  }

  function accountFlowPayload(account, mode, browserCard) {
    const payload = {
      access_token: account.token,
      flow_mode: mode,
      promo_proxy_pool: account.promoProxyPool?.length ? account.promoProxyPool : [account.promoProxy],
    };
    if (mode !== 'link_only') Object.assign(payload, {
      payment_method_id: browserCard.id,
      payment_method_publishable_key: browserCard.publishableKey,
      card_last4: browserCard.last4,
      billing: browserCard.billing,
      bind_proxy_pool: account.bindProxyPool?.length ? account.bindProxyPool : [account.bindProxy],
    });
    return payload;
  }

  async function pollAccountTask(account, taskId) {
    updateAccount(account, { stage: '同批任务已放行，正在同步执行', taskId });
    for (let i = 0; i < Math.ceil(1800000 / TASK_POLL_MS); i += 1) {
      await new Promise((resolve) => setTimeout(resolve, TASK_POLL_MS));
      if (state.stop) throw new Error('任务已停止');
      const poll = await fetch(`/api/standalone-flow/task/${taskId}?t=${Date.now()}`, { cache: 'no-store' });
      const data = await poll.json().catch(() => ({}));
      const task = data.task || {};
      const result = task.result || {};
      if (Array.isArray(task.steps)) account.steps = task.steps;
      if (task.status === 'done') {
        const checkoutLink = canonicalCheckoutLink(result);
        updateAccount(account, {
          email: result.email || account.email,
          status: 'success',
          stage: task.stage || '任务完成',
          checkoutId: String(result.checkout_id || ''),
          processorEntity: String(result.processor_entity || ''),
          link: checkoutLink,
          lastTaskId: taskId,
          error: '',
          taskId: '',
          fingerprint: result.fingerprint || account.fingerprint || '',
          steps: Array.isArray(task.steps) ? task.steps : account.steps,
        });
        return result;
      }
      if (task.status === 'action_required') {
        const checkoutLink = canonicalCheckoutLink(result, account.link);
        updateAccount(account, {
          email: result.email || account.email,
          status: 'action_required',
          stage: task.stage || (checkoutLink
            ? '订阅待确认：使用当前行的 Checkout 链接继续'
            : '订阅待确认：Checkout 链接生成失败'),
          checkoutId: String(result.checkout_id || account.checkoutId || ''),
          processorEntity: String(result.processor_entity || account.processorEntity || ''),
          link: checkoutLink,
          error: '',
          lastTaskId: taskId,
          taskId: '',
          fingerprint: result.fingerprint || account.fingerprint || '',
          steps: Array.isArray(task.steps) ? task.steps : account.steps,
        });
        return result;
      }
      if (task.status === 'error') {
        account.taskId = '';
        account.stage = task.stage || '任务执行失败';
        account.steps = Array.isArray(task.steps) ? task.steps : account.steps;
        saveAccounts();
        throw new Error(task.error || '任务执行失败');
      }
      const nextStage = task.stage || '任务处理中';
      if (nextStage !== account.stage) {
        updateAccount(account, {
          stage: nextStage,
          steps: Array.isArray(task.steps) ? task.steps : account.steps,
        });
      }
    }
    throw new Error('任务轮询超时');
  }

  async function submitAlignedWave(entries, mode) {
    entries.forEach(({ account }) => updateAccount(account, {
      stage: `本批 ${entries.length} 个账号已就绪，等待统一放行`,
    }));
    const response = await fetch('/api/standalone-flow/quick-checkout-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_delay_ms: 250,
        tasks: entries.map(({ account, browserCard }) => ({
          client_id: account.id,
          payload: accountFlowPayload(account, mode, browserCard),
        })),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false || !Array.isArray(payload.items)) {
      throw new Error(payload.error || `同步批次创建失败：HTTP ${response.status}`);
    }
    const byId = new Map(payload.items.map((item) => [String(item.client_id || ''), item.task_id]));
    entries.forEach(({ account }) => {
      const taskId = String(byId.get(account.id) || '');
      if (!taskId) throw new Error(`${account.email || account.id} 缺少同步任务 ID`);
      account.taskId = taskId;
      account.stage = `同步批次 ${String(payload.start_group || '').slice(0, 8)} 已排队`;
    });
    renderAccounts();
    return Promise.allSettled(entries.map(({ account }) => pollAccountTask(account, account.taskId)));
  }

  function setModeSteps(mode, status) {
    ['stepCard', 'stepAt', 'stepBind', 'stepLink'].forEach((id) => setStep(id, ''));
    if (mode !== 'link_only') setStep('stepCard', 'done');
    setStep('stepAt', 'done');
    if (mode === 'bind_only') setStep('stepBind', status);
    else if (mode === 'link_pay' || mode === 'link_only') setStep('stepLink', status);
    else {
      setStep('stepBind', status);
      setStep('stepLink', status);
    }
  }

  function updateProgress(done, total, label) {
    const percent = total ? Math.round((done * 100) / total) : 0;
    $('progressText').textContent = `${label} ${done}/${total}`;
    $('progressPercent').textContent = `${percent}%`;
    $('progressBar').style.width = `${percent}%`;
  }

  async function runBatch(mode, label) {
    const data = refresh();
    if ((mode === 'link_only' ? !data.linkOnlyReady : !data.ready) || state.running) return;
    const accounts = [...data.accounts];
    state.running = true;
    state.stop = false;
    hideBatchSummary();
    $('stopButton').disabled = false;
    renderAccounts();
    refresh();
    setModeSteps(mode, 'active');
    const limit = Math.max(1, Math.min(MAX_BATCH_CONCURRENCY, Number($('batchConcurrency').value) || 1));
    updateProgress(0, accounts.length, '正在分配代理');
    assignProxies(accounts, data.bind, data.promo, mode);
    appendLog(`已为 ${accounts.length} 个账号分配${mode === 'link_only' ? '提链代理' : '双代理'}，每批 ${Math.min(limit, accounts.length)} 个账号同步起跑。`);
    updateProgress(0, accounts.length, `${label}同步并发`);
    let done = 0;
    let success = 0;
    let pending = 0;

    function recordFailure(account, error) {
      const message = translateError(error?.message || error);
      failRunningAccountStep(account, message);
      const preciseStage = String(account.stage || '');
      const failedStep = [...(account.steps || [])].reverse().find((step) => step.status === 'error');
      updateAccount(account, {
        status: 'error',
        stage: failedStep?.label
          ? `${failedStep.label}失败`
          : preciseStage.includes('失败') ? preciseStage : `${label}失败`,
        error: message,
      });
      appendLog(`${account.email || account.id}：${message}`);
    }

    for (let offset = 0; offset < accounts.length && !state.stop; offset += limit) {
      const wave = accounts.slice(offset, offset + limit);
      const prepared = [];
      updateProgress(done, accounts.length, `正在准备同步批次 ${Math.floor(offset / limit) + 1}`);
      for (const account of wave) {
        if (state.stop) break;
        try {
          if (mode === 'link_only') {
            updateAccount(account, { status: 'running', stage: '提链代理已就绪，等待同批同步起跑', error: '' });
            prepared.push({ account, browserCard: null });
          } else {
            const browserCard = await prepareAccountCard(account);
            prepared.push({ account, browserCard });
          }
        } catch (error) {
          recordFailure(account, error);
          done += 1;
          updateProgress(done, accounts.length, label);
        }
      }
      if (!prepared.length || state.stop) continue;
      appendLog(`同步批次 ${Math.floor(offset / limit) + 1}：${prepared.length} 个账号已就绪，统一放行。`);
      try {
        const settled = await submitAlignedWave(prepared, mode);
        settled.forEach((result, index) => {
          const account = prepared[index].account;
          if (result.status === 'fulfilled') {
            if (account.status === 'action_required') pending += 1;
            else success += 1;
          }
          else recordFailure(account, result.reason);
          done += 1;
          updateProgress(done, accounts.length, label);
        });
      } catch (error) {
        prepared.forEach(({ account }) => {
          recordFailure(account, error);
          done += 1;
          updateProgress(done, accounts.length, label);
        });
      }
    }
    if (state.stop) {
      accounts.filter((account) => account.status === 'running' && !account.taskId).forEach((account) => {
        account.status = 'idle';
        account.stage = '已停止，尚未执行';
      });
    }
    state.running = false;
    $('stopButton').disabled = true;
    if (success === accounts.length && !state.stop) setModeSteps(mode, 'done');
    $('progressText').textContent = state.stop ? `${label}已停止` : `${label}完成`;
    showBatchSummary(
      mode === 'bind_only' ? '批量绑卡' : mode === 'link_only' ? '批量提链' : '批量支付',
      success,
      Math.max(0, accounts.length - success - pending),
      state.stop,
      pending,
    );
    renderAccounts();
    refresh();
  }

  $('accountImportInput').addEventListener('input', updateImportCount);
  $('importAtButton').addEventListener('click', () => { void importAccounts($('accountImportInput').value); });
  $('importAtFileButton').addEventListener('click', () => $('atFileInput').click());
  $('atFileInput').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (file) await importAccounts(await file.text());
    event.target.value = '';
  });
  $('selectAllAccounts').addEventListener('change', (event) => {
    state.accounts.forEach((account) => { account.selected = event.target.checked; });
    renderAccounts();
    refresh();
  });
  $('deleteSelectedButton').addEventListener('click', () => {
    const count = selectedAccounts().length;
    state.accounts = state.accounts.filter((account) => !account.selected);
    renderAccounts();
    refresh();
    appendLog(`已删除 ${count} 个账号。`);
  });
  $('clearButton').addEventListener('click', () => {
    const count = state.accounts.length;
    state.accounts = [];
    renderAccounts();
    refresh();
    $('progressBar').style.width = '0%';
    $('progressPercent').textContent = '0%';
    hideBatchSummary();
    appendLog(`账号列表已清空，共移除 ${count} 个账号。`);
  });
  $('exportCsvButton').addEventListener('click', () => {
    appendLog(exportAccountsCsv() ? 'CSV 已导出。' : '当前没有可导出的账号。');
  });
  $('loadCardButton').addEventListener('click', mountCard);
  $('bindProxyPool').addEventListener('input', () => { saveProxyDraft(); refresh(); });
  $('promoProxyPool').addEventListener('input', () => { saveProxyDraft(); refresh(); });
  $('batchConcurrency').addEventListener('input', saveProxyDraft);
  $('validateButton').addEventListener('click', () => {
    const data = refresh();
    const issues = [];
    if (!state.accounts.length) issues.push('尚未导入账号');
    else if (!data.accounts.length) issues.push('尚未选择账号');
    if (!data.bind.length) issues.push('缺少代理池 1');
    if (!data.promo.length) issues.push('缺少代理池 2');
    if (!state.mounted) issues.push('卡片输入框尚未加载');
    if (!data.billingReady) issues.push(`还有 ${data.billingMissingAccounts.length} 个账号账单地址未就绪`);
    $('progressText').textContent = issues.length
      ? `检查输入：${issues.join('；')}`
      : `检查通过：已选择 ${data.accounts.length} 个账号。`;
  });
  $('fullFlowButton').addEventListener('click', () => runBatch('full', '提链 + 绑卡 + 提链 + 支付'));
  $('batchBindLinkButton').addEventListener('click', () => runBatch('bind_only', '提链 + 绑卡'));
  $('linkPayButton').addEventListener('click', () => runBatch('link_pay', '提链 + 支付'));
  $('linkOnlyButton').addEventListener('click', () => runBatch('link_only', '提链'));
  $('stopButton').addEventListener('click', () => {
    state.stop = true;
    $('stopButton').disabled = true;
    appendLog('已停止后续账号任务。');
  });
  window.addEventListener('pagehide', saveAccountsNow);

  loadProxyDraft();
  loadAccounts();
  updateImportCount();
  renderAccounts();
  refresh();
  log(state.accounts.length ? `已恢复 ${state.accounts.length} 个账号及其状态。` : '先在上方导入账号，再填写代理与卡片。');
  void (async () => {
    await hydrateMissingAccountIds();
    await Promise.all([hydrateMissingFingerprints(), hydrateMissingBillingAddresses()]);
  })();
}());
