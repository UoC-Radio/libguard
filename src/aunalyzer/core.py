"""
Core functionality for the Aunalyzer module.
"""

import os.path
from typing import Any

from ._aunalyzer import (
	analyze_audio as _analyze_audio,
	AunlzResults,
	AunalyzerException as _AunalyzerException,
	ERR_OK, ERR_NOMEM, ERR_NOFILE, ERR_NOSTREAM, ERR_NOCODEC,
	ERR_FMT, ERR_CODEC_INIT, ERR_CODEC, ERR_EBUR128_INIT, ERR_EBUR128,
	ERR_SWR_INIT, ERR_SWR
)

from .exceptions import (
	AunalyzerException, FileNotFoundError, NoAudioStreamError,
	FormatError, CodecError, MemoryError, EBU128Error
)


class Aunalyzer:
	"""
	Main class for analyzing audio files.
	"""

	# Error code to exception mapping
	_ERROR_MAPPING = {
		ERR_NOFILE: FileNotFoundError,
		ERR_NOSTREAM: NoAudioStreamError,
		ERR_NOCODEC: CodecError,
		ERR_FMT: FormatError,
		ERR_CODEC_INIT: CodecError,
		ERR_CODEC: CodecError,
		ERR_NOMEM: MemoryError,
		ERR_EBUR128_INIT: EBU128Error,
		ERR_EBUR128: EBU128Error,
		ERR_SWR_INIT: CodecError,
		ERR_SWR: CodecError
	}

	@staticmethod
	def analyze(filepath: str, do_decode: bool = True, 
		    do_ebur128: bool = True, do_lra: bool = False) -> AunlzResults:
		"""
		Analyze an audio file and return the results.

		Args:
			filepath: Path to the audio file
			do_decode: Whether to decode the file (required for EBU R128 analysis)
			do_ebur128: Whether to perform EBU R128 loudness analysis
			do_lra: Whether to calculate loudness range

		Returns:
			AunlzResults object containing the analysis results

		Raises:
			FileNotFoundError: If the file could not be found or accessed
			NoAudioStreamError: If no audio stream was found in the file
			CodecError: If there was an error with the audio codec
			MemoryError: If there was a memory allocation error
			EBU128Error: If there was an error with the EBU R128 loudness analysis
			AunalyzerException: For other errors
		"""
		# Verify the file exists before passing to C extension
		if not os.path.isfile(filepath):
			raise FileNotFoundError(ERR_NOFILE, f"File not found: {filepath}")

		try:
			# Call the C extension and directly return its result
			return _analyze_audio(filepath, do_decode, do_ebur128, do_lra)
		except _AunalyzerException as e:
			# Map the error code to the appropriate exception type
			error_code = e.args[0] if len(e.args) > 0 else None
			exception_class = Aunalyzer._ERROR_MAPPING.get(error_code, AunalyzerException)

			# Pass through the original arguments from the C extension exception
			raise exception_class(*e.args)