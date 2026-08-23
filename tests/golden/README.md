# Golden master snapshots

One JSON file per captured endpoint, produced by `tests/test_golden.py` against a
fixed seed (2 servers, 3 admins, 5 packages, 12 keys covering every state).

These files are the **wire contract**. The panel has no `response_model`
anywhere (finding A8 in `MODERNIZATION.md`), so until every route is typed these
snapshots are the only thing that notices a renamed, added or dropped field.

A diff here is not automatically a failure — it is a change that a human has to
look at and agree to. To accept one:

    UPDATE_GOLDEN=1 pytest tests/test_golden.py

then read `git diff tests/golden/` line by line before committing it. During the
re-architecture (steps 2–3 of the plan) the expected diff is **empty**: the
domain moves, the responses do not.

Volatile values (timestamps, random token halves, request ids) are normalised by
`_norm` in the test, so a rerun with no code change produces no diff.
