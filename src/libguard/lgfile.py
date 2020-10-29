#
# Copyright 2020 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Handling of individual files
#

from abc import ABC
import time
from os import (
	setxattr,
	getxattr, 
	removexattr,
	path
)
from logging import (
	debug,
	info,
	warning,
	error
)
from libguard import (
	LgFormats,
	LgConsts,
	LgOpts,
	LgErr,
	LgException
)
import mimetypes
import magic

# For audio files
from subprocess import (
	check_call,
	CalledProcessError,
	DEVNULL
)
from mutagen import MutagenError
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import EasyMP3
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.wavpack import WavPack

#
# Top class (entry point)
#

class LgFile(ABC):

	# Xattr manipulation:
	# To save time we mark files we've already typechecked/verified through
	# user xattrs on the filesystem. For typecheck we save last ctime (+offset)
	# and the string value of type (from LgFormats), for verification we save
	# the current mtime. If the file gets renamed/moved ctime will change so we'll
	# perform the typecheck again, if the file is modified mtime will change so
	# we'll perform the verification again.
	@staticmethod
	def get_type_from_xattrs(fentry):
		stat = fentry.stat()	
		ctime = int(stat.st_ctime)
		ret = None
		try:
			check_ts = int(getxattr(fentry.path, b"user.lguard_typecheck_ts"))
		except OSError:
			debug("No typecheck_ts present, typecheck needed:\n\t%s",
			      fentry.path);
			del stat, ctime, ret
			return None

		debug("Typecheck_ts check:\n\t%s\n\t(ctime: %d, check_ts: %d)",
		      fentry.path, ctime, check_ts)

		if ctime <= check_ts:
			try:
				ftype = str(getxattr(fentry.path, b"user.lguard_ftype").decode("ascii"))
			except OSError:
				warning("Typecheck timestamp present but no ftype !: %s", fentry.path)
				removexattr(fentry.path, b"user.lguard_typecheck_ts")
				del stat, ctime, ret, check_ts
				return None
			try:
				ret = LgFormats(str(ftype))
			except ValueError:
				warning("Previous typecheck set an invalid ftype!:\n\t%s", fentry.path)
				removexattr(fentry.path, b"user.lguard_typecheck_ts")
				removexattr(fentry.path, b"user.lguard_ftype")
				del stat, ctime, check_ts, ftype
				return None

			debug("Got saved type (%s):\n\t%s", str(ret), fentry.path)
			del stat, ctime, check_ts, ftype
			return ret
		else:
			debug("File metadata changed, should typecheck again:\n\t%s", fentry.path)
			removexattr(fentry.path, b"user.lguard_typecheck_ts")
			removexattr(fentry.path, b"user.lguard_ftype")
		del stat, ctime, ret, check_ts
		return None
			
	@staticmethod
	def update_type_on_xattrs(fentry, ftype):
		# By setting xattrs we'll update ctime to localtime. Just to be on the
		# safe side (since different file systems have different resolutions for
		# ctime) add 1s to make sure we are >= updated ctime.
		new_ctime = int(time.time()) + 1
		setxattr(fentry.path, b"user.lguard_typecheck_ts", str(new_ctime).encode("ascii"))
		setxattr(fentry.path, b"user.lguard_ftype", str(ftype).encode("ascii"))
		debug("Typecheck_ts update:\n\t%s\n\t(typecheck_ts: %d, type: %s)",
		      fentry.path, new_ctime, ftype)
		del new_ctime
		return

	@staticmethod
	def get_verification_ts_from_xattrs(fentry):
		try:
			check_ts = int(getxattr(fentry.path, b"user.lguard_verification_ts").decode("ascii"))
		except OSError:
			# Check for the older attr name, if present remove it and use the new one
			# TODO: remove this once library is up to date
			try:
				check_ts = float(getxattr(fentry.path, b"user.libfile_check_ts").decode("ascii"))
			except OSError:
				debug("No check_ts present, check needed:\n\t%s", fentry.path);
				return None
			else:
				removexattr(fentry.path, b"user.libfile_check_ts")
				del check_ts
				return LgFile.update_verification_ts_on_xattrs(fentry)
		else:
			return check_ts

	@staticmethod
	def update_verification_ts_on_xattrs(fentry):
		stat = fentry.stat()
		mtime = int(stat.st_mtime)
		setxattr(fentry.path, b"user.lguard_verification_ts", str(mtime).encode("ascii"))
		debug("Updated verification_ts (%d):\n\t%s", mtime, fentry.path)
		# The above changed ctime re-set typecheck timestamp
		new_ctime = int(time.time()) + 1
		setxattr(fentry.path, b"user.lguard_typecheck_ts", str(new_ctime).encode("ascii"))
		del stat, mtime
		return new_ctime

	

	@staticmethod
	def get_type(fentry, opts):

		# Try to determine the file's format from saved
		# xattrs.
		if not LgOpts.OFORCECHECK in opts:
			saved_type = LgFile.get_type_from_xattrs(fentry)
			if not (saved_type is None):
				return saved_type

		# Determine file's format the hard way
	
		fext = path.splitext(fentry.name)[1]
		mimetype = mimetypes.guess_type(fentry.path)[0]
		mimetype_magic = magic.from_file(fentry.path, mime=True)

		if mimetype == None:
			# Some text files don't have extensions so mimetypes will
			# fail to guess the filetype, use magic to be sure.
			if mimetype_magic != None:
				mimetype_magic_major = mimetype_magic.split('/')[0]
				if mimetype_magic_major == "text":
					if not LgOpts.ODRYRUN in opts:
						LgFile.update_type_on_xattrs(fentry, LgFormats.TEXT)
					del fext, mimetype, mimetype_magic
					return LgFormats.TEXT
				# We have two markers to indicate that writes to a directory should
				# be ignored, and another marker to indicate that a directory should
				# be skiped during verification (it has known issues but we are ok
				# with it). These markers are empty text files with no extensions, make
				# sure we match them here in case the above check fails.
				elif (
					fentry.name == "lock"
					or fentry.name == "locked"
					or fentry.name == "ignore"
				     ):
					if not LgOpts.ODRYRUN in opts:
						LgFile.update_type_on_xattrs(fentry, LgFormats.TEXT)
					del fext, mimetype, mimetype_magic
					return LgFormats.TEXT
				else:
					error("Unknown file extension:\n\t%s", fentry.path)
			else:
				error("Unknown file type:\n\t%s", fentry.path)
			del fext, mimetype, mimetype_magic
			return None

		if mimetype_magic == None:
			error("Unknown magic value:\n\t%s", fentry.path)
			del fext, mimetype, mimetype_magic
			return None

		mimetype_magic_major, mimetype_magic_minor = mimetype_magic.split('/')
		mimetype_major, mimetype_minor = mimetype.split('/')

		if mimetype != mimetype_magic:
			if mimetype_major == mimetype_magic_major:
				# Check if we got x-something instead of something
				if (mimetype_minor != "x-" + mimetype_magic_minor
				    and mimetype_magic_minor != "x-" + mimetype_minor):
				    	# Major type fits but the format doesn't seem to match the extension
					warning("Inconsistent file extension:\n\t%s\n\t(is %s vs %s)",
						fentry.path, mimetype_magic, mimetype)
			else:
				# Empty text files (used as markers) don't have a magic value
				if (mimetype == "text/plain" and mimetype_magic == "inode/x-empty"):
					debug("Empty text file:\n\t%s\n\t(is %s vs %s)",
					      fentry.path, mimetype_magic, mimetype)
				# Some text files contain encoded data so they'll come back as JSON/XML etc
				# or they may have a bad encoding (e.g. wrong charset). Others are identified
				# based on their extension e.g. as md5 or json but based on their magic value
				# they come up as text/plain. Print a warning but don't treat this as fatal or
				# they'll take the whole album with them.
				elif (mimetype == "text/plain" or mimetype_magic == "text/plain"):
					warning("Inconsistent text file:\n\t%s\n\t(is %s vs %s)",
						fentry.path, mimetype_magic, mimetype)
				# On MP3s data can start anywhere in the file and so the magic value may
				# not be found at the begining. In case magic can't handle this and
				# returns application/octet-stream just ignore it, we'll assume for now
				# that this file is indeed an audio file and revisit it on verify() later on.
				elif (mimetype == "audio/mpeg" and fext == ".mp3" and
				      mimetype_magic == "application/octet-stream"):
					debug("Headerless mp3:\n\t%s\n\t(is %s vs %s)",
					      fentry.path, mimetype_magic, mimetype)
				# Some are even worse and they are being detected as something else entirely,
				# give them a chance to come up clean on verify() later on but warn the user
				# about it.
				elif (mimetype == "audio/mpeg" and fext == ".mp3"):
					warning("Inconsistent magic value on mp3:\n\t%s\n\t(is %s vs %s)",
						fentry.path, mimetype_magic, mimetype)
				else:
					# This file's extension and type are inconsistent, get rid of this
					# mess and raise the error flag.
					error("Inconsistent file format:\n\t%s\n\t(is %s vs %s)",
					      fentry.path, mimetype_magic, mimetype)
					del fext, mimetype, mimetype_magic
					del C, mimetype_magic_minor
					del mimetype_major, mimetype_minor
					return None

		if mimetype_major == "audio":
			ret = LgFormats.AUDIO
		# We have some booklets in PDF format
		elif mimetype_major == "image" or mimetype == "application/pdf":
			ret = LgFormats.ARTWORK
		elif (mimetype_major == "text" or mimetype_magic_major == "text"):
			ret = LgFormats.TEXT
		elif mimetype_major == "video":
			ret = LgFormats.VIDEO
		else:
			error("Unhandled file type:\n\t%s\n\t(%s)", fentry.path, mimetype)
			ret = None

		if not LgOpts.ODRYRUN in opts and ret is not None:
			LgFile.update_type_on_xattrs(fentry, ret)

		del fext, mimetype, mimetype_magic
		del mimetype_magic_major, mimetype_magic_minor
		del mimetype_major, mimetype_minor
		return ret

	def __new__(cls, fentry, opts):
		# Determine file's format
		ftype = LgFile.get_type(fentry, opts)
		if ftype is None:
			raise LgException(LgErr.EINVFORMAT, fentry)

		debug("Got file type (%s):\n\t%s", str(ftype), fentry.path)

		if ftype == LgFormats.AUDIO:
			del ftype
			return LgAudioFile.__new__(LgAudioFile, fentry, opts)
		# We have some booklets in PDF format
		elif ftype == LgFormats.ARTWORK:
			del ftype
			return super().__new__(LgArtworkFile)
		elif ftype == LgFormats.TEXT:
			del ftype
			return super().__new__(LgTextFile)
		elif ftype == LgFormats.VIDEO:
			del ftype
			return super().__new__(LgVideoFile)
		else:
			del ftype
			error("Unhandled file type:\n\t%s", fentry.path)
			raise LgException(LgErr.EINVFORMAT, fentry)
		
	def __init__(self, fentry, opts):
		self.fentry = fentry
		self.options = opts

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		# Remove references to fentry and opts
		# and clean up the memory of the local
		# variables.
		self.fentry = None
		del self.fentry
		self.options = None
		del self.options
		# Become a dead file
		self.__class__ = _LgRipFile
		return True

	def verify(self):
		return LgErr.EOK

	def get_path(self):
		return self.fentry.path

	def get_name(self):
		return self.fentry.name

