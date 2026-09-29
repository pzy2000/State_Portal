from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_15519_f12_png_adapter_plan import (
    inspect_png_bytes,
    select_single_new_png,
)


def _png(width: int = 64, height: int = 32) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def test_png_contract_accepts_positive_canonical_ihdr() -> None:
    assert inspect_png_bytes(_png()) == {"width": 64, "height": 32}


@pytest.mark.parametrize(
    "data",
    [b"not-png", _png(0, 32), b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IDAT" + b"\0" * 8],
)
def test_png_contract_fails_closed(data: bytes) -> None:
    with pytest.raises(ValueError):
        inspect_png_bytes(data)


def test_collector_selects_exactly_one_owned_png(tmp_path: Path) -> None:
    output = tmp_path / "screenshots/screenshot.png"
    output.parent.mkdir()
    output.write_bytes(_png())

    assert select_single_new_png(tmp_path, before=(), after=(output,)) == output.resolve()


def test_collector_rejects_multiple_or_escaped_outputs(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(_png())
    second.write_bytes(_png())
    with pytest.raises(RuntimeError, match="exactly one"):
        select_single_new_png(tmp_path, before=(), after=(first, second))

    escaped = tmp_path.parent / f"{tmp_path.name}-escaped.png"
    escaped.write_bytes(_png())
    try:
        with pytest.raises(RuntimeError, match="escaped"):
            select_single_new_png(tmp_path, before=(), after=(escaped,))
    finally:
        escaped.unlink()


def test_collector_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.png"
    target.write_bytes(_png())
    link = tmp_path / "capture.png"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="ordinary"):
        select_single_new_png(tmp_path, before=(), after=(link,))
