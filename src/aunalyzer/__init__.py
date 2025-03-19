"""
Aunalyzer - Audio file analyzer using FFmpeg and libebur128.

This package provides tools for analyzing audio files, including:
- Format information (sample rate, bit rate, etc.)
- Duration verification
- EBU R128 loudness analysis (integrated loudness, loudness range)
- Sample peak detection
- ReplayGain 2.0 gain calculation
"""

from .core import Aunalyzer
from ._aunalyzer import AunlzResults
from .exceptions import (
	AunalyzerException, FileNotFoundError, NoAudioStreamError,
	CodecError, MemoryError, EBU128Error
)

# Import constants from the C extension
from ._aunalyzer import (
	ERR_OK, ERR_NOMEM, ERR_NOFILE, ERR_NOSTREAM, ERR_NOCODEC,
	ERR_CODEC_INIT, ERR_CODEC, ERR_EBUR128_INIT, ERR_EBUR128,
	ERR_SWR_INIT, ERR_SWR
)

__version__ = "0.1.0"
__all__ = [
	"Aunalyzer", "AunlzResults", 
	"AunalyzerException", "FileNotFoundError", "NoAudioStreamError",
	"CodecError", "MemoryError", "EBU128Error"
]