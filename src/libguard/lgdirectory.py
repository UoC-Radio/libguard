#
# Copyright 2020 Nick Kossifidis <mickflemm@gmail.com>
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
	LgFormats,
	LgConsts,
	LgOpts,
	LgErr,
	LgException,
	LgRgainTrackData,
	LgRgainAlbumData
)
from libguard.lgfile import (
	LgFile,
	LgVideoFile,
	LgArtworkFile,
	LgTextFile,
	LgAudioFile
)
from libguard.lgrgainprocessor import LgRgainProcessor
from abc import ABC
from gi.repository import GLib

class LgDirectory(ABC):

	def __init__(self, dentry, parent, opts):
		self.dentry = dentry
		self.parent = parent
		self.options = opts
		self.has_audio = False
		self.has_artwork = False
		self.has_text = False
		self.has_video = False
		self.withdrawn = False
		self.part_of_set = False
		self.withdraw_err = None
		self.new_path = None
		self.audio_files = list()
		self.artwork_files = list()
		self.text_files = list()
		self.video_files = list()
		init_errors = list()
		debug("Got dir: %s", self.dentry.path)

		# Populate file arrays per type, make sure the files are ordered
		# based on their name so that __is_track_out_of_order can be used
		# for album dirs later on.
		direntries = sorted(scandir(self.dentry.path), key=lambda e: e.name)
		if len(direntries) == 0:
			info("Got empty directory:\n\t%s", self.dentry.path)
			self.withdraw_err = LgErr.EEMPTY
			del direntries
			return

		for entry in direntries:
			if entry.is_file():
				try:
					fentry = LgFile(entry, self.options)
				except LgException as status:
					init_errors.append(status.error)
				else:
					if isinstance(fentry, LgAudioFile):
						self.has_audio = True
						self.audio_files.append(fentry)
					elif isinstance(fentry, LgArtworkFile):
						self.has_artwork = True
						self.artwork_files.append(fentry)
					elif isinstance(fentry, LgTextFile):
						self.has_text = True
						self.text_files.append(fentry)
					elif isinstance(fentry, LgTextFile):
						self.has_video = True
						self.video_files.append(fentry)
		direntries.clear()
		del direntries

		# Got any erorrs ?
		# Possible values here are EINVFORMAT from LgFile's constructor,
		# EIGNORE from LgTextFile's constructor, and EINVTAGS from
		# constructors of LgAuioFile's subclasses. Since invalid format
		# and invalid tags are reasons to withdraw this dir from the library
		# set self.withdraw_err so that it propagates to the tree.
		if len(init_errors) > 0:
			# Ignore overrides the rest
			if LgErr.EIGNORE in init_errors:
				init_errors.clear()
				del init_errors
				raise LgException(LgErr.EIGNORE, self.dentry)
			# Then comes invalid format
			elif LgErr.EINVFORMAT in init_errors:
				init_errors.clear()
				del init_errors
				self.withdraw_err = LgErr.EINVFORMAT
				return
			# Then comes invalid tags
			elif LgErr.EINVTAGS in init_errors:
				init_errors.clear()
				del init_errors
				self.withdraw_err = LgErr.EINVTAGS
				return
			# Just in case throw an EUNKNOWN if we ended up here
			else:
				error("Got unknown error:\n\t%s\n\t%s",
				      self.dentry.path, str(init_errors))
				self.withdraw_err = LgErr.EUNKNOWN
				init_errors.clear()
				del init_errors
				raise LgException(LgErr.EUNKNOWN, self.dentry)

		init_errors.clear()
		del init_errors

		# If we have audio files, become and album dir and resume
		# consistency checks by invoking the __init__ of the subclass
		if self.has_audio:
			self.__class__ = LgAlbumDirectory
			self.__init__(dentry, parent, opts)


	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		# Remove references to fentry, parent and
		# opts and clean up the memory of the local
		# variables.
		self.dentry = None
		del self.dentry
		self.parent = None
		del self.parent
		self.options = None
		del self.options
		del self.has_audio
		del self.has_artwork
		del self.has_text
		del self.has_video
		del self.withdrawn
		del self.part_of_set
		del self.withdraw_err
		del self.new_path
		# Clean up file arrays and release them
		self.audio_files.clear()
		del self.audio_files
		self.artwork_files.clear()
		del self.artwork_files
		self.text_files.clear()
		del self.text_files
		self.video_files.clear()
		del self.video_files
		# Become a dead directory
		self.__class__ = _LgRipDirectory
		return False

	def arange(self):
		pass

	def verify(self):
		pass

	def update(self):
		pass

	def withdraw(self, withdraw_dir):
		if (
			self.withdrawn is True
			or self.withdraw_err is None
			or LgOpts.ODRYRUN in self.options
		):
			return

		# Is this an empty directory ? If so get rid of it.
		if self.withdraw_err == LgErr.EEMPTY:
			info("Purging empty directory:\n\t%s", self.dentry.path)
			shutil.rmtree(self.dentry.path)
			self.withdrawn = True
			return

		# Is this directory part of a larger set ?
		# If so we want to withdraw the full set, not just one of its
		# parts. Also note that the top directory is not marked as
		# part of the set since it should only contain the various
		# parts as subdirectories and not have any files inside
		# (other than lock/locked possibly, but we only care for
		# those when writing stuff to the top directory, not when
		# moving it).
		if self.parent is not None and self.part_of_set is True:
			if self.parent.withdraw_err is None:
				self.parent.withdraw_err = self.withdraw_err
				return
			# Parent directory marked for withdrawal
			# already, just wait for it.
			else:
				return

		# Make sure we don't overlap with another dir with the
		# same name (e.g. Best of) at the junkyard. This will
		# also handle the case where we couldn't determine if
		# this directory is part of a set and it has a common
		# name (e.g. Disc 1).
		junk_path = path.join(withdraw_dir, str(self.withdraw_err))
		junk_path_name = self.dentry.name
		i = 1
		while path.exists(path.join(junk_path, junk_path_name)):
			junk_path_name = "%s (%d)" %(self.dentry.name, i)
			i += 1

		junk_path = path.join(junk_path, junk_path_name)
		info("Moving\n\t%s/*\n\tto\n\t%s/*", self.dentry.path, junk_path)
		try:
			makedirs(junk_path)
		except OSError as err:
			if err.errno != errno.EEXIST:
				error("Could not create junkdir:\n\t%s\n\t", junk_path, err)
				del junk_path, junk_path_name, i, err
				return

		# Move directory contents to junk
		for entry in scandir(self.dentry.path):
			shutil.move(entry.path, junk_path)

		# Write changes to disk so that the next check works
		sync()

		# Verify directory is empty and delete it from the Library
		if len(listdir(self.dentry.path)) == 0:
			shutil.rmtree(self.dentry.path)

		self.new_path = junk_path
		self.withdrawn = True
		del junk_path, junk_path_name, i

	def should_withdraw(self):
		return (self.withdraw_err is not None)

	def get_withdraw_err(self):
		return self.withdraw_err

	def register(self, indexer):
		return LgErr.EOK

	def print_contents(self):
		info("Got directory: %s", self.dentry.path)
		for song in self.audio_files:
				info("\tAudio file: %s", song.get_name())
		if self.has_artwork:
			for artwork in self.artwork_files:
				info("\tArtwork file: %s", artwork.get_name())
		if self.has_text:
			for text in self.text_files:
				info("\tText file: %s", text.get_name())
		if self.has_video:
			for video in self.text_files:
				info("\tVideo file: %s", video.get_name())

	def get_path(self):
		if self.withdrawn:
			return self.new_path
		else:
			return self.dentry.path

	def get_name(self):
		return self.dentry.name

