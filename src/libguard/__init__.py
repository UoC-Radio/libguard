#
# Copyright 2020 Nick Kossifidis <mickflemm@gmail.com>
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

class LgFormats(Enum):

	AUDIO = "audio"
	VIDEO = "video"
	ARTWORK = "artwork"
	TEXT = "text"

	def __str__(self):
		return self.value

class LgConsts(IntEnum):
	MIN_BRATE = 128000
	MIN_SRATE = 44100
	RGAIN_REF_LVL = 89

	def __str__(self):
		consts_strmap = {
			LgConsts.MIN_BRATE:"Mininum bitrate (128Kbps)",
			LgConsts.MIN_SRATE:"Minimum sampling rate (44100Hz)",
			}
		return consts_strmap.get(self, "Unknown error")


class LgOpts(Flag):

	# Keep them powers of 2 so that we can
	# treat them as bits on a bitmask, auto()
	# does that automaticaly
	DEFAULT = 0
	ODRYRUN = auto()
	OFORCECHECK = auto()
	OFORCERGAIN = auto()
	
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
	EINVTAGS = auto()
	EMISSINGTAGS = auto()
	ECORRUPTED = auto()
	EINCONSISTENT = auto()
	EEMPTY = auto()
	EIGNORE = auto()
	EMISSINGTOOL = auto()
	ERIP = auto()
	EINVPATH = auto()
	ERGAIN = auto()
	EDBERR = auto()
	ENOGSTPLUGIN = auto()
	EUNKNOWN = auto()
		
	def __str__(self):
		err_strmap = {
			LgErr.EOK:"No error",
			LgErr.EINVFORMAT:"Invalid format",
			LgErr.EINVBRATE:"Invalid bitrate",
			LgErr.EINVTAGS:"Invalid tags",
			LgErr.EMISSINGTAGS:"Missing tags",
			LgErr.EINVSRATE:"Invalid sampling rate",
			LgErr.ECORRUPTED:"Corrupted",
			LgErr.EINCONSISTENT:"Inconsistent",
			LgErr.EEMPTY:"Empty",
			LgErr.EIGNORE:"Ignored",
			LgErr.EMISSINGTOOL:"Missing tool",
			LgErr.EINVPATH:"Invalid path",
			LgErr.ERGAIN:"Rgain processor failed",
			LgErr.EDBERR:"Database error",
			LgErr.ENOGSTPLUGIN:"Missing GSTreamer plugin",
			LgErr.ERIP:"Object rests in peace"
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
		elif self.entry is not None:
			return str(self.error) + ": \n\t" + self.entry.path
		else:
			return str(self.error)

LgRgainTrackData = namedtuple("LgRgainTrackResult", ["filename", "gain", "peak", "ref_lvl"])
LgRgainAlbumData = namedtuple("LgRgainAlbumResult", ["gain", "peak"])
