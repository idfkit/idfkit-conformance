/**
 * Typed shapes for the idfkit conformance corpus.
 *
 * The JavaScript counterpart of `model.py`, field for field: the same types, the same enum
 * members, the same validation rules, the same loaders, the same error conditions. A field on one
 * side and not the other is a defect, because this pair is what keeps the two runners honest about
 * the same data.
 *
 * Three files carry the corpus structure, and this module is the single JavaScript definition of
 * all three:
 *
 *   cases/<id>/case.toml    becomes a `Case`
 *   manifest.json           becomes a `Manifest` of `ManifestEntry`
 *   known-divergence.toml   becomes a `DivergenceRegister` of `Divergence`
 *
 * Every shape is a frozen class instance and every enumerated field is a real enum, so a runner
 * never indexes into a bare object and never compares a bare string against a spelling it hopes is
 * correct. The reader helpers below are the only place where untyped JSON and TOML crosses into
 * the model, and they throw a `CorpusError` naming the offending file and field on any rule the
 * file violates.
 *
 * `manifest.schema.json` stays the normative schema for `manifest.json`. This module mirrors it
 * field for field and adds the two constraints JSON Schema cannot express: case-id uniqueness
 * across the two sections, and agreement between a manifest entry and the case's own `case.toml`.
 *
 * There is deliberately no `truth` field in a manifest entry on disk: the section an entry sits in
 * is the truth value (FR-020). `ManifestEntry` carries `truth` in memory, derived from the section
 * it was read from, and never serializes it back.
 *
 * NAMING: the on-disk keys stay snake_case, because they are the same bytes both runners read. The
 * in-memory fields are camelCase, because that is the JavaScript idiom and the rest of
 * `@idfkit/core` is written that way. The mapping is one to one: `energyplus_version` on disk is
 * `energyplusVersion` in memory, and so on for `parse_outcome`, `expected_diagnostics`,
 * `corpus_level` and `schema_version`.
 *
 * TOML: Node has no TOML parser in its standard library and this repository has no `package.json`,
 * so rather than take a dependency this module vendors the small reader in the "Reading TOML"
 * section below. It is not a complete TOML 1.0 implementation; its limits are listed there. It
 * covers `case.toml` and `known-divergence.toml`, which are the only TOML this module reads.
 */

import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { basename, join } from 'node:path';

// ---------------------------------------------------------------------------
// Constants and patterns, mirroring manifest.schema.json
// ---------------------------------------------------------------------------

export const MANIFEST_SCHEMA_VERSION = 1;
export const MANIFEST_SCHEMA_REF = './manifest.schema.json';

export const CASE_FILE = 'case.toml';
export const EXPECTED_EPJSON = 'expected.epJSON';
export const EXPECTED_DIAGNOSTICS = 'expected.diag.json';
export const EXPECTED_VALIDATION = 'expected.validation.json';
export const EXPECTED_INTROSPECTION = 'expected.introspection.json';
export const EXPECTED_DOCS_URL = 'expected.docs-url.json';
export const EXPECTED_TYPE_LOOKUP = 'expected.type-lookup.json';

export const CASES_DIR = 'cases';
export const MANIFEST_FILE = 'manifest.json';
export const DIVERGENCE_FILE = 'known-divergence.toml';

export const CASE_ID_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/;
export const CASE_ID_MAX_LENGTH = 64;
export const ENERGYPLUS_VERSION_PATTERN = /^[0-9]+\.[0-9]+\.[0-9]+$/;
export const CORPUS_LEVEL_PATTERN = /^conformance-[0-9]{4}\.[0-9]+$/;
export const ISSUE_URL_PATTERN = /^https?:\/\/\S+$/;

// ---------------------------------------------------------------------------
// Enumerations
// ---------------------------------------------------------------------------

/**
 * Where a case's expectation comes from (FR-020).
 *
 * `ORACLE` means EnergyPlus ConvertInputFormat produced it and it is committed. `CONVENTION` means
 * the two libraries agreed on it and no external authority ruled, so no expectation file may exist.
 * The manifest keeps the two in separate sections precisely so the second is never read as the
 * first.
 *
 * @readonly
 * @enum {string}
 */
export const Truth = Object.freeze({
  ORACLE: 'oracle',
  CONVENTION: 'convention',
});

/**
 * The four assertions. `DIAGNOSTICS` is accepted and skipped by the runners until phase two.
 *
 * @readonly
 * @enum {string}
 */
export const Assertion = Object.freeze({
  PARSE_OUTCOME: 'parse-outcome',
  EPJSON: 'epjson',
  ROUND_TRIP: 'round-trip',
  DIAGNOSTICS: 'diagnostics',
  VALIDATION: 'validation',
  INTROSPECTION: 'introspection',
  DOCS_URL: 'docs-url',
  TYPE_LOOKUP: 'type-lookup',
});

/**
 * The assertion that needs an expectation file, the manifest key that names it, and the file it
 * names. One table rather than four hand-written rules, so that adding an assertion cannot leave
 * one of the four checks behind. `EPJSON`'s expectation is `expected`, which is governed by `truth`
 * rather than by an assertion, and is deliberately not in here.
 *
 * Mirrors `EXPECTATION_FILES` in `model.py`.
 *
 * @type {ReadonlyArray<{assertion: Assertion, key: string, property: string, file: string}>}
 */
export const EXPECTATION_FILES = Object.freeze([
  {
    assertion: 'diagnostics',
    key: 'expected_diagnostics',
    property: 'expectedDiagnostics',
    file: EXPECTED_DIAGNOSTICS,
  },
  {
    assertion: 'validation',
    key: 'expected_validation',
    property: 'expectedValidation',
    file: EXPECTED_VALIDATION,
  },
  {
    assertion: 'introspection',
    key: 'expected_introspection',
    property: 'expectedIntrospection',
    file: EXPECTED_INTROSPECTION,
  },
  {
    assertion: 'docs-url',
    key: 'expected_docs_url',
    property: 'expectedDocsUrl',
    file: EXPECTED_DOCS_URL,
  },
  {
    assertion: 'type-lookup',
    key: 'expected_type_lookup',
    property: 'expectedTypeLookup',
    file: EXPECTED_TYPE_LOOKUP,
  },
]);

/**
 * The hazard taxonomy. Cases are grouped by the hazard they pin, never by feature area (FR-021).
 *
 * @readonly
 * @enum {string}
 */
export const Tag = Object.freeze({
  POSITIONAL: 'positional',
  NAMING: 'naming',
  EXTENSIBLE: 'extensible',
  NUMERIC: 'numeric',
  TYPES: 'types',
  REFERENCES: 'references',
  VERSIONS: 'versions',
  ENCODING: 'encoding',
  MALFORMED: 'malformed',
  TIER1: 'tier1',
});

/**
 * The case input. The extension is what the runner dispatches its reader on.
 *
 * @readonly
 * @enum {string}
 */
export const InputFile = Object.freeze({
  IDF: 'input.idf',
  EPJSON: 'input.epJSON',
});

/**
 * The extension the runner dispatches on, including the dot.
 *
 * The counterpart of the `InputFile.suffix` property in `model.py`. A JavaScript enum is a frozen
 * object of strings and carries no methods, so the property becomes a function over the value.
 *
 * @param {InputFile} inputFile
 * @returns {string}
 */
export function suffixOf(inputFile) {
  return inputFile.slice(inputFile.lastIndexOf('.'));
}

