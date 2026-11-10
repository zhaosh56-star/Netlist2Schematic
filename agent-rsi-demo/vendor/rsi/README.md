# Vendored RSI method core

Source: https://github.com/Xi0ng-Jun/SN_benchmark.git
Branch: feat/method-v4.1
Snapshot: c01964cccb89e0cbf91717bd46994ff93c189f9b

The Python files in `src/` are copied byte for byte from the provided repository.
`SOURCE_MANIFEST.json` records the SHA-256 hash of each copied file. The subset
contains the system-independent v4.1 coordinator, calibration, workers, adapter
interface, decisions, mechanism checks and their dependency closure. It does not
include the SN workbench, its server, remote execution services or sealed
SN-specific endpoint. The v3 modules are included because v4 imports shared
selection, ledger and data utilities from them.

The circuit demo imports `V4Run` through `rsi_core.py`. A small subclass adds an
explicit `demo_round_limit` outside the copied core; the ledger records this
variant. All calibration, measurement, candidate qualification, re-measurement
and adoption decisions use the original v4.1 implementation.

The source snapshot did not include a top-level license file. This notice records
provenance and does not grant additional rights or change the upstream terms.
