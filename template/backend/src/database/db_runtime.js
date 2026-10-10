const { AsyncLocalStorage } = require('node:async_hooks');
const { initializeDatabase } = require('./init_db');

// One SQLite connection has one transaction state. Queue complete transactions
// and ordinary operations together, so another request cannot join a transaction
// accidentally (or have its successful write erased by that request's rollback).
const transactionContext = new AsyncLocalStorage();
let operationQueue = Promise.resolve();

function enqueue(work) {
  const result = operationQueue.then(work);
  operationQueue = result.catch(() => undefined);
  return result;
}

function withDatabase(work) {
  const owner = transactionContext.getStore();
  if (owner?.active) return work(owner.database);
  return enqueue(async () => work(await initializeDatabase()));
}

function normalizeParams(params) {
  if (Array.isArray(params)) {
    return params;
  }
  if (params === undefined) {
    return [];
  }
  return [params];
}

function runOnDatabase(database, sql, params = []) {
  return new Promise((resolve, reject) => {
    database.run(sql, normalizeParams(params), function handleRun(err) {
      if (err) {
        reject(err);
        return;
      }
      resolve({
        lastID: this.lastID,
        changes: this.changes,
      });
    });
  });
}

function getOnDatabase(database, sql, params = []) {
  return new Promise((resolve, reject) => {
    database.get(sql, normalizeParams(params), (err, row) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(row || null);
    });
  });
}

function allOnDatabase(database, sql, params = []) {
  return new Promise((resolve, reject) => {
    database.all(sql, normalizeParams(params), (err, rows) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(rows || []);
    });
  });
}

function execOnDatabase(database, sql) {
  return new Promise((resolve, reject) => {
    database.exec(sql, (err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

async function run(sql, params = []) {
  return withDatabase(database => runOnDatabase(database, sql, params));
}

async function get(sql, params = []) {
  return withDatabase(database => getOnDatabase(database, sql, params));
}

async function all(sql, params = []) {
  return withDatabase(database => allOnDatabase(database, sql, params));
}

async function exec(sql) {
  return withDatabase(database => execOnDatabase(database, sql));
}

async function withTransaction(work) {
  if (transactionContext.getStore()?.active) {
    throw new Error('Nested transactions are not supported; reuse the current transaction');
  }
  return enqueue(async () => {
    const database = await initializeDatabase();
    await execOnDatabase(database, 'BEGIN IMMEDIATE TRANSACTION;');
    const owner = { database, active: true };
    const scoped = operation => (...args) => owner.active
      ? operation(database, ...args)
      : Promise.reject(new Error('Transaction is no longer active'));
    const tx = {
      run: scoped(runOnDatabase), get: scoped(getOnDatabase),
      all: scoped(allOnDatabase), exec: scoped(execOnDatabase),
    };

    try {
      const result = await transactionContext.run(owner, () => work(tx));
      owner.active = false;
      await execOnDatabase(database, 'COMMIT;');
      return result;
    } catch (error) {
      owner.active = false;
      try {
        await execOnDatabase(database, 'ROLLBACK;');
      } catch (rollbackError) {
        if (error && typeof error === 'object') error.rollbackError = rollbackError;
      }
      throw error;
    }
  });
}

module.exports = {
  run,
  get,
  all,
  exec,
  withTransaction,
};
