"""
Exception classes for the Aunalyzer module.
"""

class AunalyzerException(Exception):
	"""
	Base exception for all Aunalyzer errors.
	
	Attributes:
	error_code -- numeric error code (first element of args)
	message -- explanation of the error (second element of args)
	track_info -- partial TrackInfo object (third element of args, if available)
	"""

	@property
	def error_code(self):
		return self.args[0] if len(self.args) > 0 else None

	@property
	def message(self):
		return self.args[1] if len(self.args) > 1 else str(self)

	@property
	def track_info(self):
		return self.args[2] if len(self.args) > 2 else None

	def __str__(self):
		return self.message if len(self.args) > 1 else super().__str__()

# More specific exception classes for different error types
class FileNotFoundError(AunalyzerException):
	"""Raised when the audio file could not be found or accessed."""
	pass

class NoAudioStreamError(AunalyzerException):
	"""Raised when no audio stream was found in the file."""
	pass

class FormatError(AunalyzerException):
	"""Raised when there was an error with the format ctx."""
	pass

class CodecError(AunalyzerException):
	"""Raised when there was an error with the audio codec."""
	pass

class MemoryError(AunalyzerException):
	"""Raised when there was a memory allocation error."""
	pass

class EBU128Error(AunalyzerException):
	"""Raised when there was an error with the EBU R128 loudness analysis."""
	pass