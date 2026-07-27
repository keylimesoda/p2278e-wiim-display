from __future__ import annotations

import numpy as np
import adafruit_blinka_raspberry_pi5_piomatter as piomatter

WIDTH = 64
HEIGHT = 32


def make_matrix(framebuffer):
    geometry = piomatter.Geometry(
        width=WIDTH,
        height=HEIGHT,
        n_addr_lines=4,
        rotation=piomatter.Orientation.Normal,
    )
    return piomatter.PioMatter(
        colorspace=piomatter.Colorspace.RGB888Packed,
        pinout=piomatter.Pinout.AdafruitMatrixBonnet,
        framebuffer=framebuffer,
        geometry=geometry,
    )


def image_to_framebuffer(image):
    return np.asarray(image.convert("RGB")) + 0
