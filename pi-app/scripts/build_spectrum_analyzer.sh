#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/bin" "$ROOT/build"
gcc -O3 -ffast-math -march=native -I"$ROOT/vendor/kissfft" \
  -c "$ROOT/vendor/kissfft/kiss_fft.c" \
  -o "$ROOT/build/kiss_fft.o"
g++ -O3 -std=c++17 -ffast-math -march=native -I"$ROOT/vendor/kissfft" \
  "$ROOT/native/spectrum_analyzer.cpp" "$ROOT/build/kiss_fft.o" \
  -o "$ROOT/bin/spectrum_analyzer"
"$ROOT/bin/spectrum_analyzer" --help >/dev/null
printf 'built %s using vendored KissFFT\n' "$ROOT/bin/spectrum_analyzer"
