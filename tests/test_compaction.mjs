// SPDX-License-Identifier: Apache-2.0
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
const source = await readFile(new URL('../plugins/compaction-recovery/hooks/register.js', import.meta.url));
const { register } = await import('data:text/javascript;base64,' + source.toString('base64'));

function fixture() {
  const hooks = new Map();
  register((event, hook) => hooks.set(event, hook));
  const f = { hooks, now: 1000000, calls: 0, sleeps: [], records: [], notes: [], ticks: [],
    usage: { context: { tokens: 320000, breakdown: { rawMaxTokens: 272000,
      autocompactSource: 'env', isAutoCompactEnabled: true } }, rateLimits: [] } };
  f.$ = {
    clock: { now: async () => f.now,
      sleep: async (ms, { signal }) => { f.sleeps.push(ms); if (signal.aborted) throw Error('aborted'); },
      every: (ms, fn) => { const timer = { ms, fn, cancelled: false,
        cancel() { this.cancelled = true; } }; f.ticks.push(timer); return timer; } },
    session: { usage: async () => f.usage, compact: async () => {
      f.calls += 1; return { messages: [], tokensBefore: 320000, tokensAfter: 15000 }; } },
    store: { set: async (key, value) => f.records.push(value) },
    ui: { log: async text => f.notes.push(text) },
  };
  f.dispatch = (name, e, next = async () => ({})) => {
    next.signal ??= new AbortController().signal;
    return hooks.get(name)(f.$, e, next);
  };
  f.complete = e => f.dispatch('turn.complete', { reason: 'answer', isAborted: false, ...e },
    async () => ({ text: 'original answer' }));
  return f;
}

for (const agentId of [undefined, 'fixture-child']) {
  test(`three failures recover before another request (${agentId ?? 'main'})`, async () => {
    const f = fixture(); let calls = 0;
    const e = { trigger: 'auto', agentId, messages: [{ handle: 'kept-by-engine' }] };
    const result = { messages: [{ handle: 'engine-summary' }], tokensBefore: 260000, tokensAfter: 12000 };
    const next = async input => { assert.strictEqual(input, e);
      if (++calls < 4) throw Error('transient'); return result; };
    assert.strictEqual(await f.dispatch('session.compact', e, next), result);
    assert.deepEqual(f.sleeps, [500, 1000, 2000]);
    assert.equal(f.records.at(-1).phase, 'complete');
    assert.equal(f.records.at(-1).agentId, agentId ?? null);
    assert.match(f.notes.at(-1), /压缩已恢复：260000 → 12000 tokens/);
  });
}

test('permanent failure stays bounded and preserves the error', async () => {
  const f = fixture(); let calls = 0; const error = Error('fixture');
  await assert.rejects(f.dispatch('session.compact', { trigger: 'auto' }, async () => {
    calls++; throw error; }), e => e === error);
  assert.equal(calls, 4); assert.equal(f.records.at(-1).phase, 'failed');
  assert.ok(f.notes.every(text => !text.includes('压缩已恢复')));
});

test('hook veto passes through once', async () => {
  const f = fixture(); const veto = { skip: 'user policy' }; let calls = 0;
  assert.strictEqual(await f.dispatch('session.compact', { trigger: 'auto' }, async () => {
    calls++; return veto; }), veto);
  assert.equal(calls, 1); assert.equal(f.sleeps.length, 0);
});

for (const trigger of ['manual', 'plugin', 'precompute']) {
  test(`${trigger} failure is not retried`, async () => {
    const f = fixture(); let calls = 0;
    await assert.rejects(f.dispatch('session.compact', { trigger }, async () => {
      calls++; throw Error('fixture'); }));
    assert.equal(calls, 1);
  });
}

test('cancelled dispatch makes no retry', async () => {
  const f = fixture(); const abort = new AbortController(); let calls = 0;
  const next = async () => { calls++; abort.abort(); throw Error('cancelled'); };
  next.signal = abort.signal;
  await assert.rejects(f.dispatch('session.compact', { trigger: 'auto' }, next));
  assert.equal(calls, 1); assert.equal(f.sleeps.length, 0);
});

