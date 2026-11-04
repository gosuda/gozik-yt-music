import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import grpc

from generated import music_provider_pb2 as pb
from handlers import provider


class AbortError(Exception):
    def __init__(self, code, details):
        super().__init__(details)
        self.code = code
        self.details = details


class FakeContext:
    def abort(self, code, details):
        raise AbortError(code, details)

    def is_active(self):
        return True


class HTTPFailure(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = SimpleNamespace(status_code=status_code)


class FakeClient:
    def __init__(self):
        self.limits = []
        self.error = None

    def search(self, query, filter, limit):
        self.limits.append(limit)
        if self.error:
            raise self.error
        return [{"browseId": f"album-{i}", "title": f"Album {i}"} for i in range(4)]

    def get_liked_songs(self, limit):
        self.limits.append(limit)
        return {"tracks": make_tracks(4)}

    def get_library_playlists(self, limit):
        self.limits.append(limit)
        return [{"playlistId": f"playlist-{i}", "title": f"Playlist {i}"} for i in range(4)]

    def get_playlist(self, playlistId, limit):
        self.limits.append(limit)
        return {
            "id": playlistId,
            "title": "Playlist",
            "trackCount": 4,
            "tracks": make_tracks(4),
        }

    def get_song(self, video_id):
        if self.error:
            raise self.error
        return {"videoDetails": {"title": video_id}}


def make_tracks(count):
    return [{"videoId": f"video-{i}", "title": f"Track {i}"} for i in range(count)]


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.servicer = provider.MusicProviderServicer.__new__(provider.MusicProviderServicer)
        self.servicer._lock = threading.Lock()
        self.servicer._ytm = self.client
        self.servicer._search_ytm = self.client
        self.servicer._get_client = lambda: self.client
        self.servicer._require_auth = lambda context: self.client
        self.context = FakeContext()

    def test_search_albums_pagination(self):
        response = self.servicer.Search(
            pb.SearchRequest(
                query="test",
                types=[pb.MEDIA_TYPE_ALBUM],
                limit=2,
                page_token="2",
            ),
            self.context,
        )
        self.assertEqual(self.client.limits, [5])
        self.assertEqual([album.id for album in response.albums], ["album-2", "album-3"])
        self.assertEqual(response.next_page_token, "")

    def test_search_tracks_pagination(self):
        requested_limits = []

        def search_tracks(query, limit):
            requested_limits.append(limit)
            return [pb.Track(id=f"video-{i}", title=f"Track {i}") for i in range(5)]

        self.servicer._search_tracks_ytdlp = search_tracks
        response = self.servicer.Search(
            pb.SearchRequest(query="test", types=[pb.MEDIA_TYPE_TRACK], limit=2, page_token="2"),
            self.context,
        )
        self.assertEqual(requested_limits, [5])
        self.assertEqual([track.id for track in response.tracks], ["video-2", "video-3"])
        self.assertEqual(response.next_page_token, "4")

    def test_library_pagination(self):
        response = self.servicer.GetUserLibrary(
            pb.GetUserLibraryRequest(limit=2, page_token="2"), self.context
        )
        self.assertEqual(self.client.limits, [5])
        self.assertEqual([track.id for track in response.tracks], ["video-2", "video-3"])
        self.assertEqual(response.next_page_token, "")

    def test_library_next_page(self):
        self.client.get_liked_songs = lambda limit: {"tracks": make_tracks(5)}
        response = self.servicer.GetUserLibrary(
            pb.GetUserLibraryRequest(limit=2, page_token="2"), self.context
        )
        self.assertEqual(response.next_page_token, "4")

    def test_user_playlists_pagination(self):
        response = self.servicer.GetUserPlaylists(
            pb.GetUserPlaylistsRequest(limit=2, page_token="1"), self.context
        )
        self.assertEqual(self.client.limits, [4])
        self.assertEqual([item.id for item in response.playlists], ["playlist-1", "playlist-2"])
        self.assertEqual(response.next_page_token, "3")

    def test_playlist_details_pagination(self):
        response = self.servicer.GetPlaylistDetails(
            pb.GetPlaylistDetailsRequest(playlist_id="playlist", limit=2, page_token="1"),
            self.context,
        )
        self.assertEqual(self.client.limits, [4])
        self.assertEqual([track.id for track in response.tracks], ["video-1", "video-2"])
        self.assertEqual(response.next_page_token, "3")

    def test_invalid_page_token(self):
        for token in ("bad", "-1", "1.5"):
            with self.subTest(token=token), self.assertRaises(AbortError) as caught:
                self.servicer.Search(
                    pb.SearchRequest(query="test", page_token=token), self.context
                )
            self.assertEqual(caught.exception.code, grpc.StatusCode.INVALID_ARGUMENT)

    def test_api_status_mapping(self):
        cases = [
            (400, grpc.StatusCode.INVALID_ARGUMENT),
            (401, grpc.StatusCode.UNAUTHENTICATED),
            (403, grpc.StatusCode.PERMISSION_DENIED),
            (404, grpc.StatusCode.NOT_FOUND),
            (429, grpc.StatusCode.RESOURCE_EXHAUSTED),
            (503, grpc.StatusCode.UNAVAILABLE),
        ]
        for http_status, grpc_status in cases:
            with self.subTest(http_status=http_status):
                self.assertEqual(provider._api_status_code(HTTPFailure(http_status)), grpc_status)

    def test_track_details_preserves_upstream_status(self):
        self.client.error = HTTPFailure(503)
        with self.assertRaises(AbortError) as caught:
            self.servicer.GetTrackDetails(pb.GetTrackDetailsRequest(track_id="video"), self.context)
        self.assertEqual(caught.exception.code, grpc.StatusCode.UNAVAILABLE)

    def test_search_preserves_upstream_status(self):
        self.client.error = HTTPFailure(403)
        with self.assertRaises(AbortError) as caught:
            self.servicer.Search(
                pb.SearchRequest(query="test", types=[pb.MEDIA_TYPE_ALBUM]), self.context
            )
        self.assertEqual(caught.exception.code, grpc.StatusCode.PERMISSION_DENIED)

    def test_finds_pyinstaller_internal_node(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "gozik-yt-music-server"
            executable.touch()
            node_name = "node.exe" if os.name == "nt" else "node"
            bundled_node = Path(temp_dir) / "_internal" / node_name
            bundled_node.parent.mkdir()
            bundled_node.touch()
            bundled_node.chmod(0o755)

            with mock.patch.object(provider.sys, "frozen", True, create=True), mock.patch.object(
                provider.sys, "executable", str(executable)
            ):
                self.assertEqual(provider._get_node_path(), str(bundled_node))


if __name__ == "__main__":
    unittest.main()
