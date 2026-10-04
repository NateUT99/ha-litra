"""Render the integration icon (original artwork; not Logitech's logo).

Drawn at 2048 px and downsampled for anti-aliasing. Outputs the sizes the
home-assistant/brands repo expects: icon.png (256) and icon@2x.png (512).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

N = 2048
OUT = Path(__file__).parent


def glow(box: tuple[int, int, int, int], color: tuple[int, int, int], alpha: int, radius: int):
    """A soft solid-color glow. Only the alpha mask is blurred: blurring RGBA
    directly pulls in the black of transparent pixels and turns glows gray."""
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).ellipse(box, fill=alpha)
    layer = Image.new("RGBA", (N, N), (*color, 0))
    layer.putalpha(mask.filter(ImageFilter.GaussianBlur(radius)))
    return layer


def render() -> Image.Image:
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))

    # Soft warm halo behind the panel.
    img.alpha_composite(glow((260, 140, 1788, 1668), (255, 190, 80), 140, 150))

    d = ImageDraw.Draw(img)
    # Stand: stem and foot.
    d.rounded_rectangle((944, 1430, 1104, 1800), radius=60, fill=(52, 57, 66, 255))
    d.rounded_rectangle((644, 1740, 1404, 1880), radius=70, fill=(52, 57, 66, 255))
    # Panel bezel, then the lit face.
    d.rounded_rectangle((424, 300, 1624, 1500), radius=300, fill=(43, 47, 54, 255))
    face = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(face).rounded_rectangle(
        (504, 380, 1544, 1420), radius=240, fill=(255, 233, 190, 255)
    )
    img.alpha_composite(face)
    # Brighter core so the face reads as emitting light.
    img.alpha_composite(glow((684, 560, 1364, 1240), (255, 252, 242), 255, 110))
    return img


def main() -> None:
    big = render()
    bbox = big.getbbox()
    # Trim to content, then pad back to a centered square (brands guideline).
    trimmed = big.crop(bbox)
    side = max(trimmed.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(trimmed, ((side - trimmed.width) // 2, (side - trimmed.height) // 2))
    for size, name in ((256, "icon.png"), (512, "icon@2x.png")):
        square.resize((size, size), Image.LANCZOS).save(OUT / name, optimize=True)


if __name__ == "__main__":
    main()