/**
 * What reading the input is declared to do. The `parse-outcome` assertion checks this.
 *
 * @readonly
 * @enum {string}
 */
export const ParseOutcome = Object.freeze({
  SUCCESS: 'success',
  FAILURE: 'failure',
});

/**
 * The two implementations under test.
 *
 * @readonly
 * @enum {string}
 */
export const Library = Object.freeze({
  PYTHON: 'python',
  TYPESCRIPT: 'typescript',
});

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** A corpus file violates a rule this module enforces. */
export class CorpusError extends Error {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'CorpusError';
  }
}

/** A `case.toml`, or the case directory around it, is malformed. */
export class CaseError extends CorpusError {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'CaseError';
  }
}

/** `manifest.json` is malformed, or disagrees with a case it indexes. */
export class ManifestError extends CorpusError {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'ManifestError';
  }
}

/** `known-divergence.toml` is malformed, or names a case that does not exist. */
export class DivergenceError extends CorpusError {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'DivergenceError';
  }
}

// ---------------------------------------------------------------------------
// Shapes
//
// Each class is preceded by the `@typedef` for the object its constructor takes, so the fields are
// declared once as an interface and once as the frozen instance that carries them.
// ---------------------------------------------------------------------------

/**
 * @typedef {object} CaseFields
 * @property {string} id
 * @property {string} title
 * @property {string} why
 * @property {readonly Tag[]} tags
 * @property {string} energyplusVersion
 * @property {Truth} truth
 * @property {readonly Assertion[]} assertions
 */

/**
 * One `cases/<id>/case.toml`, plus the id taken from the directory name.
 *
 * `why` is not optional: a case whose reason is unrecorded cannot be maintained.
 */
export class Case {
  /** @param {CaseFields} fields */
  constructor({ id, title, why, tags, energyplusVersion, truth, assertions }) {
    /** @type {string} */
    this.id = id;
    /** @type {string} */
    this.title = title;
    /** @type {string} */
    this.why = why;
    /** @type {readonly Tag[]} */
    this.tags = Object.freeze([...tags]);
    /** @type {string} */
    this.energyplusVersion = energyplusVersion;
    /** @type {Truth} */
    this.truth = truth;
    /** @type {readonly Assertion[]} */
    this.assertions = Object.freeze([...assertions]);

    checkCaseId(this.id, CaseError);
    checkNonEmpty(this.title, 'title', CaseError);
    checkNonEmpty(this.why, 'why', CaseError);
    checkUniqueNonEmpty(this.tags, 'tags', CaseError);
    checkUniqueNonEmpty(this.assertions, 'assertions', CaseError);
    checkEnergyplusVersion(this.energyplusVersion, CaseError);
    Object.freeze(this);
  }

  /** Whether the `diagnostics` assertion applies, which is what requires the diagnostics file. */
  get expectsDiagnostics() {
    return this.assertions.includes(Assertion.DIAGNOSTICS);
  }
}

/**
 * @typedef {object} ManifestEntryFields
 * @property {string} id
 * @property {string} title
 * @property {readonly Tag[]} tags
 * @property {string} energyplusVersion
 * @property {readonly Assertion[]} assertions
 * @property {InputFile} input
 * @property {ParseOutcome} parseOutcome
 * @property {Truth} truth
 * @property {string | null} [expected]
 * @property {string | null} [expectedDiagnostics]
 * @property {string | null} [expectedValidation]
 * @property {string | null} [expectedIntrospection]
 * @property {string | null} [expectedDocsUrl]
 * @property {string | null} [expectedTypeLookup]
 */

/**
 * One entry in `manifest.json`, in either section.
 *
 * `truth` is derived from the section the entry was read from and is never written back:
 * `additionalProperties: false` in the schema rejects it, and duplicating it on disk would let the
 * two disagree.
 */
export class WriterOptions {
  /**
   * Writer controls a case applies before re-reading its own output.
   *
   * Language-neutral. Each runner maps these onto its own writer's option names, because the two
   * writers spell the same control differently and the corpus vocabulary belongs to neither.
   *
   * Every field defaults to `null`, meaning "leave the writer's default alone". That is what all
   * 48 pre-existing cases mean, and adding this must not change a single one of their
   * expectations. A case that sets a field is asserting FR-019: a document written under that
   * control re-reads to the same structure it came from.
   *
   * @param {{ comments?: boolean | null, compressed?: boolean | null, indent?: number | null,
   *           commentColumn?: number | null, ordering?: string | null }} fields
   */
  constructor({
    comments = null,
    compressed = null,
    indent = null,
    commentColumn = null,
    ordering = null,
  } = {}) {
    /** @type {boolean | null} */
    this.comments = comments ?? null;
    /** @type {boolean | null} */
    this.compressed = compressed ?? null;
    /** @type {number | null} */
    this.indent = indent ?? null;
    /** @type {number | null} */
    this.commentColumn = commentColumn ?? null;
    /** @type {string | null} */
    this.ordering = ordering ?? null;

    if (this.ordering !== null && this.ordering !== 'sorted' && this.ordering !== 'source') {
      throw new ManifestError(
        `writer_options.ordering must be 'sorted' or 'source', got ${repr(this.ordering)}`
      );
    }
    for (const [name, value] of [
      ['indent', this.indent],
      ['comment_column', this.commentColumn],
    ]) {
      if (value !== null && /** @type {number} */ (value) < 0) {
        throw new ManifestError(`writer_options.${name} must not be negative, got ${repr(value)}`);
      }
    }
    Object.freeze(this);
  }

  /** Whether this asks for anything at all. An absent block and an empty one are the same. */
  get isDefault() {
    return (
      this.comments === null &&
      this.compressed === null &&
      this.indent === null &&
      this.commentColumn === null &&
      this.ordering === null
    );
  }
}

