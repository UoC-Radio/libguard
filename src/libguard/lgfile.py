#
# Copyright 2020 - 2025 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Handling of individual files
#

# Standard library imports
from abc import ABC, abstractmethod
import math
from typing import Optional
from dataclasses import dataclass, field
import threading
import os  # For os.R_OK in access check
from os import (
	setxattr,
	getxattr,
	access,
)
import shutil

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
	LgException,
	USER_AGENT
)
import mimetypes
import magic
import requests
import uuid
# Third-party imports

# For analyzing audio files via ffmpeg (c code)
from aunalyzer import Aunalyzer
from aunalyzer.exceptions import (
	CodecError,
	EBU128Error,
	AunalyzerException
)

# For metadata handling
from mutagen import MutagenError
from mutagen.mp3 import MP3
from mutagen.id3 import TXXX
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.wavpack import WavPack

#
# Top class (entry point)
#


class LgFile(ABC):

	#
	# HELPERS
	#

	# Xattr manipulation:
	# To save time we mark files we've already verified through user xattrs on the filesystem
	# so that we can skip them next time, otherwise we'll verify the whole library again and again
	@staticmethod
	def _check_verification_ts_from_xattrs(fentry):
		mtime = int(fentry.stat().st_mtime)
		try:
			check_ts = int(getxattr(fentry.path, b"user.lguard_verification_ts").decode("ascii"))
			if mtime != check_ts:
				warning("mtime changed from last verification_ts, verification needed:\n\t%s", fentry.path)
				return None
			del check_ts
			return mtime
		except OSError:
			debug("No verification_ts present, verification needed:\n\t%s", fentry.path)
		except Exception as err:
			error("Unexpected error getting verification on:\n\t%s\n\t%s", fentry.path, str(err))
		del mtime
		return None

	@staticmethod
	def _update_verification_ts_on_xattrs(fentry):
		# Force a filesystem sync to ensure we get the current mtime
		os.sync()

		# Get the current mtime
		stat = os.stat(fentry.path)
		mtime = int(stat.st_mtime)

		try:
			check_ts = int(getxattr(fentry.path, b"user.lguard_verification_ts").decode("ascii"))
		except OSError:
			check_ts = None

		if check_ts is None or mtime != check_ts:
			try:
				setxattr(fentry.path, b"user.lguard_verification_ts", str(mtime).encode("ascii"))
				debug("Updated verification_ts (%d):\n\t%s", mtime, fentry.path)
			
				# Make the file read-only for group after successful verification
				import stat
				current_mode = os.stat(fentry.path).st_mode
				# Remove group write permissions
				new_mode = current_mode & ~stat.S_IWGRP
				os.chmod(fentry.path, new_mode)
				debug("Made file read-only for group:\n\t%s", fentry.path)
				del current_mode, new_mode
			except OSError as err:
				warning("Failed to set verification timestamp or permissions:\n\t%s\n\t%s", fentry.path, str(err))
			except Exception as err:
				error("Unexpected error setting verification timestamp or permissions:\n\t%s\n\t%s", fentry.path, str(err))
		del check_ts, mtime

	@staticmethod
	def _get_type(fentry):

		# First check if we have one of our markers, those are usualy
		# empty files (so they should have a mime type of inode/x-empty), or
		# plain text files (with some description inside), to mark a directory
		# locked (so no modifications are allowed), or ignored (it has known
		# issues but we are ok with it so skip it).
		if (fentry.name == "lock" or fentry.name == "locked" or fentry.name == "ignore"):
			return LgFormats.MARKER

		# Try to determine the file's format

		fext = os.path.splitext(fentry.name)[1].lower()

		mimetype = mimetypes.guess_type(fentry.path)[0]
		mimetype_magic = magic.from_file(fentry.path, mime=True)

		if mimetype is None:
			# This isn't recognized as text
			if (fext == ".accurip"):
				debug("Got accurip file:\n\t%s", fentry.path)
				del fext, mimetype, mimetype_magic
				return LgFormats.TEXT
			error("Unhandled file extension:\n\t%s", fentry.path)
			del fext, mimetype, mimetype_magic
			return None

		if mimetype_magic is None:
			error("Unknown magic value:\n\t%s", fentry.path)
			del fext, mimetype, mimetype_magic
			return None

		mimetype_magic_major, mimetype_magic_minor = mimetype_magic.split('/')
		mimetype_major, mimetype_minor = mimetype.split('/')

		# Handle inconsistency between extension and actual content
		# some cases are expected so we just report them and move on.
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
					del mimetype_magic_major, mimetype_magic_minor
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

		del fext, mimetype, mimetype_magic
		del mimetype_magic_major, mimetype_magic_minor
		del mimetype_major, mimetype_minor
		return ret

	def _delete(self):
		if LgOpts.ODRYRUN in self.options:
			info("Would delete file:\n\t%s", self.fentry.path)
			self.fentry = None
			return

		try:
			info("Deleting file:\n\t%s", self.fentry.path)
			os.remove(self.fentry.path)
		except FileNotFoundError:
			warning("File already deleted:\n\t%s", self.fentry.path)
		except Exception as err:
			warning("Could not delete file:\n\t%s\n\t%s",
				self.fentry.path, err)
		else:
			self.fentry = None

	#
	# OBJECT INSTANTIATION/CLEANUP
	#

	def __new__(cls, fentry, opts):
		# Allow subclasses to handle their own creation
		if cls is not LgFile:
			return super().__new__(cls)

		# Verify that we at least have read access to the file
		if not access(fentry.path, os.R_OK):
			raise LgException(LgErr.EACCESS, fentry)

		# Determine file's format
		ftype = LgFile._get_type(fentry)
		if ftype is None:
			raise LgException(LgErr.EINVFORMAT, fentry)

		debug("Got file type (%s):\n\t%s", str(ftype), fentry.path)


		type_to_class = {
			LgFormats.AUDIO: LgAudioFile,
			LgFormats.VIDEO: LgVideoFile,
			LgFormats.ARTWORK: LgArtworkFile,
			LgFormats.MARKER: LgMarkerFile,
			LgFormats.TEXT: LgTextFile,
		}

		file_class = type_to_class.get(ftype)
		if file_class is None:
			error("Unhandled file type:\n\t%s", fentry.path)
			raise LgException(LgErr.EINVFORMAT, fentry)

		# Create instance of appropriate subclass
		return file_class.__new__(file_class, fentry, opts)

	def __init__(self, fentry, opts):
		self.fentry = fentry
		self.options = opts
		self._should_delete = False

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		# See if we should delete this file when done
		# with it.
		if self._should_delete:
			self._delete()
		# Remove references to fentry and opts
		# and clean up the memory of the local
		# variables.
		del self.fentry
		del self.options
		del self._should_delete
		# Become a dead file
		self.__class__ = _LgRipFile
		return False

	#
	# ENTRY POINTS
	#

	def mark_for_delete(self):
		self._should_delete = True

	def move(self, to_path):
		new_fentry_path = os.path.join(to_path, self.fentry.name)
		if LgOpts.ODRYRUN in self.options:
			info("Would move file:\n\t%s\n\tto\n\t%s",
			     self.fentry.path, new_fentry_path)
			self.fentry = None
			del new_fentry_path
			return

		info("Moving\n\t%s\n\tto\n\t%s", self.fentry.path, new_fentry_path)
		try:
			shutil.move(self.fentry.path, new_fentry_path)
		except Exception as err:
			error("Could not move:\n\t%s\n\t%s", self.fentry.path, err)
		self.fentry = None
		del new_fentry_path

	def get_path(self):
		return self.fentry.path

	def get_name(self):
		return self.fentry.name

