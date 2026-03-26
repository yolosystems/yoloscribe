"""Unit tests for asset path validation and helper functions (YOL-122)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from s3_helpers import (
    ASSET_ALLOWED_EXTENSIONS,
    ASSET_MAX_BYTES,
    is_safe_asset_path,
    asset_mime_type,
    asset_media_category,
    asset_page_path,
)


# ---------------------------------------------------------------------------
# is_safe_asset_path
# ---------------------------------------------------------------------------


class TestIsSafeAssetPath:
    # ── Valid paths ──────────────────────────────────────────────────────────

    def test_root_image(self):
        assert is_safe_asset_path("assets/photo.jpg")

    def test_root_png(self):
        assert is_safe_asset_path("assets/logo.png")

    def test_root_gif(self):
        assert is_safe_asset_path("assets/anim.gif")

    def test_root_webp(self):
        assert is_safe_asset_path("assets/thumb.webp")

    def test_root_mp4(self):
        assert is_safe_asset_path("assets/video.mp4")

    def test_root_m4v(self):
        assert is_safe_asset_path("assets/clip.m4v")

    def test_root_m4a(self):
        assert is_safe_asset_path("assets/audio.m4a")

    def test_page_image(self):
        assert is_safe_asset_path("intro/assets/photo.jpeg")

    def test_nested_page_image(self):
        assert is_safe_asset_path("a/b/assets/diagram.png")

    def test_filename_with_dots(self):
        assert is_safe_asset_path("assets/my.file.name.png")

    def test_filename_with_hyphens(self):
        assert is_safe_asset_path("assets/my-photo-2024.jpg")

    def test_filename_with_underscores(self):
        assert is_safe_asset_path("assets/screen_shot.png")

    def test_uppercase_extension(self):
        # Extension check is case-insensitive.
        assert is_safe_asset_path("assets/photo.JPG")

    def test_mixed_case_extension(self):
        assert is_safe_asset_path("assets/photo.Jpeg")

    # ── Invalid paths ────────────────────────────────────────────────────────

    def test_disallowed_extension(self):
        assert not is_safe_asset_path("assets/script.js")

    def test_executable_extension(self):
        assert not is_safe_asset_path("assets/malware.exe")

    def test_no_extension(self):
        assert not is_safe_asset_path("assets/noext")

    def test_traversal_in_filename(self):
        assert not is_safe_asset_path("assets/../config.json")

    def test_slash_in_filename(self):
        # Filenames may not contain slashes — only the page prefix may have them.
        assert not is_safe_asset_path("assets/sub/dir/photo.png")

    def test_empty_string(self):
        assert not is_safe_asset_path("")

    def test_no_assets_segment(self):
        assert not is_safe_asset_path("intro/photo.png")

    def test_starts_with_dot(self):
        assert not is_safe_asset_path("assets/.hidden.png")

    def test_page_segment_starts_with_digit_only(self):
        # Page segment must start with [a-z0-9].
        assert is_safe_asset_path("0intro/assets/photo.jpg")

    def test_page_segment_with_uppercase(self):
        # Page segment must be lowercase.
        assert not is_safe_asset_path("Intro/assets/photo.jpg")

    def test_svg_disallowed(self):
        # SVG not in allowlist (XSS risk).
        assert not is_safe_asset_path("assets/icon.svg")

    def test_pdf_disallowed(self):
        assert not is_safe_asset_path("assets/doc.pdf")


# ---------------------------------------------------------------------------
# asset_mime_type
# ---------------------------------------------------------------------------


class TestAssetMimeType:
    def test_jpg(self):
        assert asset_mime_type("assets/photo.jpg") == "image/jpeg"

    def test_jpeg(self):
        assert asset_mime_type("assets/photo.jpeg") == "image/jpeg"

    def test_png(self):
        assert asset_mime_type("assets/img.png") == "image/png"

    def test_gif(self):
        assert asset_mime_type("assets/anim.gif") == "image/gif"

    def test_webp(self):
        assert asset_mime_type("assets/thumb.webp") == "image/webp"

    def test_mp4(self):
        assert asset_mime_type("assets/video.mp4") == "video/mp4"

    def test_m4v(self):
        assert asset_mime_type("assets/clip.m4v") == "video/mp4"

    def test_m4a(self):
        assert asset_mime_type("assets/audio.m4a") == "audio/mp4"

    def test_uppercase_ext(self):
        assert asset_mime_type("assets/photo.JPG") == "image/jpeg"

    def test_unknown_ext_fallback(self):
        assert asset_mime_type("assets/file.xyz") == "application/octet-stream"


# ---------------------------------------------------------------------------
# asset_media_category
# ---------------------------------------------------------------------------


class TestAssetMediaCategory:
    def test_image_jpeg(self):
        assert asset_media_category("image/jpeg") == "image"

    def test_image_png(self):
        assert asset_media_category("image/png") == "image"

    def test_video_mp4(self):
        assert asset_media_category("video/mp4") == "video"

    def test_audio_mp4(self):
        assert asset_media_category("audio/mp4") == "audio"

    def test_unknown_defaults_to_audio(self):
        # Falls through to "audio" as the final else branch.
        assert asset_media_category("application/octet-stream") == "audio"


# ---------------------------------------------------------------------------
# asset_page_path
# ---------------------------------------------------------------------------


class TestAssetPagePath:
    def test_root_asset(self):
        assert asset_page_path("assets/photo.jpg") == ""

    def test_single_segment_page(self):
        assert asset_page_path("intro/assets/video.mp4") == "intro"

    def test_nested_page(self):
        assert asset_page_path("a/b/assets/audio.m4a") == "a/b"

    def test_no_assets_segment(self):
        # If there is no /assets/ segment, returns "".
        assert asset_page_path("intro/content.md") == ""


# ---------------------------------------------------------------------------
# ASSET_MAX_BYTES sanity checks
# ---------------------------------------------------------------------------


class TestAssetMaxBytes:
    def test_image_limit_20mb(self):
        assert ASSET_MAX_BYTES["image"] == 20 * 1024 * 1024

    def test_video_limit_500mb(self):
        assert ASSET_MAX_BYTES["video"] == 500 * 1024 * 1024

    def test_audio_limit_100mb(self):
        assert ASSET_MAX_BYTES["audio"] == 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# ASSET_ALLOWED_EXTENSIONS completeness
# ---------------------------------------------------------------------------


class TestAssetAllowedExtensions:
    def test_all_image_extensions_present(self):
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            assert ext in ASSET_ALLOWED_EXTENSIONS, f"{ext} missing"

    def test_video_extensions_present(self):
        for ext in (".mp4", ".m4v"):
            assert ext in ASSET_ALLOWED_EXTENSIONS, f"{ext} missing"

    def test_audio_extension_present(self):
        assert ".m4a" in ASSET_ALLOWED_EXTENSIONS

    def test_no_svg(self):
        assert ".svg" not in ASSET_ALLOWED_EXTENSIONS

    def test_no_pdf(self):
        assert ".pdf" not in ASSET_ALLOWED_EXTENSIONS
