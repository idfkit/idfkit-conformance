/**
 * The `update` operation and the `post-operation-epjson` assertion, as `model.mjs` reads them.
 *
 * The test bodies below are `test_update.py`'s, test for test and in its order, so that a rule
 * enforced on one side and not the other fails one of the two files.
 *
 * Run it from the root of the repository:
 *
 *     node --test runners/tests/test-update.mjs
 */

import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import {
  Assertion,
  CaseError,
  EXPECTED_AFTER_EPJSON,
  ManifestError,
  Operation,
  OperationKind,
  OperationOutcome,
  loadCase,
  loadManifest,
} from '../model.mjs';

const UPDATE = Object.freeze({
  op: 'update',
  type: 'Zone',
  name: 'Office',
  fields: { x_origin: '10.0' },
});

/**
 * A one-entry manifest in the convention section, carrying `operation`.
 *
 * @param {Record<string, unknown>} operation
 * @param {Record<string, unknown>} [entry]
 * @returns {string}
 */
function manifestWith(operation, entry = {}) {
  const raw = {
    id: 'update-example',
    title: 'An update',
    tags: ['tier1'],
    energyplus_version: '26.1.0',
    assertions: ['parse-outcome', 'post-operation-epjson'],
    input: 'input.idf',
    parse_outcome: 'success',
    expected_after_epjson: EXPECTED_AFTER_EPJSON,
    operations: [operation],
    ...entry,
  };
  const path = join(mkdtempSync(join(tmpdir(), 'update-')), 'manifest.json');
  writeFileSync(path, JSON.stringify({ schema_version: 1, oracle: [], convention: [raw] }));
  return path;
}

test('update is the fifth kind', () => {
  assert.equal(OperationKind.UPDATE, 'update');
  assert.equal(Object.keys(OperationKind).length, 5);
});

test('an update defaults to applied', () => {
  const operation = new Operation({
    op: OperationKind.UPDATE,
    type: 'Zone',
    name: 'Office',
    fields: { x_origin: '1' },
  });
  assert.equal(operation.outcome, OperationOutcome.APPLIED);
  assert.equal('outcome' in operation.toJsonObj(), false);
});

test('a refused update round trips its outcome', () => {
  const manifest = loadManifest(manifestWith({ ...UPDATE, outcome: 'refused' }));
  const [operation] = manifest.convention[0].operations;
  assert.equal(operation.outcome, OperationOutcome.REFUSED);
  assert.deepEqual(operation.toJsonObj(), { ...UPDATE, outcome: 'refused' });
});

test('an update may carry an empty group', () => {
  const operation = new Operation({ op: OperationKind.UPDATE, type: 'Zone', name: 'Office', fields: {} });
  assert.deepEqual({ ...operation.fields }, {});
});

test('a field value may be a list of groups', () => {
  const group = { vertex_x_coordinate: '0', vertex_y_coordinate: '1', vertex_z_coordinate: '2' };
  const raw = {
    ...UPDATE,
    type: 'BuildingSurface:Detailed',
    name: 'Wall',
    fields: { vertices: [group, group] },
  };
  const [operation] = loadManifest(manifestWith(raw)).convention[0].operations;
  assert.deepEqual(JSON.parse(JSON.stringify(operation.fields)), { vertices: [group, group] });
  assert.deepEqual(JSON.parse(JSON.stringify(operation.toJsonObj())), raw);
});

test('a group member must be text', () => {
  const raw = { ...UPDATE, fields: { vertices: [{ vertex_x_coordinate: 0 }] } };
  assert.throws(
    () => loadManifest(manifestWith(raw)),
    (error) => error instanceof ManifestError && /must be a string/.test(error.message)
  );
});

test('an update requires fields', () => {
  assert.throws(
    () => new Operation({ op: OperationKind.UPDATE, type: 'Zone', name: 'Office' }),
    (error) => error instanceof ManifestError && /'update' requires 'fields'/.test(error.message)
  );
});

for (const key of ['field', 'value', 'to']) {
  test(`an update takes no single field keys: ${key}`, () => {
    assert.throws(
      () => loadManifest(manifestWith({ ...UPDATE, [key]: 'x' })),
      (error) => error instanceof ManifestError && error.message.includes(`'${key}'`)
    );
  });
}

test('an unknown outcome is refused', () => {
  assert.throws(
    () => loadManifest(manifestWith({ ...UPDATE, outcome: 'partial' })),
    (error) => error instanceof ManifestError && /outcome/.test(error.message)
  );
});

test('outcome belongs to update alone', () => {
  const operation = {
    op: 'set-field',
    type: 'Zone',
    name: 'Office',
    field: 'x_origin',
    value: '1',
    outcome: 'applied',
  };
  assert.throws(
    () => loadManifest(manifestWith(operation)),
    (error) => error instanceof ManifestError && /takes no 'outcome'/.test(error.message)
  );
});

test('post-operation-epjson requires its manifest key', () => {
  assert.throws(
    () => loadManifest(manifestWith(UPDATE, { expected_after_epjson: null })),
    (error) => error instanceof ManifestError && /expected_after_epjson/.test(error.message)
  );
});

test('a convention entry may name the post-operation expectation', () => {
  const entry = loadManifest(manifestWith(UPDATE)).convention[0];
  assert.ok(entry.assertions.includes(Assertion.POST_OPERATION_EPJSON));
  assert.equal(entry.expectedAfterEpjson, EXPECTED_AFTER_EPJSON);
  assert.equal(entry.expected, null);
});

test('a case declaring post-operation-epjson needs the file', () => {
  const caseDir = join(mkdtempSync(join(tmpdir(), 'update-')), 'update-example');
  mkdirSync(caseDir);
  writeFileSync(
    join(caseDir, 'case.toml'),
    'title = "An update"\n' +
      'why = "Because."\n' +
      'tags = ["tier1"]\n' +
      'energyplus_version = "26.1.0"\n' +
      'truth = "convention"\n' +
      'assertions = ["parse-outcome", "post-operation-epjson"]\n'
  );
  assert.throws(
    () => loadCase(caseDir),
    (error) => error instanceof CaseError && error.message.includes(EXPECTED_AFTER_EPJSON)
  );
  writeFileSync(join(caseDir, EXPECTED_AFTER_EPJSON), '{}');
  assert.equal(loadCase(caseDir).truth, 'convention');
});