#
# A dead file that throws exceptions evrytime
# one of its functions are called
#

class _LgRipFile(LgFile):

	def delete(self):
		raise LgException(LgErr.ERIP, None)

	def move(self, to_path):
		raise LgException(LgErr.ERIP, None)

	def get_path(self):
		raise LgException(LgErr.ERIP, None)

	def get_name(self):
		raise LgException(LgErr.ERIP, None)

	def __exit__(self, exc_type, exc_value, traceback):
		return False

#
# Empty/small subclasses
#

class LgVideoFile(LgFile):
	pass

class LgArtworkFile(LgFile):
	pass

class LgMarkerFile(LgFile):
	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)
		debug("Got marker:\n\t%s", self.fentry.path)

class LgTextFile(LgFile):
	pass


#
# The audio file subclass
#

class LgAudioFile(LgFile):

	@dataclass
	class TrackInfo:
		track_number: Optional[int] = None	# Track number in album/disc
		num_tracks: Optional[int] = None	# Total number of tracks on this album/disc
		disc_number: Optional[int] = None 	# Disc number for multi-disc releases
		num_discs: Optional[int] = None		# Total number of discs
		album_id: Optional[str] = None		# MusicBrainz Album ID
		releasegroup_id: Optional[str] = None	# MusicBrainz Release Group ID
		album_gain: Optional[float] = None	# Replaygain tags
		album_peak: Optional[float] = None
		track_gain: Optional[float] = None
		track_peak: Optional[float] = None
		track_lra: Optional[float] = None	# EBUR128 Loudness Range
		track_iloud: Optional[float] = None	# EBUR128 Integrated loudness
		track_rthres: Optional[float] = None	# Relative threshold used for gating during loudness analysis */
		sample_rate: Optional[int] = None	# File format/codec info, filled by LgAudiofile.__new__()
		bit_rate: Optional[int] = None
		bit_depth: Optional[int] = None
		duration_secs: Optional[int] = None
		duration_diff: Optional[int] = None
		total_frames: Optional[int] = None
		_finalized: bool = False
		_hash: Optional[int] = None
		# Unique ID for standalone tracks (so that they don't match)
		_unique_id: str = field(default_factory=lambda: uuid.uuid4().hex, init=False)
		# Make sure the threading lock is not shared among instances, make it an instance attribute
		_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

		def __str__(self):
			try:
				# Format strings safely handling None values
				def fmt_or_na(val, fmt_str, na_str="N/A"):
					return fmt_str.format(val) if val is not None else na_str

				return (
					f"Track {fmt_or_na(self.track_number, '{}')}/"
					f"{fmt_or_na(self.num_tracks, '{}')}, "
					f"Disc {fmt_or_na(self.disc_number, '{}')}/"
					f"{fmt_or_na(self.num_discs, '{}')}, "
					f"Album ID: {fmt_or_na(self.album_id, '{}')}, "
					f"Release group ID: {fmt_or_na(self.releasegroup_id, '{}')}, "
					f"Track gain: {fmt_or_na(self.track_gain, '{:.2f} dB')}, "
					f"Track peak: {fmt_or_na(self.track_peak, '{:.6f}')}, "
					f"Track LRA: {fmt_or_na(self.track_lra, '{:.2f} LU')}, "
					f"EBUR128 Iloud: {fmt_or_na(self.track_iloud, '{:.2f}')}, "
					f"Relative threshold: {fmt_or_na(self.track_rthres, '{:.2f}')}, "
					f"Sample rate: {fmt_or_na(self.sample_rate, '{} Hz')}, "
					f"Bit rate: {fmt_or_na(self.bit_rate, '{} bps')}, "
					f"Bit depth: {fmt_or_na(self.bit_depth, '{} bit')}, "
					f"Duration: {fmt_or_na(self.duration_secs, '{} sec')}, "
					f"Duration diff: {fmt_or_na(self.duration_diff, '{} sec')}, "
					f"Total frames: {fmt_or_na(self.total_frames, '{}')}"
				)
			except Exception:
				# Fallback in case of any error during string formatting
				return f"TrackInfo(sample_rate={self.sample_rate}, bit_rate={self.bit_rate})"

		# Make this hasheable so that it can become a dictionary key,
		# include all fields that are encoding/compression level independent
		# since we want to track duplicate tracks/recordings that may be in
		# different formats. This excludes replaygain tags since the different
		# psychoacoustic models change the perceived loudness. Since we want
		# to be able to modify this both in __new__ and __init__ of LgAudioFile
		# we can't mark it as frozen=true, instead we'll hack our way around
		# this limitation by overriding __setattr__, which is basicaly what the
		# @dataclass decorator does anyway when frozen=true, it's just that
		# we do it at runtime instead of "compile" time (generation of the class)
		def __setattr__(self, key, value):
			if getattr(self, "_finalized", False):
				raise TypeError(f"Cannot modify frozen TrackInfo: {key}")
			super().__setattr__(key, value)

		# Lock the object
		def freeze(self):
			# Prevent possible race condition
			with self._lock:
				# Calculate hash of the object and freeze it
				if self._hash is None:
					# Determine if this is a standalone track
					is_standalone = (
						self.album_id is None and 
						self.releasegroup_id is None and
						(self.track_number is None or self.num_tracks is None) and
						(self.disc_number is None or self.num_discs is None)
					)

					if is_standalone:
						# For standalone tracks, use the unique ID and audio properties
						# to ensure different tracks have different hashes
						hash_tuple = (
							self._unique_id,
							self.sample_rate,
							self.bit_rate,
							self.bit_depth,
							self.duration_secs
						)
					else:
						# For album tracks, use album identifiers
						hash_tuple = (
							self.track_number,
							self.num_tracks,
							self.disc_number,
							self.num_discs,
							self.album_id,
							self.releasegroup_id
						)
					self._hash = hash(hash_tuple)
					self._finalized = True

		def __hash__(self):
			if self._hash is None:
				raise TypeError("Cannot hash an unfinalized TrackInfo instance")
			return self._hash

		def __eq__(self, other):
			if not isinstance(other, type(self)):
				return NotImplemented
			return hash(self) == hash(other)

	#
	# HELPERS
	#
	def _get_string_from_tag(self, tag_key, fallback_key=None):
		try:
			# Try primary key first
			tag_obj = self.tag_handler.tags.get(tag_key)

			# Try fallback if primary key failed
			if tag_obj is None and fallback_key is not None:
				tag_obj = self.tag_handler.tags.get(fallback_key)

			# Handle different tag object types
			if tag_obj is None:
				return None
			elif isinstance(tag_obj, list):
				# Handle Vorbis comments (FLAC/WavPack) which store tags as lists
				return str(tag_obj[0]) if tag_obj else None
			elif hasattr(tag_obj, 'text') and tag_obj.text:
				# Handle ID3 tags which have a text attribute containing a list
				return str(tag_obj.text[0])
			else:
				# Fallback to string conversion
				return str(tag_obj)
		except (IndexError, AttributeError, TypeError) as err:
			error("Error extracting string tag %s for file:\n\t%s\n\t%s", 
			      tag_key, self.fentry.path, str(err))
			return None
		except Exception as err:
			error("Unexpected error extracting tag %s for file:\n\t%s\n\t%s", 
			      tag_key, self.fentry.path, str(err))
			raise LgException(LgErr.EINVTAGS, self.fentry) from err

	def _get_int_from_tag(self, tag_key, fallback_key=None):
		str_value = self._get_string_from_tag(tag_key, fallback_key)
		if str_value is None:
			return None

		try:
			# Handle case where the string might contain extra text
			# Try to extract the first number-like part
			import re
			number_match = re.search(r'\d+', str_value)
			if number_match:
				return int(number_match.group(0))
			# If no match found, try direct conversion
			return int(str_value) if str_value.strip().isdigit() else None
		except (ValueError, TypeError, AttributeError) as err:
			error("Error converting tag %s to integer for file:\n\t%s\n\t%s", 
			      tag_key, self.fentry.path, str(err))
			raise LgException(LgErr.EINVTAGS, self.fentry) from err


	def _get_intpair_from_tag(self, tag_key, fallback_key=None, separator="/"):
		try:
			# First try to get a string that might contain a separator
			tag_value = self._get_string_from_tag(tag_key, fallback_key)

			if tag_value is None:
				return None, None

			# Check if the value contains the separator
			if separator in tag_value:
				# Split by separator and ensure we have at least two elements
				parts = tag_value.split(separator) + [None]

				# Parse first integer
				first_value = int(parts[0]) if parts[0] and parts[0].isdigit() else None

				# Parse second integer
				second_value = int(parts[1]) if parts[1] and parts[1].isdigit() else None

				return first_value, second_value
			else:
				# If no separator, assume it's just the first value
				first_value = int(tag_value) if tag_value.isdigit() else None
				return first_value, None
		except Exception as err:
			error("Error parsing split integer tag %s for file:\n\t%s\n\t%s", 
			      tag_key, self.fentry.path, str(err))
			raise LgException(LgErr.EINVTAGS, self.fentry) from err

	def _get_float_from_tag(self, tag_key, fallback_key=None, unit_suffix=None):

		# Get the tag value, checking the primary key first
		tag_value = self._get_string_from_tag(tag_key, fallback_key)

		# Return None if no tag was found
		if tag_value is None:
			return None
		try:
			# If there's a unit suffix, extract just the numeric part
			if unit_suffix and unit_suffix in tag_value:
				numeric_part = tag_value.split(unit_suffix, maxsplit=1)[0].strip()
			elif unit_suffix:
				# If we expect a suffix but don't find it, try to get the first word
				numeric_part = tag_value.split()[0].strip()
			else:
				# No expected suffix, use the full string
				numeric_part = tag_value
			# Convert to float and return
			return float(numeric_part)
		except (ValueError, IndexError) as err:
			error("Invalid tag format, could not convert to float:\n\t%s\n\t%s",
			      self.fentry.path, tag_value)
			raise LgException(LgErr.EINVTAGS, self.fentry) from err

	# Adds/updates/cleans up a tag value
	def _update_tag(self, upper_key, lower_key, value, format_str):

		# Remove existing tags
		try:
			self.tag_handler.tags.pop(upper_key)
		except (KeyError, ValueError, TypeError):
			pass

		try:
			self.tag_handler.tags.pop(lower_key)
		except (KeyError, ValueError, TypeError):
			pass

		# Set new tag with formatted value
		if value is not None:
			if format_str is not None:
				formatted_value = format_str % value
				self.tag_handler.tags[upper_key] = formatted_value
			else:
				self.tag_handler.tags[upper_key] = value


	# Need a source of truth when we try to compare two files
	# that end up having a significant duration difference
	# this shouldn't be a common scenario, so we can afford
	# to do a few API calls to MusicBrainz.
	def _fetch_musicbrainz_duration_secs(self):

		# Those should be the same between the two songs
		# since we've compared their track_info before
		# ending up here.
		track_number = self.track_info.track_number
		album_id = self.track_info.album_id

		# What a world we live in...
		if track_number is None or album_id is None:
			return None

		# MusicBrainz API URL with format=json
		mb_url = f"https://musicbrainz.org/ws/2/release/{album_id}?inc=recordings&fmt=json"

		headers = { 'User-Agent': USER_AGENT }  # Required by MusicBrainz API

		try:
			response = requests.get(mb_url, headers=headers)
			del mb_url, headers
			if response.status_code != 200:
				del track_number, album_id, response
				return None

			# Parse JSON response
			data = response.json()

			# Navigate the JSON structure to find the track
			for medium in data.get('media', []):
				for track in medium.get('tracks', []):
					if track.get('position') == track_number:
						recording = track.get('recording', {})
						length = recording.get('length')
						if length:
							del track_number, album_id, data
							del medium, track, recording
							# Convert from milliseconds to seconds
							return int(length) / 1000.0
			del track_number, album_id, data
			del medium, track, recording
			return None
		except Exception as err:
			warning("Error fetching MusicBrainz data: %s", err)
			del track_number, album_id, mb_url, headers, err
			return None

	# Check if both files have similar duration, if not decide
	# the winner, hopefully via MusicBrainz
	def _compare_duration(self, other):
		self_duration = self.track_info.duration_secs or 0
		other_duration = other.get_track_info().duration_secs or 0

		# If either file has no duration, prefer the one that does
		if self_duration == 0 and other_duration > 0:
			return -1  # Other is better
		if self_duration > 0 and other_duration == 0:
			return 1   # Self is better

		# If both durations are very close (within 2 seconds), consider them equal
		if abs(self_duration - other_duration) <= 2:
			return 0

		# We are stuck, ask MusicBrainz for help
		reference_duration = self._fetch_musicbrainz_duration_secs()
		if reference_duration is not None:
			my_diff = abs(self_duration - reference_duration)
			other_diff = abs(other_duration - reference_duration)

			# If one file is significantly closer to the reference
			if abs(my_diff - other_diff) > 2:
				return 1 if my_diff < other_diff else -1

			# If both are equally close to the reference
			return 0

		# This sucks, try a larger tolerance up to 5sec and
		# prefer the longer one, give it a smaller boost.
		if abs(self_duration - other_duration) <= 5:
			return 0.5 if self_duration > other_duration else -0.5

		# Give up, it's game over, we can't make a reliable decision
		return None


	#
	# INTERNAL METHODS
	#

	# Calculate a quality metric used for sorting files from the same
	# recording/track, __new__ will use this with an appropriate fixed format_weight.
	# In order to scale this quality metric across the set of duplicate files, we use
	# the scaling_factor provided at the directory level through set_qm_scaling_factor(),
	# after getting all qm values for the files to be compared (through get_qm()).
	def _calculate_quality_metric(self, format_weight=0.5):
		# If file verification failed, the file should always fail during comparison.
		if self._verification_state != LgErr.EOK:
			self._quality_metric = None

		# Reference values for normalization
		REF_SAMPLE_RATE = 48000     # 48kHz reference
		REF_BIT_DEPTH = 24          # 24-bit reference
		REF_BITRATE = 705600        # 16-bit/44.1kHz * 2 channels FLAC bitrate (~705.6 kbps)
		REF_LRA = 10.0              # Reference LRA value in LU (typical for well-mastered music)
		DEFAULT_LRA_FACTOR = 0.5    # Default value when LRA is missing

		# Log-scaled normalization
		sample_rate_factor = math.log2(self.track_info.sample_rate / REF_SAMPLE_RATE)
		bit_depth_factor = math.log2(self.track_info.bit_depth / REF_BIT_DEPTH)
		bitrate_factor = math.log2(self.track_info.bit_rate / REF_BITRATE)

		# Dynamic range factor - use LRA if available
		dynamic_range_factor = DEFAULT_LRA_FACTOR
		if self.track_info.track_lra is not None:
			lra = self.track_info.track_lra
			if lra > 0:
				# Bell curve centered at REF_LRA with acceptable spread
				lra_ratio = lra / REF_LRA
				dynamic_range_factor = math.exp(-0.5 * ((lra_ratio - 1) ** 2) / 0.5)
				# Scale it to ensure good differentiation
				dynamic_range_factor = max(0.2, min(1.0, dynamic_range_factor))
				del lra_ratio
			del lra

		# Sample peak factor - penalize tracks with peaks too close to 0dBFS
		# which often indicates problematic mastering or clipping
		peak_factor = 1.0  # Default - no penalty
		if self.track_info.track_peak is not None:
			peak = self.track_info.track_peak
			if peak > 0:
				# Apply penalty for peaks very close to 0dBFS
				# No penalty for peaks below 0.95 (-0.45dB)
				# Maximum penalty of 10% for peaks at exactly 1.0 (0dB)
				if peak > 0.95:
					# Linear scaling from 0% penalty at 0.95 to 10% penalty at 1.0
					penalty_percentage = (peak - 0.95) * 2
					peak_factor = 1.0 - penalty_percentage

					# Increase penalty if we also have low LRA (this indicates
					# both high peak levels and compressed dynamics - worst case)
					if self.track_info.track_lra is not None:
						lra = self.track_info.track_lra
						if lra < 6.0:  # Very compressed dynamics
							# Additional penalty based on low LRA
							# Max additional 10% for LRA of 0
							peak_factor -= (6.0 - lra) / 60.0
						del lra
					del penalty_percentage
			del peak

		# Weighted sum of quality factors
		self._quality_metric = (
			(sample_rate_factor * 0.25) +    # Sample rate influence (25%)
			(bit_depth_factor * 0.25) +      # Bit depth influence (25%)
			(bitrate_factor * 0.15) +        # Reduced bitrate influence (15%)
			(dynamic_range_factor * 0.15) +  # Increased dynamic range influence (15%)
			(peak_factor * 0.10) +           # Increased peak level quality factor (10%)
			(format_weight * 1.1)            # Slightly boosted format-specific adjustment
		)

		return self._quality_metric

	@abstractmethod
	def _sync_track_info(self):
		return LgErr.EOK

	#
	# OBJECT INSTANTIATION/CLEANUP/COMPARISON
	#

	FORMAT_MAP = {}

	# This is used to register subclasses and their properties to FORMAT_MAP
	# dynamicaly, otherwise since FORMAT_MAP is part of the class definition
	# and the subclasses are defined further below, it wouldn't be possible
	# to reference them here. This also allows to add more subclasses without
	# modifying the superclass's factory/code.
	def __init_subclass__(cls, *, format_name=None, extension=None, tag_class=None,
			      min_bitrate=0, format_weight=1.0, **kwargs):
		super().__init_subclass__(**kwargs)

		if format_name:
			LgAudioFile.FORMAT_MAP[format_name] = {
				"extension": extension,
				"handler_class": cls,
				"tag_class": tag_class,
				"min_bitrate": min_bitrate,
				"format_weight": format_weight,
			}

	def __new__(cls, fentry, opts):
		# Allow subclasses to handle their own creation
		if cls is not LgAudioFile:
			return super().__new__(cls, fentry, opts)

		# Run aunalizer on the file to determine its contents and integrity
		# and copy the results to a new track_info instance.
		format_name = None
		track_info = cls.TrackInfo()
		status = None
		# Check if we verified this file before, if so don't do decode
		# and loudness analysis.
		check_ts = LgFile._check_verification_ts_from_xattrs(fentry)

		try:
			if LgOpts.OFORCECHECK in opts or check_ts is None:
				aunlzr_info = Aunalyzer.analyze(fentry.path, do_lra=True)
			else:
				aunlzr_info = Aunalyzer.analyze(fentry.path, do_decode=False, do_ebur128=False)
		except AunalyzerException as err:
			expc = err # Bring it in scope
			# See if we at least got a partially filled structure
			if hasattr(expc, 'track_info') and expc.track_info is not None:
				format_name = expc.track_info.format_name
				track_info.sample_rate = expc.track_info.sample_rate
				track_info.bit_rate = expc.track_info.bit_rate
				track_info.bit_depth = expc.track_info.bit_depth
				track_info.duration_secs = expc.track_info.duration_secs
				# Keep the file around and let directory's update() handle
				# it, in case for example we have other duplicates we can use
				if isinstance(expc, CodecError):
					status = LgErr.ECORRUPTED
				elif isinstance(expc, EBU128Error):
					status = LgErr.ERGAIN
				track_info.duration_diff = None
				track_info.total_frames = None
				track_info.track_gain = None
				track_info.track_peak = None
				track_info.track_lra = None
				track_info.track_iloud = None
				track_info.track_rthres = None
				del expc, check_ts
			else:
				# We check for file's existence and access at the directory
				# level, so this could only be a processing error or an
				# out of memory situation, in any case raisse the EINVFORMAT
				# exception, and let this be handled above.
				del expc, format_name, track_info, status, check_ts
				raise LgException(LgErr.EINVFORMAT, fentry) from err
		else:
			format_name = aunlzr_info.format_name
			track_info.sample_rate = aunlzr_info.sample_rate
			track_info.bit_rate = aunlzr_info.bit_rate
			track_info.bit_depth = aunlzr_info.bit_depth
			track_info.duration_secs = aunlzr_info.duration_secs
			if LgOpts.OFORCECHECK in opts or check_ts is None:
				track_info.duration_diff = aunlzr_info.duration_diff
				track_info.total_frames = aunlzr_info.total_frames
				track_info.track_gain = aunlzr_info.rg2_gain
				track_info.track_peak = aunlzr_info.sample_peak
				track_info.track_lra = aunlzr_info.ebur128_lra
				track_info.track_iloud = aunlzr_info.ebur128_iloud
				track_info.track_rthres = aunlzr_info.relative_threshold
			else:
				# We'll get those from the tags we already put there on __init__
				track_info.track_gain = None
				track_info.track_peak = None
				track_info.track_lra = None
				# We'll never get those
				track_info.duration_diff = None
				track_info.total_frames = None
				track_info.track_iloud = None
				track_info.track_rthres = None
			status = LgErr.EOK
			del check_ts, aunlzr_info

		# Determine the file's actual format, from the codec used and
		# make sure it's consistent with its extension.
		if format_name not in cls.FORMAT_MAP:
			error("Unhandled audio format %s for\n\t%s", format_name, fentry.path)
			del format_name, track_info, status
			raise LgException(LgErr.EINVFORMAT, fentry)

		fext = os.path.splitext(fentry.name)[1].lower()
		format_info = cls.FORMAT_MAP[format_name]
		expected_ext = format_info['extension']
		handler_class = format_info['handler_class']
		min_bitrate = format_info['min_bitrate']
		del format_name

		if fext != expected_ext:
			warning("Audio format mismatch, extension is %s but should be %s:\n\t%s",
				fext, expected_ext, fentry.path)
		del fext, expected_ext

		# Do some basic checks for sample/bit rate and bit_depth, if status is already
		# set to LgErr.ECORRUPTED don't update it since it has a higher priority
		if status is not LgErr.ECORRUPTED:
			if min_bitrate > 0 and track_info.bit_rate < min_bitrate:
				error("Bit rate below threshold (%i):\n\t%s",
				      track_info.bit_rate, fentry.path)
				status = LgErr.EINVBRATE
			del min_bitrate

			if track_info.sample_rate < LgConsts.MIN_SRATE:
				error("Sample rate below threshold (%i):\n\t%s",
					track_info.sample_rate, fentry.path)
				status = LgErr.EINVSRATE

			if track_info.bit_depth < 16:
				error("Bit rate below threshold (16)\n\t%s", fentry.path)
				status = LgErr.EINVBITS

		# To avoid code duplication among subclasses just because we need a different
		# class for tag_handler per format, and since we already have format_info at
		# hand, initialize the tag handler here.
		tag_class = format_info['tag_class']
		tag_handler = None
		try:
			tag_handler = tag_class(fentry.path)
		except MutagenError as err:
			error("Mutagen failed to open file, treating tags as invalid:\n\t%s\n\t(%s)",
			      fentry.path, str(err))
			del status, track_info, handler_class, format_info
			del tag_class, tag_handler
			raise LgException(LgErr.EINVTAGS, fentry) from err
		del tag_class


		# Done, instantiate a new LgAudioFile subclass object
		# partially initialize it.
		try:
			instance = handler_class.__new__(handler_class, fentry, opts)
			instance.track_info = track_info
			instance.tag_handler = tag_handler
			instance._verification_state = status
			instance._format_weight = format_info['format_weight']
			del status, track_info, tag_handler, handler_class, format_info
		except Exception as err:
			debug("Got exception when creating an audiofile instance: %s", err)
			traceback.print_exc()
			return None
		else:
			return instance

	def __init__(self, fentry, opts):
		super().__init__(fentry, opts)
		# Note: The following fields are initialized on __new__
		# self.track_info
		# self.tag_handler
		# self._verification_state
		# self._format_weight

		# Finish initialization of self.track_info
		self.tags_updated = False
		self._sync_track_info()
		self.track_info.freeze()
		# Now that we have track_info calculate _quality_metric
		self._quality_metric = None
		self._calculate_quality_metric(self._format_weight)
		del self._format_weight
		# This will be updated by the caller if comparison is
		# needed across duplicates.
		self._qm_scaling_factor = None
		debug("Audio file ready: %s\n\t%s", fentry.name, self.track_info)

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		# Don't attempt to update a file that failed verification, is
		# inside a failed directory (since we'll move it anyway), or has
		# been (or will be) deleted.
		if (self._verification_state == LgErr.EOK and 
		    (exc_value is None or (isinstance(exc_value, LgException) and exc_value.error == LgErr.EOK)) and 
		    self.fentry is not None and not self._should_delete):
			# Make sure this isn't a dry run
			if LgOpts.ODRYRUN in self.options:
				info("Would update:\n\t%s", self.fentry.path)
			elif self.tags_updated:
				try:
					info("Updating tags on: %s", self.fentry.name)
					# This should be synchronous
					self.tag_handler.save()
				except MutagenError as err:
					error("Update of tags failed:\n\t%s\n\t%s",
					      self.fentry.path, str(err))
				else:
					LgFile._update_verification_ts_on_xattrs(self.fentry)
		if self._should_delete:
			self._delete()
		self.tag_handler.clear()
		del self.fentry
		del self.options
		del self.track_info
		del self.tag_handler
		del self._verification_state
		del self.tags_updated
		del self._quality_metric
		del self._qm_scaling_factor
		# Become a dead file
		self.__class__ = _LgRipAudioFile
		return False

	def __lt__(self, other):
		# Can't compare apples to oranges
		if not isinstance(other, LgAudioFile):
			return NotImplemented

		# Only compare files that represent the same track
		# (this uses track_info, see __eq__ below)
		if self != other:
			return NotImplemented

		duration_score = self._compare_duration(other)
		# Raise an exception here since we have a mistagged song
		# or a truncated one, we can't tolerate such a mess
		if duration_score is None:
			raise ValueError("Unreliable duration comparison between %s and %s" %
					 (self.fentry.path, other.get_path()))

		# This can happen in case verification failed in one of the files
		# just cripple its score here, so that it gets removed, if
		# both files suck, cripple them both (we may have more than two
		# duplicates), and continue. We'll detect files that failed
		# verification at the directory level afterwareds.
		# (duplicate cleanup is done in directory's arange())
		self_normalized_quality = self.get_qm()
		if self_normalized_quality is None:
			self_normalized_quality = 0.0

		other_normalized_quality = other.get_qm()
		if other_normalized_quality is None:
			other_normalized_quality = 0.0

		quality_score = (self_normalized_quality - other_normalized_quality)

		# Weighted combination
		QUALITY_WEIGHT = 0.6
		DURATION_WEIGHT = 0.4

		final_score = quality_score * QUALITY_WEIGHT + duration_score * DURATION_WEIGHT

		if final_score == 0:
			return False
		return final_score < 0

	def __eq__(self, other):
		if not isinstance(other, LgAudioFile):
			return NotImplemented
		return self.track_info == other.get_track_info()

	#
	# ENTRY POINTS
	#

	def get_track_info(self):
		return self.track_info

	def get_verification_status(self):
		return self._verification_state

	def get_qm(self):
		if self._quality_metric is None:
			return None

		# Return normalized qm if we got _qm_scaling_factor
		if self._qm_scaling_factor is not None:
			return (self._quality_metric / (self._quality_metric + self._qm_scaling_factor))
		else:
			return self._quality_metric

	def set_qm_scaling_factor(self, scaling_factor):
		self._qm_scaling_factor = scaling_factor

	@abstractmethod
	def update_album_rgain_vals(self, album_gain, album_peak):
		return LgErr.EOK


