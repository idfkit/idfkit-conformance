# checks/

Claims about a library's **behaviour** rather than about a document's content.

The corpus contract reserved this directory when the corpus landed and said it would be created when
the first such check was written, not before. `weather-monthly` is the first. It is not one of the
two claims the contract named, both of which stay open; it is a third meeting the same criterion.

## Why a claim lands here rather than in `cases/`

A case is an input file, a parsed document, and an assertion about what the library made of it,
compared against an expectation `ConvertInputFormat` produced. That shape cannot express a claim
whose subject is not a model. The contract named the first two below; the third meets the same
criterion and is what arrived first:

| Claim | Status |
| ----- | ------ |
| Reading a file from disk | still open; the runners hand each library a string |
| Retrieving weather | still open; resolving a station is not a document |
| Computing figures from a weather file | **`weather-monthly`**, below |

## What a check owes

A check is a directory carrying its own `check.md`, which states what is claimed, what the oracle is
and why it qualifies, the assertions, the fixtures, and the coverage the check does not have. It
carries its own fixtures and its own committed expectations.

**The oracle rule is the corpus's, unchanged.** An expectation is produced by something that is not
either library. `cases/` uses `ConvertInputFormat`; `weather-monthly` uses the EnergyPlus Weather
Converter's own summary of the same file. A check whose expectation came from a library under test
proves nothing and does not belong here.

## Checks

- **`weather-monthly`**: each library's monthly means from an EPW against the Weather Converter's
  summary of the same archive, plus the reserved-value table both libraries read. 288 comparisons
  over six stations, and one sentinel-bearing file the six cannot substitute for.
