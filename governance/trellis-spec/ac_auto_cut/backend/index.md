# ac_auto_cut Backend Authority Consumer

`ac_auto_cut` owns the Pipeline Runtime and infrastructure adapters. It consumes an exact `autocut_kernel` build and generated authority consumer lock.

Before modifying this package, read:

- [Backend Authority Bootstrap](../../backend/index.md)
- [Authority Bootstrap Block](../../backend/authority-bootstrap-block.md)
- [Implementation Conformance Gates](../../backend/implementation-conformance-gates.md)

This routing shim cannot authorize Schema, Registry, Admission or publication changes.
