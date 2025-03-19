#
# Copyright 2020 - 2025 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Helper constants and structures
#

from enum import (
	Enum,
	IntEnum,
	Flag,
	auto
)
from collections import namedtuple

# Version information
__version__ = "0.8"

# User agent for external API calls
USER_AGENT = f"LibraryGuard/{__version__} (https://radio.uoc.gr)"

class LgFormats(Enum):

	AUDIO = "audio"
	VIDEO = "video"
	ARTWORK = "artwork"
	MARKER = "marker"
	TEXT = "text"

	def __str__(self):
		return self.value

class LgConsts(IntEnum):
	MIN_BRATE_MP3 = 128000
	MIN_BRATE_OGG = 112000
	MIN_SRATE = 44100
	DEF_DIR_WORKERS = 2
	MAX_FILE_WORKERS = 4

	# Too many file workers and our bottleneck will be the
	# HDD's seek time. We are on a RAID5/6 system with multiple
	# discs so we can get away with 4-6 workers. Given that the
	# throughput of such a system would be in the order of ~250MB/s,
	# and that we also want to distribute not only I/O but also processing
	# a bit, the 2 * 4 is a good compromise. We'll have 8 workers in
	# total doing I/O which shouldn't saturate our system, just stress
	# it a bit, and not consume too much CPU time on waiting.

	def __str__(self):
		consts_strmap = {
			LgConsts.MIN_BRATE_MP3:"Mininum bitrate for MP3 (128Kbps)",
			LgConsts.MIN_BRATE_OGG:"Mininum bitrate for Ogg/Vorbis (112Kbps)",
			LgConsts.MIN_SRATE:"Minimum sampling rate (44100Hz)",
			LgConsts.DEF_DIR_WORKERS:"Default number of directory workers (4)",
			LgConsts.MAX_FILE_WORKERS:"Maximum number of file workers (8)",
			}
		return consts_strmap.get(self, "Unknown error")


class LgOpts(Flag):

	# Keep them powers of 2 so that we can
	# treat them as bits on a bitmask, auto()
	# does that automaticaly
	DEFAULT = 0
	ODRYRUN = auto()
	OFORCECHECK = auto()
	
	def __str__(self):
		opts_strmap = {
			LgOpts.DEFAULT:"Default options",
			LgOpts.ODRYRUN:"Dry run",
			LgOpts.OFORCECHECK:"Force check",
			}
		return opts_strmap.get(self, "Unknown option")
		
class LgErr(Enum):

	EOK = 0
	EINVFORMAT = auto()
	EINVBRATE = auto()
	EINVSRATE = auto()
	EINVBITS = auto()
	EINVTAGS = auto()
	EMISSINGTAGS = auto()
	ECORRUPTED = auto()
	EINCONSISTENT = auto()
	EEMPTY = auto()
	EIGNORE = auto()
	ERIP = auto()
	EINVPATH = auto()
	ERGAIN = auto()
	EDBERR = auto()
	EACCESS = auto()
	ETERMINATE = auto()
	EUNKNOWN = auto()
		
	def __str__(self):
		err_strmap = {
			LgErr.EOK:"No error",
			LgErr.EINVFORMAT:"Invalid format",
			LgErr.EINVBRATE:"Invalid bitrate",
			LgErr.EINVSRATE:"Invalid sampling rate",
			LgErr.EINVBITS:"Invalid bit depth",
			LgErr.EINVTAGS:"Invalid tags",
			LgErr.EMISSINGTAGS:"Missing tags",
			LgErr.ECORRUPTED:"Corrupted",
			LgErr.EINCONSISTENT:"Inconsistent",
			LgErr.EEMPTY:"Empty",
			LgErr.EIGNORE:"Ignored",
			LgErr.ERIP:"Object rests in peace",
			LgErr.EINVPATH:"Invalid path",
			LgErr.ERGAIN:"Rgain processor failed",
			LgErr.EDBERR:"Database error",
			LgErr.EACCESS:"Couldn't access resource",
			LgErr.ETERMINATE:"Termination requested",
			}
		return err_strmap.get(self, "Unknown error")

class LgException(Exception):
	def __init__(self, error, entry, msg = None):
		self.error = error
		self.entry = entry
		self.msg = msg

	def __str__(self):
		if self.msg is not None:
			return str(self.error) + ": " + self.msg
		if hasattr(self.entry, 'path'):
			return str(self.error) + ": \n\t" + self.entry.path
		elif self.entry is not None:
			return str(self.error) + ": \n\t" + str(self.entry)
		else:
			return str(self.error)
