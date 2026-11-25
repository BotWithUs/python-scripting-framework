# layout_dump: the producer's snapshot layout as a test fixture

`dump_layout.cpp` includes NXTLibrary's own `src/ipc/SharedLayout.h` and prints every snapshot
offset, row field offset, row size and v24 name/flag constant as JSON, under the names
`src/botwithus/core/layout.py` uses. Each number comes from `offsetof` / `sizeof` against the
producer header, so none of them is typed by hand.

Its output is committed as `tests/fixtures/shared_layout.json`. `tests/test_layout.py` checks
it two ways:

- every emitted constant must equal `layout.py`'s;
- every `SNAP_` / `SZ_` / `LP_` / `NPC_` / `PL_` constant in `layout.py` must be one the header
  knows.

The fixture records the producer commit and the header's git blob id. The test asserts both, so
a stale fixture is visible.

## Regenerate (on every protocol bump)

From a Visual Studio x64 developer prompt, with `<sha>` the NXTLibrary commit that shipped
the change:

```bat
mkdir %TEMP%\layout_dump
git -C <NXTLibrary> show <sha>:src/ipc/SharedLayout.h > %TEMP%\layout_dump\SharedLayout.h
git -C <NXTLibrary> rev-parse <sha>:src/ipc/SharedLayout.h
cl /nologo /std:c++20 /EHsc /W4 /I %TEMP%\layout_dump /Fe%TEMP%\layout_dump\dump_layout.exe /Fo%TEMP%\layout_dump\ tools\layout_dump\dump_layout.cpp
%TEMP%\layout_dump\dump_layout.exe <sha> <blob id> > tests\fixtures\shared_layout.json
```

Then update `layout.py` until `pytest tests/test_layout.py` passes, and move the pinned sha and
blob id in `test_fixture_is_the_v25_producer_header`. If the header renames or removes a field
the dumper names, the dumper fails to compile, and that is intended.