#
# A dead file that throws exceptions evrytime
# one of its functions are called
#

class _LgRipFile(LgFile):
	def verify(self):
		raise LgException(LgErr.ERIP, None)

	def get_path(self):
		raise LgException(LgErr.ERIP, None)

	def get_name(self):
		raise LgException(LgErr.ERIP, None)

#
# Small subclasses
#

class LgVideoFile(LgFile):

	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)

class LgArtworkFile(LgFile):

	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)

class LgTextFile(LgFile):

	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)
		# We know there are issues with this directory and we want them
		# ignored through this file/marker. Propagate this to the dir object.
		if self.fentry.name == "ignore":
			info("Got ignore marker:\n\t%s", self.fentry.path)
			raise LgException(LgErr.EIGNORE, fentry)

#
# The audio file subclass
#

class LgAudioFile(LgFile):

	def __new__(cls, fentry, opts):
		# Determine file's format
		try:
			fext = path.splitext(fentry.name)[1]
		except IndexError:
			error("Unhandled audio file type:\n\t%s", fentry.path)
			raise LgException(LgErr.EINVFORMAT, fentry)

		if fext == ".mp3":
			del fext
			return super(LgFile, cls).__new__(LgMP3File)
		elif fext == ".flac":
			del fext
			return super(LgFile, cls).__new__(LgFlacFile)
		elif fext == ".ogg":
			del fext
			return super(LgFile, cls).__new__(LgOggFile)
		elif fext == ".wv":
			del fext
			return super(LgFile, cls).__new__(LgWavpackFile)
		else:
			del fext
			error("Unhandled audio file type:\n\t%s", fentry.path)
			raise LgException(LgErr.EINVFORMAT, fentry)
			
	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)
		self.verify_cmd = None
		self.verify_cmd_args = None
		self.mutagen_handle = None
		self.tgain_tag_key = "replaygain_track_gain"
		self.tpeak_tag_key = "replaygain_track_peak"
		self.again_tag_key = "replaygain_album_gain"
		self.apeak_tag_key = "replaygain_album_peak"
		self.reflvl_tag_key = "replaygain_reference_loudness"


	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		# Remove references to fentry and opts
		# and clean up the memory of the local
		# variables.
		self.fentry = None
		del self.fentry
		self.options = None
		del self.options
		del self.verify_cmd
		del self.verify_cmd_args
		del self.mutagen_handle
		del self.tgain_tag_key
		del self.tpeak_tag_key
		del self.again_tag_key
		del self.apeak_tag_key
		del self.reflvl_tag_key
		# Become a dead file
		self.__class__ = _LgRipAudioFile
		return True
		
	def verify_bitrate(self):
		if self.mutagen_handle.info.bitrate < LgConsts.MIN_BRATE:
			error("Bitrate below threshold (%i):\n\t%s",
			      self.mutagen_handle.info.bitrate, self.fentry.path)
			return LgErr.EINVBRATE
		else:
			return LgErr.EOK

	def verify_sampling_rate(self):
		if self.mutagen_handle.info.sample_rate < LgConsts.MIN_SRATE:
			error("Sample rate below threshold (%i):\n\t%s",
			      self.mutagen_handle.info.sample_rate, self.fentry.path)
			return LgErr.EINVSRATE
		else:
			return LgErr.EOK

	def verify(self, force = False):
		if not LgOpts.OFORCECHECK in self.options or force:
			stat = self.fentry.stat()
			mtime = int(stat.st_mtime)
			check_ts = LgFile.get_verification_ts_from_xattrs(self.fentry)
			if check_ts is not None and mtime == check_ts:
				del stat, mtime, check_ts
				return LgErr.EOK
		
		ret = self.verify_bitrate()
		if ret is not LgErr.EOK:
			return ret

		ret = self.verify_sampling_rate()
		if ret is not LgErr.EOK:
			return ret

		del ret
		
		try:
			check_call([self.verify_cmd] + [self.verify_cmd_args] + [self.fentry.path],
				   stdout=DEVNULL, stderr=DEVNULL)
		except CalledProcessError as err:
			# If a needed tool doesn't exist raise an exception
			if hasattr(err, "errno") and err.errno == errno.ENOENT:
				del err
				raise LgException(LgErr.EMISSINGTOOL, self.fentry)
			else:
				# Check failed
				error("Integrity check failed:\n\t%s", self.fentry.path)
				del err
				return LgErr.ECORRUPTED
		# Check passed
		debug("File verified:\n\t%s", self.fentry.path)
		if not LgOpts.ODRYRUN in self.options:
			LgFile.update_verification_ts_on_xattrs(self.fentry)
		return LgErr.EOK
				
	def get_albuminfo(self):
		num_discs = None
		num_tracks = None
		album_id = None
		album_gain = None
		releasegroup_id = None
		
		# OGG/FLAC Total number of disks 
		num_discs = self.mutagen_handle.tags.get('DISCTOTAL')
		if num_discs is None:	# ID3 Number of disks (position in set)
			num_discs = self.mutagen_handle.tags.get('TPOS')
			if num_discs is None:	# APEv2 TPOS equivalent
				num_disks = self.mutagen_handle.tags.get('Disc')
				if num_discs is None:	# Mutagen's EasyID3 representation
					num_discs = self.mutagen_handle.tags.get('Discnumber')
					if num_discs is not None:
						num_discs = num_discs[0]
			if num_discs is not None:
				try:
					num_discs = int(str(num_discs).split('/')[1])
				except IndexError:
					pass
		else:
			num_discs = int(num_discs[0])

		# OGG/FLAC Total number of tracks 
		num_tracks = self.mutagen_handle.tags.get('TRACKTOTAL')
		if num_tracks is None:	# ID3 Number of tracks
			num_tracks = self.mutagen_handle.tags.get('TRCK')
			if num_tracks is None:	# APEv2 TRCK equivalent
				num_tracks = self.mutagen_handle.tags.get('Track')
				if num_tracks is None:	# Mutagen's EasyID3 representation
					num_tracks = self.mutagen_handle.tags.get('Tracknumber')
					if num_tracks is not None:
						num_tracks = num_tracks[0]
			if num_tracks is not None:
				try:
					num_tracks = int(str(num_tracks).split('/')[1])
				except IndexError:
					pass
		else:
			num_tracks = int(num_tracks[0])

		# OGG/FLAC/APEv2/EasyMP3 Musicbrainz Album ID
		album_id = self.mutagen_handle.tags.get('musicbrainz_albumid')
		if album_id is None:
			# ID3 Musicbrainz Album ID in TXXX form
			album_id = self.mutagen_handle.tags.get('TXXX:MusicBrainz Album Id')
			if album_id is not None:
				album_id = album_id[0]
		else:
			album_id = album_id[0]


		# OGG/FLAC/APEv2/EasyMP3 Replaygain album gain
		album_gain = self.mutagen_handle.tags.get('replaygain_album_gain')
		if album_gain is None:
			# ID3 Album gain in TXXX form
			album_gain = self.mutagen_handle.tags.get('TXXX:replaygain_album_gain')
			if album_gain is not None:
				album_gain = album_gain[0]
		else:
			album_gain = album_gain[0]

		# OGG/FLAC/APEv2/EasyMP3 Replaygain album gain
		releasegroup_id = self.mutagen_handle.tags.get('musicbrainz_releasegroupid')
		if releasegroup_id is None:
			# ID3 Album gain in TXXX form
			releasegroup_id = self.mutagen_handle.tags.get('TXXX:MusicBrainz Release Group Id')
			if releasegroup_id is not None:
				releasegroup_id = releasegroup_id[0]
		else:
			releasegroup_id = releasegroup_id[0]
			
		return num_discs, num_tracks, album_id, album_gain, releasegroup_id

	def get_rgain_values(self):
		tgain = self.mutagen_handle.tags.get(self.tgain_tag_key)
		tpeak = self.mutagen_handle.tags.get(self.tpeak_tag_key)
		again = self.mutagen_handle.tags.get(self.again_tag_key)
		apeak = self.mutagen_handle.tags.get(self.apeak_tag_key)
		ref_lvl = self.mutagen_handle.tags.get(self.reflvl_tag_key)
		return tgain, tpeak, again, apeak, ref_lvl

	def update_rgain_values(self, track_gain, track_peak, album_gain, album_peak, ref_lvl):

		if LgOpts.ODRYRUN in self.options:
			return LgErr.EOK

		if track_gain is None or track_peak is None:
			error("Got empty ReplayGain info to tag")
			return LgErr.ERGAIN

		# Make sure the file we are about to edit is verified, we don't
		# want to mess with a file already messed up.
		ret = self.verify()
		if ret is not LgErr.EOK:
			return ret

		self.mutagen_handle.tags[self.tgain_tag_key] = ("%.8f dB" % track_gain)
		self.mutagen_handle.tags[self.tpeak_tag_key] = ("%.8f" % track_peak)
		self.mutagen_handle.tags[self.reflvl_tag_key] = ("%.1f dB" % ref_lvl)

		if album_gain is not None and album_peak is not None:
			self.mutagen_handle.tags[self.again_tag_key] = ("%.8f dB" % album_gain)
			self.mutagen_handle.tags[self.apeak_tag_key] = ("%.8f" % album_peak)

		# Note that the above will modify mtime but we'll re-verify this file
		# after saving the tags anyway, since mutagen may corrupt it while
		# adding new tags (better safe than sorry).
		try:
			self.mutagen_handle.save()
		except MutagenError as err:
			error("Update of ReplayGain data failed:\n\t%s\n\t%s",
			      self.fentry.path, str(err))
			return LgErr.ERGAIN

		info("Updated ReplayGain info:\n\t%s", self.fentry.path) 
		return self.verify(force = True)

