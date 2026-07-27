from pathlib import Path

from PIL import Image

from p2278e_display.render import (
    album_tile,
    apply_brightness,
    effective_brightness,
    fit_square_without_cropping,
)


def test_non_square_art_is_padded_not_cropped():
    source = Image.new("RGB", (40, 20), (255, 0, 0))
    fitted = fit_square_without_cropping(source)
    assert fitted.size == (40, 40)
    assert fitted.getpixel((0, 0)) == (0, 0, 0)
    assert fitted.getpixel((0, 10)) == (255, 0, 0)
    assert fitted.getpixel((39, 29)) == (255, 0, 0)


def test_album_tile_is_32px_and_cached(tmp_path: Path):
    path = tmp_path / "cover.png"
    Image.new("RGB", (128, 128), (80, 120, 200)).save(path)
    first = album_tile(str(path))
    second = album_tile(str(path))
    assert first.size == (32, 32)
    assert first is second


def test_wide_art_content_preserved_not_cropped(tmp_path: Path):
    # A wide cover with distinct left/right halves must keep both halves; a
    # cropping pipeline would discard the extreme columns.
    src = Image.new("RGB", (64, 32), (0, 0, 0))
    for x in range(64):
        src.putpixel((x, 16), (10, 220, 40) if x < 32 else (220, 40, 10))
    fitted = fit_square_without_cropping(src)
    assert fitted.size == (64, 64)
    # Original row 16 lands at padded row 16 + (64-32)//2 = 32.
    assert fitted.getpixel((0, 32)) == (10, 220, 40)
    assert fitted.getpixel((63, 32)) == (220, 40, 10)


def test_effective_brightness_floor_and_black():
    assert effective_brightness(0.0) == 0.0
    assert effective_brightness(0.05) == 0.25
    assert effective_brightness(0.25) == 0.25
    assert abs(effective_brightness(0.6) - 0.6) < 1e-9
    assert effective_brightness(2.0) == 1.0


def test_apply_brightness_zero_is_black_nonzero_is_lit():
    img = Image.new("RGB", (4, 4), (200, 200, 200))
    black = apply_brightness(img, 0.0)
    assert black.getpixel((0, 0)) == (0, 0, 0)
    # A tiny requested brightness is floored to 0.25, so the pixel stays lit.
    lit = apply_brightness(img, 0.05)
    r, g, b = lit.getpixel((0, 0))
    assert r > 0 and g > 0 and b > 0
    assert r == int(200 * 0.25 + 0.5)