#
# A dead directory
#
class _LgRipDirectory(LgDirectory):
	def arange(self):
		raise LgException(LgErr.ERIP, None)

	def verify(self):
		raise LgException(LgErr.ERIP, None)

	def update(self):
		raise LgException(LgErr.ERIP, None)
		
	def withdraw(self, withdraw_dir):
		raise LgException(LgErr.ERIP, None)

	def should_withdraw(self):
		raise LgException(LgErr.ERIP, None)

	def get_withdraw_err(self):
		raise LgException(LgErr.ERIP, None)

	def register(self, indexer):
		raise LgException(LgErr.ERIP, None)
		
	def print_contents(self):
		raise LgException(LgErr.ERIP, None)

	def get_path(self):
		raise LgException(LgErr.ERIP, None)

	def get_name(self):
		raise LgException(LgErr.ERIP, None)

#
# An album directory
#

class LgAlbumDirectory(LgDirectory):

	# This is an "overlay" on top of LgDirectory that
	# adds extra fields/methods related to album directories.
	# Do not allow direct instantiation since we won't be
	# able to call __init__ later on, its __init__ should
	# only run after its parent's __init__.
	def __new__(cls, *args):
		return None

	def __init__(self, dentry, parent, opts):
		self.num_discs = None
		self.num_tracks = None
		self.album_id = None
		self.album_gain = None
		self.releasegroup_id = None
		self.last_track_no = 0
		debug("Got audio dir:\n\t%s", self.dentry.path)
		
		# Since we tell Picard to move images/text that are on the same
		# folder with the audio files to the album folder when tagging, there is a
		# good chance we can mess things up and e.g. add a file from the
		# Downloads folder to Picard, in which case Picard will move all images
		# and text files from the Download folder to the alum folder. To catch
		# this unfortunate scenario, print a warning in case the sum of non-audio
		# files in this directory -if it's an album directory- is greater than
		# the number of audio files.
		if len(self.artwork_files) + len(self.text_files) + len(self.video_files) > len(self.audio_files):
			warning("Dir contains more non-audio files than audio files:\n\t%s",
				self.dentry.path)

		# Verify the consistency of album info, do all files report the same
		# album_id and number of tracks / discs ? Is the track order correct ?
		for entry in self.audio_files:
			num_discs = None
			num_tracks = None
			album_id = None
			album_gain = None
			releasegroup_id = None
			
			if self.__is_track_out_of_order(entry):
				self.withdraw_err = LgErr.EINCONSISTENT
				return

			num_discs, num_tracks, album_id, album_gain, releasegroup_id = entry.get_albuminfo()
			
			if self.num_discs is None and num_discs is not None:
				self.num_discs = num_discs
				del num_discs
			elif self.num_discs != num_discs:
				error("Album metadata inconsistency:\n\t%s\n\tDifferent num_discs",
				      self.dentry.path)
				self.withdraw_err = LgErr.EINCONSISTENT
				del num_discs, num_tracks, album_id, album_gain, releasegroup_id
				return

			if self.num_tracks is None and num_tracks is not None:
				self.num_tracks = num_tracks
				del num_tracks
			elif self.num_tracks != num_tracks:
				error("Album metadata inconsistency:\n\t%s\n\tDifferent num_tracks",
				      self.dentry.path)
				self.withdraw_err = LgErr.EINCONSISTENT
				del num_discs, num_tracks, album_id, album_gain, releasegroup_id
				return

			if self.album_id is None and album_id is not None:
				self.album_id = album_id
				del album_id
			elif self.album_id != album_id:
				error("Album metadata inconsistency:\n\t%s\n\tMixed releases (%s, %s)",
				      self.dentry.path, self.album_id, album_id)
				self.withdraw_err = LgErr.EINCONSISTENT
				del num_discs, num_tracks, album_id, album_gain, releasegroup_id
				return

			if self.album_gain is None and album_gain is not None:
				self.album_gain = album_gain
				del album_gain
			elif self.album_gain != album_gain:
				error("Album metadata inconsistency:\n\t%s\n\tDifferent album gain (%s, %s)",
				      self.dentry.path, self.album_gain, album_gain)
				self.withdraw_err = LgErr.EINCONSISTENT
				del num_discs, num_tracks, album_id, album_gain, releasegroup_id
				return

			if self.releasegroup_id is None and releasegroup_id is not None:
				self.releasegroup_id = releasegroup_id
				del releasegroup_id
			elif self.releasegroup_id != releasegroup_id:
				error("Album metadata inconsistency:\n\t%s\n\tMixed release groups (%s, %s)",
				      self.dentry.path, self.releasegroup_id, releasegroup_id)
				self.withdraw_err = LgErr.EINCONSISTENT
				del num_discs, num_tracks, album_id, album_gain, releasegroup_id
				return

		# We place non-album tracks on a folder named "Standalone Recordings", if we are in such
		# a folder and didn't get num_discs/num_tracks there is no need to warn the user, not
		# having track/disc numbers is expected in this case. In any other case print a warning
		# since it's not a fatal error.
		if (self.num_discs == None or self.num_tracks == None):
			if self.dentry.name != "Standalone Recordings":
				warning("Could not determine number of discs/tracks on album/disc:\n\t%s",
					self.dentry.path)
			return

		debug("Got number of discs (%d):\n\t%s", int(self.num_discs), self.dentry.path)
		debug("Got number of tracks (%d):\n\t%s", int(self.num_tracks), self.dentry.path)
		if self.album_gain is not None:
			debug("Got album gain (%s):\n\t%s", self.album_gain, self.dentry.path)

		if self.releasegroup_id is not None:
			debug("Got release group id (%s):\n\t%s", self.releasegroup_id, self.dentry.path)
		elif self.dentry.name != "Standalone Recordings":
			warning("Could not determine release group id:\n\t%s", self.dentry.path)

		# Mark this directory as part of a set, so that withdraw() gathers
		# the whole set in case we trigger an exception later on, and register()
		# registers the top-level directory instead of its sub-directories.
		if int(self.num_discs) > 1:
			self.part_of_set = True

		if self.num_tracks < len(self.audio_files):
			error("More files than tracks:\n\t%s", self.dentry.path)
			self.withdraw_err = LgErr.EINCONSISTENT
			return
		elif self.num_tracks > len(self.audio_files):
			# Handle the corner case when we miss the last track of the disc.
			# Tracks will still be in order so the check above won't work.
			if self.num_tracks == len(self.audio_files) + 1:
				error("Last track missing (%i):\n\t%s", self.num_tracks, self.dentry.path)
				self.withdraw_err = LgErr.EINCONSISTENT
				return
			warning("More tracks than files:\n\t%s", self.dentry.path)


	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		# Remove references to fentry, parent and
		# opts and clean up the memory of the local
		# variables.
		self.dentry = None
		del self.dentry
		self.parent = None
		del self.parent
		self.options = None
		del self.options
		del self.has_audio
		del self.has_artwork
		del self.has_text
		del self.has_video
		del self.withdrawn
		del self.part_of_set
		del self.withdraw_err
		del self.new_path
		# Clean up file arrays and release them
		self.audio_files.clear()
		del self.audio_files
		self.artwork_files.clear()
		del self.artwork_files
		self.text_files.clear()
		del self.text_files
		self.video_files.clear()
		del self.video_files
		# Clen up audio-dir specific vars
		del self.num_discs
		del self.num_tracks
		del self.album_id
		del self.album_gain
		del self.releasegroup_id
		del self.last_track_no
		# Become a dead directory
		self.__class__ = _LgRipDirectory
		return False

	def __is_track_out_of_order(self, fentry):
		# The asumption here is that we have album tracks with filenames starting
		# with the track number, so its e.g. "01 Intro.ext". This is consistent
		# with our Picard configuration that places discs in different folders
		# and uses this naming approach for files. This way it's not possible to
		# have two tracks starting with 01 because they belong to another disc, if
		# we get that it's either a duplicate or a track from another release of
		# this album (with different ordering). Another asumption is that if
		# we get an album track, starting with a number, all other tracks must
		# follow the same approach, we do this in Picard by not mixing album
		# and non-album tracks. This way (and because we 've previously ordered
		# the files based on their filename) we can also verify the ordering of the
		# tracks and catch the case of a missing album track. In case we are in
		# a folder with non-album tracks, this test is skipped and another check
		# will follow later on in __init__.
		fentry_prefix = fentry.get_name().split()[0]
		if self.last_track_no == 0 and fentry_prefix.isdigit():
			this_track_no = int(fentry_prefix)
			# Check if we start from a track number other than 1, indicating
			# track 1 is missing. Also handle the case where an album starts
			# from track 0 (e.g. HTOA).
			if this_track_no == 1 or this_track_no == 0:
				self.last_track_no = this_track_no
			else:
				error("Track out of order (missing track %d):\n\t%s",
				      self.last_track_no + 1, fentry.get_path())
				del fentry_prefix, this_track_no
				return True
		elif fentry_prefix.isdigit():
			this_track_no = int(fentry_prefix)
			# The only valid case is this_track_no == self.last_track_no + 1
			if this_track_no <= self.last_track_no:
				error("Track out of order (duplicate or mixed up releases):\n\t%s",
					      fentry.get_path())
				del fentry_prefix, this_track_no
				return True
			elif this_track_no > self.last_track_no + 1:
				error("Track out of order (missing track %d):\n\t%s",
					      self.last_track_no + 1, fentry.get_path())
				del fentry_prefix, this_track_no
				return True
			else:
				self.last_track_no = this_track_no
				del fentry_prefix, this_track_no
				return False
		elif not self.last_track_no == 0 and not fentry_prefix.isdigit():
			error("Got non-album track inside album:\n\t%s", fentry.get_path())
			del fentry_prefix, this_track_no
			return True
		else:
			del fentry_prefix
			return False

	def arange(self):
		if LgOpts.ODRYRUN in self.options:
			return
		# Check if we have a messy directory that includes both audio and artwork / infos
		# If so, move text files to the Info subdirectory, and artwork to the Artwork
		# subdirectory, leaving only album_cover.jpg there that should exist on every
		# album folder (so if there is only one image on the folder it's probably
		# album_cover.jpg, hence the > 1 below to ignore this case).
		if self.has_artwork and len(self.artwork_files) > 1:
			artwork_path = path.join(self.dentry.path, "Artwork");
			try:
				makedirs(artwork_path)
			except OSError as err:
				if err.errno != errno.EEXIST:
					error("Could not create Artwork subdir:\n\t%s\n\t%s",
					      artwork_path, err)
				del artwork_path, err
			else:
				for fentry in self.artwork_files:
					with fentry:
						# Skip album_cover.jpg
						if fentry.get_name() == "album_cover.jpg":
							continue
						info("Moving\n\t%s\n\tto\n\t%s", fentry.get_name(), artwork_path)
						new_fentry_path = path.join(artwork_path, fentry.get_name())
						shutil.move(fentry.get_path(), new_fentry_path)
						del new_fentry_path
				del artwork_path
			# Done with artwork files, let them go
			self.artwork_files.clear()
		if self.has_text:
			info_path = path.join(self.dentry.path, "Info");
			try:
				makedirs(info_path)
			except OSError as err:
				if err.errno != errno.EEXIST:
					error("Could not create Info subdir:\n\t%s\n\t%s",
					      info_path, err)
				del info_path, err
			else:
				for fentry in self.text_files:
					with fentry:
						# Don't move locks but print a warning, they shouldn't exist
						# inside the album but next to the album(s) directory (inside
						# the artist dir).
						if fentry.get_name() == "lock" or fentry.get_name() == "locked":
							warning("Lock inside album dir:\n\t%s", fentry.get_path())
							continue
						info("Moving\n\t%s\n\tto\n\t%s", fentry.get_name(), info_path)
						new_fentry_path = path.join(info_path, fentry.get_name())
						shutil.move(fentry.get_path(), new_fentry_path)
				del info_path
			# Done with text files, let them go
			self.text_files.clear()

	def _get_track_by_filename(self, filename):
		for fentry in self.audio_files:
			if filename == fentry.get_path():
				return fentry
		return None
			
	def update(self):
		# If we don't have self.album_gain calculate replaygain for
		# the whole album. Note that if we are here all files have
		# the same album_gain value, so if it's None, it's None for
		# everyone.
		if (
			self.album_gain is not None
			and LgOpts.OFORCERGAIN not in self.options
		   ):
			return LgErr.EOK

		filenames = list()

		# In case we are in a non-album folder with standalone
		# recordings, at least make sure we only do this for those
		# without ReplayGain info.
		for fentry in self.audio_files:
			tgain, tpeak, again, apeak, ref_lvl = fentry.get_rgain_values()
			if tgain is None or tpeak is None:
				filenames.append(fentry.get_path())
			elif LgOpts.OFORCERGAIN in self.options:
				filenames.append(fentry.get_path())
			del tgain, tpeak, again, apeak, ref_lvl

		# Are there any files that need updating or we didn't add
		# anything above (e.g. we are in a folder with standalone
		# recordings where all of them have ReplayGain info) ?
		if len(filenames) == 0:
			debug("All files already contain ReplayGain info:\n\t%s",
			      self.dentry.path)
			return LgErr.EOK

		track_results = None
		album_results = None
		try:
			with LgRgainProcessor(filenames) as rgain_processor:
				debug("Calculating ReplayGain for:\n\t%s", self.dentry.path)
				track_results, album_results = rgain_processor.process()
		except LgException as err:
			error("%s", str(err))
			filenames.clear()
			del filenames
			return LgErr.ERGAIN

		filenames.clear()
		del filenames

		# We calculate album gain/peak per disc/folder. This isn't always
		# correct for multi-disc albums that were intended to be played
		# continuously. However we can't determine that programmaticaly.
		# We could warn the user about it but it'll mostly generate noise,
		# plus in most cases per-disc album gain makes more sense.
		again = album_results.gain
		apeak = album_results.peak
		del album_results
				
		# If we didn't get number of tracks on this dir, it's probably not
		# an album but a set of standalone recordings. Ignore album data
		# in this case since it doesn't make sense.
		if self.num_tracks is None:
			again = None
			apeak = None
		else:
			debug("ReplayGain info for disc %s:\n\tGain: %s, Peak: %s",
			      self.dentry.name, again, apeak)

		for results in track_results:
			debug("ReplayGain info for track %s:\n\tGain: %s, Peak: %s, Ref.lvl: %s",
			      results.filename, results.gain, results.peak, results.ref_lvl)
			fentry = self._get_track_by_filename(results.filename)
			if fentry is None:
				error("Rgain to local file list mismatch !:\n\t%s", results.filename)
				del track_results, again, apeak, results
				return LgErr.ERGAIN

			ret = fentry.update_rgain_values(results.gain, results.peak,
							 again, apeak,
							 results.ref_lvl)

			del fentry
			if ret is not LgErr.EOK:
				del track_results, again, apeak, results, ret
				return LgErr.ERGAIN
			del ret

		# Write changes to disk so that verify runs after this
		sync()
		
		del track_results, again, apeak, results
		return LgErr.EOK


	def __verify_one(self, fentry):
		with fentry:
			# We already got a file that failed, no need
			# to keep checking, this folder is on its way
			# to the junkyard.
			if (self.withdraw_err is None):
				ret = fentry.verify()
				if ret is not LgErr.EOK:
					self.withdraw_err = ret
				del ret

	
	def verify(self):
		# Verify the integrity of the audio files on this album
		with concurrent.futures.ThreadPoolExecutor(max_workers = 4) as executor:
			futures = list()
			for fentry in self.audio_files:
				futures.append(executor.submit(self.__verify_one, fentry))
			executor.shutdown(wait=True)

		# We are done processing audio files and all entries on
		# self.audio_files are finalized, clear the list as well.
		self.audio_files.clear()

	def register(self, indexer):
		if self.withdraw_err is not None:
			return self.withdraw_err
		
		if self.releasegroup_id is None:
			return LgErr.EMISSINGTAGS

		dentry = self.dentry
		if self.part_of_set is True:
			dentry = self.parent.dentry

		try:
			indexer.add_album(dentry, self.releasegroup_id, self.album_id)
		except LgException as err:
			return err.error

		return LgErr.EOK
