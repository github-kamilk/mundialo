"""Warstwa graficzna: MediaReactionPackage -> PNG karuzeli na Instagram."""

from app.render.specs import (
    RENDERABLE_STATUSES,
    REVIEW_STATUSES,
    build_caption_text,
    build_slide_specs,
    build_x_post_text,
    load_iso2_map,
)

__all__ = [
    "RENDERABLE_STATUSES",
    "REVIEW_STATUSES",
    "build_caption_text",
    "build_x_post_text",
    "build_slide_specs",
    "load_iso2_map",
    "render_slides",
]


def render_slides(*args, **kwargs):  # leniwy import: playwright tylko gdy faktycznie renderujemy
    from app.render.renderer import render_slides as _render_slides

    return _render_slides(*args, **kwargs)
