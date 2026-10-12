"""Domain errors translated by the HTTP layer into clear API responses."""


class FrameNotesError(Exception):
    """Base class for expected user-facing errors."""


class SourceParseError(FrameNotesError):
    """An input source is malformed or unsupported."""


class SourceAccessError(FrameNotesError):
    """A source is valid but cannot be opened or downloaded."""


class MediaExtractionError(FrameNotesError):
    """A video/image could not be decoded into frames."""


class CancelledProcessing(FrameNotesError):
    """The user requested cancellation while processing."""


class JobNotFound(FrameNotesError):
    """The requested job does not exist."""


class ExportError(FrameNotesError):
    """An export could not be generated or validated."""
