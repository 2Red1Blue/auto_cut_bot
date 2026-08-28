# Ark Responses SDK integration requirements

The current Pipeline adapter, rather than the legacy `autocut-core` adapter, is
the only in-scope implementation target. Implement the accepted design in
`docs/ark-responses-sdk-integration-design.md` without passing arbitrary headers,
blindly re-uploading after an unknown result, or weakening Kernel attempts and
receipts.
