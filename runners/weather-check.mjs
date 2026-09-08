#!/usr/bin/env node
/**
 * The JavaScript entry point for `checks/weather-monthly`.
 *
 * Run it from the root of this repository, pointing `--library` at a checkout of the JavaScript
 * monorepo. The flag takes a **path**, never a language word, exactly as `run.mjs` does: the
 * runner file already fixes the language, and `weather_check.py` drives Python.
 *
 *     node runners/weather-check.mjs --library /path/to/idfkit-js
 *
 * WHY THIS IS A SEPARATE ENTRY POINT AND NOT A FLAG ON `run.mjs`
 *
 * `run.mjs` runs cases. A case is an input file, a parsed document, and an assertion about what the
 * library made of it against an expectation `ConvertInputFormat` produced. This check has no model,
 * no epJSON, and an oracle that is not `ConvertInputFormat`. Bending the case runner around it
 * would put a second shape inside the loop that every case shares, and `checks/README.md` exists
 * precisely because the two shapes do not fit each other.
 *
 * WHAT IT CHECKS, which is `checks/weather-monthly/check.md` in code:
 *
 * A. **The aggregates.** For each summary-bearing station, the library's monthly means of dry bulb,
 *    dew point, relative humidity and wind speed against `expected/<station>.json`, within the
 *    tolerance that file carries. 288 comparisons over six stations.
 * B. **The absent values.** For the sentinel-bearing file, every value `sentinels.toml` calls
 *    missing reads absent, and every value it calls an observation does not. Checked against the
 *    table rather than against an external aggregate, because an absence has no external aggregate.
 *
 * A station whose expectation marks a field `"covered": false` is reported as uncovered for that
 * field rather than passing silently or failing as a defect.
 *
 * NO NETWORK, and no dependency. The fixtures are committed gzipped and decompressed here with
 * `zlib.gunzipSync`, which is the only compression both standard libraries hold: Node has no xz
 * and Python has no brotli.
 *
 * LOADING THE LIBRARY. As `run.mjs` does, this imports the package's build output, which is what
 * a consumer installing from npm would get. A missing `dist/` is an unusable run rather than a
 * failure, and says to build the checkout.
 *
 * Exit codes: 0 when every comparison is green, 1 for any failure, and 2 when the run could not
 * start at all.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { pathToFileURL } from 'node:url';
import { gunzipSync } from 'node:zlib';

import { parseToml } from './model.mjs';

const RUNNERS_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = dirname(RUNNERS_DIR);
const CHECK_DIR = join(REPO_ROOT, 'checks', 'weather-monthly');

/** EPW is written latin-1, and latin-1 never fails on any byte sequence. */
const DECODER = new TextDecoder('latin1');

/**
 * The four fields the summary aggregates, and what each library calls the column.
 *
 * The mapping is the runner's, not the corpus's: the expectation names the quantity and each
 * library names its own column, which is what an aligned register entry over two idioms means.
 */
const FIELDS = {
  dry_bulb: 'dryBulbTemperature',
  dew_point: 'dewPointTemperature',
  relative_humidity: 'relativeHumidity',
  wind_speed: 'windSpeed',
};

/** The run could not start. Distinct from a failure on purpose. */
class Unusable extends Error {}

// ---------------------------------------------------------------------------
// The corpus side
// ---------------------------------------------------------------------------

/** Decompress one committed fixture into text. */
function fixtureText(name) {
  return DECODER.decode(gunzipSync(readFileSync(join(CHECK_DIR, 'fixtures', name))));
}

/**
 * The sentinel-bearing fixture: the one EPW with no summary beside it.
 *
 * Derived rather than named, so adding a seventh archive cannot leave a stale constant behind. The
 * division of labour is check.md's: the summary-bearing archives carry the aggregate claim and this
 * one carries the absent-value claim, which is why it has no `.stat`.
 */
function sentinelFixture(fixtures) {
  const withoutSummary = fixtures
    .filter((name) => name.endsWith('.epw.gz'))
    .filter((name) => !fixtures.includes(name.replace(/\.epw\.gz$/, '.stat.gz')));
  if (withoutSummary.length !== 1) {
    throw new Unusable(
      `checks/weather-monthly/fixtures holds ${withoutSummary.length} EPW files with no summary ` +
        `beside them and the check is written for exactly one: ${withoutSummary.join(', ') || '(none)'}`
    );
  }
  return withoutSummary[0];
}

/** `sentinels.toml`, as `{ position: { missing: number[], observation: number[] } }`. */
function reservedValues() {
  const path = join(CHECK_DIR, 'sentinels.toml');
  if (!existsSync(path)) throw new Unusable(`${path}: missing`);
  const table = parseToml(readFileSync(path, 'utf8'), path);
  const byPosition = new Map();
  for (const field of table.field ?? []) {
    byPosition.set(field.position, {
      name: field.field,
      missing: (field.values ?? []).filter((v) => v.kind === 'missing').map((v) => v.value),
      observation: (field.values ?? []).filter((v) => v.kind === 'observation').map((v) => v.value),
    });
  }
  return byPosition;
}

