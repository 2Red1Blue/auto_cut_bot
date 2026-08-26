# Stage3 local-run source review

Decision: accepted for the installed policy-source slice, not complete HTTP
activation or remote acceptance. Only local-run source advances to v4.

Stage3CommandPolicy remains the sole typed policy decoder. The source requires
complete policy content, its canonical hash and the registered Doubao model;
missing fields, unknown fields, mismatched hashes and old source versions are
not upgraded with defaults. Source changes alter current resource identity while
preserving narrative/shadow and accepted calibration inputs. Anchored local-run
versions cannot be overwritten; deploying a changed registry uses a new version.

No extra loader/compiler implementation was needed: those owners already carry
the complete source bytes/hash and invoke the typed decoder. Synthetic source
and installed-resource fixtures now explicitly supply Stage3 policy.

Owner evidence: 331 source/codec tests and 163 source-chain/emitter/packaging/
runtime-boundary tests passed, including root and standalone offline wheels
with exact Stage1/2/3 hashes. Independent review reproduced 83 focused pure
checks (excluding the two already-run wheel builds), checked the changed schema
against the typed owner and confirmed no app/native-model/database imports.
Ruff and production typing passed. Counts overlap; no real database/provider
or calibration measurement ran.
