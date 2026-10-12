from __future__ import annotations

import pytest

from app.errors import SourceParseError
from app.urls import discover_playlist, parse_source, parse_sources


def test_youtube_forms_are_canonical_and_multiline_input_is_deduplicated() -> None:
    sources = parse_sources(
        """
        https://www.youtube.com/watch?v=abcDEF_1234&feature=share
        https://youtu.be/abcDEF_1234?si=tracking
        https://www.youtube.com/shorts/short_ID_12
        https://www.youtube.com/playlist?list=PL_123456
        """
    )
    assert [source.kind for source in sources] == [
        "youtube_video",
        "youtube_video",
        "youtube_playlist",
    ]
    assert sources[0].canonical == "https://www.youtube.com/watch?v=abcDEF_1234"
    assert sources[1].canonical == "https://www.youtube.com/watch?v=short_ID_12"


def test_local_paths_are_typed_and_generic_network_urls_are_rejected() -> None:
    local = parse_source("./lecture.mp4")
    assert local.kind == "local"
    with pytest.raises(SourceParseError, match="allow-listed"):
        parse_source("https://cdn.example.test/video.mp4?token=abc")


@pytest.mark.parametrize(
    "value",
    ["ftp://example.test/video.mp4", "https://www.youtube.com/watch"],
)
def test_unsupported_or_empty_youtube_inputs_have_clear_errors(value: str) -> None:
    with pytest.raises(SourceParseError):
        parse_sources(value)


def test_playlist_discovery_is_injectable_and_deduplicates_entries() -> None:
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {
                "entries": [
                    {"id": "abcDEF_1234", "title": "First"},
                    {"url": "https://youtu.be/abcDEF_1234", "title": "Duplicate"},
                    {"id": "another_123", "title": "Second"},
                ]
            }

    class FakeModule:
        YoutubeDL = FakeYoutubeDL

    playlist = parse_source("https://www.youtube.com/playlist?list=PL_123456")
    sources = discover_playlist(playlist, FakeModule, max_items=10)
    assert [item.video_id for item in sources] == ["abcDEF_1234", "another_123"]
    assert sources[0].title == "First"


def test_playlist_discovery_retains_unavailable_entry_as_reportable_error() -> None:
    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {
                "playlist_count": 2,
                "entries": [
                    {"availability": "private", "title": "Private lesson"},
                    {"id": "public_1234", "title": "Public lesson"},
                ],
            }

    class FakeModule:
        YoutubeDL = FakeYoutubeDL

    playlist = parse_source("https://www.youtube.com/playlist?list=PL_123456")
    sources = discover_playlist(playlist, FakeModule, max_items=10)
    assert len(sources) == 2
    assert sources[0].error and "private" in sources[0].error
    assert sources[1].video_id == "public_1234"
