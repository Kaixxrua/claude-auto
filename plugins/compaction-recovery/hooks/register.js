// SPDX-License-Identifier: Apache-2.0
// Uses Claude Code's public mods API. The engine owns every summary and message.
const RETRY_DELAYS = [500, 1000, 2000];
const IDLE_COOLDOWN = 300000;

async function record($, values) {
  // Metadata only: never persist prompts, summaries, errors or credentials.
  try {
    await $.store.set('last-compaction', { at: await $.clock.now(), ...values });
  } catch { /* Observability must not turn a successful compaction into failure. */ }
}

async function note($, text) {
  try { await $.ui.log(text); } catch { /* A headless run has no UI. */ }
}

async function quotaBlocked($) {
  try {
    const usage = await $.session.usage();
    return usage.rateLimits.some(limit => limit.percentUsed >= 100);
  } catch { return false; }
}

export function register(on) {
  const state = { running: false, checking: false, suspended: false,
    lastAttempt: -Infinity, timer: undefined };

  on('session.compact', async ($, e, next) => {
    // A user cancellation, a hook veto, and a manual request must retain their
    // original meaning. Precomputation is also left to the native scheduler.
    const delays = e.trigger === 'auto' ? RETRY_DELAYS : [];
    for (let attempt = 0; ; attempt += 1) {
      try {
        const result = await next(e);
        await record($, {
          agentId: e.agentId ?? null, trigger: e.trigger, attempts: attempt + 1,
          phase: result.skip ? 'skipped' : 'complete',
          tokensBefore: result.tokensBefore ?? null,
          tokensAfter: result.tokensAfter ?? null,
        });
        if (attempt > 0 && !result.skip) {
          const size = Number.isFinite(result.tokensBefore) && Number.isFinite(result.tokensAfter)
            ? `：${result.tokensBefore} → ${result.tokensAfter} tokens` : '';
          await note($, `${e.agentId ? '子代理' : '上下文'}压缩已恢复${size}。`);
        }
        return result;
      } catch (error) {
        if (next.signal.aborted) throw error;
        if (attempt >= delays.length || await quotaBlocked($)) {
          await record($, { agentId: e.agentId ?? null, trigger: e.trigger,
            phase: 'failed', attempts: attempt + 1 });
          await note($, delays.length ? '压缩失败，已保留原上下文；有限重试已停止。'
            : '压缩失败，已保留原上下文。');
          throw error;
        }
        await record($, { agentId: e.agentId ?? null, trigger: e.trigger,
          phase: 'retrying', attempts: attempt + 1 });
        await note($, `压缩失败，正在自动重试 ${attempt + 1}/${delays.length}。`);
        await $.clock.sleep(delays[attempt], { signal: next.signal });
        if (next.signal.aborted) throw error;
      }
    }
  });

  on('turn.start', ($, e, next) => {
    state.running = true;
    state.suspended = false;
    return next(e);
  });
  on('turn.complete', async ($, e, next) => {
    const result = await next(e);
    if (!e.agentId) {
      state.running = false;
      state.suspended = e.isAborted || e.reason !== 'answer';
      if (!state.suspended) await recoverIdle($, state);
    }
    return result;
  });
  on('session.start', async ($, e, next) => {
    const result = await next(e);
    state.timer?.cancel();
    state.timer = $.clock.every(30000, () => recoverIdle($, state));
    return result;
  });
  on('session.end', ($, e, next) => {
    state.timer?.cancel();
    state.timer = undefined;
    state.suspended = true;
    return next(e);
  });
}

async function recoverIdle($, state) {
  if (state.running || state.checking || state.suspended) return;
  state.checking = true;
  try {
    const now = await $.clock.now();
    if (now - state.lastAttempt < IDLE_COOLDOWN) return;
    // "summary" estimates locally; it makes no token-count API request.
    // The engine resolves env / --autocompact / per-model / settings for us.
    const { context, rateLimits } = await $.session.usage({ breakdown: 'summary' });
    const window = context.breakdown;
    if (state.running || state.suspended || !window?.isAutoCompactEnabled || window.autocompactSource === 'auto' ||
        !Number.isFinite(window.rawMaxTokens) || window.rawMaxTokens < 100000 ||
        !Number.isFinite(context.tokens) || context.tokens < window.rawMaxTokens ||
        rateLimits.some(limit => limit.percentUsed >= 100)) return;
    state.lastAttempt = now;
    await record($, { phase: 'idle-recovery', tokensBefore: context.tokens,
      window: window.rawMaxTokens });
    await note($, `上下文已超过 ${window.rawMaxTokens} tokens，正在空闲时补做压缩。`);
    // This public API refuses to run if another turn won the race. It neither
    // interrupts a turn nor clears the prompt box. No synthetic "continue".
    const result = await $.session.compact();
    if (result.skip) state.suspended = true;
    await record($, { phase: result.skip ? 'skipped' : 'complete',
      trigger: 'idle-recovery', tokensBefore: result.tokensBefore ?? context.tokens,
      tokensAfter: result.tokensAfter ?? null });
  } catch (error) {
    if (/abort|cancel|interrupt/i.test(String(error))) state.suspended = true;
    await record($, { phase: 'idle-deferred' });
    // Busy, interrupted or offline: retry only at a later safe boundary.
  } finally {
    state.checking = false;
  }
}