#
# A dead audio file
#

class _LgRipAudioFile(_LgRipFile):
	def verify_bitrate(self):
		raise LgException(LgErr.ERIP, None)

	def verify_sampling_rate(self):
		raise LgException(LgErr.ERIP, None)

	def get_albuminfo(self):
		raise LgException(LgErr.ERIP, None)

#
# Audio file subclasses
#

class LgMP3File(LgAudioFile):

	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)
		self.verify_cmd = "mpck"
		self.verify_cmd_args = "-q"			
		try:
			self.mutagen_handle = EasyMP3(self.fentry.path)
		except MutagenError as err:
			self.mutagen_handle = None
			error("Mutagen failed to open file, treating tags as invalid:\n\t%s\n\t(%s)",
			      self.fentry.path, str(err))
			raise LgException(LgErr.EINVTAGS, self.fentry)
		else:
			eid3 = self.mutagen_handle.tags
			eid3.RegisterTXXXKey(("TXXX:%s" % self.tgain_tag_key), self.tgain_tag_key)
			self.tgain_tag_key = ("TXXX:%s" % self.tgain_tag_key)
			
			eid3.RegisterTXXXKey(("TXXX:%s" % self.tpeak_tag_key), self.tpeak_tag_key)
			self.tpeak_tag_key = ("TXXX:%s" % self.tpeak_tag_key)

			eid3.RegisterTXXXKey(("TXXX:%s" % self.again_tag_key), self.again_tag_key)
			self.again_tag_key = ("TXXX:%s" % self.again_tag_key)

			eid3.RegisterTXXXKey(("TXXX:%s" % self.apeak_tag_key), self.apeak_tag_key)
			self.apeak_tag_key = ("TXXX:%s" % self.apeak_tag_key)

			eid3.RegisterTXXXKey(("TXXX:%s" % self.reflvl_tag_key), self.reflvl_tag_key)
			self.reflvl_tag_key = ("TXXX:%s" % self.reflvl_tag_key)
			del eid3

