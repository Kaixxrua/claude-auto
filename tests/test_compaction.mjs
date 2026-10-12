// SPDX-License-Identifier: Apache-2.0
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
const source = await readFile(new URL('../plugins/compaction-recovery/hooks/register.js', import.meta.url));
const { register } = await import('data:text/javascript;base64,' + source.toString('base64'));

function fixture() {
  const hooks = new Map();
  register((event, hook) => hooks.set(event, hook));
  const f = { hooks, now: 1000000, sessionId: 'fixture-session', records: [], notes: [], stored: new Map() };
  const forbidden = () => { throw Error('Observation must not schedule compaction or query usage'); };
  f.$ = {
    clock: { now: async () => f.now, sleep: forbidden, every: forbidden },
    session: { id: async () => f.sessionId, usage: forbidden, compact: forbidden },
    store: { set: async (key, value) => { f.records.push({ key, ...value }); f.stored.set(key, value); } },
    ui: { log: async text => f.notes.push(text) },
  };
  f.dispatch = (e, next) => {
    next.signal ??= new AbortController().signal;
    return hooks.get('session.compact')(f.$, e, next);
  };
  return f;
}

for (const trigger of ['auto', 'manual', 'plugin', 'precompute']) {
  for (const agentId of [undefined, 'fixture-child']) {
    test(`${trigger}: native success is dispatched once and unchanged (${agentId ?? 'main'})`, async () => {
      const f = fixture(); let calls = 0;
      const e = { trigger, agentId, messages: [{ handle: 'original-message' }] };
      const result = { messages: [{ handle: 'native-summary' }], tokensBefore: 370000, tokensAfter: 12000 };
      const returned = await f.dispatch(e, async input => {
        assert.strictEqual(input, e); calls++; f.now += 720;
        return result;
      });
      assert.strictEqual(returned, result); assert.equal(calls, 1);
      assert.equal(f.records[0].phase, 'running');
      assert.equal(f.records.at(-1).phase, trigger === 'precompute' ? 'prepared' : 'complete');
      assert.equal(f.records.at(-1).elapsedMs, 720);
      assert.equal(f.records.at(-1).agentId, agentId ?? null);
      assert.equal(f.notes.length, 0);
    });
  }

  test(`${trigger}: failure is not retried, vetoed, or converted to success`, async () => {
    const f = fixture(); let calls = 0; const error = Error('Connection dropped (ECONNRESET)');
    await assert.rejects(f.dispatch({ trigger }, async () => { calls++; throw error; }), e => e === error);
    assert.equal(calls, 1); assert.equal(f.records.at(-1).phase, 'failed');
    assert.equal(f.records.at(-1).failure, 'connection');
    assert.equal(f.notes.length, 1);
    assert.match(f.notes[0], trigger === 'precompute' ? /后台预摘要未完成/ : /上下文压缩未完成/);
    assert.doesNotMatch(f.notes[0], /重试|已恢复|成功|已停止/);
  });
}

test('native veto remains unchanged and is never recorded as a completed summary', async () => {
  const f = fixture(); const veto = { skip: 'user policy' }; let calls = 0;
  assert.strictEqual(await f.dispatch({ trigger: 'auto' }, async () => { calls++; return veto; }), veto);
  assert.equal(calls, 1); assert.equal(f.records.at(-1).phase, 'skipped'); assert.equal(f.notes.length, 0);
});

test('cancellation preserves the exact error, with no failure notice or retry', async () => {
  const f = fixture(); const abort = new AbortController(); const error = Error('cancelled'); let calls = 0;
  const next = async () => { calls++; abort.abort(); throw error; }; next.signal = abort.signal;
  await assert.rejects(f.dispatch({ trigger: 'auto' }, next), e => e === error);
  assert.equal(calls, 1); assert.equal(f.records.at(-1).phase, 'cancelled'); assert.deepEqual(f.notes, []);
});