#
# A dead audio file
#

class _LgRipAudioFile(_LgRipFile):
	def _get_float_from_tag(self, primary_key, fallback_key=None, unit_suffix=None):
		raise LgException(LgErr.ERIP, None)

	def _fetch_musicbrainz_duration_secs(self):
		raise LgException(LgErr.ERIP, None)

	def _compare_duration(self, other):
		raise LgException(LgErr.ERIP, None)

	def _calculate_quality_metric(self, format_weight=0.5):
		raise LgException(LgErr.ERIP, None)

	def __lt__(self, other):
		raise LgException(LgErr.ERIP, None)

	def __eq__(self, other):
		raise LgException(LgErr.ERIP, None)

	def _sync_track_info(self, fentry):
		raise LgException(LgErr.ERIP, None)

	def get_track_info(self):
		raise LgException(LgErr.ERIP, None)

	def get_verification_status(self):
		raise LgException(LgErr.ERIP, None)

	def get_qm(self):
		raise LgException(LgErr.ERIP, None)

	def set_qm_scaling_factor(self, scaling_factor):
		raise LgException(LgErr.ERIP, None)

	def update_album_rgain_vals(self, album_gain, album_peak):
		raise LgException(LgErr.ERIP, None)


#
# Audio file subclasses
#

class LgMP3File(LgAudioFile, format_name="mp3", extension=".mp3", tag_class=MP3,
		min_bitrate=LgConsts.MIN_BRATE_MP3, format_weight=0.55):

	# Handle ID3v2 tags
	def _update_tag(self, upper_key, lower_key, value, format_str):

		# Reuse superclass method to remove existing tags
		super()._update_tag(upper_key, lower_key, None, "")

		if value is None:
			return

		if format_str is not None:
			formatted_value = format_str % value
			str_val = formatted_value
		else:
			str_val = value

		# ID3-specific handling
		if upper_key.startswith('TXXX:'):
			# Handle user-defined text frame (TXXX)
			tag_name = upper_key.split(':', 1)[1]
			frame = TXXX(encoding=3, desc=tag_name, text=str_val)
			self.tag_handler.tags[upper_key] = frame
		else:
			# This is a direct tag, just use the string
			self.tag_handler.tags[upper_key] = str_val


	def _sync_track_info(self):
		try:
			# Stored as <track no>/<num_tracks>
			self.track_info.track_number, self.track_info.num_tracks = self._get_intpair_from_tag('TRCK')

			# Stored as <disc_no>/<num_discs>
			self.track_info.disc_number, self.track_info.num_discs = self._get_intpair_from_tag('TPOS')

			self.track_info.album_id = self._get_string_from_tag('TXXX:MusicBrainz Album Id')

			self.track_info.releasegroup_id = self._get_string_from_tag('TXXX:MusicBrainz Release Group Id')

			# If we did loudness analysis already on __new__ overwrite any existing track tags
			# we'll update album tags later on. If we skipped analysis, load the existing track/album tracks.
			if (self.track_info.track_gain is None):

				self.track_info.album_gain = self._get_float_from_tag('TXXX:REPLAYGAIN_ALBUM_GAIN',
										      'TXXX:replaygain_album_gain',
										      'dB')

				self.track_info.album_peak = self._get_float_from_tag('TXXX:REPLAYGAIN_ALBUM_PEAK',
										      'TXXX:replaygain_album_peak')

				self.track_info.track_gain = self._get_float_from_tag('TXXX:REPLAYGAIN_TRACK_GAIN',
										      'TXXX:replaygain_track_gain',
										      'dB')

				self.track_info.track_peak = self._get_float_from_tag('TXXX:REPLAYGAIN_TRACK_PEAK',
										      'TXXX:replaygain_track_peak')

				self.track_info.track_lra = self._get_float_from_tag('TXXX:REPLAYGAIN_TRACK_RANGE',
										      'TXXX:replaygain_track_range',
										      'dB')

				self.tags_updated = False
			else:
				if LgOpts.ODRYRUN in self.options:
					return LgErr.EOK

				self._update_tag('TXXX:REPLAYGAIN_TRACK_GAIN', 'TXXX:replaygain_track_gain', 
 						self.track_info.track_gain, "%.2f dB")

				self._update_tag('TXXX:REPLAYGAIN_TRACK_PEAK', 'TXXX:replaygain_track_peak', 
						self.track_info.track_peak, "%.6f")

				self._update_tag('TXXX:REPLAYGAIN_TRACK_RANGE', 'TXXX:replaygain_track_range', 
						self.track_info.track_lra, "%.2f dB")

				# Clean up reference loudness tags
				self._update_tag('TXXX:REPLAYGAIN_REFERENCE_LOUDNESS', 'TXXX:replaygain_reference_loudness', None, "")

				self.tags_updated = True

		except MutagenError as err:
			error("Error while syncing tags (%s) on:\n\t%s", str(err), self.fentry.path)
			raise LgException(LgErr.EINVTAGS, self.fentry) from err
		return LgErr.EOK

	def update_album_rgain_vals(self, album_gain, album_peak):
		if LgOpts.ODRYRUN in self.options:
			return LgErr.EOK

		if album_gain is None or album_peak is None:
			error("Got empty ReplayGain album info to tag")
			return LgErr.ERGAIN

		self._update_tag('TXXX:REPLAYGAIN_ALBUM_GAIN', 'TXXX:replaygain_album_gain', 
 				album_gain, "%.2f dB")

		self._update_tag('TXXX:REPLAYGAIN_ALBUM_PEAK', 'TXXX:replaygain_album_peak', 
				album_peak, "%.6f")

		self.tags_updated = True
		return LgErr.EOK