export class ManifestEntry {
  /** @param {ManifestEntryFields} fields */
  constructor({
    id,
    title,
    tags,
    energyplusVersion,
    assertions,
    input,
    parseOutcome,
    truth,
    expected = null,
    expectedDiagnostics = null,
    expectedValidation = null,
    expectedIntrospection = null,
    expectedDocsUrl = null,
    expectedTypeLookup = null,
    writerOptions = null,
  }) {
    /** @type {string} */
    this.id = id;
    /** @type {string} */
    this.title = title;
    /** @type {readonly Tag[]} */
    this.tags = Object.freeze([...tags]);
    /** @type {string} */
    this.energyplusVersion = energyplusVersion;
    /** @type {readonly Assertion[]} */
    this.assertions = Object.freeze([...assertions]);
    /** @type {InputFile} */
    this.input = input;
    /** @type {ParseOutcome} */
    this.parseOutcome = parseOutcome;
    /** @type {Truth} */
    this.truth = truth;
    /** @type {string | null} */
    this.expected = expected ?? null;
    /** @type {string | null} */
    this.expectedDiagnostics = expectedDiagnostics ?? null;
    /** @type {string | null} */
    this.expectedValidation = expectedValidation ?? null;
    /** @type {string | null} */
    this.expectedIntrospection = expectedIntrospection ?? null;
    /** @type {string | null} */
    this.expectedDocsUrl = expectedDocsUrl ?? null;
    /** @type {string | null} */
    this.expectedTypeLookup = expectedTypeLookup ?? null;
    /** @type {WriterOptions | null} */
    this.writerOptions = writerOptions ?? null;

    checkCaseId(this.id, ManifestError);
    checkNonEmpty(this.title, 'title', ManifestError);
    checkUniqueNonEmpty(this.tags, 'tags', ManifestError);
    checkUniqueNonEmpty(this.assertions, 'assertions', ManifestError);
    checkEnergyplusVersion(this.energyplusVersion, ManifestError);

    // FR-020, enforced here as well as by the schema: the section is the truth value, so an oracle
    // entry must carry an expectation and a convention entry must not.
    if (this.truth === Truth.ORACLE && this.expected !== EXPECTED_EPJSON) {
      throw new ManifestError(
        `case ${repr(this.id)}: truth = oracle requires expected = ${repr(EXPECTED_EPJSON)}, ` +
          `got ${repr(this.expected)}`
      );
    }
    if (this.truth === Truth.CONVENTION && this.expected !== null) {
      throw new ManifestError(
        `case ${repr(this.id)}: truth = convention forbids an expectation, ` +
          `got expected = ${repr(this.expected)}`
      );
    }

    // An assertion that reads an expectation file must name it, and an entry must not name a file
    // for an assertion it does not declare: a named expectation nobody reads looks like coverage
    // and is not.
    for (const { assertion, key, property, file } of EXPECTATION_FILES) {
      const declared = this.assertions.includes(assertion);
      const named = /** @type {string | null} */ (this[property]);
      if (declared && named !== file) {
        throw new ManifestError(
          `case ${repr(this.id)}: the ${repr(assertion)} assertion requires ` +
            `${key} = ${repr(file)}, got ${repr(named)}`
        );
      }
      if (!declared && named !== null) {
        throw new ManifestError(
          `case ${repr(this.id)}: ${key} is set to ${repr(named)} but ${repr(assertion)} is not ` +
            `among the assertions`
        );
      }
    }
    Object.freeze(this);
  }

  /**
   * The entry as it is written to `manifest.json`. The JSON boundary, not a model type.
   *
   * @returns {Record<string, unknown>}
   */
  toJsonObj() {
    /** @type {Record<string, unknown>} */
    const entry = {
      id: this.id,
      title: this.title,
      tags: [...this.tags],
      energyplus_version: this.energyplusVersion,
      assertions: [...this.assertions],
      input: this.input,
      parse_outcome: this.parseOutcome,
    };
    if (this.expected !== null) {
      entry.expected = this.expected;
    }
    for (const { key, property } of EXPECTATION_FILES) {
      const named = /** @type {string | null} */ (this[property]);
      if (named !== null) {
        entry[key] = named;
      }
    }
    return entry;
  }
}

/**
 * @typedef {object} ManifestFields
 * @property {readonly ManifestEntry[]} [oracle]
 * @property {readonly ManifestEntry[]} [convention]
 * @property {string | null} [corpusLevel]
 * @property {number} [schemaVersion]
 * @property {string | null} [schemaRef]
 */

/** `manifest.json`: the index over every case, in two sections that never mix. */
export class Manifest {
  /** @param {ManifestFields} [fields] */
  constructor({
    oracle = [],
    convention = [],
    corpusLevel = null,
    schemaVersion = MANIFEST_SCHEMA_VERSION,
    schemaRef = MANIFEST_SCHEMA_REF,
  } = {}) {
    /** @type {readonly ManifestEntry[]} */
    this.oracle = Object.freeze([...oracle]);
    /** @type {readonly ManifestEntry[]} */
    this.convention = Object.freeze([...convention]);
    /** @type {string | null} */
    this.corpusLevel = corpusLevel ?? null;
    /** @type {number} */
    this.schemaVersion = schemaVersion;
    /** @type {string | null} */
    this.schemaRef = schemaRef ?? null;

    if (this.schemaVersion !== MANIFEST_SCHEMA_VERSION) {
      throw new ManifestError(
        `schema_version must be ${MANIFEST_SCHEMA_VERSION}, got ${repr(this.schemaVersion)}`
      );
    }
    if (this.corpusLevel !== null && !CORPUS_LEVEL_PATTERN.test(this.corpusLevel)) {
      throw new ManifestError(
        `corpus_level must match conformance-YYYY.N, got ${repr(this.corpusLevel)}`
      );
    }
    for (const [section, expectedTruth] of [
      [this.oracle, Truth.ORACLE],
      [this.convention, Truth.CONVENTION],
    ]) {
      for (const entry of /** @type {readonly ManifestEntry[]} */ (section)) {
        if (entry.truth !== expectedTruth) {
          throw new ManifestError(
            `case ${repr(entry.id)} carries truth = ${entry.truth} but sits in the ` +
              `${expectedTruth} section`
          );
        }
      }
    }

    // JSON Schema's uniqueItems compares whole objects within one array, so it cannot catch an id
    // reused across the two sections. That check lives here.
    /** @type {Set<string>} */
    const seen = new Set();
    for (const entry of this.entries()) {
      if (seen.has(entry.id)) {
        throw new ManifestError(
          `case id ${repr(entry.id)} appears more than once across the two sections`
        );
      }
      seen.add(entry.id);
    }
    Object.freeze(this);
  }

  /**
   * Every entry, oracle first, then convention.
   *
   * @returns {Generator<ManifestEntry>}
   */
  *entries() {
    yield* this.oracle;
    yield* this.convention;
  }

  /**
   * The entry for `caseId`, from whichever section holds it.
   *
   * @param {string} caseId
   * @returns {ManifestEntry}
   */
  entry(caseId) {
    for (const entry of this.entries()) {
      if (entry.id === caseId) {
        return entry;
      }
    }
    throw new ManifestError(`no case ${repr(caseId)} in the manifest`);
  }

  /**
   * The manifest as it is written to `manifest.json`. The JSON boundary, not a model type.
   *
   * @returns {Record<string, unknown>}
   */
  toJsonObj() {
    /** @type {Record<string, unknown>} */
    const document = {};
    if (this.schemaRef !== null) {
      document.$schema = this.schemaRef;
    }
    document.schema_version = this.schemaVersion;
    document.corpus_level = this.corpusLevel;
    document.oracle = this.oracle.map((entry) => entry.toJsonObj());
    document.convention = this.convention.map((entry) => entry.toJsonObj());
    return document;
  }
}

/**
 * @typedef {object} DivergenceFields
 * @property {string} case
 * @property {Library} library
 * @property {Assertion} assertion
 * @property {string} issue
 * @property {string} observed
 * @property {string} expected
 */

/**
 * One `[[divergence]]` table in `known-divergence.toml`: an accepted disagreement.
 *
 * `issue` is mandatory (FR-018). An exception without a tracked resolution is indistinguishable
 * from an accepted bug, so the constructor rejects a missing one rather than leaving the rule to a
 * comment.
 */
