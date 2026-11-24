# Puzzle loose-piece contracts

The workstation photo-interpretation records use the same contract mechanism
as the 745 materials:

* `writer-contract.json` (`pw1`) — what the workstation app files per
  photograph: piece number, face zone, four edge marks, allowed orientations,
  the edition the photograph can establish (`X` / `Y` / `X/Y`) and the back
  paper code (`X` / `Y` / `UNREAD`).
* `reader-contract.json` (`pr1`) — the loose-piece ledger the packing desk
  reads through `IngestionLibrary.publish()`. It adds `photo_id` and `note`,
  both with in-domain TEXT defaults, so the writer/reader pair stays SAFE when
  a workstation revision omits a ledger-only column.

Every field is non-nullable `TEXT`; the domain rules specific to puzzle pieces
(edge alphabet, orientation set, edition tokens, back-code/photo consistency)
are enforced when published rows are reconstructed in
`extcontract/puzzle/pieces.py`, keeping the generic contract toolkit unaware
of the puzzle vocabulary.

Re-photographing the same physical piece reuses `piece_id` with a higher
`revision`; the highest revision wins through the unchanged ingestion rules,
which is how a back-code correction preserves the original piece number and
its provenance.