// ---------------------------------------------------------------------------
// The library side
// ---------------------------------------------------------------------------

/** Import `@idfkit/weather`'s build output out of the checkout `--library` named. */
async function importLibrary(root) {
  const resolved = resolve(root);
  const dist = join(resolved, 'packages', 'weather', 'dist', 'index.js');
  if (!existsSync(dist)) {
    throw new Unusable(
      `no built @idfkit/weather under ${resolved}. Looked for ${dist}.\n` +
        '  Build the checkout with `npm run build`, or `npx tsc --build`.\n' +
        '  weather-check.mjs drives the JavaScript library; use ' +
        "'python runners/weather_check.py --library <path>' for idfkit."
    );
  }
  const module = await import(pathToFileURL(dist).href);
  for (const name of ['parseEpw', 'monthlyMeans']) {
    if (typeof module[name] !== 'function') {
      throw new Unusable(`@idfkit/weather at ${dist} exports no ${name}`);
    }
  }
  return module;
}

// ---------------------------------------------------------------------------
// The two assertions
// ---------------------------------------------------------------------------

/** Assertion A, for one station. */
function checkAggregates(library, station, expectation, report) {
  const epw = library.parseEpw(fixtureText(`${station}.epw.gz`));

  for (const [key, column] of Object.entries(FIELDS)) {
    const field = expectation.fields?.[key];
    if (field === undefined) {
      report.uncovered.push(`${station}/${key}: the expectation names no such field`);
      continue;
    }
    if (field.covered === false) {
      report.uncovered.push(`${station}/${key}: ${field.reason ?? 'the summary has no such section'}`);
      continue;
    }
    const means = library.monthlyMeans(epw, column);
    for (let month = 0; month < 12; month += 1) {
      const expected = field.monthly[month];
      const actual = means[month].mean;
      report.compared += 1;
      if (!Number.isFinite(actual)) {
        report.failures.push(
          `${station}/${key} month ${month + 1}: the library reports no value, and the summary says ${expected}`
        );
        continue;
      }
      if (Math.abs(actual - expected) > field.tolerance) {
        report.failures.push(
          `${station}/${key} month ${month + 1}: library ${actual.toFixed(4)}, summary ${expected}, ` +
            `difference ${Math.abs(actual - expected).toFixed(4)} over a tolerance of ${field.tolerance} ` +
            `(the summary prints ${field.decimal_places} decimal place(s))`
        );
      }
    }
  }
}

/**
 * Assertion B.
 *
 * The rows are split here rather than asked of the library, because the claim is about what the
 * library did with a value the FILE holds, so the check has to know the file's own text.
 */
function checkAbsentValues(library, fixture, reserved, report) {
  const text = fixtureText(fixture);
  const rows = text
    .split(/\r\n|\n|\r/)
    .slice(8)
    .filter((line) => line.trim() !== '')
    .map((line) => line.split(','));
  const epw = library.parseEpw(text);
  const columns = columnsByPosition(epw);

  for (const [position, entry] of reserved) {
    const column = columns.get(position);
    if (column === undefined) continue; // A text column reserves nothing.

    for (const kind of ['missing', 'observation']) {
      for (const value of entry[kind]) {
        const rowsAtValue = [];
        for (let row = 0; row < rows.length; row += 1) {
          if (Number(rows[row][position]) === value) rowsAtValue.push(row);
        }
        if (rowsAtValue.length === 0) continue;

        report.compared += 1;
        const wrong = rowsAtValue.filter((row) =>
          kind === 'missing' ? !Number.isNaN(column[row]) : Number.isNaN(column[row])
        );
        if (wrong.length > 0) {
          report.failures.push(
            kind === 'missing'
              ? `${entry.name}: ${value} means the measurement was not made, and the library read it as a ` +
                `number in ${wrong.length} of ${rowsAtValue.length} rows, first at row ${wrong[0] + 1}`
              : `${entry.name}: ${value} is an observation and not an absence, and the library blanked it ` +
                `in ${wrong.length} of ${rowsAtValue.length} rows, first at row ${wrong[0] + 1}`
          );
        } else {
          report.notes.push(
            `${entry.name} ${value} (${kind}): ${rowsAtValue.length} rows, all read as ${
              kind === 'missing' ? 'absent' : 'a value'
            }`
          );
        }
      }
    }
  }
}