export class Divergence {
  /** @param {DivergenceFields} fields */
  constructor({ case: caseId, library, assertion, issue, observed, expected }) {
    /** @type {string} */
    this.case = caseId;
    /** @type {Library} */
    this.library = library;
    /** @type {Assertion} */
    this.assertion = assertion;
    /** @type {string} */
    this.issue = issue;
    /** @type {string} */
    this.observed = observed;
    /** @type {string} */
    this.expected = expected;

    checkCaseId(this.case, DivergenceError);
    if (this.issue.trim() === '') {
      throw new DivergenceError(
        `divergence for case ${repr(this.case)} on ${this.library}/${this.assertion}: ` +
          `'issue' is mandatory (FR-018). An exception without a tracked resolution is ` +
          `indistinguishable from an accepted bug`
      );
    }
    if (!ISSUE_URL_PATTERN.test(this.issue)) {
      throw new DivergenceError(
        `divergence for case ${repr(this.case)}: 'issue' must be a tracker URL, ` +
          `got ${repr(this.issue)}`
      );
    }
    checkNonEmpty(this.observed, 'observed', DivergenceError);
    checkNonEmpty(this.expected, 'expected', DivergenceError);
    Object.freeze(this);
  }
}

/**
 * @typedef {object} DivergenceRegisterFields
 * @property {readonly Divergence[]} [entries]
 */

/**
 * `known-divergence.toml` as a whole: the accepted disagreements, at most one per case, library
 * and assertion.
 */
export class DivergenceRegister {
  /** @param {DivergenceRegisterFields} [fields] */
  constructor({ entries = [] } = {}) {
    /** @type {readonly Divergence[]} */
    this.entries = Object.freeze([...entries]);

    /** @type {Set<string>} */
    const seen = new Set();
    for (const entry of this.entries) {
      const key = [entry.case, entry.library, entry.assertion].join(' ');
      if (seen.has(key)) {
        throw new DivergenceError(
          `duplicate divergence for case ${repr(entry.case)} on ` +
            `${entry.library}/${entry.assertion}`
        );
      }
      seen.add(key);
    }
    Object.freeze(this);
  }

  /**
   * The accepted exception for this case, library and assertion, or `null` if the failure blocks.
   *
   * @param {string} caseId
   * @param {Library} library
   * @param {Assertion} assertion
   * @returns {Divergence | null}
   */
  find(caseId, library, assertion) {
    for (const entry of this.entries) {
      if (entry.case === caseId && entry.library === library && entry.assertion === assertion) {
        return entry;
      }
    }
    return null;
  }

  /**
   * Every exception recorded against one library, in file order.
   *
   * @param {Library} library
   * @returns {readonly Divergence[]}
   */
  forLibrary(library) {
    return Object.freeze(this.entries.filter((entry) => entry.library === library));
  }
}

/**
 * @typedef {object} CorpusFields
 * @property {string} root
 * @property {Manifest} manifest
 * @property {readonly Case[]} cases
 * @property {DivergenceRegister} divergences
 */

/** A loaded corpus: the manifest, every case it indexes, and the exception register. */
export class Corpus {
  /** @param {CorpusFields} fields */
  constructor({ root, manifest, cases, divergences }) {
    /** @type {string} */
    this.root = root;
    /** @type {Manifest} */
    this.manifest = manifest;
    /** @type {readonly Case[]} */
    this.cases = Object.freeze([...cases]);
    /** @type {DivergenceRegister} */
    this.divergences = divergences;
    Object.freeze(this);
  }

  /**
   * The loaded `case.toml` for `caseId`.
   *
   * @param {string} caseId
   * @returns {Case}
   */
  case(caseId) {
    for (const record of this.cases) {
      if (record.id === caseId) {
        return record;
      }
    }
    throw new CaseError(`no case ${repr(caseId)} in ${join(this.root, CASES_DIR)}`);
  }

  /**
   * The directory holding `caseId`, whether or not it exists.
   *
   * @param {string} caseId
   * @returns {string}
   */
  caseDir(caseId) {
    return join(this.root, CASES_DIR, caseId);
  }
}

// ---------------------------------------------------------------------------
// Shared field checks
// ---------------------------------------------------------------------------

/**
 * Render a value the way Python's `!r` does, so the two runners report a bad file in the same
 * words. Strings come out single-quoted, and `null` comes out as Python's `None`.
 *
 * @param {unknown} value
 * @returns {string}
 */
