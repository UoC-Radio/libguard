#
# Copyright 2020 - 2025 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Directory / album handling
#

import shutil
import errno
import concurrent.futures
import os
from os import (
	path,
	listdir,
	scandir,
	makedirs,
	sync
)
from logging import (
	debug,
	info,
	warning,
	error
)
from libguard import (
	LgOpts,
	LgConsts,
	LgErr,
	LgException,
)
from libguard.lgfile import (
	LgFile,
	LgVideoFile,
	LgArtworkFile,
	LgTextFile,
	LgAudioFile,
	_LgRipFile,
	LgMarkerFile,
)
from statistics import (
	mean,
	stdev
)
from enum import IntFlag
import contextlib
from abc import ABC
from collections import defaultdict
import math
import statistics
import traceback

class LgDirectory(ABC):

	class _DirectoryFlags(IntFlag):
		HAS_AUDIO = 1 << 0
		HAS_VIDEO = 1 << 1
		HAS_ARTWORK = 1 << 2
		HAS_TEXT = 1 << 3
		HAS_MARKER = 1 << 4
		HAS_SUBDIRS = 1 << 5
		WITHDRAWN = 1 << 6
		NEEDS_RGAIN = 1 << 7
		STANDALONE = 1 << 8
		CHECK_DUPLICATES = 1 << 9
		PARTIAL_RELEASE = 1 << 10
		PART_OF_SET = 1 << 11

	#
	# HELPERS
	#

	# Among the failures that occured during processing
	# of this directory, pick the worst one so that we
	# can take proper action and categorize it / move it
	# to the appropriate subfolder in junkyard.
	@staticmethod
	def _pick_withdraw_err(errors):
		if len(errors) == 0:
			return LgErr.EOK
		# Invalid format is the worst of all
		# it means for at least one file in this
		# directory we couldn't even determine what
		# type it was, or it was a totaly unexpected
		# one that shouldn't be there.
		if LgErr.EINVFORMAT in errors:
			return LgErr.EINVFORMAT
		# Then comes invalid tags, it means we could
		# process files but we couldn't verify their
		# tags, so we can't verify the directory's
		# layout, if it's part of a set etc.
		elif LgErr.EINVTAGS in errors:
			return LgErr.EINVTAGS
		# We read the files and tags successfully but
		# some critical tags were missing so we don't
		# have enough for a consistency check, the above
		# errors occur during the creation of a file object
		# the following two come from verify_metadata
		# at the directory level.
		elif LgErr.EMISSINGTAGS in errors:
			return LgErr.EMISSINGTAGS
		# We have enough data for the consistency check,
		# and the check failed, so we still can't reliably
		# determine for example if this directory is
		# part of a set or not.
		elif LgErr.EINCONSISTENT in errors:
			return LgErr.EINCONSISTENT
		# Everything is consistent when it comes to metadata
		# but file data is either corrupt, doesn't match our
		# sampling rate or bitrate requirements etc. These come
		# from verify_data
		elif LgErr.ECORRUPTED in errors:
			return LgErr.ECORRUPTED
		elif LgErr.EINVSRATE in errors:
			return LgErr.EINVSRATE
		elif LgErr.EINVBRATE in errors:
			return LgErr.EINVBRATE
		# Just in case throw an EUNKNOWN if we ended up here
		else:
			return LgErr.EUNKNOWN

	@staticmethod
	def _cleanup_files(error, file_list):
		if file_list is None:
			return

		if error is not None:
			# Create a custom exception to signal directory-level error
			# so that the file's __exit__ knows about it and e.g. skips
			# unnecessary writes/modifications.
			exc_type = LgException
			exc_value = LgException(error, None)
		else:
			exc_type, exc_value = None, None

		# Clean up each file that hasn't been exited yet
		with contextlib.ExitStack() as stack:
			for file_obj in file_list:
				if file_obj is not None and not isinstance(file_obj, _LgRipFile):
					try:
						# Call __exit__ method to properly clean up resources
						stack.callback(file_obj.__exit__, exc_type, exc_value, None)
					except Exception as err:
						# Log error but continue cleaning other files
						debug(f"Error cleaning up file: {err}")
						traceback.print_exc()

		# Clear the list
		file_list.clear()

	@staticmethod
	def _process_file_entry(entry, opts):
		if not entry.is_file():
			return None, None
		try:
			fentry = LgFile(entry, opts)
			if isinstance(fentry, LgAudioFile):
				return fentry, LgDirectory._DirectoryFlags.HAS_AUDIO
			elif isinstance(fentry, LgArtworkFile):
				return fentry, LgDirectory._DirectoryFlags.HAS_ARTWORK
			elif isinstance(fentry, LgVideoFile):
				return fentry, LgDirectory._DirectoryFlags.HAS_VIDEO
			elif isinstance(fentry, LgTextFile):
				return fentry, LgDirectory._DirectoryFlags.HAS_TEXT
			elif isinstance(fentry, LgMarkerFile):
				return fentry, LgDirectory._DirectoryFlags.HAS_MARKER
			else:
				return fentry, None
		except LgException as status:
			return status.error, None
		except Exception as err:
			error("Unhandled exception when creating file object: %s", err)
			raise

	#
	# OBJECT INSTANTIATION/CLEANUP
	#

	def __new__(cls, dentry_path, parent, opts):
		# Allow subclasses to handle their own creation
		if cls is not LgDirectory:
			return super().__new__(cls)

		audio_files = list()
		artwork_files = list()
		video_files = list()
		text_files = list()
		flags = cls._DirectoryFlags(0)
		errors = list()

		debug("Got dir: %s", dentry_path)

		# Populate file arrays per type
		try:
			direntries = list(scandir(dentry_path))
			if len(direntries) == 0:
				if LgOpts.ODRYRUN not in opts:
					info("Purging empty directory:\n\t%s", dentry_path)
					try:
						shutil.rmtree(dentry_path)
					except Exception as e:
						error("Got error while purging:\n\t%s\n\t%s", dentry_path, str(e))
				else:
					info("Would purge empty directory:\n\t%s", dentry_path)
				del audio_files, artwork_files, video_files, text_files
				del flags, errors
				raise LgException(LgErr.EEMPTY, dentry_path)
		except LgException:
			raise
		except Exception as err:
			error("Cannot scan directory:\n\t%s\n\t%s", dentry_path, err)
			del audio_files, artwork_files, video_files, text_files
			del flags, errors
			raise LgException(LgErr.EACCESS, dentry_path) from err

		file_entries = [entry for entry in direntries if entry.is_file()]
		dir_entries = [entry for entry in direntries if entry.is_dir()]

		if dir_entries:
			flags |= LgDirectory._DirectoryFlags.HAS_SUBDIRS
		del dir_entries[:]

		# Process files in parallel
		if len(file_entries) > 0:
			num_workers = min(LgConsts.MAX_FILE_WORKERS, len(file_entries))
			with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor: 
				# Submit all file processing tasks
				future_to_entry = {executor.submit(cls._process_file_entry, entry, opts): entry for entry in file_entries}

				# Process results as they complete
				for future in concurrent.futures.as_completed(future_to_entry):
					result, flag = future.result()

					if isinstance(result, LgErr):  # This is an error code
						errors.append(result)
					elif result is not None:  # This is a file object
						if flag == LgDirectory._DirectoryFlags.HAS_AUDIO:
							flags |= LgDirectory._DirectoryFlags.HAS_AUDIO
							audio_files.append(result)
						elif flag == LgDirectory._DirectoryFlags.HAS_ARTWORK:
							flags |= LgDirectory._DirectoryFlags.HAS_ARTWORK
							artwork_files.append(result)
						elif flag == LgDirectory._DirectoryFlags.HAS_VIDEO:
							flags |= LgDirectory._DirectoryFlags.HAS_VIDEO
							video_files.append(result)
						elif flag == LgDirectory._DirectoryFlags.HAS_TEXT:
							flags |= LgDirectory._DirectoryFlags.HAS_TEXT
							text_files.append(result)
						elif flag == LgDirectory._DirectoryFlags.HAS_MARKER:
							flags |= LgDirectory._DirectoryFlags.HAS_MARKER
					del result, flag
		del direntries[:], file_entries[:]

		# If we have a marker, we should ignore this directory and any
		# subdirectories, return an instance of LgIgnoredDirectory and move on
		if LgDirectory._DirectoryFlags.HAS_MARKER in flags:
			info("Got marked directory, ignoring subtree:\n\t%s", dentry_path)
			cls._cleanup_files(None, audio_files)
			cls._cleanup_files(None, artwork_files)
			cls._cleanup_files(None, video_files)
			cls._cleanup_files(None, text_files)
			del audio_files, artwork_files, video_files, text_files
			del flags, errors
			raise LgException(LgErr.EIGNORE, dentry_path)

		# We missed files due to errors
		# Possible values here are EINVFORMAT from LgFile's and LgAudioFile's
		# constructor, and EINVTAGS from LgAudiofile's constructor (or one of
		# its subclasses). Create an LgFailedDirectory, that we'll later
		# withdraw.
		if len(errors) > 0:
			subclass = LgFailedDirectory
			instance = subclass.__new__(subclass, dentry_path, parent, opts)
			instance.flags = flags
			instance.errors = errors
			withdraw_err = cls._pick_withdraw_err(errors)
			cls._cleanup_files(withdraw_err, audio_files)
			cls._cleanup_files(withdraw_err, artwork_files)
			cls._cleanup_files(withdraw_err, video_files)
			cls._cleanup_files(withdraw_err, text_files)
			del audio_files, artwork_files, video_files, text_files, subclass
			del flags, withdraw_err, errors
			return instance

		# If it only contains subdirectories return an LgIntermediateDirectory
		# that may later become LgAlbumDirectory in case of a multi-disc release
		if flags == LgDirectory._DirectoryFlags.HAS_SUBDIRS:
			subclass = LgIntermediateDirectory
			instance = subclass.__new__(subclass, dentry_path, parent, opts)
			instance.flags = flags
			instance.errors = errors
			del audio_files, artwork_files, video_files, text_files, subclass
			del flags, errors
			return instance

		# If it only has artwork / video / text, return the appropriate instance, and
		# only keep the relevant lists. Note that if it has subdirs it could also be
		# an album / boxset etc, do only do this for leaf dirs (hence the ==). Such
		# leaf directories are treated as part of a set (a box set, album, disc), so
		# that they are not treated as individual directories.
		if flags == LgDirectory._DirectoryFlags.HAS_ARTWORK:
			subclass = LgArtworkDirectory
			instance = subclass.__new__(subclass, dentry_path, parent, opts)
			instance.artwork_files = artwork_files
			flags |= LgDirectory._DirectoryFlags.PART_OF_SET
			instance.flags = flags
			instance.errors = errors
			del audio_files, video_files, text_files, subclass
			del flags, errors
			return instance

		if flags == LgDirectory._DirectoryFlags.HAS_VIDEO:
			subclass = LgVideoDirectory
			instance = subclass.__new__(subclass, dentry_path, parent, opts)
			instance.video_files = video_files
			flags |= LgDirectory._DirectoryFlags.PART_OF_SET
			instance.flags = flags
			instance.errors = errors
			del audio_files, artwork_files, text_files, subclass
			del flags, errors
			return instance
		
		if flags == LgDirectory._DirectoryFlags.HAS_TEXT:
			subclass = LgInfoDirectory
			instance = subclass.__new__(subclass, dentry_path, parent, opts)
			instance.text_files = text_files
			flags |= LgDirectory._DirectoryFlags.PART_OF_SET
			instance.flags = flags
			instance.errors = errors
			del audio_files, artwork_files, video_files, subclass
			del flags, errors
			return instance

		# If it has audio files consider this an LgAudioDirectory, it may also include
		# artwork, video and text that we'll need to arrange into subfolders if needed
		# so preserve those as well, it may become an LgAlbumDirectory or an LgDiscDirectory
		if LgDirectory._DirectoryFlags.HAS_AUDIO in flags:
			subclass = LgAudioDirectory
			instance = subclass.__new__(subclass, dentry_path, parent, opts)
			instance.audio_files = audio_files
			instance.flags = flags
			instance.errors = errors
			del subclass
			if LgDirectory._DirectoryFlags.HAS_ARTWORK in flags:
				instance.artwork_files = artwork_files
			else:
				del artwork_files
			if LgDirectory._DirectoryFlags.HAS_VIDEO in flags:
				instance.video_files = video_files
			else:
				del video_files
			if LgDirectory._DirectoryFlags.HAS_TEXT in flags:
				instance.text_files = text_files
			else:
				del text_files
			del flags, errors
			return instance

		if LgDirectory._DirectoryFlags.HAS_SUBDIRS not in flags:
			# This is a dirty leaf directory with mixed artwork/video/text
			warning("Got dirty leaf directory:\n\t%s", dentry_path)
			subclass = LgDirtyLeafDirectory
		else:
			# This is a dirty intermediate directory with mixed
			# artwork/video/text/subdirs
			warning("Got dirty intermediate directory:\n\t%s", dentry_path)
			subclass = LgDirtyIntermediateDirectory
		instance = subclass.__new__(subclass, dentry_path, parent, opts)
		instance.flags = flags
		instance.errors = errors
		del subclass
		if LgDirectory._DirectoryFlags.HAS_ARTWORK in flags:
			instance.artwork_files = artwork_files
		else:
			del artwork_files
		if LgDirectory._DirectoryFlags.HAS_VIDEO in flags:
			instance.video_files = video_files
		else:
			del video_files
		if LgDirectory._DirectoryFlags.HAS_TEXT in flags:
			instance.text_files = text_files
		else:
			del text_files
		del flags, errors
		return instance


	def __init__(self, dentry_path, parent, opts):
		self.dentry_path = dentry_path
		self.parent = parent
		self.options = opts
		self.new_path = None
		# Note: flags and errors are set by __new__

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		withdraw_err = LgDirectory._pick_withdraw_err(self.errors)
		if hasattr(self, "audio_files"):
			LgDirectory._cleanup_files(withdraw_err, self.audio_files)
			del self.audio_files[:]
		if hasattr(self, "artwork_files"):
			LgDirectory._cleanup_files(withdraw_err, self.artwork_files)
			del self.artwork_files[:]
		if hasattr(self, "video_files"):
			LgDirectory._cleanup_files(withdraw_err, self.video_files)
			del self.video_files[:]
		if hasattr(self, "text_files"):
			LgDirectory._cleanup_files(withdraw_err, self.text_files)
			del self.text_files[:]

		# Make successfully processed audio directories read-only for group
		if (isinstance(self, LgAudioDirectory) and len(self.errors) == 0 
		    and LgDirectory._DirectoryFlags.WITHDRAWN not in self.flags
		    and LgOpts.ODRYRUN not in self.options):
			try:
				import stat
				path = self.dentry_path
				current_mode = os.stat(path).st_mode
				# Remove group write permissions
				new_mode = current_mode & ~stat.S_IWGRP
				os.chmod(path, new_mode)
				debug("Made directory read-only for group: %s", path)
				del path, current_mode, new_mode
			except Exception as err:
				warning("Failed to set directory permissions: %s - %s", self.get_path(), str(err))

		del withdraw_err
		del self.errors[:]
		del self.flags
		del self.dentry_path
		del self.parent
		del self.options
		del self.new_path
		# Become a dead directory
		self.__class__ = _LgRipDirectory
		return False

	#
	# ENTRY POINTS
	#

	def get_path(self):
		if LgDirectory._DirectoryFlags.WITHDRAWN in self.flags:
			return self.new_path
		else:
			return self.dentry_path

	def get_name(self):
		return self.dentry_path.name

	def is_set_member(self):
		return LgDirectory._DirectoryFlags.PART_OF_SET in self.flags

	def has_parent(self):
		return self.parent is not None

	def set_parent(self, parent):
		debug("Set parent:\n\t%s\n\t%s", parent, self.dentry_path)
		self.parent = parent

	def print_contents(self):
		info("Got directory: %s", self.dentry_path)
		if hasattr(self, "audio_files"):
			for song in self.audio_files:
				info("\tAudio file: %s", song.get_name())
		if hasattr(self, "artwork_files"):
			for artwork in self.artwork_files:
				info("\tArtwork file: %s", artwork.get_name())
		if hasattr(self, "video_files"):
			for video in self.video_files:
				info("\tVideo file: %s", video.get_name())
		if hasattr(self, "text_files"):
			for text in self.text_files:
				info("\tText file: %s", text.get_name())

	def process(self):
		return LgErr.EOK

	def register(self, indexer):
		return LgErr.EOK

	def should_withdraw(self):
		return (
			(len(self.errors) > 0)
			and LgDirectory._DirectoryFlags.WITHDRAWN not in self.flags
			)

	def get_withdraw_err(self):
		return LgDirectory._pick_withdraw_err(self.errors)

	def withdraw(self, withdraw_dir):
		if not self.should_withdraw():
			return

		withdraw_err = self.get_withdraw_err()

		# Is this directory part of a larger set ?
		# If so we want to withdraw the full set, not just one of its
		# parts. The idea is that the PART_OF_SET flag will only be set
		# in case we are on a directory that has passed at least some basic
		# metadata checks and we are confident for example on the number of
		# discs consistency. We only propagate this one level above so that
		# we withdraw the album in that case, and not the box set or the whole
		# artist folder, it's up to the caller to make sure we don't move
		# the whole library folder in the junkyard (in case the library contains
		# albums directly and not on per-artist directories for example). 
		if self.parent is not None and LgDirectory._DirectoryFlags.PART_OF_SET in self.flags:
			if not self.parent.should_withdraw():
				self.parent.errors.append(withdraw_err)
				return
			# Parent directory marked for withdrawal
			# already, just wait for it.
			else:
				return

		if LgOpts.ODRYRUN in self.options:
			info("Would withdraw:\n\t%s", self.dentry_path)
			del withdraw_err
			return

		# Make sure we don't overlap with another dir with the
		# same name (e.g. Best of) at the junkyard. This will
		# also handle the case where we couldn't determine if
		# this directory is part of a set and it has a common
		# name (e.g. Disc 1).
		junk_path = path.join(withdraw_dir, str(withdraw_err))
		junk_path_name = self.dentry_path.name
		i = 1
		while path.exists(path.join(junk_path, junk_path_name)):
			junk_path_name = "%s (%d)" %(self.dentry_path.name, i)
			i += 1

		junk_path = path.join(junk_path, junk_path_name)
		info("Moving\n\t%s/*\n\tto\n\t%s/*", self.dentry_path, junk_path)
		try:
			makedirs(junk_path)
		except OSError as err:
			if err.errno != errno.EEXIST:
				error("Could not create junkdir:\n\t%s\n\t%s", junk_path, err)
				del withdraw_err, junk_path, junk_path_name, i, err
				return

		try:
			# Use shutil.move as it's more atomic
			for entry in scandir(self.dentry_path):
				try:
					# Move the file/directory atomically - junk_path is already a directory
					shutil.move(entry.path, junk_path)
				except (OSError, IOError) as move_err:
					error("Failed to move:\n\t%s\n\tto\n\t%s\n\t%s",
					      entry.path, junk_path, str(move_err))
		except Exception as scan_err:
			error("Failed to scan directory for withdraw:\n\t%s\n\t%s", self.dentry_path, str(scan_err))
		else:
			# Write changes to disk so that the next check works
			sync()

			# Verify directory is empty and delete it from the Library
			if len(listdir(self.dentry_path)) == 0:
				shutil.rmtree(self.dentry_path)

			self.new_path = junk_path
			self.flags |= LgDirectory._DirectoryFlags.WITHDRAWN

		del withdraw_err, junk_path, junk_path_name, i