/**
 * The numeric columns of a weather file, by their position in the row.
 *
 * The library exposes columns by NAME, which is the whole point of the reader, and this check has
 * to speak positions because the reserved-value table is keyed on them. The bridge is the one place
 * this runner knows the field order, and it is written out rather than derived so that a library
 * that renamed a column fails here loudly rather than silently skipping it.
 */
const POSITION_TO_COLUMN = new Map([
  [6, 'dryBulbTemperature'],
  [7, 'dewPointTemperature'],
  [8, 'relativeHumidity'],
  [9, 'atmosphericStationPressure'],
  [10, 'extraterrestrialHorizontalRadiation'],
  [11, 'extraterrestrialDirectNormalRadiation'],
  [12, 'horizontalInfraredRadiationIntensityFromSky'],
  [13, 'globalHorizontalRadiation'],
  [14, 'directNormalRadiation'],
  [15, 'diffuseHorizontalRadiation'],
  [16, 'globalHorizontalIlluminance'],
  [17, 'directNormalIlluminance'],
  [18, 'diffuseHorizontalIlluminance'],
  [19, 'zenithLuminance'],
  [20, 'windDirection'],
  [21, 'windSpeed'],
  [22, 'totalSkyCover'],
  [23, 'opaqueSkyCover'],
  [24, 'visibility'],
  [25, 'ceilingHeight'],
  [26, 'presentWeatherObservation'],
  [28, 'precipitableWater'],
  [29, 'aerosolOpticalDepth'],
  [30, 'snowDepth'],
  [31, 'daysSinceLastSnowfall'],
  [32, 'albedo'],
  [33, 'liquidPrecipitationDepth'],
  [34, 'liquidPrecipitationQuantity'],
]);

function columnsByPosition(epw) {
  const columns = new Map();
  for (const [position, name] of POSITION_TO_COLUMN) {
    const column = epw.hours[name];
    if (column === undefined) {
      throw new Unusable(`the library's hourly table has no column named ${name}`);
    }
    columns.set(position, column);
  }
  return columns;
}

// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = { library: undefined, verbose: false };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--library') args.library = argv[++i];
    else if (argv[i] === '--verbose') args.verbose = true;
    else throw new Unusable(`unknown argument ${JSON.stringify(argv[i])}`);
  }
  if (args.library === undefined) {
    throw new Unusable('--library <path to an idfkit-js checkout> is required');
  }
  return args;
}

async function main(argv) {
  const args = parseArgs(argv);
  const library = await importLibrary(args.library);

  if (!existsSync(CHECK_DIR)) throw new Unusable(`${CHECK_DIR}: missing`);
  const fixtures = readdirSync(join(CHECK_DIR, 'fixtures'));
  const expectations = readdirSync(join(CHECK_DIR, 'expected')).filter((name) => name.endsWith('.json'));
  if (expectations.length === 0) {
    throw new Unusable('checks/weather-monthly/expected holds no expectation, so a green run would prove nothing');
  }

  const report = { compared: 0, failures: [], uncovered: [], notes: [] };

  console.log('idfkit weather-monthly check: JavaScript');
  console.log(`  library     ${resolve(args.library)}`);
  console.log(`  check       ${CHECK_DIR}`);
  console.log('');

  for (const name of expectations.sort()) {
    const expectation = JSON.parse(readFileSync(join(CHECK_DIR, 'expected', name), 'utf8'));
    const station = basename(name, '.json');
    if (!fixtures.includes(`${station}.epw.gz`)) {
      throw new Unusable(`${name} expects a fixture ${station}.epw.gz, which is not committed`);
    }
    checkAggregates(library, station, expectation, report);
  }

  const sentinel = sentinelFixture(fixtures);
  checkAbsentValues(library, sentinel, reservedValues(), report);

  console.log(`  A. aggregates   ${expectations.length} stations, ${Object.keys(FIELDS).length} fields`);
  console.log(`  B. absent values ${sentinel}`);
  if (args.verbose) for (const note of report.notes) console.log(`     ${note}`);
  console.log('');

  for (const line of report.uncovered) console.log(`  UNCOVERED  ${line}`);
  for (const line of report.failures) console.error(`  FAIL       ${line}`);
  console.log('');

  if (report.failures.length > 0) {
    console.error(
      `FAIL: ${report.failures.length} of ${report.compared} comparisons disagree ` +
        `(${report.uncovered.length} uncovered).`
    );
    return 1;
  }
  console.log(
    `PASS: ${report.compared} comparisons against an EnergyPlus artifact and the reserved-value ` +
      `table (${report.uncovered.length} uncovered).`
  );
  return 0;
}

try {
  process.exit(await main(process.argv.slice(2)));
} catch (error) {
  if (error instanceof Unusable) {
    console.error(`The check could not run: ${error.message}`);
    process.exit(2);
  }
  throw error;
}
