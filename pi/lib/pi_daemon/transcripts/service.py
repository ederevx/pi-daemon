"""The transcripts domain's state: the index, the handles and the sweep."""

import os
import threading

import pi_transcript


class TranscriptService:
    """It owns the index and the read handles; a reader is made per
    request, so a sidecar rebuilt underneath never leaves one stale."""

    def __init__(self, config, store_root, live_files, recorded_file):
        self._config = config
        self._root = os.path.abspath(store_root) if store_root else store_root
        self._live_files = live_files
        self._recorded_file = recorded_file
        self._stopping = threading.Event()
        self._index = None
        self._handles = None
        self._watch = None

    def start(self):
        self._index = pi_transcript.TranscriptIndex(
            max_bytes=self._config.transcript_index_max_bytes,
            max_line=self._config.transcript_index_max_line,
            root=self._root)
        self._handles = pi_transcript.TranscriptHandles()
        self._watch = pi_transcript.TranscriptIndexWatch(
            self._index, self._root, self._live_files, self._stopping,
            interval=self._config.transcript_index_interval
            if self._config.transcript_index else 0.0)
        self._watch.start()

    def stop(self):
        self._stopping.set()

    def root(self):
        return self._root

    def owns(self, file):
        """The daemon never serves a path outside the conversation store."""
        return self._index is not None and self._index.owns(file)

    def announced(self, session):
        """The file a session announced, never a path the wire chose."""
        return self._recorded_file(session) if session else None

    def handle(self, transcript_id):
        """The file a handle issued by transcript-open still names."""
        if self._index is None:
            return None
        return self._handles.resolve(transcript_id)

    def reader(self, file):
        return pi_transcript.TranscriptReader(self._index, file)

    def issue(self, file):
        return self._handles.issue(file)

    def forget(self, file):
        self._index.forget(file)