class LgOggFile(LgAudioFile, format_name="ogg", extension=".ogg", tag_class=OggVorbis,
		min_bitrate=LgConsts.MIN_BRATE_OGG, format_weight=0.7):

	# Hanlde tags stored as VorbisComments (common for OggVorbis/opus, FLAC etc)
	def _sync_track_info(self):
		try:
			self.track_info.track_number = self._get_int_from_tag('TRACKNUMBER')
			self.track_info.num_tracks = self._get_int_from_tag('TOTALTRACKS')

			self.track_info.disc_number = self._get_int_from_tag('DISCNUMBER')
			self.track_info.num_discs = self._get_int_from_tag('TOTALDISCS')

			self.track_info.album_id = self._get_string_from_tag('MUSICBRAINZ_ALBUMID')
			self.track_info.releasegroup_id = self._get_string_from_tag('MUSICBRAINZ_RELEASEGROUPID')

			if (self.track_info.track_gain is None):

				self.track_info.album_gain = self._get_float_from_tag('REPLAYGAIN_ALBUM_GAIN',
										      'replaygain_album_gain',
										      'dB')

				self.track_info.album_peak = self._get_float_from_tag('REPLAYGAIN_ALBUM_PEAK',
										      'replaygain_album_peak')

				self.track_info.track_gain = self._get_float_from_tag('REPLAYGAIN_TRACK_GAIN',
										      'replaygain_track_gain',
										      'dB')

				self.track_info.track_peak = self._get_float_from_tag('REPLAYGAIN_TRACK_PEAK',
										      'replaygain_track_peak')

				self.track_info.track_lra = self._get_float_from_tag('REPLAYGAIN_TRACK_RANGE',
										     'replaygain_track_range',
										     'dB')

				self.tags_updated = False
			else:
				if LgOpts.ODRYRUN in self.options:
					return LgErr.EOK

				self._update_tag('REPLAYGAIN_TRACK_GAIN', 'replaygain_track_gain', 
 						self.track_info.track_gain, "%.2f dB")

				self._update_tag('REPLAYGAIN_TRACK_PEAK', 'replaygain_track_peak', 
						self.track_info.track_peak, "%.6f")

				self._update_tag('REPLAYGAIN_TRACK_RANGE', 'replaygain_track_range', 
						self.track_info.track_lra, "%.2f dB")

				self._update_tag('REPLAYGAIN_REFERENCE_LOUDNESS', 'replaygain_reference_loudness', None, "")

				self.tags_updated = True

		except MutagenError as err:
			error("Error while reading tags from:\n\t%s\n\t%s",self.fentry.path, err)
			raise LgException(LgErr.EINVTAGS, self.fentry) from err
		return LgErr.EOK

	def update_album_rgain_vals(self, album_gain, album_peak):
		if LgOpts.ODRYRUN in self.options:
			return LgErr.EOK

		if album_gain is None or album_peak is None:
			error("Got empty ReplayGain album info to tag")
			return LgErr.ERGAIN

		self._update_tag('REPLAYGAIN_ALBUM_GAIN', 'replaygain_album_gain', 
 				album_gain, "%.2f dB")

		self._update_tag('REPLAYGAIN_ALBUM_PEAK', 'replaygain_album_peak', 
				album_peak, "%.6f")

		self.tags_updated = True
		return LgErr.EOK