for (const operation of ['session.id', 'clock.now', 'store.set', 'ui.log']) {
  test(`${operation} failure cannot change native success or failure`, async () => {
    const f = fixture(); const [noun, method] = operation.split('.');
    f.$[noun][method] = async () => { throw Error('diagnostic failure'); };
    const result = { messages: [{ handle: 'native-summary' }] }; let calls = 0;
    assert.strictEqual(await f.dispatch({ trigger: 'auto' }, async () => { calls++; return result; }), result);
    const error = Error('upstream failure');
    await assert.rejects(f.dispatch({ trigger: 'auto' }, async () => { calls++; throw error; }), e => e === error);
    assert.equal(calls, 2);
    if (operation === 'session.id') assert.equal(f.records.length, 0);
  });
}

test('there are no turn-end or timer hooks to start idle compaction', () => {
  assert.deepEqual([...fixture().hooks.keys()], ['session.compact']);
});

test('concurrent foreground and precomputed summaries keep separate records', async () => {
  const f = fixture(); let finish;
  const waiting = new Promise(resolve => { finish = resolve; });
  const foreground = f.dispatch({ trigger: 'auto' }, () => waiting);
  await new Promise(resolve => setImmediate(resolve));
  await assert.rejects(f.dispatch({ trigger: 'precompute' }, async () => { throw Error('ECONNRESET'); }));
  assert.equal(f.stored.get('compaction:fixture-session:main:auto').phase, 'running');
  assert.equal(f.stored.get('compaction:fixture-session:main:precompute').phase, 'failed');
  assert.match(f.notes.at(-1), /后台预摘要.*前台压缩状态/);
  finish({ messages: [], tokensBefore: 370000, tokensAfter: 15000 }); await foreground;
  assert.equal(f.stored.get('compaction:fixture-session:main:auto').phase, 'complete');
});

test('older completion cannot overwrite a newer dispatch in the same lane', async () => {
  const f = fixture(); let failOld, finishNew;
  const old = f.dispatch({ trigger: 'precompute' }, () => new Promise((_, reject) => { failOld = reject; }));
  await new Promise(resolve => setImmediate(resolve));
  f.now += 1000;
  const newer = f.dispatch({ trigger: 'precompute' }, () => new Promise(resolve => { finishNew = resolve; }));
  await new Promise(resolve => setImmediate(resolve));
  const rejected = assert.rejects(old);
  failOld(Error('old request failed')); await rejected;
  assert.equal(f.stored.get('compaction:fixture-session:main:precompute').phase, 'running');
  assert.equal(f.notes.length, 0);
  finishNew({ messages: [], tokensBefore: 370000 }); await newer;
  assert.equal(f.records.at(-1).phase, 'prepared'); assert.equal(f.records.at(-1).tokensAfter, null);
});

test('session and agent identity prevent shared-store status collisions', async () => {
  const f = fixture(); const next = async () => ({ messages: [] });
  await f.dispatch({ trigger: 'auto' }, next);
  await f.dispatch({ trigger: 'auto', agentId: 'child' }, next);
  f.sessionId = 'another-session'; await f.dispatch({ trigger: 'auto' }, next);
  assert.equal(f.stored.size, 3);
});

for (const [message, kind] of [
  ['No response from API (waited 299s)', 'timeout'],
  ["You've hit your session limit (HTTP 429)", 'quota'],
  ['ECONNRESET', 'connection'],
  ['fixture-private-error-detail', 'other'],
]) {
  test(`only a safe ${kind} category is recorded, never the error or prompt`, async () => {
    const f = fixture();
    await assert.rejects(f.dispatch({ trigger: 'auto', messages: [{ text: 'fixture-private-prompt' }] },
      async () => { throw Error(message); }));
    assert.equal(f.records.at(-1).failure, kind);
    const output = JSON.stringify([f.records, f.notes]);
    assert.ok(!output.includes(message)); assert.ok(!output.includes('fixture-private-prompt'));
    assert.ok(!output.includes('messages'));
  });
}
