'use strict';
// Fixed quarter/prefix partitions keep the existing model history bounded.
// The original unpartitioned ledger is retained as immutable inherited history.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { canonical } = require('../lib/strategy-claim-patches');
const LEGACY = 'web/data/semantic-model-ledger.json';
const digest = value => crypto.createHash('sha256').update(canonical(value)).digest('hex');
function fail() { const error = new Error('SEMANTIC_LEDGER_INVALID'); error.code = error.message; throw error; }
function fileFor(key) {
  const match = /^(2026Q[23]):(\d{6})$/.exec(key); if (!match) fail();
  return `web/data/periods/${match[1]}/semantic-model-ledgers/${match[2].slice(0, 2)}.json`;
}
function read(file, maximum) {
  if (!fs.existsSync(file)) return { schemaVersion: 1, kind: 'strategy-semantic-ledger', products: {} };
  if (fs.lstatSync(file).isSymbolicLink() || fs.statSync(file).size > maximum) fail();
  const value = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (value?.schemaVersion !== 1 || value.kind !== 'strategy-semantic-ledger'
    || !value.products || typeof value.products !== 'object' || Array.isArray(value.products)) fail();
  return value;
}
function validEntry(value) {
  if (!value || !Array.isArray(value.usageHistory) || !Array.isArray(value.attempts)
    || value.usageHistory.length > 64 || value.attempts.length > 128
    || value.usageHistoryDigest !== digest(value.usageHistory)) fail();
  return value;
}
class Ledger {
  constructor(root) { this.root = root; this.legacy = read(path.join(root, LEGACY), 10000000); this.shards = new Map(); this.changed = new Set(); }
  shard(key) {
    const relative = fileFor(key);
    if (!this.shards.has(relative)) {
      const value = read(path.join(this.root, relative), 4000000);
      if (Object.keys(value.products).some(stored => fileFor(stored) !== relative)) fail();
      this.shards.set(relative, value);
    }
    return this.shards.get(relative);
  }
  get(key) {
    const inherited = this.legacy.products[key], current = this.shard(key).products[key];
    if (inherited) validEntry(inherited);
    if (current) validEntry(current);
    if (current && inherited && (digest(current.usageHistory.slice(0, inherited.usageHistory.length)) !== digest(inherited.usageHistory)
      || digest(current.attempts.slice(0, inherited.attempts.length)) !== digest(inherited.attempts))) fail();
    return current || inherited || null;
  }
  set(key, value) { validEntry(value); this.shard(key).products[key] = value; this.changed.add(fileFor(key)); }
  save() {
    for (const relative of this.changed) {
      const file = path.join(this.root, relative), raw = JSON.stringify(this.shards.get(relative)) + '\n';
      if (Buffer.byteLength(raw) > 4000000) fail();
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const temporary = file + '.tmp'; fs.writeFileSync(temporary, raw, { mode: 0o600 }); fs.renameSync(temporary, file);
    }
    this.changed.clear();
  }
}
module.exports = { Ledger, fileFor, LEGACY };