# Tags same as Ogg/Vorbis
class LgFlacFile(LgOggFile, format_name="flac", extension=".flac",
		 tag_class=FLAC, format_weight=1.0):
	pass


class LgWavpackFile(LgAudioFile, format_name="wavpack", extension=".wv",
		    tag_class=WavPack, format_weight=0.95):

	# Handle APEv2 tags
	def _sync_track_info(self):
		try:
			# Equivalent to ID3 TRCK
			self.track_info.track_number, self.track_info.num_tracks = self._get_intpair_from_tag('Track')

			# Equivalent to IDV3 TOPS
			self.track_info.disc_number, self.track_info.num_discs = self._get_intpair_from_tag('Disc')

			# Same as IDv3 without the TXXX prefix
			self.track_info.album_id = self._get_string_from_tag('MusicBrainz Album Id')
			self.track_info.releasegroup_id = self._get_string_from_tag('MusicBrainz Release Group Id')

			# Same as Vorbis Comments
			if (self.track_info.track_gain is None):
				self.track_info.album_gain = self._get_float_from_tag('REPLAYGAIN_ALBUM_GAIN',
										      'replaygain_album_gain',
										      'dB')

				self.track_info.album_peak = self._get_float_from_tag('REPLAYGAIN_ALBUM_PEAK',
										      'replaygain_album_peak')

				self.track_info.track_gain = self._get_float_from_tag('REPLAYGAIN_TRACK_GAIN',
										      'replaygain_track_gain',
										      'dB')

				self.track_info.track_peak = self._get_float_from_tag('REPLAYGAIN_TRACK_PEAK',
										      'replaygain_track_peak')

				self.track_info.track_lra = self._get_float_from_tag('REPLAYGAIN_TRACK_RANGE',
										     'replaygain_track_range',
										     'dB')

				self.tags_updated = False
			else:
				if LgOpts.ODRYRUN in self.options:
					return LgErr.EOK

				self._update_tag('REPLAYGAIN_TRACK_GAIN', 'replaygain_track_gain', 
 						self.track_info.track_gain, "%.2f dB")

				self._update_tag('REPLAYGAIN_TRACK_PEAK', 'replaygain_track_peak', 
						self.track_info.track_peak, "%.6f")

				self._update_tag('REPLAYGAIN_TRACK_RANGE', 'replaygain_track_range', 
						self.track_info.track_lra, "%.2f dB")

				self._update_tag('REPLAYGAIN_REFERENCE_LOUDNESS', 'replaygain_reference_loudness', None, "")

				self.tags_updated = True

		except MutagenError as err:
			error("Error while reading tags from:\n\t%s\n\t%s",self.fentry.path, err)
			raise LgException(LgErr.EINVTAGS, self.fentry) from err
		return LgErr.EOK

	def update_album_rgain_vals(self, album_gain, album_peak):
		if LgOpts.ODRYRUN in self.options:
			return LgErr.EOK

		if album_gain is None or album_peak is None:
			error("Got empty ReplayGain album info to tag")
			return LgErr.ERGAIN

		self._update_tag('REPLAYGAIN_ALBUM_GAIN', 'replaygain_album_gain', 
 				album_gain, "%.2f dB")

		self._update_tag('REPLAYGAIN_ALBUM_PEAK', 'replaygain_album_peak', 
				album_peak, "%.6f")

		self.tags_updated = True
		return LgErr.EOK
