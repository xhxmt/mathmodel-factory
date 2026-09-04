# Phase9 gap matrix

This matrix separates fixed-offline implementation, first-party immutable
package evidence, independent audit and external production state. A generated
package may use `CLOSED_OFFLINE` only when its cited source/fresh records, raw
logs and derived summary pass the package builder's closed-world checks. That
status is still neither independent acceptance nor production readiness.

| Area | Implementation | Immutable candidate evidence | Production/stage state | Remaining gate |
| --- | --- | --- | --- | --- |
| Candidate and executed-source binding | Fixed offline | Package manifest/inventory and all source/fresh records bind one commit/tree/parent and full tracked-byte inventory; independent recomputation remains pending | Candidate only | Independent auditor recomputes Git and ZIP identity |
| P0 evidence root | Fixed offline | Packaged entry/focused source/fresh logs exercise formal/test domains, fixed runner semantics and adversarial inventories; these are regression evidence, not production receipts | No real P0 receipts | Generate all nine live receipts with the formal producer from the independently accepted source |
| Trusted time and one-use authorization | Fixed offline | Packaged focused source/fresh logs exercise start/precommit expiry, skew, nonce reuse and altered-request rejection | No live authorization | Controlled-account authorizations valid at trusted UTC time |
| Official-input inventory | Fixed offline | Packaged focused source/fresh logs exercise links, hard links, special files, collisions and replacement races | No official input | Freeze and verify exact production input bytes twice |
| Entry gate and live recheck | Fixed offline | Packaged entry/focused source/fresh logs exercise query-only state, revision binding and post-READY deterioration | Phase9-A entry `NOT READY` | Real Authority state plus nine formal P0 receipts return a current `READY` |
| Run-generation create/rotate | Fixed offline | Packaged focused source/fresh logs exercise source/input/context recheck, complete authorization target and atomic rollback | No production generation | Apply the required migration suffix with a verified backup, then use the supported narrow API only |
| Predecessor terminal and pointer CAS | Fixed offline | Packaged focused source/fresh logs exercise missing/stale/wrong terminal and concurrent revision conflicts | No production rotation | Current predecessor has the exact immutable terminal receipt |
| A2_0016-A2_0019 migrations | Additive fixed-offline implementation; earlier published statement bytes retained | Packaged migration source/fresh logs exercise prefix/checksum, upgrade, interruption and restore behavior, including Authority-backed P0 runner and replay/runtime evidence attestation | All four `NOT APPLIED` in production | Explicit migration approval, verified backup and durable journal |
| Typed forensic evidence | Fixed offline | Packaged focused source/fresh fixture logs exercise missing/extra/duplicate/cross-generation receipts and provenance failures | No real replay receipts | Real packet, role/provider/process/outbox/snapshot/verdict receipts |
| Terminal and semantic collector | Fixed offline | Packaged focused source/fresh logs exercise zero-side-effect failures and hash-correct/semantic-wrong SQL graphs | Run4 `NOT RUN` | One-use gate, exact typed inventory and both live checks succeed |
| AR-007 and release fence | Fixed offline | Packaged entry/release source/fresh logs exercise technical/ablation/override/stale-generation rejection with zero release artifacts | Delivery `DISABLED` | Phase9 never enables delivery; a later phase needs separate authority |
| Audit command/log/summary closure | Fixed-offline policy and tooling | Package requires seven source/fresh pairs (14 records plus 14 complete raw logs); `full_repository` additionally binds Python, exact frontend scripts, a safe inventoried production build, and Chromium-test stages in two parent-captured composite streams with identical dependency/runtime/output inventories; a derived exact-match summary retains append-only failed attempts | Independent audit `PENDING` | Independent verifier recomputes every record, per-stage log slice/outcome, source/fresh composite contract and ZIP closure |
| Independent Phase9 candidate audit | Not part of implementation | The generated ZIP is a review candidate; its hashes and self-checks are not an independent verdict | `PENDING` | Independent auditor recomputes code, tests and package closure |
| Formal Phase9-A / Run4 | Contract available | Fixture tests only | `NOT RUN` | All live entry, migration, authorization and evidence gates |
| Phase 9 completion | Incomplete | Offline candidate/package evidence only | `NOT COMPLETE` | Formal Phase9-A completes and receives an independent terminal verdict |
| Phase10-B clean-room acceptance | Out of Phase9 repair scope | None | `NOT STARTED` | Separate authorization after Phase 9 completion |

The missing production Authority database, backup, official input, execution
context, credentials, live authorizations and real replay receipts are expected
external gates. Their absence is not an offline implementation failure, but no
fixture, template, old PASS file or audit package can substitute for them.