function repr(value) {
  if (value === null || value === undefined) {
    return 'None';
  }
  if (typeof value === 'string') {
    return `'${value.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
  }
  if (typeof value === 'boolean') {
    return value ? 'True' : 'False';
  }
  if (Array.isArray(value)) {
    return `[${value.map(repr).join(', ')}]`;
  }
  if (typeof value === 'object') {
    return `{${Object.entries(value)
      .map(([key, item]) => `${repr(key)}: ${repr(item)}`)
      .join(', ')}}`;
  }
  return String(value);
}

/**
 * @param {string} value
 * @param {typeof CorpusError} error
 */
function checkCaseId(value, error) {
  if (!value || value.length > CASE_ID_MAX_LENGTH || !CASE_ID_PATTERN.test(value)) {
    throw new error(
      `case id ${repr(value)} must be a lowercase hyphenated slug of 1 to ` +
        `${CASE_ID_MAX_LENGTH} characters`
    );
  }
}

/**
 * @param {string} value
 * @param {string} fieldName
 * @param {typeof CorpusError} error
 */
function checkNonEmpty(value, fieldName, error) {
  if (value.trim() === '') {
    throw new error(`${repr(fieldName)} must be a non-empty string`);
  }
}

/**
 * @param {string} value
 * @param {typeof CorpusError} error
 */
function checkEnergyplusVersion(value, error) {
  if (!ENERGYPLUS_VERSION_PATTERN.test(value)) {
    throw new error(
      `energyplus_version must be three dotted numbers such as '26.1.0', got ${repr(value)}`
    );
  }
}

/**
 * @param {readonly string[]} values
 * @param {string} fieldName
 * @param {typeof CorpusError} error
 */
function checkUniqueNonEmpty(values, fieldName, error) {
  if (values.length === 0) {
    throw new error(`${repr(fieldName)} needs at least one entry`);
  }
  if (new Set(values).size !== values.length) {
    throw new error(`${repr(fieldName)} must not repeat an entry, got ${repr([...values])}`);
  }
}

// ---------------------------------------------------------------------------
// Reading TOML
//
// Node ships no TOML parser and this repository has no package.json, so the corpus vendors this
// one rather than take a dependency. It is not a complete TOML 1.0 implementation. It supports:
// comments, bare and quoted keys, dotted keys, `[table]` and `[[array of tables]]` headers, basic
// and literal strings including their triple-quoted multi-line forms with the usual escapes and
// line-ending backslash, arrays that may span lines and carry a trailing comma, integers, floats
// and booleans.
//
// It does NOT support: inline tables (`{ a = 1 }`), dates and times, hexadecimal, octal, binary or
// underscore-separated numbers, `inf` and `nan`, or a multi-line basic string whose content ends
// with a quote character. Each of those raises a `TomlError` naming the line rather than parsing
// wrong. None of them appears in `case.toml` or `known-divergence.toml`; if one ever needs to,
// this reader gets extended in the same commit.
// ---------------------------------------------------------------------------

/** The vendored reader rejected a file. The counterpart of `tomllib.TOMLDecodeError`. */
class TomlError extends Error {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'TomlError';
  }
}

const BARE_KEY_CHARACTER = /[A-Za-z0-9_-]/;
const HEX_DIGITS = /^[0-9A-Fa-f]+$/;
const NUMBER = /^[+-]?(?:[0-9]+\.[0-9]+(?:[eE][+-]?[0-9]+)?|[0-9]+(?:[eE][+-]?[0-9]+)?)/;
const LINE_ENDING_BACKSLASH = /^[ \t]*\n[ \t\n]*/;

class TomlReader {
  /**
   * @param {string} text
   * @param {string} path
   */
  constructor(text, path) {
    this.text = text.replace(/\r\n/g, '\n');
    this.path = path;
    this.pos = 0;
    // Tables have a null prototype, so a key such as `__proto__` stays data instead of becoming a
    // prototype write.
    this.root = /** @type {Record<string, unknown>} */ (Object.create(null));
    this.table = this.root;
  }

  /** @returns {Record<string, unknown>} */
  parse() {
    for (;;) {
      this.skipIgnorable();
      if (this.pos >= this.text.length) {
        return this.root;
      }
      if (this.peek() === '[') {
        this.readTableHeader();
      } else {
        this.readKeyValue();
      }
    }
  }

  /**
   * @param {string} problem
   * @returns {never}
   */
  fail(problem) {
    const line = this.text.slice(0, this.pos).split('\n').length;
    throw new TomlError(`${this.path}: line ${line}: ${problem}`);
  }

  /**
   * @param {number} [offset]
   * @returns {string | undefined}
   */
  peek(offset = 0) {
    return this.text[this.pos + offset];
  }

  /** Consume spaces and tabs, but not newlines. */
  skipInline() {
    while (this.peek() === ' ' || this.peek() === '\t') {
      this.pos += 1;
    }
  }

  /** Consume whitespace, newlines and comments. */
  skipIgnorable() {
    for (;;) {
      const character = this.peek();
      if (character === ' ' || character === '\t' || character === '\n') {
        this.pos += 1;
        continue;
      }
      if (character === '#') {
        this.skipComment();
        continue;
      }
      return;
    }
  }

  skipComment() {
    while (this.pos < this.text.length && this.peek() !== '\n') {
      this.pos += 1;
    }
  }

  /** After a value only whitespace, a comment and the end of the line may follow. */
  endOfLine() {
    this.skipInline();
    if (this.peek() === '#') {
      this.skipComment();
    }
    const character = this.peek();
    if (character === undefined) {
      return;
    }
    if (character === '\n') {
      this.pos += 1;
      return;
    }
    this.fail(`unexpected ${repr(character)} at the end of a line`);
  }

  readTableHeader() {
    this.pos += 1;
    const isArray = this.peek() === '[';
    if (isArray) {
      this.pos += 1;
    }
    this.skipInline();
    const keys = this.readKeyPath();
    this.skipInline();
    if (this.peek() !== ']') {
      this.fail("expected ']' to close a table header");
    }
    this.pos += 1;
    if (isArray) {
      if (this.peek() !== ']') {
        this.fail("expected ']]' to close an array-of-tables header");
      }
      this.pos += 1;
    }
    this.endOfLine();

    let table = this.root;
    for (const key of keys.slice(0, -1)) {
      table = this.descend(table, key);
    }
    const last = keys[keys.length - 1];
    const entry = /** @type {Record<string, unknown>} */ (Object.create(null));
    if (isArray) {
      let existing = table[last];
      if (existing === undefined) {
        existing = [];
        table[last] = existing;
      }
      if (!Array.isArray(existing)) {
        this.fail(`${repr(keys.join('.'))} is already defined and is not an array of tables`);
      }
      existing.push(entry);
    } else {
      if (Object.hasOwn(table, last)) {
        this.fail(`table ${repr(keys.join('.'))} is defined more than once`);
      }
      table[last] = entry;
    }
    this.table = entry;
  }

  readKeyValue() {
    const keys = this.readKeyPath();
    this.skipInline();
    if (this.peek() !== '=') {
      this.fail("expected '=' after a key");
    }
    this.pos += 1;
    this.skipInline();
    const value = this.readValue();
    this.endOfLine();

    let table = this.table;
    for (const key of keys.slice(0, -1)) {
      table = this.descend(table, key);
    }
    const last = keys[keys.length - 1];
    if (Object.hasOwn(table, last)) {
      this.fail(`key ${repr(keys.join('.'))} is defined more than once`);
    }
    table[last] = value;
  }

  /**
   * @param {Record<string, unknown>} table
   * @param {string} key
   * @returns {Record<string, unknown>}
   */
  descend(table, key) {
    let next = table[key];
    if (next === undefined) {
      next = Object.create(null);
      table[key] = next;
    }
    if (Array.isArray(next)) {
      next = next[next.length - 1];
    }
    if (typeof next !== 'object' || next === null) {
      this.fail(`${repr(key)} is not a table`);
    }
    return /** @type {Record<string, unknown>} */ (next);
  }

  /** @returns {string[]} */
  readKeyPath() {
    const keys = [this.readKey()];
    for (;;) {
      this.skipInline();
      if (this.peek() !== '.') {
        return keys;
      }
      this.pos += 1;
      this.skipInline();
      keys.push(this.readKey());
    }
  }

  /** @returns {string} */
  readKey() {
    const character = this.peek();
    if (character === '"' || character === "'") {
      return this.readSingleLineString();
    }
    const start = this.pos;
    while (BARE_KEY_CHARACTER.test(this.peek() ?? '')) {
      this.pos += 1;
    }
    if (this.pos === start) {
      this.fail(`expected a key, got ${repr(character ?? null)}`);
    }
    return this.text.slice(start, this.pos);
  }

  /** @returns {unknown} */
  readValue() {
    const character = this.peek();
    if (character === '"' || character === "'") {
      if (this.peek(1) === character && this.peek(2) === character) {
        return this.readMultiLineString(character);
      }
      return this.readSingleLineString();
    }
    if (character === '[') {
      return this.readArray();
    }
    if (character === '{') {
      this.fail('inline tables are not supported by this reader');
    }
    if (this.text.startsWith('true', this.pos)) {
      this.pos += 4;
      return true;
    }
    if (this.text.startsWith('false', this.pos)) {
      this.pos += 5;
      return false;
    }
    return this.readNumber();
  }

  /** @returns {string} */
  readSingleLineString() {
    const quote = this.peek();
    this.pos += 1;
    let out = '';
    for (;;) {
      const character = this.peek();
      if (character === undefined || character === '\n') {
        this.fail('unterminated string');
      }
      if (character === quote) {
        this.pos += 1;
        return out;
      }
      if (quote === '"' && character === '\\') {
        out += this.readEscape();
        continue;
      }
      out += character;
      this.pos += 1;
    }
  }

  /**
   * @param {string} quote
   * @returns {string}
   */
  readMultiLineString(quote) {
    const closing = quote.repeat(3);
    this.pos += 3;
    // A newline immediately after the opening delimiter is not part of the value.
    if (this.peek() === '\n') {
      this.pos += 1;
    }
    let out = '';
    for (;;) {
      if (this.pos >= this.text.length) {
        this.fail('unterminated multi-line string');
      }
      if (this.text.startsWith(closing, this.pos)) {
        this.pos += 3;
        return out;
      }
      const character = this.peek();
      if (quote === '"' && character === '\\') {
        // A backslash ending a line swallows the newline and the whitespace that follows it.
        const trimmed = LINE_ENDING_BACKSLASH.exec(this.text.slice(this.pos + 1));
        if (trimmed !== null) {
          this.pos += 1 + trimmed[0].length;
          continue;
        }
        out += this.readEscape();
        continue;
      }
      out += character;
      this.pos += 1;
    }
  }

  /** @returns {string} */
  readEscape() {
    this.pos += 1;
    const character = this.peek();
    this.pos += 1;
    switch (character) {
      case 'b':
        return '\b';
      case 't':
        return '\t';
      case 'n':
        return '\n';
      case 'f':
        return '\f';
      case 'r':
        return '\r';
      case '"':
        return '"';
      case '\\':
        return '\\';
      case 'u':
        return this.readCodePoint(4);
      case 'U':
        return this.readCodePoint(8);
      default:
        return this.fail(`unknown escape '\\${character ?? ''}'`);
    }
  }

  /**
   * @param {number} width
   * @returns {string}
   */
  readCodePoint(width) {
    const digits = this.text.slice(this.pos, this.pos + width);
    if (digits.length !== width || !HEX_DIGITS.test(digits)) {
      this.fail(`malformed unicode escape ${repr(digits)}`);
    }
    this.pos += width;
    return String.fromCodePoint(Number.parseInt(digits, 16));
  }

  /** @returns {unknown[]} */
  readArray() {
    this.pos += 1;
    /** @type {unknown[]} */
    const items = [];
    for (;;) {
      this.skipIgnorable();
      if (this.pos >= this.text.length) {
        this.fail('unterminated array');
      }
      if (this.peek() === ']') {
        this.pos += 1;
        return items;
      }
      items.push(this.readValue());
      this.skipIgnorable();
      const character = this.peek();
      if (character === ',') {
        this.pos += 1;
        continue;
      }
      if (character === ']') {
        this.pos += 1;
        return items;
      }
      this.fail(`expected ',' or ']' in an array, got ${repr(character ?? null)}`);
    }
  }

  /** @returns {number} */
  readNumber() {
    const match = NUMBER.exec(this.text.slice(this.pos));
    if (match === null) {
      this.fail(`unsupported value ${repr(this.text.slice(this.pos).split('\n')[0])}`);
    }
    const after = this.peek(match[0].length);
    if (after === '-' || after === ':') {
      this.fail('dates and times are not supported by this reader');
    }
    this.pos += match[0].length;
    return match[0].includes('.') || /[eE]/.test(match[0])
      ? Number.parseFloat(match[0])
      : Number.parseInt(match[0], 10);
  }
}

/**
 * Parse TOML text into plain tables. The counterpart of `tomllib.loads`.
 *
 * @param {string} text
 * @param {string} path used only in the error message
 * @returns {Record<string, unknown>}
 */
function parseToml(text, path) {
  return new TomlReader(text, path).parse();
}

// ---------------------------------------------------------------------------
// Reading untyped JSON and TOML
//
// These helpers are the only code that touches a parsed table. Everything above this line, and
// every caller below, works in model classes.
// ---------------------------------------------------------------------------

/**
 * @param {typeof CorpusError} error
 * @param {string} path
 * @param {string} where
 * @param {string} problem
 * @returns {never}
 */
function fail(error, path, where, problem) {
  throw new error(`${path}: ${where}: ${problem}`);
}

/**
 * @param {unknown} value
 * @param {string} path
 * @param {string} where
 * @param {typeof CorpusError} error
 * @returns {Record<string, unknown>}
 */
function readMapping(value, path, where, error) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    fail(error, path, where, `expected a table, got ${describeType(value)}`);
  }
  return /** @type {Record<string, unknown>} */ (value);
}

/**
 * The counterpart of Python's `type(value).__name__` in the "expected a table" message.
 *
 * @param {unknown} value
 * @returns {string}
 */
function describeType(value) {
  if (value === null) {
    return 'NoneType';
  }
  if (Array.isArray(value)) {
    return 'list';
  }
  switch (typeof value) {
    case 'string':
      return 'str';
    case 'boolean':
      return 'bool';
    case 'number':
      return Number.isInteger(value) ? 'int' : 'float';
    default:
      return typeof value;
  }
}

/**
 * @typedef {object} ReadOptions
 * @property {string} path
 * @property {string} where
 * @property {typeof CorpusError} error
 */

/**
 * @param {Record<string, unknown>} raw
 * @param {string} key
 * @param {ReadOptions} options
 * @returns {string}
 */
function readString(raw, key, { path, where, error }) {
  if (!Object.hasOwn(raw, key)) {
    fail(error, path, where, `missing required field ${repr(key)}`);
  }
  const value = raw[key];
  if (typeof value !== 'string' || value.trim() === '') {
    fail(error, path, where, `${repr(key)} must be a non-empty string, got ${repr(value)}`);
  }
  return value;
}

/**
 * @param {Record<string, unknown>} raw
 * @param {string} key
 * @param {ReadOptions} options
 * @returns {string | null}
 */
function readOptionalString(raw, key, { path, where, error }) {
  if (!Object.hasOwn(raw, key) || raw[key] === null) {
    return null;
  }
  return readString(raw, key, { path, where, error });
}

/**
 * @template {Record<string, string>} E
 * @param {Record<string, unknown>} raw
 * @param {string} key
 * @param {E} enumeration
 * @param {ReadOptions} options
 * @returns {E[keyof E]}
 */
function readEnum(raw, key, enumeration, { path, where, error }) {
  const text = readString(raw, key, { path, where, error });
  const allowed = Object.values(enumeration);
  if (!allowed.includes(text)) {
    const problem = `${repr(key)} must be one of ${allowed.join(', ')}, got ${repr(text)}`;
    fail(error, path, where, problem);
  }
  return /** @type {E[keyof E]} */ (text);
}

/**
 * @template {Record<string, string>} E
 * @param {Record<string, unknown>} raw
 * @param {string} key
 * @param {E} enumeration
 * @param {ReadOptions} options
 * @returns {E[keyof E][]}
 */
function readEnumArray(raw, key, enumeration, { path, where, error }) {
  if (!Object.hasOwn(raw, key)) {
    fail(error, path, where, `missing required field ${repr(key)}`);
  }
  const values = raw[key];
  if (!Array.isArray(values) || values.length === 0) {
    fail(error, path, where, `${repr(key)} must be a non-empty list, got ${repr(values)}`);
  }
  /** @type {E[keyof E][]} */
  const members = [];
  const allowed = Object.values(enumeration);
  for (const value of values) {
    if (typeof value !== 'string') {
      fail(error, path, where, `${repr(key)} must hold strings, got ${repr(value)}`);
    }
    if (!allowed.includes(value)) {
      const problem = `${repr(key)} must be drawn from ${allowed.join(', ')}, got ${repr(value)}`;
      fail(error, path, where, problem);
    }
    members.push(/** @type {E[keyof E]} */ (value));
  }
  return members;
}

/**
 * @param {Record<string, unknown>} raw
 * @param {ReadonlySet<string>} known
 * @param {ReadOptions} options
 */
function rejectUnknownKeys(raw, known, { path, where, error }) {
  const unknown = Object.keys(raw)
    .filter((key) => !known.has(key))
    .sort();
  if (unknown.length > 0) {
    fail(error, path, where, `unknown field(s) ${unknown.map(repr).join(', ')}`);
  }
}

/**
 * @param {string} path
 * @returns {boolean}
 */
function isFile(path) {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

/**
 * @param {string} path
 * @returns {boolean}
 */
function isDir(path) {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Loaders
// ---------------------------------------------------------------------------

const CASE_KEYS = new Set(['title', 'why', 'tags', 'energyplus_version', 'truth', 'assertions']);
const ENTRY_KEYS = new Set([
  'id',
  'title',
  'tags',
  'energyplus_version',
  'assertions',
  'input',
  'parse_outcome',
  'expected',
  'expected_diagnostics',
  'expected_validation',
  'expected_introspection',
  'expected_docs_url',
  'expected_type_lookup',
  'writer_options',
]);
const MANIFEST_KEYS = new Set([
  '$schema',
  'schema_version',
  'corpus_level',
  'oracle',
  'convention',
]);
const DIVERGENCE_KEYS = new Set([
  'case',
  'library',
  'assertion',
  'issue',
  'observed',
  'expected',
]);

/**
 * Read `caseDir/case.toml` into a `Case`, with the id taken from the directory name.
 *
 * Beyond the field rules, this enforces the two file rules the case directory must satisfy:
 * `truth = oracle` requires `expected.epJSON` to be present, `truth = convention` forbids it, and
 * the `diagnostics` assertion requires `expected.diag.json`.
 *
 * @param {string} caseDir
 * @returns {Case}
 */
export function loadCase(caseDir) {
  const path = join(caseDir, CASE_FILE);
  if (!isFile(path)) {
    throw new CaseError(`${path}: missing. Every case directory needs a ${CASE_FILE}`);
  }
  const raw = readMapping(parseToml(readFileSync(path, 'utf8'), path), path, 'case', CaseError);
  const options = { path, where: 'case', error: CaseError };
  rejectUnknownKeys(raw, CASE_KEYS, options);

  const record = new Case({
    id: basename(caseDir),
    title: readString(raw, 'title', options),
    why: readString(raw, 'why', options),
    tags: readEnumArray(raw, 'tags', Tag, options),
    energyplusVersion: readString(raw, 'energyplus_version', options),
    truth: readEnum(raw, 'truth', Truth, options),
    assertions: readEnumArray(raw, 'assertions', Assertion, options),
  });

  const expectedPath = join(caseDir, EXPECTED_EPJSON);
  if (record.truth === Truth.ORACLE && !isFile(expectedPath)) {
    throw new CaseError(
      `${path}: truth = oracle but ${EXPECTED_EPJSON} is missing from ${caseDir}. ` +
        `Generate it with tools/regenerate.sh and commit it`
    );
  }
  if (record.truth === Truth.CONVENTION && isFile(expectedPath)) {
    throw new CaseError(
      `${path}: truth = convention but ${expectedPath} exists. A convention case has no ` +
        `external expectation, and keeping one would let agreed convention pass as truth`
    );
  }
  for (const { assertion, file } of EXPECTATION_FILES) {
    if (record.assertions.includes(assertion) && !isFile(join(caseDir, file))) {
      throw new CaseError(
        `${path}: the ${repr(assertion)} assertion requires ${file} in ${caseDir}`
      );
    }
  }
  return record;
}

/**
 * @param {unknown} rawEntry
 * @param {Truth} truth
 * @param {string} path
 * @param {number} index
 * @returns {ManifestEntry}
 */
function loadEntry(rawEntry, truth, path, index) {
  const where = `${truth}[${index}]`;
  const raw = readMapping(rawEntry, path, where, ManifestError);
  const options = { path, where, error: ManifestError };
  rejectUnknownKeys(raw, ENTRY_KEYS, options);
  return new ManifestEntry({
    id: readString(raw, 'id', options),
    title: readString(raw, 'title', options),
    tags: readEnumArray(raw, 'tags', Tag, options),
    energyplusVersion: readString(raw, 'energyplus_version', options),
    assertions: readEnumArray(raw, 'assertions', Assertion, options),
    input: readEnum(raw, 'input', InputFile, options),
    parseOutcome: readEnum(raw, 'parse_outcome', ParseOutcome, options),
    truth,
    expected: readOptionalString(raw, 'expected', options),
    expectedDiagnostics: readOptionalString(raw, 'expected_diagnostics', options),
    expectedValidation: readOptionalString(raw, 'expected_validation', options),
    expectedIntrospection: readOptionalString(raw, 'expected_introspection', options),
    expectedDocsUrl: readOptionalString(raw, 'expected_docs_url', options),
    expectedTypeLookup: readOptionalString(raw, 'expected_type_lookup', options),
    writerOptions: readWriterOptions(raw, options),
  });
}

const WRITER_OPTION_KEYS = new Set([
  'comments',
  'compressed',
  'indent',
  'comment_column',
  'ordering',
]);

/**
 * Read the optional `writer_options` block, or `null` when the case declares none.
 *
 * An absent block and an empty one both mean every writer default, which is what every case
 * written before feature 002 means and must keep meaning.
 *
 * @param {Record<string, unknown>} raw
 * @param {{ path: string, where: string, error: * }} options
 * @returns {WriterOptions | null}
 */
function readWriterOptions(raw, options) {
  const value = raw['writer_options'];
  if (value === undefined || value === null) return null;

  const where = `${options.where}.writer_options`;
  const block = readMapping(value, options.path, where, ManifestError);
  rejectUnknownKeys(block, WRITER_OPTION_KEYS, { ...options, where });

  /** @param {string} key */
  const flag = (key) => {
    const found = block[key];
    if (found === undefined || found === null) return null;
    if (typeof found !== 'boolean') {
      throw new ManifestError(
        `${options.path}: ${where}.${key} must be a boolean, got ${repr(found)}`
      );
    }
    return found;
  };

  /** @param {string} key */
  const count = (key) => {
    const found = block[key];
    if (found === undefined || found === null) return null;
    if (typeof found !== 'number' || !Number.isInteger(found)) {
      throw new ManifestError(
        `${options.path}: ${where}.${key} must be an integer, got ${repr(found)}`
      );
    }
    return found;
  };

  const ordering = block['ordering'];
  if (ordering !== undefined && ordering !== null && typeof ordering !== 'string') {
    throw new ManifestError(
      `${options.path}: ${where}.ordering must be a string, got ${repr(ordering)}`
    );
  }

  return new WriterOptions({
    comments: flag('comments'),
    compressed: flag('compressed'),
    indent: count('indent'),
    commentColumn: count('comment_column'),
    ordering: ordering === undefined ? null : /** @type {string | null} */ (ordering),
  });
}

/**
 * Read `manifest.json` into a `Manifest`, deriving each entry's truth from its section.
 *
 * @param {string} path
 * @returns {Manifest}
 */
export function loadManifest(path) {
  if (!isFile(path)) {
    throw new ManifestError(`${path}: missing`);
  }
  const raw = readMapping(
    JSON.parse(readFileSync(path, 'utf8')),
    path,
    'manifest',
    ManifestError
  );
  rejectUnknownKeys(raw, MANIFEST_KEYS, { path, where: 'manifest', error: ManifestError });

  const schemaVersion = raw.schema_version;
  if (typeof schemaVersion !== 'number' || !Number.isInteger(schemaVersion)) {
    fail(
      ManifestError,
      path,
      'manifest',
      `'schema_version' must be an integer, got ${repr(schemaVersion)}`
    );
  }

  /** @type {Record<string, ManifestEntry[]>} */
  const sections = {};
  for (const truth of [Truth.ORACLE, Truth.CONVENTION]) {
    const rawSection = raw[truth];
    if (!Array.isArray(rawSection)) {
      const problem = `${repr(truth)} must be an array, got ${repr(rawSection)}`;
      fail(ManifestError, path, 'manifest', problem);
    }
    sections[truth] = rawSection.map((rawEntry, index) => loadEntry(rawEntry, truth, path, index));
  }

  const corpusLevel = raw.corpus_level ?? null;
  if (corpusLevel !== null && typeof corpusLevel !== 'string') {
    fail(
      ManifestError,
      path,
      'manifest',
      `'corpus_level' must be a string or null, got ${repr(corpusLevel)}`
    );
  }
  const schemaRef = raw.$schema ?? null;
  if (schemaRef !== null && typeof schemaRef !== 'string') {
    fail(ManifestError, path, 'manifest', `'$schema' must be a string, got ${repr(schemaRef)}`);
  }

  return new Manifest({
    oracle: sections[Truth.ORACLE],
    convention: sections[Truth.CONVENTION],
    corpusLevel,
    schemaVersion,
    schemaRef,
  });
}

/**
 * Write `manifest` back to `path`, in the committed formatting: two-space indent, trailing
 * newline.
 *
 * @param {Manifest} manifest
 * @param {string} path
 */
export function dumpManifest(manifest, path) {
  const text = JSON.stringify(manifest.toJsonObj(), null, 2);
  writeFileSync(path, `${text}\n`, 'utf8');
}

/**
 * Read `known-divergence.toml` into a `DivergenceRegister`.
 *
 * A missing file is an error, not an empty register: the corpus ships this file so it can go green
 * on arrival, and a silently absent register would turn every accepted exception into a blocking
 * failure without saying so.
 *
 * @param {string} path
 * @returns {DivergenceRegister}
 */
export function loadDivergences(path) {
  if (!isFile(path)) {
    throw new DivergenceError(
      `${path}: missing. The exception register is part of the corpus, even when empty`
    );
  }
  const raw = readMapping(
    parseToml(readFileSync(path, 'utf8'), path),
    path,
    'register',
    DivergenceError
  );
  rejectUnknownKeys(raw, new Set(['divergence']), {
    path,
    where: 'register',
    error: DivergenceError,
  });

  const rawEntries = Object.hasOwn(raw, 'divergence') ? raw.divergence : [];
  if (!Array.isArray(rawEntries)) {
    fail(
      DivergenceError,
      path,
      'register',
      `'divergence' must be an array of tables, got ${repr(rawEntries)}`
    );
  }

  /** @type {Divergence[]} */
  const entries = [];
  rawEntries.forEach((rawEntry, index) => {
    const where = `divergence[${index}]`;
    const entry = readMapping(rawEntry, path, where, DivergenceError);
    const options = { path, where, error: DivergenceError };
    rejectUnknownKeys(entry, DIVERGENCE_KEYS, options);
    entries.push(
      new Divergence({
        case: readString(entry, 'case', options),
        library: readEnum(entry, 'library', Library, options),
        assertion: readEnum(entry, 'assertion', Assertion, options),
        issue: readString(entry, 'issue', options),
        observed: readString(entry, 'observed', options),
        expected: readString(entry, 'expected', options),
      })
    );
  });
  return new DivergenceRegister({ entries });
}

/**
 * The manifest entry and the case's own `case.toml` must say the same thing.
 *
 * JSON Schema cannot express this: it never sees `case.toml`. Two records of the same facts that
 * are allowed to disagree eventually do, and the runner would then be testing the wrong
 * declaration.
 *
 * @param {ManifestEntry} entry
 * @param {Case} record
 * @param {string} path
 */
function checkAgreement(entry, record, path) {
  if (entry.title !== record.title) {
    throw new ManifestError(
      `${path}: case ${repr(entry.id)}: title ${repr(entry.title)} does not match ` +
        `${repr(record.title)} in ${CASE_FILE}`
    );
  }
  if (!sameMembers(entry.tags, record.tags)) {
    throw new ManifestError(
      `${path}: case ${repr(entry.id)}: tags ${repr([...entry.tags])} do not match ` +
        `${repr([...record.tags])} in ${CASE_FILE}`
    );
  }
  if (entry.energyplusVersion !== record.energyplusVersion) {
    throw new ManifestError(
      `${path}: case ${repr(entry.id)}: energyplus_version ${repr(entry.energyplusVersion)} does ` +
        `not match ${repr(record.energyplusVersion)} in ${CASE_FILE}`
    );
  }
  if (entry.truth !== record.truth) {
    throw new ManifestError(
      `${path}: case ${repr(entry.id)}: sits in the ${entry.truth} section but ${CASE_FILE} ` +
        `declares truth = ${record.truth}`
    );
  }
  if (!sameMembers(entry.assertions, record.assertions)) {
    throw new ManifestError(
      `${path}: case ${repr(entry.id)}: assertions ${repr([...entry.assertions])} do not match ` +
        `${repr([...record.assertions])} in ${CASE_FILE}`
    );
  }
}

/**
 * @param {readonly string[]} left
 * @param {readonly string[]} right
 * @returns {boolean}
 */
function sameMembers(left, right) {
  const first = new Set(left);
  const second = new Set(right);
  return first.size === second.size && [...first].every((value) => second.has(value));
}

/**
 * @typedef {object} LoadCorpusOptions
 * @property {boolean} [requireDivergences]
 */

/**
 * Load the whole corpus at `root` and check every rule that spans more than one file.
 *
 * Set `requireDivergences` to `false` only for a corpus that has not written its exception
 * register yet. Everything else is mandatory.
 *
 * @param {string} root
 * @param {LoadCorpusOptions} [options]
 * @returns {Corpus}
 */
export function loadCorpus(root, { requireDivergences = true } = {}) {
  const manifestPath = join(root, MANIFEST_FILE);
  const manifest = loadManifest(manifestPath);
  const casesRoot = join(root, CASES_DIR);

  /** @type {Case[]} */
  const cases = [];
  for (const entry of manifest.entries()) {
    const caseDir = join(casesRoot, entry.id);
    if (!isDir(caseDir)) {
      throw new ManifestError(
        `${manifestPath}: case ${repr(entry.id)} has no directory at ${caseDir}`
      );
    }
    const record = loadCase(caseDir);
    checkAgreement(entry, record, manifestPath);
    if (!isFile(join(caseDir, entry.input))) {
      throw new CaseError(`${caseDir}: declared input ${repr(entry.input)} is missing`);
    }
    cases.push(record);
  }

  if (isDir(casesRoot)) {
    const indexed = new Set([...manifest.entries()].map((entry) => entry.id));
    const children = readdirSync(casesRoot, { withFileTypes: true })
      .filter((child) => child.isDirectory())
      .map((child) => child.name)
      .sort();
    for (const name of children) {
      if (!indexed.has(name)) {
        throw new ManifestError(
          `${manifestPath}: ${join(casesRoot, name)} is not indexed. An unindexed case never ` +
            `runs, which is indistinguishable from having no case at all`
        );
      }
    }
  }

  const divergencePath = join(root, DIVERGENCE_FILE);
  const divergences =
    requireDivergences || isFile(divergencePath)
      ? loadDivergences(divergencePath)
      : new DivergenceRegister();
  for (const divergence of divergences.entries) {
    if (![...manifest.entries()].some((entry) => entry.id === divergence.case)) {
      throw new DivergenceError(
        `${divergencePath}: divergence names case ${repr(divergence.case)}, which is not in the ` +
          `manifest. A stale exception is removed by the change that fixes the bug`
      );
    }
  }

  return new Corpus({ root, manifest, cases, divergences });
}
