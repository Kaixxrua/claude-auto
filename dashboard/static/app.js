'use strict';
const $ = id => document.getElementById(id);
$('server-address').textContent = window.location.host;
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const stamp = new Intl.DateTimeFormat('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
const clock = new Intl.DateTimeFormat('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const day = new Intl.DateTimeFormat('zh-CN', {month:'2-digit',day:'2-digit'});
let data = null, fetching = false, offline = false, offset = 0, privacy = true, toastTimer;
try { privacy = localStorage.getItem('claude-monitor-show-email') !== 'yes'; } catch (_) {}
const now = () => Date.now()/1000 + offset;
const pct = value => Number(value).toLocaleString('zh-CN', {maximumFractionDigits:1});
const date = value => value ? stamp.format(new Date(value*1000)) : '尚未提供';
function maskEmail(value) {
  if (!value) return '等待官方登录';
  if (!privacy) return value;
  const [name,domain] = value.split('@');
  return `${name.slice(0,2)}···@${domain || '···'}`;
}
function age(value) {
  if (!value) return '尚未查询';
  const delta = Math.max(0,now()-value);
  if (delta<10) return '刚刚';
  if (delta<60) return `${Math.floor(delta)} 秒前`;
  if (delta<3600) return `${Math.floor(delta/60)} 分钟前`;
  if (delta<86400) return `${Math.floor(delta/3600)} 小时前`;
  return `${Math.floor(delta/86400)} 天前`;
}
function countdown(value) {
  if (!value) return '等待重置时间';
  let s = Math.max(0,Math.ceil(value-now()));
  if (!s) return '已到时间 · 待确认';
  const d = Math.floor(s/86400); s %= 86400;
  const h = Math.floor(s/3600); s %= 3600;
  const m = Math.floor(s/60);
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  return `${m} 分 ${s%60} 秒`;
}
function toast(message) {
  clearTimeout(toastTimer); $('toast').textContent=message; $('toast').hidden=false;
  toastTimer=setTimeout(()=>{$('toast').hidden=true;},4200);
}
const stateLabels = {disabled:'已停用',available:'额度可用',limited:'额度达上限',recorded:'上次记录',unknown:'待确认',logged_out:'未登录',pending:'等待查询'};
const cardLabels = {unknown:'待查询',available:'持有重置卡',none:'无剩余次数',used:'已使用',expired:'已过期',ineligible:'暂不符合资格'};
const scopes = {five_hour:'5 小时',seven_day:'7 天',seven_day_opus:'Opus 周额度',seven_day_sonnet:'Sonnet 周额度',seven_day_overage_included:'周额外包含额度',both:'5 小时和 7 天',unknown:'范围未记录'};
let resetProfile=null, resetBusy=false, confirmation=null, redeemBusy=false, batchCards=false;
const scopeText = grant => (grant.clears||[]).map(k=>scopes[k]||k).join('、');
function cardLabel(card) {
  if(card.pending) return '提交结果待确认';
  if(card.error) return '查询未完成';
  if(card.source==='official_oauth_api' && card.stale) return '记录待更新';
  if(card.source==='official_oauth_api' && card.status==='available') return `剩余 ${card.resets_left} 次 · 可用 ${card.usable_count} 次`;
  return cardLabels[card.status]||'待查询';
}
const planNames = {claude_pro:'Pro',claude_max:'Max',claude_team:'Team',claude_enterprise:'Enterprise'};
const billingNames = {apple_subscription:'苹果内购',google_play_subscription:'Google Play',stripe_subscription:'网页订阅'};
const periodNames = {monthly:'月付',annual:'年付'};
const named = (map,key) => key && Object.hasOwn(map,key) ? map[key] : null;
function remaining(value) {
  const s=Math.max(0,value-now());
  return s>=86400?`剩 ${Math.floor(s/86400)} 天`:s>=3600?`剩 ${Math.floor(s/3600)} 小时`:`剩 ${Math.max(1,Math.ceil(s/60))} 分钟`;
}
function planHTML(a) {
  const p=a.plan;
  const row=(value,tone,detail)=>`<div class="reset-card-row plan-row"><span class="ticket-icon plan-icon">◔</span><span>套餐到期</span><span class="card-state plan-state ${tone}">${value}</span></div><div class="card-detail plan-detail">${escapeHTML(detail)}</div>`;
  if(!p) return row('尚未取得','',a.loggedIn?'等待官方查询账号资料':'登录后显示');
  // The official profile gives the start only; the server infers the end of the current period.
  const details=[named(planNames,p.type)||(p.type?'未订阅付费套餐':'套餐类型未知')];
  if(p.billing) details.push((named(billingNames,p.billing)||p.billing)+(named(periodNames,p.period)||''));
  if(p.started_at) details.push(`${day.format(new Date(p.started_at*1000))} 开通`);
  if(p.ends_at) details.push(p.renewals?`已续 ${p.renewals} 期`:'首期',p.period?'按开通日推算':'按月付推算');
  if(p.status) details.push(`官方状态 ${p.status}`);
  if(p.source==='client_profile') details.push(p.checked_at?`客户端 ${day.format(new Date(p.checked_at*1000))} 记录`:'客户端记录');
  if(!p.ends_at) return row(named(planNames,p.type)?'到期时间未知':'—','',details.join(' · '));
  const rest=p.ends_at-now();
  return row(`${date(p.ends_at)} 到期 · <span data-plan-end="${p.ends_at}">${remaining(p.ends_at)}</span>`,rest<=86400?'urgent':rest<=3*86400?'soon':'',details.join(' · '));
}
function quotaHTML(window, key) {
  const label=key==='five_hour'?'5 小时':'7 天';
  const abbreviation=key==='five_hour'?'SESSION':'WEEKLY';
  const expired=window?.resets_at && window.resets_at<=now();
  const used=window?.used_percentage;
  const tone=used>=100?'exhausted':used>=80?'warn':'';
  const value=window ? (expired ? `<span class="quota-value unknown">待确认</span>` : `<span class="quota-value ${tone}">${pct(100-used)}<small>% 剩余</small></span>`) : '<span class="quota-value unknown">—</span>';
  const reset=window?.resets_at;
  return `<div class="quota-window"><div class="quota-heading"><span class="quota-title">${label}<span>${abbreviation}</span></span>${value}</div>
    <div class="bar-track ${!window||expired?'unknown':''}" role="meter" aria-label="${label}已用额度" ${window&&!expired?`aria-valuenow="${used}" aria-valuemin="0" aria-valuemax="100"`:''}>${window&&!expired?`<div class="bar-fill ${tone}" style="width:${used}%"></div>`:''}</div>
    <div class="quota-meta"><span>${window?`上次已用 ${pct(used)}%`:'尚无额度记录'}</span><span ${reset?`data-reset="${reset}"`:''} class="reset-count ${expired?'waiting':''}">${reset?`${expired?'': '重置倒计时 '}${countdown(reset)}`:'未提供重置时间'}</span></div>
    <div class="quota-meta"><span>${window?.source==='session_statusline'?'状态栏':window?.source==='delegated_session'?'委派会话':window?.source==='official_oauth_api'?'官方接口':'官方 /usage'}</span><span>${reset?`${window.reset_precision==='minute'?'约 ':''}${date(reset)} 重置`:(window?.reset_display?escapeHTML(window.reset_display):'—')}</span></div></div>`;
}
function accountHTML(a,index) {
  const card=a.reset_card||{status:'unknown'};
  const state=a.state==='disabled'?stateLabels.disabled:a.refreshing?'查询中':a.state==='limited'?stateLabels.limited:a.live?'状态栏跟随':a.delegated?'委派任务中':stateLabels[a.state];
  const badge=['disabled','limited'].includes(a.state)?a.state:(a.live||a.delegated)?'live':a.state;
  const cardDetails=[];
  if (card.source==='official_oauth_api') {
    cardDetails.push(card.error || card.reason_text || (card.checked_at?`${age(card.checked_at)}查询官方余额`:'尚未完成查询'));
    const ends=(card.grants||[]).filter(g=>g.active&&g.resets_left>0).map(g=>g.ends_at);
    if(ends.length) cardDetails.push(`${date(Math.min(...ends))} 最早到期`);
  } else if (card.source==='manual') {
    if(card.status==='available'||card.status==='expired') cardDetails.push(scopes[card.scope]||scopes.unknown);
    if(card.expires_at) cardDetails.push(`${date(card.expires_at)} 到期`);
    cardDetails.push(`${age(card.verified_at)}核验${card.stale?' · 建议复核':''}`);
  } else cardDetails.push(a.state==='disabled'?'已停用，跳过查询':'点击查询，核对官方余额与使用条件');
  const content = a.state==='disabled' ? `<div class="quota-empty"><span>○</span><div>已停用，自动切换会跳过此账号</div><div>${escapeHTML(a.disabled_reason)}</div></div>` : a.loggedIn ? quotaHTML(a.windows?.five_hour,'five_hour')+quotaHTML(a.windows?.seven_day,'seven_day') : `<div class="quota-empty"><span>○</span><div>${a.state==='pending'?'等待首次登录检查':'此账号位置尚未登录'}</div><code class="login-command">claude-accounts login ${escapeHTML(a.profile)}</code></div>`;
  return `<article class="account-card ${a.state==='limited'?'is-limited':''} ${a.loggedIn===false?'is-offline':''}" data-profile="${escapeHTML(a.profile)}">
    <div class="account-header"><span class="account-avatar">${String(index+1).padStart(2,'0')}</span><div class="account-identity"><div class="account-name"><h3>${escapeHTML(a.profile)}</h3>${a.subscriptionType?`<span class="plan-pill">${escapeHTML(a.subscriptionType)}</span>`:''}</div><span class="email">${escapeHTML(maskEmail(a.email))}</span></div><div class="account-actions"><span class="status-badge ${badge}"><i></i>${escapeHTML(state)}</span><button class="icon-button ${a.refreshing?'spinning':''}" data-action="refresh" data-profile="${escapeHTML(a.profile)}" aria-label="刷新 ${escapeHTML(a.profile)}" title="查询一次此账号的官方额度" ${a.refreshing||a.state==='disabled'?'disabled':''}>↻</button></div></div>
    ${content}${a.state!=='disabled'&&a.quota_limit?.active?`<div class="account-error">官方客户端已报告限额，约 ${date(a.quota_limit.blocked_until)} 后可重试。额度查询不完整时仍保留此提示。</div>`:''}${a.error&&a.loggedIn&&a.state!=='disabled'?`<div class="account-error">${escapeHTML(a.error)}</div>`:a.auto?.error&&a.loggedIn&&a.state!=='disabled'?`<div class="account-error">自动查询：${escapeHTML(a.auto.error)}</div>`:''}
    <div class="card-divider"></div>${planHTML(a)}<div class="reset-card-row"><span class="ticket-icon">✧</span><span>重置卡</span><span class="card-state ${card.status}">${escapeHTML(cardLabel(card))}</span></div><div class="card-detail">${escapeHTML(cardDetails.join(' · '))}</div>
    <div class="card-controls"><button class="button secondary" data-action="cards" data-profile="${escapeHTML(a.profile)}" ${a.state==='disabled'?'disabled':''}>查看 / 查询</button><button class="button primary" data-action="use-card" data-profile="${escapeHTML(a.profile)}" ${a.state==='disabled'||!(card.usable_count>0)||card.stale||card.pending?'disabled':''}>使用重置卡</button></div>
    <div class="account-footer"><span class="footer-source ${a.live||a.delegated?'live':''}"><i></i>${a.live?'跟随活动会话':a.delegated?`委派任务运行中 · ${a.delegated} 个`:a.loggedIn?'闲置时保留记录':'等待账号登录'}</span><span ${a.last_observed_at?`data-age="${a.last_observed_at}"`:''}>${a.last_observed_at?`${age(a.last_observed_at)}更新`:'尚无额度记录'}</span></div></article>`;
}
function filteredAccounts() {
  const query=$('search').value.trim().toLowerCase(), filter=$('filter').value;
  return data.accounts.filter(a=>{
    const matches= !query || `${a.profile} ${a.email||''}`.toLowerCase().includes(query);
    const category=filter==='all'||(filter==='live'&&a.live)||(filter==='cards'&&a.reset_card?.status==='available')||a.state===filter;
    return matches&&category;
  });
}
function renderAccounts() {
  if(!data) return;
  const list=filteredAccounts();
  const focused=document.activeElement;
  const focusAction=focused?.dataset?.action, focusProfile=focused?.dataset?.profile;
  $('accounts').innerHTML=list.map(a=>accountHTML(a,data.accounts.indexOf(a))).join('');
  $('visible-count').textContent=`${list.length} 个账号`;
  $('empty').hidden=list.length>0;
  if(focusAction && focusProfile) {
    const replacement=[...document.querySelectorAll('[data-action]')].find(el=>el.dataset.action===focusAction&&el.dataset.profile===focusProfile);
    replacement?.focus({preventScroll:true});
  }
}
function renderResets() {
  const resets=[];
  for(const a of data.accounts.filter(a=>a.state!=='disabled')) for(const key of ['five_hour','seven_day']) {
    const w=a.windows?.[key];
    if(w?.resets_at) resets.push({profile:a.profile,key,time:w.resets_at});
  }
  const pending=resets.filter(r=>r.time<=now());
  const future=resets.filter(r=>r.time>now()).sort((a,b)=>a.time-b.time);
  const displayed=future.slice(0,6);
  $('reset-list').innerHTML=displayed.length?displayed.map(r=>`<div class="reset-row"><span class="reset-dot ${r.key==='seven_day'?'week':''}"></span><span class="reset-profile">${escapeHTML(r.profile)}</span><span class="reset-kind">${r.key==='five_hour'?'5 小时窗口':'7 天窗口'}</span><span class="reset-date">${date(r.time)}</span><span class="reset-remaining" data-countdown="${r.time}">${countdown(r.time)}</span></div>`).join(''):`<div class="loading-card">${pending.length?'已到重置时间，等待状态栏更新或手动刷新':'暂无已知的重置时间'}</div>`;
  if(future.length>6||pending.length) $('reset-list').insertAdjacentHTML('beforeend',`<div class="reset-more">${future.length>6?`另有 ${future.length-6} 个窗口将在更晚重置。 `:''}${pending.length?`${pending.length} 个窗口已到重置时间，待新数据确认。`:''}</div>`);
}
const jobLabels = {queued:'排队中',selecting:'正在挑账号',waiting:'等空账号',running:'运行中',completed:'已完成',failed:'失败',blocked:'没有空账号',cancelled:'已取消',timed_out:'到了时限',quota_exhausted:'额度用完',interrupted:'已中断',model_mismatch:'模型不符，已停'};
const jobWaiting = ['queued','selecting','waiting'];
function elapsed(from) {
  const s=Math.max(0,Math.floor(now()-from)), h=Math.floor(s/3600), m=Math.floor(s%3600/60);
  return h?`${h} 小时 ${m} 分`:`${m} 分`;
}
function accountQuota(name, limits) {
  // The job's own session reading is the newest; otherwise the account card's.
  const windows=limits?.windows||data.accounts.find(a=>a.profile===name)?.windows||{};
  const part=(key,label)=>{const w=windows[key]; return w&&Number.isFinite(w.used_percentage)?`${label}剩 ${pct(Math.max(0,100-w.used_percentage))}%`:'';};
  return [part('five_hour','5 小时'),part('seven_day','本周')].filter(Boolean).join(' · ');
}
function jobHTML(j) {
  const running=j.status==='running';
  // Long commands such as renders write nothing to the session for a while; 20 quiet minutes is worth a look.
  const quiet=running&&j.alive&&j.activity_at&&now()-j.activity_at>1200;
  const tone=running?(j.alive&&!quiet?'live':'warn'):j.status==='completed'?'done':jobWaiting.includes(j.status)?'wait':j.status==='interrupted'?'warn':'stop';
  const label=running?(j.alive?(quiet?'运行中 · 久未动作':'运行中'):'进程已退出'):(jobLabels[j.status]||'状态未知');
  const detail=[j.project, j.created_at?`${date(j.created_at)} 开始`:'', running&&j.created_at?`已运行 ${elapsed(j.created_at)}`:'', j.session?`会话 ${j.session}`:'', j.attempt>1?`第 ${j.attempt} 次`:'', j.continues?`已自动续做 ${j.continues} 次`:'', running&&j.shared?'与其他任务共用账号':''].filter(Boolean).join(' · ');
  const note=j.auto_stop||(!running?j.reason:'');
  const reason=note?`<small class="job-detail job-reason">${escapeHTML(note)}</small>`:'';
  const quota=running&&j.account?accountQuota(j.account,j.limits):'';
  const right=running?(j.activity_at?`最近动作 ${age(j.activity_at)}`:'等待第一步'):(j.updated_at?`${age(j.updated_at)}${jobWaiting.includes(j.status)?'更新':'结束'}`:'—');
  return `<div class="reset-row job-row" title="${escapeHTML(j.reason||j.title||'')}"><span class="reset-dot job-${tone}"></span><span class="job-main"><span class="reset-profile">${escapeHTML(j.title||j.job)}</span><small class="job-detail">${escapeHTML(detail)}</small>${reason}</span><span class="job-state ${tone}">${escapeHTML(label)}<small>${j.account?`账号 ${escapeHTML(j.account)}`:'尚未分配账号'}</small>${quota?`<small class="job-quota">${escapeHTML(quota)}</small>`:''}</span><span class="reset-remaining" ${running&&j.activity_at?`data-activity="${j.activity_at}"`:''}>${escapeHTML(right)}</span></div>`;
}
function renderDelegations() {
  const jobs=data.delegations||[];
  const running=jobs.filter(j=>j.status==='running'&&j.alive).length;
  const waiting=jobs.filter(j=>jobWaiting.includes(j.status)).length;
  $('nav-delegations').textContent=running;
  $('delegation-count').textContent=jobs.length?`${running} 个在跑${waiting?` · ${waiting} 个在等账号`:''} · 共 ${jobs.length} 个`:'';
  const p=data.delegation_policy||{};
  const rules=[p.share_when_short&&'账号不够时共用',p.wait_for_account&&'没号时排队',p.auto_continue&&'额度用完换号续做',p.enforce_model&&'模型不符即停',p.interactive_cap>0&&`主会话的号用到 ${p.interactive_cap}% 停`].filter(Boolean);
  $('delegation-policy').textContent=rules.length?`自动：${rules.join(' · ')} · 续做守护${p.supervisor?'运行中':'未运行'}`:'未开启自动规则 · 最近 24 小时';
  $('delegation-policy').classList.toggle('warn',!!(p.auto_continue&&!p.supervisor));
  $('delegation-list').innerHTML=jobs.length?jobs.map(jobHTML).join(''):'<div class="loading-card">最近 24 小时没有委派任务</div>';
}
function render() {
  const accounts=data.accounts;
  $('nav-count').textContent=accounts.length;
  const enabled=accounts.filter(a=>a.state!=='disabled');
  const logged=enabled.filter(a=>a.loggedIn).length;
  $('summary-login').innerHTML=`${logged}<span class="denom">/ ${enabled.length}</span>`;
  $('summary-total').textContent=`${accounts.length-enabled.length} 个已停用 · ${enabled.length-logged} 个待登录或检查`;
  const capacity=data.availability;
  $('summary-available').textContent=capacity?capacity.available:accounts.filter(a=>a.state==='available').length;
  $('summary-available-note').textContent=capacity?`${capacity.eligible} 个已登录且启用 · ${capacity.unconfirmed} 个待确认`:'正在核验额度';
  $('availability-title').textContent=capacity?.eligible&&capacity.limited===capacity.eligible?'已登录且启用的账号均已触发限额':capacity?.available?'有账号额度可用':'暂未确认有可用账号';
  $('availability-detail').textContent=capacity?`${capacity.available} 个可用 · ${capacity.limited} 个达上限 · ${capacity.unconfirmed} 个待确认${capacity.next_reset_at?`；预计最早 ${date(capacity.next_reset_at)}，${capacity.next_profile} 重置后再核验。`:'。'}`:'正在读取额度状态';
  $('summary-limited').textContent=accounts.filter(a=>a.state==='limited').length;
  const known=enabled.filter(a=>a.reset_card?.source==='official_oauth_api' && !a.reset_card.stale && !a.reset_card.error && a.reset_card.status!=='unknown');
  const usable=known.reduce((sum,a)=>sum+(a.reset_card.usable_count||0),0);
  const held=known.reduce((sum,a)=>sum+(a.reset_card.resets_left||0),0);
  $('summary-cards').textContent=known.length?usable:'—';
  $('summary-cards-note').textContent=`已查询持有 ${held} 次 · ${enabled.length-known.length} 个账号待确认`;
  const auto=data.auto_refresh;
  $('sync-note').textContent=data.busy?`正在用 /usage 查询 ${data.busy}。`:auto?.running?`有会话或委派任务的账号每 ${Math.round(auto.busy_seconds/60)} 分钟、闲置账号每 ${Math.round(auto.idle_seconds/60)} 分钟自动查一次官方额度；委派会话每次请求都会回报。${auto.error?' '+auto.error:''}`:'网页每 5 秒读取最新记录。';
  $('refresh-all').disabled=!!data.busy;
  const icon=$('refresh-all').querySelector('.refresh-icon'); icon.classList.toggle('spinning',!!data.busy);
  $('error-banner').textContent=data.collector_error||''; $('error-banner').hidden=!data.collector_error;
  renderAccounts(); renderResets(); renderDelegations(); if($('reset-dialog').open) renderInventory(); tick();
}
function updatePrivacy() {
  $('privacy-toggle').textContent=privacy?'显示邮箱':'隐藏邮箱';
  $('privacy-toggle').setAttribute('aria-pressed',String(!privacy));
}
function tick() {
  $('local-clock').textContent=clock.format(new Date());
  document.querySelectorAll('[data-reset]').forEach(el=>{ const t=Number(el.dataset.reset); el.textContent=(t>now()?'重置倒计时 ':'')+countdown(t); el.classList.toggle('waiting',t<=now()); });
  document.querySelectorAll('[data-countdown]').forEach(el=>el.textContent=countdown(Number(el.dataset.countdown)));
  document.querySelectorAll('[data-age]').forEach(el=>el.textContent=age(Number(el.dataset.age))+'更新');
  document.querySelectorAll('[data-activity]').forEach(el=>el.textContent='最近动作 '+age(Number(el.dataset.activity)));
  document.querySelectorAll('[data-plan-end]').forEach(el=>el.textContent=remaining(Number(el.dataset.planEnd)));
}
async function fetchStatus() {
  if(fetching) return;
  fetching=true;
  try {
    const response=await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(12000)});
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    data=await response.json(); offset=data.server_time-Date.now()/1000;
    offline=false; $('connection').classList.remove('offline'); $('connection').innerHTML='<i></i>本地连接正常'; render();
  } catch(_) {
    offline=true; $('connection').classList.add('offline'); $('connection').innerHTML='<i></i>服务器连接中断';
    $('error-banner').textContent='暂时无法连接服务器，正在显示上次取得的数据。连接恢复后会自动更新。'; $('error-banner').hidden=false;
  } finally { fetching=false; }
}
async function post(path,payload,timeout=12000) {
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(timeout)});
  const result=await response.json();
  if(!response.ok) throw new Error(result.error||`请求失败 (${response.status})`);
  return result;
}
async function refresh(profile) {
  try { await post('/api/refresh',profile?{profile}:{}); toast(profile?`已安排查询 ${profile}`:'已安排统一查询所有账号'); await fetchStatus(); }
  catch(error){toast(error.message);}
}
function openCard(profile) {
  const a=data.accounts.find(item=>item.profile===profile); if(!a) return;
  const c=a.reset_card||{};
  $('card-profile').value=profile; $('card-account').textContent=`${profile} · ${a.email}`;
  $('card-status').value=c.status==='expired'?'available':(c.status||'unknown');
  $('card-scope').value=c.scope||'unknown'; $('card-note').value=c.note||''; $('card-error').textContent='';
  const dt=c.expires_at?new Date(c.expires_at*1000):null;
  $('card-expiry').value=dt?new Date(dt.getTime()-dt.getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  $('card-dialog').showModal();
}
$('accounts').addEventListener('click',event=>{const el=event.target.closest('[data-action]');if(!el||el.disabled)return; if(el.dataset.action==='refresh') refresh(el.dataset.profile); else openReset(el.dataset.profile,el.dataset.action==='use-card');});
$('refresh-all').addEventListener('click',()=>refresh());
$('privacy-toggle').addEventListener('click',()=>{privacy=!privacy;try{localStorage.setItem('claude-monitor-show-email',privacy?'no':'yes');}catch(_){} updatePrivacy();renderAccounts();});
$('search').addEventListener('input',renderAccounts); $('filter').addEventListener('change',renderAccounts);
$('clear-filters').addEventListener('click',()=>{$('search').value='';$('filter').value='all';renderAccounts();});
$('help-open').addEventListener('click',()=>$('help-dialog').showModal());
function closeDialog(dialog) { if(!redeemBusy) dialog.close(); }
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>closeDialog(button.closest('dialog'))));
document.querySelectorAll('dialog').forEach(dialog=>{
  dialog.addEventListener('cancel',event=>{if(redeemBusy)event.preventDefault();});
  dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)closeDialog(dialog);}});
});
$('card-form').addEventListener('submit',async event=>{
  event.preventDefault(); $('save-card').disabled=true; $('card-error').textContent='';
  try {
    const expiry=$('card-expiry').value;
    await post('/api/reset-card',{profile:$('card-profile').value,status:$('card-status').value,scope:$('card-scope').value,expires_at:expiry?new Date(expiry).getTime()/1000:null,note:$('card-note').value.trim()});
    $('card-dialog').close();toast('已保存核验记录');await fetchStatus();
  }catch(error){$('card-error').textContent=error.message;}finally{$('save-card').disabled=false;}
});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)fetchStatus();});
function renderInventory() {
  const a=data?.accounts.find(a=>a.profile===resetProfile); if(!a)return;
  const c=a.reset_card||{};
  $('reset-account').textContent=`${a.profile} · ${maskEmail(c.email||a.email)}`;
  let html=`<p class="inventory-status">${escapeHTML(cardLabel(c))}</p>`;
  if(c.source==='manual') html+='<p class="dialog-note">这是手动备注，请查询官方余额后使用。</p>';
  if(c.error||c.reason_text) html+=`<p class="form-error">${escapeHTML(c.error||c.reason_text)}</p>`;
  if(c.pending) html+='<p class="dialog-note">上次提交结果尚未确认，可能已经消耗卡片。可以查询最新余额；暂时禁止重复使用。</p>';
  if(c.last_result) html+=`<p class="dialog-note">最近一次操作：${escapeHTML(c.last_result.message)}</p>`;
  for(const g of c.grants||[]) {
    const condition=g.ends_at<=now()?'已过期':g.starts_at>now()?'尚未开始':g.paused?'已暂停':g.resets_left===0?'已用尽':g.usable_now?'当前可用':'当前不可用';
    html+=`<section class="grant"><h3>${escapeHTML(g.label)}</h3><div class="grant-balance"><strong>${g.resets_left}<small> / ${g.resets_total} 次</small></strong><span>${condition}</span></div><dl class="grant-details"><dt>重置范围</dt><dd>${escapeHTML(scopeText(g))}</dd><dt>到期时间</dt><dd>${date(g.ends_at)} · ${escapeHTML(zone)}</dd></dl><button class="button primary" data-grant="${escapeHTML(g.id)}" ${!g.usable_now||resetBusy||a.state==='disabled'?'disabled':''}>使用重置卡</button></section>`;
  }
  if(c.checked_at) html+=`<p class="inventory-time">官方查询于 ${date(c.checked_at)} · ${escapeHTML(zone)}${c.stale?' · 请重新查询':''}</p>`;
  $('reset-inventory').innerHTML=html;
  $('query-card').disabled=resetBusy||redeemBusy||a.state==='disabled';
  $('query-card').textContent=resetBusy?'正在核对…':'重新查询';
  $('manual-card').disabled=resetBusy||!a.email||a.state==='disabled';
}
async function queryCard(profile) {
  const card=await post('/api/reset-cards/query',{profile},35000);
  const a=data?.accounts.find(a=>a.profile===profile); if(a)a.reset_card=card;
  return card;
}
async function openReset(profile,prepare=false) {
  if(resetBusy||redeemBusy)return;
  resetProfile=profile; confirmation=null; $('reset-message').textContent='正在查询官方卡片余额…';
  if(!$('reset-dialog').open)$('reset-dialog').showModal();
  resetBusy=true; renderInventory();
  let card;
  try {card=await queryCard(profile);$('reset-message').textContent=card.error||'已取得最新查询结果。';}
  catch(error){$('reset-message').textContent=error.message||'查询失败，请稍后重试';}
  finally {resetBusy=false;renderInventory();await fetchStatus();}
  const grant=card?.grants?.find(g=>g.usable_now);
  if(prepare&&grant&&$('reset-dialog').open)await prepareCard(grant.id);
}
async function prepareCard(gid) {
  if(resetBusy||redeemBusy)return;
  resetBusy=true;renderInventory();$('reset-message').textContent='正在核对账号与使用条件…';
  try {
    confirmation=await post('/api/reset-cards/prepare',{profile:resetProfile,grant_id:gid},35000);
    const c=confirmation,g=c.grant;
    $('confirm-reset-details').innerHTML=`<p class="dialog-account"><b>${escapeHTML(c.profile)}</b><br>${escapeHTML(c.email)}</p><dl class="grant-details"><dt>卡片</dt><dd>${escapeHTML(g.label)}</dd><dt>剩余次数</dt><dd>${g.resets_left} 次，本次消耗 1 次</dd><dt>重置范围</dt><dd>${escapeHTML(scopeText(g))}</dd><dt>卡片到期</dt><dd>${date(g.ends_at)} · ${escapeHTML(zone)}</dd><dt>确认有效期</dt><dd>3 分钟</dd></dl>`;
    $('confirm-reset-message').textContent='';$('confirm-use-card').disabled=false;
    $('confirm-use-card').textContent='确认消耗 1 次';
    $('reset-message').textContent='';$('confirm-reset-dialog').showModal();
  }catch(error){$('reset-message').textContent=error.message;await fetchStatus();}
  finally{resetBusy=false;renderInventory();}
}
$('query-card').addEventListener('click',()=>openReset(resetProfile));
$('manual-card').addEventListener('click',()=>openCard(resetProfile));
$('reset-inventory').addEventListener('click',event=>{const b=event.target.closest('[data-grant]');if(b&&!b.disabled)prepareCard(b.dataset.grant);});
$('confirm-use-card').addEventListener('click',async()=>{
  if(!confirmation||redeemBusy)return;
  redeemBusy=true; const dialog=$('confirm-reset-dialog');
  dialog.querySelectorAll('button').forEach(b=>b.disabled=true);
  $('confirm-use-card').textContent='正在提交…';$('confirm-reset-message').textContent='正在等待官方确认，请勿重复操作。';
  try {
    const result=await post('/api/reset-cards/use',{confirmation_id:confirmation.confirmation_id,confirmed:true},90000);
    $('confirm-reset-message').textContent=result.message+(result.refresh_error?' '+result.refresh_error:'');
    const a=data?.accounts.find(a=>a.profile===confirmation.profile);if(a&&result.card)a.reset_card=result.card;
  }catch(error){$('confirm-reset-message').textContent=`${error.message||'请求中断'}。请关闭此窗口并查询最新状态，本次不会自动重试。`;}
  finally {
    redeemBusy=false;confirmation=null;dialog.querySelectorAll('[data-close]').forEach(b=>b.disabled=false);
    $('confirm-use-card').disabled=true;$('confirm-use-card').textContent='提交已结束';
    renderInventory();await fetchStatus();
  }
});
$('query-cards-all').addEventListener('click',async()=>{
  if(batchCards||!data)return;
  batchCards=true;const button=$('query-cards-all');button.disabled=true;
  const accounts=data.accounts.filter(a=>a.state!=='disabled');let failed=0;
  for(let i=0;i<accounts.length;i++) {
    button.textContent=`查询中 ${i+1}/${accounts.length}`;
    try{const c=await queryCard(accounts[i].profile);if(c.error)failed++;}catch(_){failed++;}
    await fetchStatus();
  }
  batchCards=false;button.disabled=false;button.textContent='查询重置卡';
  toast(`查询完成${failed?`，${failed} 个账号待确认`:'，已更新官方余额'}；已跳过停用账号。`);
});
$('timezone').textContent=`时间按 ${zone} 显示`;
updatePrivacy(); fetchStatus();
setInterval(()=>{if(!document.hidden)fetchStatus();},5000); setInterval(tick,1000);
