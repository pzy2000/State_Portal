"""Helpers for producing fixed-size application-owned policy frames."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from PIL import Image


def copy_fixed_policy_frame(
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    backend: str,
    error_type: type[Exception] = RuntimeError,
) -> dict[str, Any]:
    """Copy or deterministically downsample a macOS Retina framebuffer.

    Native screenshot APIs can return device pixels even though their input APIs
    use logical window pixels.  The Agent-S v2 coordinate space is the latter, so
    an integer-scaled framebuffer must be reduced before it becomes a policy
    frame.  Arbitrary aspect or non-integer size drift remains fail-closed.
    """

    expected = (width, height)
    try:
        with Image.open(source) as image:
            image.load()
            actual = image.size
            scale_x, remainder_x = divmod(actual[0], width)
            scale_y, remainder_y = divmod(actual[1], height)
            if (
                remainder_x
                or remainder_y
                or scale_x != scale_y
                or scale_x < 1
                or scale_x > 4
            ):
                raise error_type(
                    f"{backend} framebuffer is {actual[0]}x{actual[1]}; "
                    f"expected {width}x{height} or an integer Retina multiple"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if actual == expected:
                shutil.copyfile(source, destination)
                normalization = "none"
            else:
                image.resize(expected, Image.Resampling.LANCZOS).save(
                    destination, format="PNG"
                )
                normalization = "integer_retina_downsample_lanczos_v1"
    except error_type:
        raise
    except Exception as error:
        raise error_type(f"{backend} framebuffer normalization failed: {error}") from error

    try:
        with Image.open(destination) as normalized:
            normalized.load()
            normalized_size = normalized.size
    except Exception as error:
        raise error_type(f"{backend} policy frame validation failed: {error}") from error
    if normalized_size != expected:
        raise error_type(
            f"{backend} policy frame is {normalized_size[0]}x{normalized_size[1]}; "
            f"expected {width}x{height}"
        )
    return {
        "source_frame": [actual[0], actual[1]],
        "policy_frame": [width, height],
        "source_to_policy_scale": scale_x,
        "normalization": normalization,
    }


__all__ = ["copy_fixed_policy_frame"]
