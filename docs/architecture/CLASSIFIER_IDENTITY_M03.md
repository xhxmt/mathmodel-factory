# Dirty Classifier Identity M0.3

Status: M0.3 shadow/prototype contract. It is not imported by the production
engine, does not change persisted v9 rows, and does not authorize a scheduler
cutover.

## Split identities

`dirty-classifier-semantic-contract-v1` is the behavior identity. It is rebuilt
from the current `DirtyFlag` order, semantic subset, `DirtyChange` shape,
tracked/excluded/ignored path rules, file and UTF-8 error handling, all
classification regex patterns and flags, math/citation/prose/format
fingerprints, inactive-LaTeX and dependency behavior, the complete ordered
artifact-owner registry/matcher, classification ordering/deduplication, and the
validated M0.2 Step-13 operands. Public validation compares the supplied value
with that source-derived projection; a supplied self-hash is not authority.

`dirty_classifier_operational_implementation_sha256` is distinct. Its trusted
manifest has exactly three sorted regular source members:

- `factory_core/artifact_ownership.py`
- `factory_core/dirty.py`
- `factory_core/paper_sources.py`

Runtime validation consumes checked-in path/size/SHA/role values and performs
no source reads. Build and evidence tests regenerate the manifest from actual
bytes. Consequently, an implementation-only or comment change can move the
operational/analysis identity without moving the behavior projection; a
transitive `paper_sources.py` change is no longer invisible.

The historical `classifier_contract_sha256()` remains unchanged and is exposed
to new code only through `legacy_classifier_compat.py`. Its fixed v9 value is
verified for compatibility evidence, but new Snapshot, ContractPinSet,
CommandEnvelope and CAS validators neither call nor reinterpret it. Existing
dirty rows use the explicit field name
`legacy_classifier_contract_sha256`.

## Analysis identity

Analysis binds both behavior and operational identities plus source locators,
conformance corpus and evidence references. Those references are structurally
validated, but changing only a reference does not change the semantic hash.
The compiler/toolchain implementation identity is separately regenerated at
build/evidence time; it is not a replacement for behavior pins.

## Compatibility boundary

`dirty.py`, `artifact_ownership.py` and `paper_sources.py` remain byte-frozen in
M0.3. Changing the legacy function would silently alter rebase/checkpoint and
receipt behavior in `engine.py`, `dirty_rebase.py` and specialized steps, so any
rename/migration of persisted legacy pins belongs to a later Phase-2 migration.
M0.3 adds an explicit compatibility alias instead.
