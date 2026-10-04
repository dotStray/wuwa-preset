"""The image stage: one portrait per variant, the same shape for every game.

A portrait comes from, in order: ``manual/<game>/images/``, then the character list's own
picture. Each is cropped the way the source's picture needs — ``config`` → ``portraits.crop``, or
the variant's own crop where the source gave one (an outfit's full card keeps its top square) —
scaled to at most 512 pixels, and saved as WebP under 80 KB in the pack's ``images/``.

``upstream/<game>/images.json`` records where each portrait came from and a checksum of the
original. A rebuild downloads a picture only when its address changed or it has never been
fetched, so a weekly run asks the picture hosts for new characters only.
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import re
from dataclasses import dataclass

from PIL import Image

from packbuilder.files import write_bytes
from packbuilder.http import Fetcher, FetchError

MAX_SIDE = 512
MAX_BYTES = 80 * 1024


@dataclass
class ImageResult:
    images: dict[str, str]  # variant → "images/<name>.webp"
    missing: list[tuple[str, str]]  # (variant, reason)
    sources: dict[str, dict]


def build(variants, pack_images: pathlib.Path, record: dict, crop: str, fetcher: Fetcher | None) -> ImageResult:
    """Brings ``pack_images`` up to date. ``fetcher=None`` builds from what is already there.

    ``record`` is what ``upstream/<game>/images.json`` said last time; the new record is in the
    result, for the caller to save once the whole build has succeeded.
    """
    images: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    sources: dict[str, dict] = {}
    pack_images.mkdir(parents=True, exist_ok=True)

    for variant in variants:
        target = pack_images / f"{variant.name}.webp"
        relative = f"images/{variant.name}.webp"
        previous = record.get(variant.name, {})

        if variant.image_path is not None:
            source = f"manual/{pathlib.Path(variant.image_path).parent.parent.name}/images/{pathlib.Path(variant.image_path).name}"
            if pathlib.Path(variant.image_path).is_symlink():
                # A link in manual/ is not a picture: it would publish whatever it points at (P9).
                missing.append((variant.name, f"{source} is a link, and a link is not read."))
                continue
            original = pathlib.Path(variant.image_path).read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            if previous.get("source") != source or previous.get("sha256") != digest or not target.is_file():
                try:
                    write_bytes(target, normalise(original, "none"))
                except (OSError, ValueError) as error:
                    missing.append((variant.name, f"{source} could not be read as a picture: {error}"))
                    continue
            images[variant.name] = relative
            sources[variant.name] = {"source": source, "sha256": digest}
            continue

        url = variant.image_url
        if not url:
            missing.append((variant.name, "no picture in any source"))
            continue
        own_crop = getattr(variant, "image_crop", None)
        if previous.get("source") == url and previous.get("crop") == own_crop and target.is_file():
            images[variant.name] = relative
            sources[variant.name] = previous
            continue
        if fetcher is None:
            if target.is_file():
                images[variant.name] = relative
                sources[variant.name] = previous
            else:
                missing.append((variant.name, f"{url} not downloaded yet (built without the network)"))
            continue
        try:
            original = fetcher.get(url, fresh=False)
            write_bytes(target, normalise(original, own_crop or crop))
        except FetchError as error:
            if target.is_file():
                images[variant.name] = relative
                sources[variant.name] = previous
                continue
            missing.append((variant.name, str(error)))
            continue
        except (OSError, ValueError) as error:
            missing.append((variant.name, f"{url} is not a picture this builder can read: {error}"))
            continue
        images[variant.name] = relative
        sources[variant.name] = {
            "source": url,
            **({"crop": own_crop} if own_crop else {}),
            "sha256": hashlib.sha256(original).hexdigest(),
        }

    wanted = {f"{name}.webp" for name in images}
    for stale in pack_images.glob("*"):
        if stale.is_file() and stale.name not in wanted:
            stale.unlink()
    return ImageResult(images, missing, dict(sorted(sources.items())))


# What a picture from a source may be. Pillow otherwise opens any format it knows, and EPS is run
# through Ghostscript when it is installed — in the job that holds the publishing token (audit P4).
PICTURE_FORMATS = ["PNG", "JPEG", "WEBP", "GIF"]


def _open(data: bytes) -> Image.Image:
    """Opens a picture from a source: the formats above only, and a decompression bomb refused as
    ``ValueError`` like any other unreadable picture, rather than escaping as its own kind (P4)."""
    try:
        return Image.open(io.BytesIO(data), formats=PICTURE_FORMATS)
    except Image.DecompressionBombError as error:
        raise ValueError(f"the picture claims to be larger than any portrait could be ({error})") from error


def normalise(data: bytes, crop: str) -> bytes:
    """A portrait as the pack stores it: cropped, at most 512 px, WebP, under 80 KB."""
    with _open(data) as opened:
        image = opened.convert("RGBA")
    return encode(_crop(image, crop))


# A game's page on Google Play. robots.txt allows it; the US storefront, because some games' pages
# answer 404 elsewhere.
PLAY_PAGE = "https://play.google.com/store/apps/details?id={app}&hl=en&gl=US"
PLAY_ICON = re.compile(r'<meta property="og:image" content="(https://play-lh\.googleusercontent\.com/[^"=]+)[^"]*"')


def store_icon_url(app: str, fetcher: Fetcher) -> str:
    """The address of a game's current app icon, square and 512 pixels, from its Google Play page."""
    page = PLAY_PAGE.format(app=app)
    found = PLAY_ICON.search(fetcher.get(page).decode("utf-8", errors="replace"))
    if found is None:
        raise FetchError(f"{page}: the page has no app icon in it; Google Play may have changed its pages.")
    # "=s512": the picture at 512 pixels, without the rounded corners the page asks for.
    return f"{found.group(1)}=s512"


def game_icon(data: bytes) -> bytes:
    """The game's icon as the pack stores it: square, with any spare space left transparent, never cut."""
    with _open(data) as opened:
        image = opened.convert("RGBA")
    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return encode(square)


def encode(image: Image.Image) -> bytes:
    """At most 512 px, WebP, under 80 KB."""
    image = image.copy()
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    best = b""
    for quality in (85, 75, 65, 50, 35):
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=quality, method=6)
        best = buffer.getvalue()
        if len(best) <= MAX_BYTES:
            break
    return best


def _crop(image: Image.Image, mode: str) -> Image.Image:
    if mode == "none":
        return image
    alpha = image.getchannel("A").point(lambda v: 255 if v > 32 else 0)
    box = alpha.getbbox()
    if box is None:
        return image
    if mode == "top-square":
        # An outfit's card is a picture taller than it is wide, the face near the top: keep the top square.
        left, top, right, _ = box
        side = right - left
        return image.crop((left, top, right, top + side))
    raise ValueError(f"unknown crop '{mode}'")