#
# A dead directory
#
class _LgRipDirectory(LgDirectory):

	def get_path(self):
		raise LgException(LgErr.ERIP, None)

	def get_name(self):
		raise LgException(LgErr.ERIP, None)

	def is_set_member(self):
		raise LgException(LgErr.ERIP, None)

	def has_parent(self):
		raise LgException(LgErr.ERIP, None)

	def set_parent(self, parent):
		raise LgException(LgErr.ERIP, None)

	def print_contents(self):
		raise LgException(LgErr.ERIP, None)

	def process(self):
		raise LgException(LgErr.ERIP, None)

	def register(self, indexer):
		raise LgException(LgErr.ERIP, None)

	def should_withdraw(self):
		raise LgException(LgErr.ERIP, None)

	def get_withdraw_err(self):
		raise LgException(LgErr.ERIP, None)

	def withdraw(self, withdraw_dir):
		raise LgException(LgErr.ERIP, None)

#
# A directory containing audio files
#

class LgAudioDirectory(LgDirectory):

	def __init__(self, dentry_path, parent, opts):
		super().__init__(dentry_path, parent, opts)

		debug("Got audio dir:\n\t%s", self.dentry_path)

		# Since we tell Picard to move images/text that are on the same
		# folder with the audio files to the album folder when tagging, there is a
		# good chance we can mess things up and e.g. add a file from the
		# Downloads folder to Picard, in which case Picard will move all images
		# and text files from the Download folder to the alum folder. To catch
		# this unfortunate scenario, print a warning in case the sum of non-audio
		# files in this directory -if it's an album directory- is greater than
		# the number of audio files.
		non_audio_stuff_count = 0
		if getattr(self, "artwork_files", None) and len(self.artwork_files) > 0:
			non_audio_stuff_count += len(self.artwork_files)
		if getattr(self, "video_files", None) and len(self.video_files) > 0:
			non_audio_stuff_count += len(self.video_files)
		if getattr(self, "text_files", None) and len(self.text_files) > 0:
			non_audio_stuff_count += len(self.text_files)

		if non_audio_stuff_count > len(self.audio_files):
			warning("Dir contains more non-audio files than audio files:\n\t%s",
				self.dentry_path)
		del non_audio_stuff_count

		# If we have a folder with standalone recordings we can't expect any
		# consistency regarding track/disc numbers etc, it's just a bunch of
		# files, we only expect the artist to be the same, but that's a complicated
		# check since we may have multiple artists with different order etc, so let's
		# treat this with flexibility in mind and just skip all consistency checks.
		if self.dentry_path.name == "Standalone Recordings":
			info("Got standalone recordings folder:\n\t%s", self.dentry_path)
			self.flags |= LgDirectory._DirectoryFlags.STANDALONE
			return

		# Fields from track_info that should be consistent
		# among all audio files on this directory
		num_tracks = None
		num_discs = None
		disc_number = None
		album_id = None
		releasegroup_id = None
		album_gain = None
		album_peak = None

		track_number = None
		first_track_no = None
		last_track_no = None
		track_info = None
		file_prefix = None

		# The logic below expects audio_files to be sorted by filename/track number
		# but if we created them in parallel that won't be the case, so order the
		# list before we move on to be sure.
		self.audio_files.sort(key=lambda file: int(file.get_track_info().track_number))

		for entry in self.audio_files:

			# This shouldn't be None by now, or else the file wouldn't have ended up
			# in audio_files to begin with.
			track_info = entry.get_track_info()
			if track_info.track_number is None:
				error("Got empty/invalid track_number:\n\t%s", entry.get_path())
				self.errors.append(LgErr.EINVTAGS)
				break

			# Make sure that track numbers are continuous, and that the track
			# number is a prefix of the filename as expected. We'll handle
			# the case of missing tracks before/after later on, here we want
			# to catch gaps in track numbers, which is not acceptable in
			# any of the possible use cases.
			if last_track_no is None:
				first_track_no = track_info.track_number
				last_track_no = first_track_no
			elif track_info.track_number != last_track_no + 1:
				# We may have duplicates, the same track multiple times in
				# different formats, we can recover from that.
				if track_info.track_number == last_track_no:
					warning("Got duplicate track:\n\t%s", entry.get_path())
					self.flags |= LgDirectory._DirectoryFlags.CHECK_DUPLICATES
				else:
					error("Got missing track %d:\n\t%s", last_track_no +1,
					      self.dentry_path)
					self.errors.append(LgErr.EINCONSISTENT)
					break
			last_track_no = track_info.track_number

			try:
				file_parts = entry.get_name().split()
				if not file_parts:
					error("Empty filename: %s", entry.get_path())
					self.errors.append(LgErr.EINCONSISTENT)
					break
				file_prefix = file_parts[0]
				if not file_prefix.isdigit():
					error("Got non-album track inside album:\n\t%s\n\t%s",
					      self.dentry_path, entry.get_path())
					self.errors.append(LgErr.EINCONSISTENT)
					break
				if int(file_prefix) != int(track_info.track_number):
					error("Track number mismatch between tags and filename:\n\t%s", entry.get_path())
					self.errors.append(LgErr.EINCONSISTENT)
					break
				del file_parts, file_prefix
			except Exception as err:
				error("Error parsing filename: %s for file %s", str(err), entry.get_path())
				self.errors.append(LgErr.EINCONSISTENT)
				break

			if num_tracks is None and track_info.num_tracks is not None:
				num_tracks = track_info.num_tracks
			elif num_tracks != track_info.num_tracks:
				error("Album metadata inconsistency:\n\t%s\n\tDifferent num_tracks",
				      self.dentry_path)
				self.errors.append(LgErr.EINCONSISTENT)
				break

			if num_discs is None and track_info.num_discs is not None:
				num_discs = track_info.num_discs
			elif num_discs != track_info.num_discs:
				error("Album metadata inconsistency:\n\t%s\n\tDifferent num_discs",
				      self.dentry_path)
				self.errors.append(LgErr.EINCONSISTENT)
				break

			if disc_number is None and track_info.disc_number is not None:
				disc_number = track_info.disc_number
			elif disc_number != track_info.disc_number:
				error("Album metadata inconsistency:\n\t%s\n\tDifferent disc_number",
				      self.dentry_path)
				self.errors.append(LgErr.EINCONSISTENT)
				break

			if album_id is None and track_info.album_id is not None:
				album_id = track_info.album_id
			elif album_id != track_info.album_id:
				error("Album metadata inconsistency:\n\t%s\n\tMixed releases: AlbumID (%s, %s)",
				      self.dentry_path, album_id, track_info.album_id)
				self.errors.append(LgErr.EINCONSISTENT)
				break

			if releasegroup_id is None and track_info.releasegroup_id is not None:
				releasegroup_id = track_info.releasegroup_id
			elif releasegroup_id != track_info.releasegroup_id:
				error("Album metadata inconsistency:\n\t%s\n\tMixed releases: Release Group ID(%s, %s)",
				      self.dentry_path, releasegroup_id, track_info.releasegroup_id)
				self.errors.append(LgErr.EINCONSISTENT)
				break

			# Inconsistent replaygain tags is non-fatal, maybe we partially processed this
			# directory on a previous run, we can recover from that
			if album_gain is None and track_info.album_gain is not None:
				album_gain = track_info.album_gain
			elif album_gain != track_info.album_gain:
				warning("Album metadata inconsistency:\n\t%s\n\tDifferent album_gain",
				      self.dentry_path)
				self.flags |= LgDirectory._DirectoryFlags.NEEDS_RGAIN

			if album_peak is None and track_info.album_peak is not None:
				album_peak = track_info.album_peak
			elif album_peak != track_info.album_peak:
				warning("Album metadata inconsistency:\n\t%s\n\tDifferent album_peak",
				      self.dentry_path)
				self.flags |= LgDirectory._DirectoryFlags.NEEDS_RGAIN

			track_info = None
		del track_info
		del track_number

		if len(self.errors) > 0:
			del num_tracks, num_discs, disc_number, album_id
			del releasegroup_id, album_gain, album_peak
			del first_track_no, last_track_no
			return

		# If we are here it means that the tags we are interested in are consistent
		# (with the exception of album_gain/peak, but we'll recover from this on update)
		# Proceed with further checks to determine what we are dealing with.

		if album_id is None:
			error("Missing album id:\n\t%s", self.dentry_path)
			self.errors.append(LgErr.EMISSINGTAGS)
			del num_tracks, num_discs, disc_number, album_id
			del releasegroup_id, album_gain, album_peak
			del first_track_no, last_track_no
			return		

		# This is either an album or a disc from an album, in any case we should
		# have number of tracks initialized, otherwise something is wrong.
		if num_tracks is None or num_tracks == 0:
			error("Got album/disc with invalid num_tracks:\n\t%s", self.dentry_path)
			self.errors.append(LgErr.EINVTAGS)
			del num_tracks, num_discs, disc_number, album_id
			del releasegroup_id, album_gain, album_peak
			del first_track_no, last_track_no
			return
		debug("Got number of tracks (%d):\n\t%s", int(num_tracks), self.dentry_path)

		# Let's see if we have more files than expected number of tracks,
		# this may happen in case we have duplicates in different formats,
		# or an HTOA track (track 0). We can recover from this in arange(),
		# for now flag this dir and move on.
		if num_tracks < len(self.audio_files):
			warning("Got more files than tracks (duplicates?):\n\t%s", self.dentry_path)
			self.flags |= LgDirectory._DirectoryFlags.CHECK_DUPLICATES

		# It's also possible to have more tracks than files, there are cases
		# where this is acceptable, for example if we have an album/disc with
		# extras in different formats/mixes (e.g. video tracks after audio,
		# or 5.1 mixes together with stereo mixes), and we only keep a part
		# of the album. The other case where we have missing tracks in between
		# and have gaps, is unacceptable but we got that above.
		elif num_tracks > len(self.audio_files):
			# Handle the corner case when we miss the first/last track of the disc.
			# Tracks will still be in order so we won't catch it above.
			if num_tracks == len(self.audio_files) + 1:
				error("First/last track missing (%i):\n\t%s", num_tracks, self.dentry_path)
				self.errors.append(LgErr.EINCONSISTENT)
				del num_tracks, num_discs, disc_number, album_id
				del releasegroup_id, album_gain, album_peak
				del first_track_no, last_track_no
				return
			warning("Got more tracks than files (partial release?):\n\t%s", self.dentry_path)
			self.flags |= LgDirectory._DirectoryFlags.PARTIAL_RELEASE

		if num_discs is not None and num_discs != 0:
			debug("Got number of discs (%d):\n\t%s", num_discs, self.dentry_path)
			# This directory is a disc that belongs to a larger release/album.
			# Mark it as part of a set, so that withdraw() gathers the whole set
			# in case we trigger an exception later on, and register() registers
			# the top-level directory instead of its sub-directories.
			if num_discs > 1:
				self.flags |= LgDirectory._DirectoryFlags.PART_OF_SET

		if album_gain is None or album_peak is None:
			self.flags |= LgDirectory._DirectoryFlags.NEEDS_RGAIN

		# If this is directory is part of a multi-disc release, mutate it to an
		# LgDiscDirectory, else (since we have number of tracks, album_id etc)
		# it's an LgAlbumDirectory.
		if LgDirectory._DirectoryFlags.PART_OF_SET in self.flags:
			self.__class__ = LgDiscDirectory
		else:
			self.__class__ = LgAlbumDirectory
		del num_tracks, num_discs, disc_number, album_id
		del releasegroup_id, album_gain, album_peak
		del first_track_no, last_track_no
		return

	def process(self):
		# We have unrecoverable errors, this dir is going away,
		# don't waste any time here.
		if len(self.errors) > 0:
			return

		# Check if we have a messy directory that includes both audio and artwork / infos
		# If so, move text files to the Info subdirectory, and artwork to the Artwork
		# subdirectory, leaving only album_cover.jpg there that should exist on every
		# album folder (so if there is only one image on the folder it's probably
		# album_cover.jpg, hence the > 1 below to ignore this case). Any failures here
		# are non-fatal, if we failed to create folders/move files, we'll do it next
		# time.
		if getattr(self, "artwork_files", None) and len(self.artwork_files) > 1:
			artwork_path = path.join(self.dentry_path, "Artwork")
			try:
				if LgOpts.ODRYRUN in self.options:
					info("Would create:\n\t%s", artwork_path)
				else:
					makedirs(artwork_path, exist_ok=True)
			except Exception as err:
				error("Could not create Artwork subdir:\n\t%s\n\t%s",
				      artwork_path, err)
				del err
			else:
				for file in self.artwork_files:
					# Use with, so that when we are done with it, its
					# __exit__ function is called
					with file:
						# Skip album_cover.jpg
						if file.get_name() == "album_cover.jpg":
							continue
						file.move(artwork_path)
			del artwork_path
			# Done with artwork files, let them go
			self.artwork_files.clear()
		if getattr(self, "text_files", None) and len(self.text_files) > 0:
			info_path = path.join(self.dentry_path, "Info")
			try:
				if LgOpts.ODRYRUN in self.options:
					info("Would create:\n\t%s", info_path)
				else:
					makedirs(info_path, exist_ok=True)
			except Exception as err:
				error("Could not create Info subdir:\n\t%s\n\t%s",
				      info_path, err)
				del err
			else:
				for file in self.text_files:
					with file:
						file.move(info_path)
			del info_path
			# Done with text files, let them go
			self.text_files.clear()

		# Try to handle duplicates and recover if possible
		# The goal here is to create a dictionary of lists, where each list will contain all
		# duplicates of a specific track/recording. We'll then sort this list and __lt__ of
		# LgAudioFile will do the rest.
		if (LgDirectory._DirectoryFlags.CHECK_DUPLICATES in self.flags
		    and LgDirectory._DirectoryFlags.STANDALONE not in self.flags):

			# Create a dictionary, with key being file.track_info (which is hashable)
			# and value being a list of files with that track_info.
			duplicates = {}
			for file in self.audio_files:
				duplicates.setdefault(file.get_track_info(), []).append(file)

			# Remove non-duplicates directly
			for track_info in list(duplicates.keys()):
				if len(duplicates[track_info]) <= 1:
					del duplicates[track_info]

			# If we have any duplicates left, try to clean them up
			if duplicates:
				for group in duplicates.values():
					try:
						# Step 1: Compute quality metrics for all files in the group
						quality_metrics = [file.get_qm() for file in group]

						# Step 2: Calculate mean and standard deviation
						quality_mean = mean(quality_metrics)
						quality_std = stdev(quality_metrics) if len(quality_metrics) > 1 else 1
						scale_factor = quality_mean + quality_std

						# Step 3: Update quality metrics with scaling factor
						for file in group:
							file.set_qm_scaling_factor(scale_factor)

						# Step 4: Sort in reverse order, best file first
						group.sort(reverse=True)

					except Exception as err:
						error("Sorting failed for duplicate group %s:\n\t%s", group, err)
						traceback.print_exc()
						# Game over, we can't recover, move this abomination to junkyard
						self.errors.append(LgErr.EINCONSISTENT)
						break

					# Keep the best file (group[0]), remove the rest
					for file in group[1:]:
						with file:
							try:
								self.audio_files.remove(file)
								file.mark_for_delete()
							except Exception as err:
								error("Failed to remove duplicate:\n\t%s\n\t%s", file.get_path(), err)
								self.errors.append(LgErr.EINCONSISTENT)
								break

		# Failed to recover from duplicate files bail out.
		if len(self.errors) > 0:
			return

		# We now have an audio directory that seems consistent and so far error-free,
		# check if the remaining audio files are error-free as well.
		have_rgain = True
		for file in self.audio_files:
			file_status = file.get_verification_status()
			# If loudness analysis failed we can't do much about it,
			# but it's also not a fatal error.
			if file_status is LgErr.ERGAIN:
				have_rgain = False
			elif file_status is not LgErr.EOK:
				self.errors.append(file_status)
				return
			# Loudness analysis didn't happen (we already processed
			# the file before)
			if file.get_track_info().track_iloud is None:
				have_rgain = False

		# Do we need to update album_gain/peak/lra values ?
		if LgDirectory._DirectoryFlags.NEEDS_RGAIN in self.flags and have_rgain and len(self.audio_files) >= 2:
			try:
				energy_sum = 0.0
				gated_duration_sum = 0.0
				album_peak = 0.0

				for file in self.audio_files:
					tinfo = file.get_track_info()

					if tinfo.track_peak > album_peak:
						album_peak = tinfo.track_peak

					# Convert track loudness back to energy domain
					track_energy = pow(10, (tinfo.track_iloud + 0.691) / 10)

					# Use relative threshold to estimate gated content
					track_relative_threshold_energy = pow(10, (tinfo.track_rthres + 0.691) / 10)
					# Cap the ratio to prevent unreasonable values
					ratio_above_threshold = min(track_energy / track_relative_threshold_energy, 1.0)
					gated_frames = tinfo.total_frames * ratio_above_threshold
					del tinfo

					energy_sum += track_energy * gated_frames
					gated_duration_sum += gated_frames
					del track_energy, ratio_above_threshold, gated_frames

				# Re-integrate for the whole album as if it was a single track
				album_energy = energy_sum / gated_duration_sum
				album_loudness = 10 * math.log10(album_energy) - 0.691
				album_gain = -18.0 - album_loudness	# -18LUFS as per Replaygain2 (same as track_gain)
				# Apply a small correction to match rsgain more closely
				correction_factor = -0.05
				album_gain = album_gain + correction_factor

				del energy_sum, gated_duration_sum, correction_factor
			except (ValueError, ZeroDivisionError, OverflowError) as err:
				error("Could not calculate album gain:\n\t%s\n\t%s", self.dentry_path, err)
			else:
				debug("Loudness analysis for audio directory:\n\t%s\n\tGain: %.2f dB, Peak: %.6f",
				      self.dentry_path, album_gain, album_peak)

				for file in self.audio_files:
					file.update_album_rgain_vals(album_gain, album_peak)


	def register(self, indexer):
		# Try registering this with the indexer, to track down duplicate albums
		# We only warn the user in this case to handle things manually, since comparing
		# albums/releases can't be done automatically.

		if len(self.errors) > 0:
			return LgDirectory._pick_withdraw_err(self.errors)

		# Since no errors have been recorded, the directory is consistent, meaning that
		# if this is an album/part of an album, the album_id and releasegroup_id will
		# be the same among tracks and not None. So grab their values from the first
		# track in self.audio_files.
		album_id = self.audio_files[0].get_track_info().album_id
		releasegroup_id = self.audio_files[0].get_track_info().releasegroup_id
		if album_id is None or releasegroup_id is None:
			return LgErr.EMISSINGTAGS

		try:
			if LgDirectory._DirectoryFlags.PART_OF_SET in self.flags and self.parent is not None:
				indexer.add_album(self.parent.get_path(), releasegroup_id, album_id)
			else:
				indexer.add_album(self.dentry_path, releasegroup_id, album_id)
		except LgException as err:
			warning(f"Failed to register album: {err}")

		return LgErr.EOK

#
# Empty subclasses for tracking
#

class LgIntermediateDirectory(LgDirectory):
	pass

class LgArtworkDirectory(LgDirectory):
	pass

class LgVideoDirectory(LgDirectory):
	pass

class LgInfoDirectory(LgDirectory):
	pass

class LgDirtyLeafDirectory(LgDirectory):
	pass

class LgDirtyIntermediateDirectory(LgIntermediateDirectory):
	pass

class LgFailedDirectory(LgDirectory):
	pass

class LgAlbumDirectory(LgAudioDirectory):
	pass

class LgDiscDirectory(LgAudioDirectory):
	pass