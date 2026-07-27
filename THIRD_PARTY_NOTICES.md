# Third-party notices

The project source is licensed under `GPL-2.0-only`; see [`LICENSE`](LICENSE).
Some bundled components retain their own licenses:

## Adafruit Blinka Raspberry Pi 5 PioMatter

The Pi hardware backend imports
[`Adafruit-Blinka-Raspberry-Pi5-Piomatter`](https://github.com/adafruit/Adafruit_Blinka_Raspberry_Pi5_Piomatter),
which is installed separately from PyPI and licensed `GPL-2.0-only` by its
copyright holders. It is not copied into this repository.

## KissFFT

`pi-app/vendor/kissfft/` contains KissFFT by Mark Borgerding, licensed under
the BSD 3-Clause license. The required copyright and license text are retained
at [`pi-app/vendor/kissfft/COPYING`](pi-app/vendor/kissfft/COPYING).

## Doto font

`pi-app/assets/fonts/Doto-Regular.ttf` and `Doto-Bold.ttf` are static instances
of [Doto](https://github.com/oliverlalan/Doto), copyright 2024 The Doto Project
Authors, licensed under the SIL Open Font License 1.1. The license is retained
at [`pi-app/assets/fonts/OFL.txt`](pi-app/assets/fonts/OFL.txt).

## Runtime dependencies

The application also uses NumPy, Pillow, ffmpeg, Python, and systemd under
their respective upstream licenses. They are installed separately and are not
vendored here.

## Names and trademarks

This is an independent community project. It is not affiliated with, endorsed
by, or supported by WiiM/Linkplay, Adafruit Industries, Raspberry Pi Ltd, or
Dutch & Dutch. Product names and trademarks belong to their respective owners.