class LgFlacFile(LgAudioFile):

	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)
		self.verify_cmd = "flac"
		self.verify_cmd_args = "-t"
		try:
			self.mutagen_handle = FLAC(self.fentry.path)
		except MutagenError as err:
			self.mutagen_handle = None
			error("Mutagen failed to open file, treating tags as invalid:\n\t%s\n\t(%s)",
			      self.fentry.path, str(err))
			raise LgException(LgErr.EINVTAGS, self.fentry)

	def verify_bitrate(self):
		return LgErr.EOK
				
class LgOggFile(LgAudioFile):

	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)
		self.verify_cmd = "ogginfo"
		self.verify_cmd_args = "-q"
		try:
			self.mutagen_handle = OggVorbis(self.fentry.path)
		except MutagenError as err:
			self.mutagen_handle = None
			error("Mutagen failed to open file, treating tags as invalid:\n\t%s\n\t(%s)",
			      self.fentry.path, str(err))
			raise LgException(LgErr.EINVTAGS, self.fentry)
			
class LgWavpackFile(LgAudioFile):

	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)
		self.verify_cmd = "wvunpack"
		self.verify_cmd_args = "-vv"
		try:
			self.mutagen_handle = WavPack(self.fentry.path)
		except MutagenError as err:
			self.mutagen_handle = None
			error("Mutagen failed to open file, treating tags as invalid:\n\t%s\n\t(%s)",
			      self.fentry.path, str(err))
			raise LgException(LgErr.EINVTAGS, self.fentry)

	def verify_bitrate(self):
		return LgErr.EOK
