// SPDX-License-Identifier: Apache-2.0
// Observe the native scheduler. Never start, repeat, or replace a compaction.

async function observe(operation) {
  try { return await operation(); } catch { return undefined; }
}

function failureKind(error) {
  const message = String(error?.message ?? error);
  if (/ECONNRESET|ECONNREFUSED|EPIPE|connection (?:dropped|lost|reset)/i.test(message)) return 'connection';
  if (/timeout|timed out|no response from API/i.test(message)) return 'timeout';
  if (/rate.?limit|hit your .+ limit|HTTP 429/i.test(message)) return 'quota';
  return 'other';
}

function failureNotice(trigger, agentId, kind) {
  const reason = { connection: '连接中断', timeout: '请求超时', quota: '额度限制', other: '请求失败' }[kind];
  // A precomputed summary has not replaced the transcript. Its failure says
  // nothing about a concurrent foreground compaction or its spinner.
  if (trigger === 'precompute') return `后台预摘要未完成（${reason}）；前台压缩状态以 Claude Code 为准。`;
  return `${agentId ? '子代理' : '上下文'}压缩未完成（${reason}）；交由 Claude Code 原版处理，插件不追加请求。`;
}

export function register(on) {
  const active = new Map();
  on('session.compact', async ($, e, next) => {
    const [sessionId, startedAt] = await Promise.all([
      observe(() => $.session.id()), observe(() => $.clock.now()),
    ]);
    const key = sessionId ? `compaction:${sessionId}:${e.agentId ?? 'main'}:${e.trigger}` : null;
    const operation = {};
    if (key) active.set(key, operation);
    const current = () => !key || active.get(key) === operation;
    async function record(values) {
      if (!key || !current()) return;
      await observe(async () => {
        const at = await $.clock.now();
        if (!current()) return;
        await $.store.set(key, {
          version: 2, sessionId, agentId: e.agentId ?? null, trigger: e.trigger,
          startedAt: startedAt ?? null, at,
          elapsedMs: Number.isFinite(startedAt) ? Math.max(0, at - startedAt) : null,
          ...values,
        });
      });
    }
    await record({ phase: 'running' });
    try {
      // Exactly one native dispatch, with the original input and result.
      // Native retries, precomputation, cancellation and history stay native.
      const result = await next(e);
      await record({
        phase: result.skip ? 'skipped' : e.trigger === 'precompute' ? 'prepared' : 'complete',
        tokensBefore: result.tokensBefore ?? null, tokensAfter: result.tokensAfter ?? null,
      });
      return result;
    } catch (error) {
      const cancelled = next.signal.aborted;
      const kind = failureKind(error);
      await record({ phase: cancelled ? 'cancelled' : 'failed', failure: kind });
      if (!cancelled && current()) await observe(() => $.ui.log(failureNotice(e.trigger, e.agentId, kind)));
      // Propagate the same failure. Returning a fabricated summary or a veto
      // here would change the native scheduler's outcome.
      throw error;
    } finally {
      if (key && current()) active.delete(key);
    }
  });
}