test('observed exhausted quota is handed back to native rotation', async () => {
  const f = fixture(); let calls = 0;
  f.usage.rateLimits = [{ percentUsed: 100 }];
  await assert.rejects(f.dispatch('session.compact', { trigger: 'auto' }, async () => {
    calls++; throw Error('quota'); }));
  assert.equal(calls, 1);
});

test('recording failure cannot replace a successful summary', async () => {
  const f = fixture(); f.$.store.set = async () => { throw Error('disk full'); };
  const result = { messages: [{ handle: 'original-summary' }] };
  assert.strictEqual(await f.dispatch('session.compact', { trigger: 'auto' }, async () => result), result);
});

test('idle main exceeds its explicit window and recovers, without changing answer', async () => {
  const f = fixture(); assert.deepEqual(await f.complete(), { text: 'original answer' });
  assert.equal(f.calls, 1); assert.equal(f.records.at(-1).phase, 'complete');
  await f.complete(); assert.equal(f.calls, 1);
  f.now += 300000; await f.complete(); assert.equal(f.calls, 2);
});

for (const reason of ['aborted', 'error', 'refusal']) {
  test(`${reason} suspends timer recovery until another main turn`, async () => {
    const f = fixture(); await f.dispatch('session.start', {});
    await f.complete({ reason, isAborted: reason === 'aborted' });
    await f.ticks[0].fn(); assert.equal(f.calls, 0);
    await f.dispatch('turn.start', {}); await f.ticks[0].fn(); assert.equal(f.calls, 0);
    await f.complete(); assert.equal(f.calls, 1);
  });
}

for (const scenario of ['disabled', 'auto', 'below', 'missing', 'quota', 'agent']) {
  test(`idle recovery skips ${scenario}`, async () => {
    const f = fixture();
    if (scenario === 'disabled') f.usage.context.breakdown.isAutoCompactEnabled = false;
    if (scenario === 'auto') f.usage.context.breakdown.autocompactSource = 'auto';
    if (scenario === 'below') f.usage.context.tokens = 270000;
    if (scenario === 'missing') delete f.usage.context.tokens;
    if (scenario === 'quota') f.usage.rateLimits = [{ percentUsed: 100 }];
    await f.complete(scenario === 'agent' ? { agentId: 'child' } : {});
    assert.equal(f.calls, 0);
  });
}

test('busy race defers and obeys cooldown; no prompt submission or abort API exists', async () => {
  const f = fixture(); f.$.session.compact = async () => { f.calls++; throw Error('busy'); };
  await f.complete(); await f.complete(); assert.equal(f.calls, 1);
  assert.equal(f.records.at(-1).phase, 'idle-deferred');
});

test('session end cancels its recovery timer', async () => {
  const f = fixture(); await f.dispatch('session.start', {});
  await f.dispatch('session.end', {});
  assert.equal(f.ticks[0].cancelled, true);
  await f.ticks[0].fn(); assert.equal(f.calls, 0);
});

test('cancelling an idle compaction also suspends the periodic retry', async () => {
  const f = fixture(); await f.dispatch('session.start', {});
  f.$.session.compact = async () => { f.calls++; throw Error('AbortError: cancelled'); };
  await f.complete(); f.now += 300001; await f.ticks[0].fn();
  assert.equal(f.calls, 1);
});

test('idle hook veto is respected until another user turn', async () => {
  const f = fixture(); await f.dispatch('session.start', {});
  f.$.session.compact = async () => { f.calls++; return { skip: 'policy' }; };
  await f.complete(); f.now += 300001; await f.ticks[0].fn();
  assert.equal(f.calls, 1);
});

test('400K window uses the resolved setting instead of the previous 272K value', async () => {
  const f = fixture();
  f.usage.context.breakdown.rawMaxTokens = 400000;
  f.usage.context.tokens = 399999;
  await f.complete(); assert.equal(f.calls, 0);
  f.usage.context.tokens = 410000;
  await f.complete(); assert.equal(f.calls, 1);
  assert.equal(f.records.find(record => record.phase === 'idle-recovery').window, 400000);
});
