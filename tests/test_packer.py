import slicer


def _sl(root, files, syms=None):
    return slicer.Slice(root=root, root_type="unknown",
                        symbols=set(syms or [root]), files=set(files))


def _df(path, n):
    return slicer.DiffFile(path=path, text="x" * n, changed_ranges=[(1, 1)])


def test_pack_slices_merges_up_to_target_and_preserves_coverage():
    diff_files = {f"f{i}.cs": _df(f"f{i}.cs", 300) for i in range(5)}
    slices = [_sl(f"r{i}", [f"f{i}.cs"], [f"s{i}"]) for i in range(5)]
    # target 800 -> each file 300, so up to 2 files (600) per pack, not 3 (900).
    packed = slicer.pack_slices(slices, diff_files, target=800)
    for p in packed:
        assert slicer.slice_char_size(p, diff_files) <= 800
    # Coverage preserved: every original symbol still present.
    covered = set().union(*(p.symbols for p in packed))
    assert covered == {f"s{i}" for i in range(5)}
    # Actually packed (fewer than 5).
    assert len(packed) < 5


def test_pack_slices_keeps_oversized_slice_alone():
    diff_files = {"big.cs": _df("big.cs", 900), "small.cs": _df("small.cs", 100)}
    slices = [_sl("big", ["big.cs"]), _sl("small", ["small.cs"])]
    packed = slicer.pack_slices(slices, diff_files, target=800)
    # big.cs (900) already over target -> stays its own pack (no split here;
    # sub_split handled that upstream). small stays separate or merges only if
    # it fits with another; here it ends up alone too.
    sizes = sorted(slicer.slice_char_size(p, diff_files) for p in packed)
    assert sizes == [100, 900]
