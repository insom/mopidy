import glob
import glib
import logging
import os
import shutil

from daap import DAAPClient

from pykka.actor import ThreadingActor
from pykka.registry import ActorRegistry

from mopidy import settings, DATA_PATH
from mopidy.backends.base import (Backend, CurrentPlaylistController,
    LibraryController, BaseLibraryProvider, PlaybackController,
    BasePlaybackProvider, StoredPlaylistsController,
    BaseStoredPlaylistsProvider)
from mopidy.models import Playlist, Track, Album, Artist
from mopidy.gstreamer import GStreamer

from mopidy.backends.dummy import DummyStoredPlaylistsProvider

logger = logging.getLogger(u'mopidy.backends.daap')

class DaapBackend(ThreadingActor, Backend):
    """
    A backend for playing music from a DAAP share.

    **Dependencies:**

    - None
    """

    def __init__(self, *args, **kwargs):
        super(DaapBackend, self).__init__(*args, **kwargs)

        self.current_playlist = CurrentPlaylistController(backend=self)

        library_provider = DaapLibraryProvider(backend=self)
        self.library_provider = library_provider
        self.library = LibraryController(backend=self,
            provider=library_provider)

        playback_provider = DaapPlaybackProvider(backend=self)
        self.playback = DaapPlaybackController(backend=self,
            provider=playback_provider)

        stored_playlists_provider = DummyStoredPlaylistsProvider(backend=self)
        self.stored_playlists = StoredPlaylistsController(backend=self,
            provider=stored_playlists_provider)

        self.uri_schemes = [u'daap']

        self.gstreamer = None

    def on_start(self):
        gstreamer_refs = ActorRegistry.get_by_class(GStreamer)
        assert len(gstreamer_refs) == 1, \
            'Expected exactly one running GStreamer.'
        self.gstreamer = gstreamer_refs[0].proxy()


class DaapPlaybackController(PlaybackController):
    def __init__(self, *args, **kwargs):
        super(DaapPlaybackController, self).__init__(*args, **kwargs)

        # XXX Why do we call stop()? Is it to set GStreamer state to 'READY'?
        self.stop()

    @property
    def time_position(self):
        return self.backend.gstreamer.get_position().get()


class DaapPlaybackProvider(BasePlaybackProvider):
    def pause(self):
        return self.backend.gstreamer.pause_playback().get()

    def play(self, track):
        self.backend.gstreamer.prepare_change()
        self.backend.library_provider.uri_daap_mapping[track.uri].save('/tmp/a.mp3')
        self.backend.gstreamer.set_uri('file:///tmp/a.mp3').get()
        return self.backend.gstreamer.start_playback().get()

    def resume(self):
        return self.backend.gstreamer.start_playback().get()

    def seek(self, time_position):
        return self.backend.gstreamer.set_position(time_position).get()

    def stop(self):
        return self.backend.gstreamer.stop_playback().get()


class DaapLibraryProvider(BaseLibraryProvider):
    def __init__(self, *args, **kwargs):
        super(DaapLibraryProvider, self).__init__(*args, **kwargs)
        self._uri_mapping = {}
        self.daap_client = DAAPClient()
        self.daap_client.connect('localhost')
        self.daap_session = self.daap_client.login()
        self.uri_daap_mapping = {}
        self.refresh()

    def refresh(self, uri=None):
        tracks = []
        i = 0
        for track in self.daap_session.library().tracks():
            artist = Artist(name=track.artist)
            album = Album(name=track.album, artists=[artist])
            f = Track(uri='daap://%d' % i, length=60000, album=album, artists=[artist], name=track.name)
            tracks.append(f)
            self.uri_daap_mapping[f.uri] = track
            i = i + 1

        for track in tracks:
            self._uri_mapping[track.uri] = track

    def lookup(self, uri):
        try:
            return self._uri_mapping[uri]
        except KeyError:
            raise LookupError('%s not found.' % uri)

    def find_exact(self, **query):
        self._validate_query(query)
        result_tracks = self._uri_mapping.values()

        for (field, values) in query.iteritems():
            if not hasattr(values, '__iter__'):
                values = [values]
            # FIXME this is bound to be slow for large libraries
            for value in values:
                q = value.strip()

                track_filter = lambda t: q == t.name
                album_filter = lambda t: q == getattr(t, 'album', Album()).name
                artist_filter = lambda t: filter(
                    lambda a: q == a.name, t.artists)
                uri_filter = lambda t: q == t.uri
                any_filter = lambda t: (track_filter(t) or album_filter(t) or
                    artist_filter(t) or uri_filter(t))

                if field == 'track':
                    result_tracks = filter(track_filter, result_tracks)
                elif field == 'album':
                    result_tracks = filter(album_filter, result_tracks)
                elif field == 'artist':
                    result_tracks = filter(artist_filter, result_tracks)
                elif field == 'uri':
                    result_tracks = filter(uri_filter, result_tracks)
                elif field == 'any':
                    result_tracks = filter(any_filter, result_tracks)
                else:
                    raise LookupError('Invalid lookup field: %s' % field)
        return Playlist(tracks=result_tracks)

    def search(self, **query):
        self._validate_query(query)
        result_tracks = self._uri_mapping.values()

        for (field, values) in query.iteritems():
            if not hasattr(values, '__iter__'):
                values = [values]
            # FIXME this is bound to be slow for large libraries
            for value in values:
                q = value.strip().lower()

                track_filter  = lambda t: q in t.name.lower()
                album_filter = lambda t: q in getattr(
                    t, 'album', Album()).name.lower()
                artist_filter = lambda t: filter(
                    lambda a: q in a.name.lower(), t.artists)
                uri_filter = lambda t: q in t.uri.lower()
                any_filter = lambda t: track_filter(t) or album_filter(t) or \
                    artist_filter(t) or uri_filter(t)

                if field == 'track':
                    result_tracks = filter(track_filter, result_tracks)
                elif field == 'album':
                    result_tracks = filter(album_filter, result_tracks)
                elif field == 'artist':
                    result_tracks = filter(artist_filter, result_tracks)
                elif field == 'uri':
                    result_tracks = filter(uri_filter, result_tracks)
                elif field == 'any':
                    result_tracks = filter(any_filter, result_tracks)
                else:
                    raise LookupError('Invalid lookup field: %s' % field)
        return Playlist(tracks=result_tracks)

    def _validate_query(self, query):
        for (_, values) in query.iteritems():
            if not values:
                raise LookupError('Missing query')
            for value in values:
                if not value:
                    raise LookupError('Missing query')
